"""Tests for caller-scoped API keys across chat, models, conversations, workspaces, legacy usage, and console UI."""

import asyncio
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from kotoyose import auth
from kotoyose.api_key_store import ApiKeyStore
from kotoyose.app import app
from kotoyose.config import (
    Client, HarnessConfig, get_clients, get_config, save_config,
)
from kotoyose.conversation.contracts import Continue, Limits, Model as ConvModel, Start
from kotoyose.conversation.ports import Delta, Round, ToolCall
from kotoyose.conversation.service import Conversations
from kotoyose.execution_admission import ExecutionAdmission, workspace_path
from kotoyose.execution_contract import CommandExecution
from kotoyose.execution_resources import (
    ExecutionError, NativeExecutionConfig,
)
from kotoyose.execution_service import ExecutionService
from kotoyose.agent_execution_contract import AgentExecutionError
from kotoyose.agent_work_admission import pin_work
from kotoyose.routes import chat


class _ScriptedProvider:
    def __init__(self, rounds):
        self.rounds = list(rounds)

    async def stream(self, max_tokens, tool_results=()):
        for event in self.rounds.pop(0):
            yield event


def _build_config() -> HarnessConfig:
    return HarnessConfig.model_validate({
        "connections": {
            "claude-oauth": {"provider": "claude", "kind": "subscription", "data_policy": "no_training", "secret": "claude-oauth"},
            "cursor": {"provider": "cursor", "kind": "api_key", "data_policy": "no_training", "secret": "env:CURSOR_API_KEY"},
        },
        "routes": {
            "claude-route": {"driver": "claude.agent-sdk", "model": "claude-sonnet-4-5", "connection": "claude-oauth"},
            "cursor-route": {"driver": "cursor.sdk", "model": "grok-test", "connection": "cursor"},
        },
        "models": {
            "chat-allowed": {"model": "claude-sonnet-4-5"},
            "chat-other": {"model": "claude-haiku-4-5"},
            "agent-iso-read": {"route": "claude-route", "agent": {"place": "isolated", "surface": "none"}},
            "agent-iso-read-2": {"route": "claude-route", "agent": {"place": "isolated", "surface": "none"}},
            "agent-iso-write": {"route": "claude-route", "agent": {"place": "isolated", "surface": "write"}},
            "agent-host-read": {"route": "cursor-route", "agent": {"place": "host", "surface": "read"}},
            "pool-iso-read": {"candidates": ["agent-iso-read", "agent-iso-read-2"], "policy": "first_available"},
            "pool-iso-write": {"candidates": ["agent-iso-write"], "policy": "first_available"},
            "pool-host-only": {"candidates": ["agent-host-read"], "policy": "first_available"},
        },
    })


def _caller_entry(**overrides) -> dict:
    return {
        "workspace_roots": [],
        "models": [],
        "places": ["isolated"],
        "max_run_seconds": 60,
        "max_memory_bytes": 1024 * 1024 * 1024,
        "max_processes": 64,
        "max_cpus": 1,
        "max_concurrent_runs": 1,
        **overrides,
    }


