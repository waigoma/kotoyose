"""Tests for ADR-0010 Stage 6b 4-axis config, owner allow.full gating, and conversion script."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from kotoyose import auth
from kotoyose.api_key_store import ApiKeyStore
from kotoyose.app import app
from kotoyose.config import (
    AliasAllow,
    AllowCapability,
    Client,
    Connection,
    HarnessConfig,
    ModelPreset,
    save_config,
)
from kotoyose.execution_resources import NativeExecutionConfig
from kotoyose.execution_service import ExecutionService
from kotoyose.routes import chat


def test_connection_on_validation_and_serialization():
    default_conn = Connection(provider="claude", kind="api_key", data_policy="no_training", secret="env:ANTHROPIC_API_KEY")
    assert default_conn.on == "local"
    assert "on" not in default_conn.model_dump()

    remote_conn = Connection(provider="codex", kind="subscription", data_policy="no_training", on="child:win1")
    assert remote_conn.on == "child:win1"
    assert remote_conn.model_dump()["on"] == "child:win1"

    for bad_on in ("", " ", "child 1", "child\t1"):
        with pytest.raises(ValidationError):
            Connection(provider="claude", kind="api_key", data_policy="no_training", on=bad_on)


def test_alias_allow_and_model_preset_axes_validation():
    default_allow = AliasAllow()
    assert default_allow.workspace == "none"
    assert default_allow.capabilities == []
    assert default_allow.egress == []
    assert default_allow.full is False
    assert default_allow.model_dump() == {}

    cap: AllowCapability = "web"
    deduped = AliasAllow(workspace="read", capabilities=[cap, "browser", "web"], egress=["api.example.com"])
    assert deduped.capabilities == ["web", "browser"]

    with pytest.raises(ValidationError):
        AliasAllow(capabilities=["shell"])  # type: ignore[list-item]
    with pytest.raises(ValidationError):
        AliasAllow(egress=["bad host"])
    with pytest.raises(ValidationError):
        AliasAllow(unknown=True)  # type: ignore[call-arg]

    # Unset 4-axis fields omit defaults to keep existing digests stable
    legacy = ModelPreset()
    assert legacy.on == "local"
    assert legacy.place is None
    assert legacy.allow is None
    dumped = legacy.model_dump()
    assert "on" not in dumped
    assert "place" not in dumped
    assert "allow" not in dumped

    with pytest.raises(ValidationError):
        ModelPreset(on="host 2")

    # Host place cannot grant workspace read/write or egress unless allow.full=True
    with pytest.raises(ValidationError):
        ModelPreset(place="host", allow=AliasAllow(workspace="read"))
    with pytest.raises(ValidationError):
        ModelPreset(place="host", allow=AliasAllow(workspace="write"))
    with pytest.raises(ValidationError):
        ModelPreset(place="host", allow=AliasAllow(egress=["api.example.com"]))

    # Valid combinations
    host_web = ModelPreset(place="host", allow=AliasAllow(workspace="none", capabilities=["web"]))
    assert host_web.place == "host"
    assert host_web.allow is not None and host_web.allow.capabilities == ["web"]

    host_full = ModelPreset(place="host", allow=AliasAllow(workspace="write", full=True))
    assert host_full.allow is not None and host_full.allow.full is True

    jail_write = ModelPreset(on="child:linux1", place="jail", allow=AliasAllow(workspace="write", egress=["pypi.org"]))
    assert jail_write.on == "child:linux1"
    assert jail_write.place == "jail"


def test_harness_config_rejects_config_dir_connection_with_jail_place():
    with pytest.raises(ValidationError, match="host placement only"):
        HarnessConfig.model_validate({
            "connections": {
                "claude-sub": {
                    "provider": "claude",
                    "kind": "subscription", "data_policy": "no_training",
                    "config_dir": "/tmp/claude-home",
                },
            },
            "routes": {
                "claude-sub": {
                    "driver": "claude.agent-sdk",
                    "model": "claude-sonnet-4-5",
                    "connection": "claude-sub",
                },
            },
            "models": {
                "bad-jail": {
                    "route": "claude-sub",
                    "place": "jail",
                },
            },
        })


def test_client_normalizes_jail_place_and_tracks_allow_full():
    default_client = Client()
    assert default_client.places == ["isolated"]
    assert default_client.allow_full is False
    assert "allow_full" not in default_client.model_dump()

    owner = Client(models=["*"], places=["jail", "host", "isolated"], allow_full=True)
    assert owner.places == ["isolated", "host"]
    assert owner.allow_full is True
    assert owner.model_dump()["allow_full"] is True

    with pytest.raises(ValidationError):
        Client(places=["container"])  # type: ignore[list-item]


def _setup_allow_full_app(tmp_path: Path, monkeypatch, *, with_execution_service: bool):
    cfg_path = tmp_path / "config.yml"
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(cfg_path))
    monkeypatch.delenv("KOTOYOSE_COMPATIBILITY_CONFIG", raising=False)
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig())

    cfg = HarnessConfig.model_validate({
        "connections": {
            "claude-key": {"provider": "claude", "kind": "api_key", "data_policy": "no_training",
                           "secret": "env:ANTHROPIC_API_KEY"},
        },
        "routes": {
            "claude-sonnet": {"driver": "claude.agent-sdk", "model": "claude-sonnet-4-5", "connection": "claude-key"},
        },
        "models": {
            "chat-safe": {
                "route": "claude-sonnet",
                "tools": [],
                "place": "host",
                "allow": {"workspace": "none"},
            },
            "host-full": {
                "route": "claude-sonnet",
                "place": "host",
                "allow": {"full": True},
            },
        },
        "clients": {
            "limited": {
                "models": ["chat-safe", "host-full"],
                "places": ["jail", "host"],
                "max_surface": "write",
                "allow_full": False,
            },
            "owner": {
                "models": ["*"],
                "places": ["jail", "host"],
                "allow_full": True,
            },
        },
    })
    save_config(cfg)

    monkeypatch.setattr(auth, "API_KEY", "legacy-admin-secret")
    monkeypatch.setattr(auth, "BASIC_AUTH_USER", "admin")

    store = ApiKeyStore()
    monkeypatch.setattr(app.state, "api_key_store", store, raising=False)
    monkeypatch.setattr(app.state, "compatibility_adapter", None, raising=False)

    if with_execution_service:
        native_cfg = NativeExecutionConfig.model_validate({
            "schema_version": "1",
            "image_id": "sha256:" + "2" * 64,

            "drivers": {"claude.agent-sdk": {"cli_binary": "/bin/sh"}},
        })
        exec_store = SimpleNamespace(pool=None, model_pools=None)
        service = SimpleNamespace(
            config=native_cfg,
            store=exec_store,
            admission=SimpleNamespace(settings=native_cfg),
        )
        service._register_workspace_record = MethodType(ExecutionService._register_workspace_record, service)
        service.register_workspace_response = MethodType(ExecutionService.register_workspace_response, service)
        monkeypatch.setattr(app.state, "execution_service", service, raising=False)
    else:
        monkeypatch.setattr(app.state, "execution_service", None, raising=False)

    monkeypatch.setattr(chat, "get_or_resume_client", AsyncMock(return_value=(AsyncMock(), False)))
    monkeypatch.setattr(chat, "ask_claude", AsyncMock(return_value=("ok-reply", None, "sess-1")))
    monkeypatch.setattr(chat, "_cache_identity", lambda *args: ("claude", "alias", "digest"))
    for name in (
        "record_completed", "record_failed", "_safe_upsert_session",
        "_rotate_hash_key", "backfill_conversation_key", "_safe_disconnect_client",
    ):
        monkeypatch.setattr(chat, name, AsyncMock())
    monkeypatch.setattr(chat, "record_received", AsyncMock(return_value=None))
    monkeypatch.setattr(chat, "start_event_writer", lambda *args: asyncio.Queue())
    monkeypatch.setattr(chat, "dispatch_native_chat", AsyncMock(return_value=None), raising=False)
    return store


@pytest.mark.asyncio
@pytest.mark.parametrize("with_execution_service", [True, False])
async def test_allow_full_models_and_chat_enforcement(tmp_path, monkeypatch, with_execution_service):
    store = _setup_allow_full_app(tmp_path, monkeypatch, with_execution_service=with_execution_service)
    _, limited_key = await store.create("sample-client-key", "limited")
    _, owner_key = await store.create("owner-cli", "owner")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        limited_models = await client.get("/v1/models", headers={"Authorization": f"Bearer {limited_key}"})
        assert limited_models.status_code == 200
        limited_ids = [item["id"] for item in limited_models.json()["data"]]
        assert limited_ids == ["chat-safe"]

        owner_models = await client.get("/v1/models", headers={"Authorization": f"Bearer {owner_key}"})
        assert owner_models.status_code == 200
        owner_ids = [item["id"] for item in owner_models.json()["data"]]
        assert owner_ids == ["chat-safe", "host-full"]

        denied = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {limited_key}", "X-Conversation-Key": "conv-denied"},
            json={"model": "host-full", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["message"] == "Model 'host-full' is outside this API key's client scope"

        allowed = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {owner_key}", "X-Conversation-Key": "conv-owner"},
            json={"model": "host-full", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert allowed.status_code == 200
        assert "ok-reply" in allowed.text