def _setup_environment(tmp_path, monkeypatch, *, callers: dict):
    cfg_path = tmp_path / "config.yml"
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(cfg_path))
    monkeypatch.delenv("KOTOYOSE_COMPATIBILITY_CONFIG", raising=False)
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig())
    cfg = _build_config().model_copy(update={
        "clients": {name: Client.model_validate(entry) for name, entry in callers.items()},
    })
    save_config(cfg)

    monkeypatch.setattr(auth, "API_KEY", "legacy-admin-secret")
    monkeypatch.setattr(auth, "BASIC_AUTH_USER", "admin")

    native_cfg = NativeExecutionConfig.model_validate({
        "schema_version": "1",
        "image_id": "sha256:" + "2" * 64,
        "drivers": {
            "claude.agent-sdk": {"cli_binary": "/bin/sh"},
            "cursor.sdk": {},
        },
    })
    store = ApiKeyStore()
    exec_store = SimpleNamespace(pool=None, model_pools=None)
    service = SimpleNamespace(
        config=native_cfg,
        store=exec_store,
        admission=SimpleNamespace(settings=native_cfg),
    )
    service._register_workspace_record = MethodType(ExecutionService._register_workspace_record, service)
    service.register_workspace_response = MethodType(ExecutionService.register_workspace_response, service)

    monkeypatch.setattr(app.state, "api_key_store", store, raising=False)
    monkeypatch.setattr(app.state, "execution_service", service, raising=False)
    monkeypatch.setattr(app.state, "compatibility_adapter", None, raising=False)

    monkeypatch.setattr(chat, "get_or_resume_client", AsyncMock(return_value=(AsyncMock(), False)))
    monkeypatch.setattr(chat, "ask_claude", AsyncMock(return_value=("mocked reply", None, "sess-1")))
    monkeypatch.setattr(chat, "_cache_identity", lambda *args: ("claude", "alias", "digest"))
    for name in (
        "record_received", "record_completed", "record_failed", "_safe_upsert_session",
        "_rotate_hash_key", "backfill_conversation_key", "_safe_disconnect_client",
    ):
        monkeypatch.setattr(chat, name, AsyncMock())
    monkeypatch.setattr(chat, "record_received", AsyncMock(return_value=None))
    monkeypatch.setattr(chat, "start_event_writer", lambda *args: asyncio.Queue())
    monkeypatch.setattr(chat, "dispatch_native_chat", AsyncMock(return_value=None), raising=False)
    return store, service


@pytest.mark.asyncio
async def test_caller_scoped_chat_and_models_with_api_key(tmp_path, monkeypatch):
    store, _ = _setup_environment(tmp_path, monkeypatch, callers={
        "chat-caller": _caller_entry(
            models=[
                "chat-allowed", "agent-iso-read", "agent-iso-write", "agent-host-read",
                "pool-iso-read", "pool-iso-write", "pool-host-only",
            ],
            max_surface="read",
        ),
    })
    _, key = await store.create("scoped-client", "chat-caller")
    headers = {"Authorization": f"Bearer {key}", "X-Conversation-Key": "conv-1"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Allowed plain chat alias works
        ok = await client.post("/v1/chat/completions", headers=headers, json={
            "model": "chat-allowed", "messages": [{"role": "user", "content": "hi"}],
        })
        assert ok.status_code == 200
        assert "mocked reply" in ok.text

        # Disallowed alias returns exact 403 error message with no alias hint
        denied = await client.post("/v1/chat/completions", headers=headers, json={
            "model": "chat-other", "messages": [{"role": "user", "content": "hi"}],
        })
        assert denied.status_code == 403
        err = denied.json()["error"]
        assert err["type"] == "permission_denied"
        assert err["message"] == "Model 'chat-other' is outside this API key's client scope"

        # Exceeding surface ceiling is 403
        over_surface = await client.post("/v1/chat/completions", headers=headers, json={
            "model": "agent-iso-write", "messages": [{"role": "user", "content": "hi"}],
        })
        assert over_surface.status_code == 403
        assert over_surface.json()["error"]["message"] == "Model 'agent-iso-write' is outside this API key's client scope"

        # Exceeding surface ceiling on write-only pool is 403
        write_pool = await client.post("/v1/chat/completions", headers=headers, json={
            "model": "pool-iso-write", "messages": [{"role": "user", "content": "hi"}],
        })
        assert write_pool.status_code == 403
        assert write_pool.json()["error"]["message"] == "Model 'pool-iso-write' is outside this API key's client scope"

        # Exceeding places ceiling on host-only pool is 403
        host_pool = await client.post("/v1/chat/completions", headers=headers, json={
            "model": "pool-host-only", "messages": [{"role": "user", "content": "hi"}],
        })
        assert host_pool.status_code == 403
        assert host_pool.json()["error"]["message"] == "Model 'pool-host-only' is outside this API key's client scope"

        # Disallowed alias on /v1/runtime-authentication/resolve is also 403
        rt_denied = await client.post("/v1/runtime-authentication/resolve", headers=headers, json={
            "schema_version": "1", "model": "chat-other",
        })
        assert rt_denied.status_code == 403
        assert rt_denied.json()["error"]["message"] == "Model 'chat-other' is outside this API key's client scope"

        # /v1/models lists only aliases allowed by agent_models + places + max_surface
        models_resp = await client.get("/v1/models", headers=headers)
        assert models_resp.status_code == 200
        ids = {item["id"] for item in models_resp.json()["data"]}
        assert ids == {"chat-allowed", "agent-iso-read", "pool-iso-read"}


@pytest.mark.asyncio
async def test_pool_resolution_respects_caller_profile_ceiling(tmp_path, monkeypatch):
    store, service = _setup_environment(tmp_path, monkeypatch, callers={
        "iso-reader": _caller_entry(models=["pool-iso-read"], max_surface="read"),
    })
    service.store = SimpleNamespace(
        pool=object(),
        model_pools=SimpleNamespace(select=AsyncMock(return_value=["agent-iso-write", "agent-iso-read"])),
    )
    _, key = await store.create("pool-key", "iso-reader")

    seen_models = []

    async def fake_inner(req, request, response, conv_key):
        seen_models.append(req.model)
        from fastapi import Response
        return Response(content="ok")

    monkeypatch.setattr(chat, "_chat_completions", fake_inner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": "pool-iso-read", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        assert seen_models == ["agent-iso-read"]
        assert resp.headers["x-resolved-model"] == "agent-iso-read"


@pytest.mark.asyncio
async def test_caller_scoped_conversations_create_models_and_continue(tmp_path, monkeypatch):
    store, _ = _setup_environment(tmp_path, monkeypatch, callers={
        "conv-caller": _caller_entry(
            models=["chat-allowed", "agent-iso-read", "agent-host-read"],
            max_surface="read",
        ),
    })
    _, key = await store.create("conv-key", "conv-caller")
    provider = _ScriptedProvider([
        [Round((ToolCall("c1", "ping", {}),), 10)],
        [Delta("done"), Round((), 2)],
    ])
    conv_service = Conversations(
        Limits(),
        lambda _: provider,
        {
            "chat-allowed": ConvModel(input_token_budget=1000, max_output_tokens=100),
            "chat-other": ConvModel(input_token_budget=1000, max_output_tokens=100),
            "agent-host-read": ConvModel(input_token_budget=1000, max_output_tokens=100),
        },
        frozenset({"conv-caller"}),
    )
    monkeypatch.setattr(app.state, "conversations", conv_service, raising=False)
    headers = {"Authorization": f"Bearer {key}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listed = await client.get("/v1/conversations/models", headers=headers)
        assert listed.status_code == 200
        assert [m["id"] for m in listed.json()["models"]] == ["chat-allowed"]

        disallowed = await client.post("/v1/conversations/executions", headers=headers, json=Start(
            schema_version="1", model="chat-other", instructions="hi",
            messages=[{"role": "user", "content": "hi"}], max_output_tokens=50,
        ).model_dump())
        assert disallowed.status_code == 403
        assert disallowed.json()["error"]["message"] == "Model 'chat-other' is outside this API key's client scope"

        started = await client.post("/v1/conversations/executions", headers=headers, json=Start(
            schema_version="1", model="chat-allowed", instructions="hi",
            messages=[{"role": "user", "content": "hi"}], max_output_tokens=50,
            tools=[{"name": "ping", "description": "ping", "input_schema": {"type": "object"}}],
        ).model_dump())
        assert started.status_code == 200
        execution_id = started.headers["x-kotoyose-execution-id"]

        # Simulate client scope shrinking before tool-results continuation
        get_config().clients["conv-caller"] = Client.model_validate(
            _caller_entry(models=[]),
        )
        cont_denied = await client.post(
            f"/v1/conversations/executions/{execution_id}/tool-results",
            headers=headers,
            json=Continue(schema_version="1", round=1, results=[{"call_id": "c1", "output": {"ok": True}}]).model_dump(),
        )
        assert cont_denied.status_code == 403
        assert cont_denied.json()["error"]["message"] == "Model 'chat-allowed' is outside this API key's client scope"


@pytest.mark.asyncio
async def test_api_key_without_caller_rejected_on_create_and_auth(tmp_path, monkeypatch):
    store, _ = _setup_environment(tmp_path, monkeypatch, callers={
        "a": _caller_entry(models=["chat-allowed"]),
    })
    _, unbound_key = await store.create("legacy-unbound", None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for payload in ({"name": "bad1"}, {"name": "bad2", "client": None}, {"name": "bad3", "client": ""}):
            resp = await client.post("/admin/api-keys", auth=("admin", "legacy-admin-secret"), json=payload)
            assert resp.status_code == 422

        for endpoint, method in (("/v1/models", "GET"), ("/v1/chat/completions", "POST")):
            if method == "GET":
                auth_resp = await client.get(endpoint, headers={"Authorization": f"Bearer {unbound_key}"})
            else:
                auth_resp = await client.post(endpoint, headers={"Authorization": f"Bearer {unbound_key}"}, json={
                    "model": "chat-allowed", "messages": [{"role": "user", "content": "hi"}],
                })
            assert auth_resp.status_code == 401
            assert "re-issue" in auth_resp.text.lower()


@pytest.mark.asyncio
async def test_key_only_caller_without_token_env_and_empty_workspace_roots(tmp_path, monkeypatch):
    store, service = _setup_environment(tmp_path, monkeypatch, callers={
        "key-only-chat": _caller_entry(models=["chat-allowed"]),
        "key-only-chat-2": _caller_entry(models=["chat-allowed"]),
    })
    service.config.validate_client_resources()
    policy = get_clients()["key-only-chat"]
    assert policy.workspace_roots == []

    # Workspace helpers / admissions reject empty workspace_roots with 403 permission_denied
    with pytest.raises(ExecutionError) as ws_exc:
        workspace_path(policy, str(tmp_path))
    assert ws_exc.value.status == 403 and ws_exc.value.code == "permission_denied"

    admission = ExecutionAdmission(store=SimpleNamespace(), manifest=SimpleNamespace(), image_id="sha256:" + "0" * 64)
    cmd_req = CommandExecution.model_validate({
        "schema_version": "1",
        "kind": "command",
        "idempotency_key": "k1",
        "caller_run_ref": "run-1",
        "workspace_ref": "ws:missing",
        "execution_grant_ref": "grant:1",
        "budgets_ref": "budget:1",
        "command_spec_ref": "cmd:1",
        "argv_ref": "argv:1",
        "cwd_ref": "cwd:1",
        "timeout_policy_ref": "to:1",
        "requirements": {"isolation": "sandbox", "capabilities": ["command_execution"]},
    })
    with pytest.raises(ExecutionError) as adm_exc:
        await admission.resolve("key-only-chat", policy, cmd_req)
    assert adm_exc.value.status == 403 and adm_exc.value.code == "permission_denied"

    agent_req = SimpleNamespace(
        execution=SimpleNamespace(
            work=SimpleNamespace(
                workspace_ref="ws:missing",
                requirements=SimpleNamespace(capabilities=["agent_work"]),
            ),
        ),
    )
    with pytest.raises(AgentExecutionError) as agent_exc:
        await pin_work(SimpleNamespace(store=SimpleNamespace(), admission=admission), "key-only-chat", policy, agent_req)
    assert agent_exc.value.status == 403 and agent_exc.value.code == "permission_denied"

    # Key-only caller still works through its bound API key for chat/models and gets 403 on /v1/workspaces
    _, key = await store.create("key-only", "key-only-chat")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ws_resp = await client.post(
            "/v1/workspaces",
            headers={"Authorization": f"Bearer {key}"},
            json={"schema_version": "1", "caller_workspace_ref": "w1", "path": str(tmp_path)},
        )
        assert ws_resp.status_code == 403
        assert ws_resp.json()["error"]["type"] == "permission_denied"

        chat_resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "X-Conversation-Key": "conv-key-only"},
            json={"model": "chat-allowed", "messages": [{"role": "user", "content": "hello"}]},
        )
        assert chat_resp.status_code == 200
