"""外部 service を起動せず、alias 設定・認証・HTTP 経路・hot reload を検証する。"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import pytest
import yaml

from kotoyose.app import app
from kotoyose.compatibility_execution import CompatibilityExecution
from kotoyose.config import AgentRoute, AliasAgent, Connection, HarnessConfig, ModelPreset, get_config, reload_config, save_config
from kotoyose.execution_resources import DriverInstall, NativeExecutionConfig
from kotoyose.routes import chat

from tests.fixtures.api_keys import sync_fixture_key_store


@pytest.fixture
def routing(tmp_path, monkeypatch):
    path = tmp_path / "config.yml"
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    monkeypatch.delenv("KOTOYOSE_COMPATIBILITY_CONFIG", raising=False)
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig())
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    native = {
        "runtime_profile": "profile-chat", "workspace_path": str(tmp_path),
        "caller_workspace_ref": "chat-workspace", "policy_ref": "policy:chat:1",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "instructions": "Use the supplied data within the configured permissions.",
        "callers": ["caller-a"],
    }
    clients = {}
    for suffix in ("a", "b"):
        clients["caller-" + suffix] = {
            "workspace_roots": [str(tmp_path)],
            "models": ["native-chat" if suffix == "a" else "other-native"],
            "allowed_provenance": ["trusted", "external_untrusted", "agent_generated"],
            "max_run_seconds": 600, "max_memory_bytes": 1024 * 1024 * 1024,
            "max_processes": 256, "max_cpus": 1, "max_concurrent_runs": 2,
        }
    save_config(HarnessConfig(
        models={
            "legacy-chat": {},
            "native-chat": {"execution": "native", "native": native},
            "other-native": {"execution": "native", "native": {**native, "callers": ["caller-b"]}},
            "blocked-runtime": {"execution": "native", "native": {**native, "runtime_profile": "blocked"}},
            "inactive-native": {"native": native},
        },
        clients=clients,
    ))
    service = SimpleNamespace(
        config=NativeExecutionConfig.model_validate({
            "schema_version": "1", "image_id": "sha256:" + "1" * 64,
        }),
        store=SimpleNamespace(pool=None),
    )
    monkeypatch.setattr(app.state, "execution_service", service, raising=False)
    monkeypatch.setattr(app.state, "compatibility_adapter", None, raising=False)
    legacy = AsyncMock(return_value=(AsyncMock(), False))
    answer = AsyncMock(return_value=("legacy provider output", None, "legacy-session"))
    monkeypatch.setattr(chat, "get_or_resume_client", legacy)
    monkeypatch.setattr(chat, "ask_claude", answer)
    monkeypatch.setattr(chat, "_cache_identity", lambda *args: ("claude", "alias", "digest"))
    for name in (
        "record_received", "record_completed", "record_failed", "_safe_upsert_session",
        "_rotate_hash_key", "backfill_conversation_key", "_safe_disconnect_client",
    ):
        monkeypatch.setattr(chat, name, AsyncMock())
    monkeypatch.setattr(chat, "record_received", AsyncMock(return_value=None))
    monkeypatch.setattr(chat, "start_event_writer", lambda *args: asyncio.Queue())

    async def legacy_stream(*args):
        yield "legacy provider output"
        yield (None, None, "legacy-session")

    monkeypatch.setattr(chat, "stream_claude", legacy_stream)
    dispatched = []

    async def dispatch(adapter, principal, policy, body, **kwargs):
        # Admission 以降だけを置換し、既存の Caller policy 検証は通す。
        profile = adapter.profile(principal, policy, body.model)
        dispatched.append((adapter, profile, principal, body.model))
        return "attempt-test", "request-test"

    monkeypatch.setattr(CompatibilityExecution, "dispatch", dispatch)
    monkeypatch.setattr("kotoyose.compatibility_http._wait_for_completion", AsyncMock(return_value={
        "id": "chatcmpl-native", "object": "chat.completion", "created": 1, "model": "native-chat",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "native output"}, "finish_reason": "stop"}],
        "kotoyose": {"attempt_id": "attempt-test"}, "usage": None,
    }))
    monkeypatch.setattr(app.state, "api_key_store", sync_fixture_key_store(**{'caller-a': 'alias-token-a', 'caller-b': 'alias-token-b'}), raising=False)
    yield SimpleNamespace(
        client=TestClient(app, headers={"Authorization": "Bearer kys_alias-token-a"}),
        path=path, native=native, service=service, legacy=legacy, answer=answer, dispatched=dispatched,
    )


def body(model="legacy-chat", **updates):
    return {"model": model, "messages": [{"role": "user", "content": "hello"}], **updates}


@pytest.mark.parametrize("principal", ["a", "b"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(("model", "status"), [
    ("legacy-chat", 200), ("unknown-model", 404), ("inactive-native", 200),
])
def test_legacy_alias_routing_with_native_enabled(routing, principal, stream, model, status):
    h = routing
    cfg = get_config().model_copy(deep=True)
    client_cfg = cfg.clients["caller-" + principal]
    cfg.clients["caller-" + principal] = client_cfg.model_copy(update={
        "models": [*client_cfg.models, "legacy-chat", "unknown-model", "inactive-native"],
    })
    save_config(cfg)
    response = h.client.post(
        "/v1/chat/completions", json=body(model, stream=stream),
        headers={"Authorization": "Bearer kys_alias-token-" + principal, "X-Conversation-Key": "legacy-key"},
    )
    assert response.status_code == status, response.text
    if status == 404:
        assert response.json()["error"]["code"] == "model_not_found"
        h.legacy.assert_not_awaited()
        h.answer.assert_not_awaited()
        assert not h.dispatched
        return
    assert "legacy provider output" in response.text
    h.legacy.assert_awaited_once_with("legacy-key", model, cwd_override=None)
    assert h.answer.await_count == (0 if stream else 1)
    assert not h.dispatched


@pytest.mark.parametrize("stream", [False, True])
def test_native_alias_routes_through_compatibility_adapter(routing, stream):
    h = routing
    response = h.client.post("/v1/chat/completions", json=body("native-chat", stream=stream))
    assert response.status_code == 200, response.text
    assert "native output" in response.text
    assert response.headers["x-kotoyose-attempt-id"] == "attempt-test"
    assert len(h.dispatched) == 1
    adapter, profile, principal, model = h.dispatched[0]
    assert adapter is app.state.compatibility_adapter
    assert (principal, model) == ("caller-a", "native-chat")
    assert profile.workspace_path == h.path.parent
    h.legacy.assert_not_called()


@pytest.mark.parametrize("model,authorization", [
    ("native-chat", "Bearer kys_alias-token-b"),
    ("other-native", "Bearer kys_alias-token-a"),
    ("blocked-runtime", "Bearer kys_alias-token-a"),
])
def test_native_alias_rejects_unlisted_caller_or_runtime(routing, model, authorization):
    h = routing
    response = h.client.post("/v1/chat/completions", json=body(model), headers={"Authorization": authorization})
    assert response.status_code == 403, response.text
    assert response.json()["error"]["type"] == "permission_denied"
    assert not h.dispatched
    h.legacy.assert_not_called()
    for path in ("/v1/models", "/v1/conversations/key/events/stream", "/v1/chat/completions"):
        response = (
            h.client.post(path, json=body(), headers={"Authorization": "Bearer wrong"})
            if path.endswith("completions") else h.client.get(path, headers={"Authorization": "Bearer wrong"})
        )
        assert response.status_code == 401


@pytest.mark.parametrize("principal,native_alias", [("a", "native-chat"), ("b", "other-native")])
def test_models_union_filters_native_callers_and_runtime_policy(routing, principal, native_alias):
    response = routing.client.get("/v1/models", headers={"Authorization": "Bearer kys_alias-token-" + principal})
    assert response.status_code == 200
    assert response.json()["data"] == [
        {"id": native_alias, "object": "model", "owned_by": "local", "agent": None,
         "data_policy": "unknown", "training_ack": False}
    ]


def _declare_agent_aliases(cfg: HarnessConfig) -> None:
    cfg.connections["claude-oauth"] = Connection(provider="claude", kind="subscription", data_policy="no_training", secret="claude-oauth")
    cfg.connections["cursor"] = Connection(provider="cursor", kind="api_key", data_policy="no_training", secret="env:CURSOR_API_KEY")
    cfg.routes["claude-route"] = AgentRoute(
        driver="claude.agent-sdk", model="claude-sonnet-4-5", connection="claude-oauth",
    )
    cfg.routes["cursor-route"] = AgentRoute(driver="cursor.sdk", model="grok-test", connection="cursor")
    cfg.models["agent-sandbox"] = ModelPreset(
        route="claude-route",
        agent=AliasAgent(place="isolated", surface="write"),
    )
    cfg.models["agent-host"] = ModelPreset(
        route="cursor-route",
        agent=AliasAgent(place="host", surface="write"),
    )


def _install_agent_drivers(settings) -> None:
    settings.drivers = {
        "claude.agent-sdk": DriverInstall(cli_binary=Path("/bin/sh")),
        "cursor.sdk": DriverInstall(),
    }


def test_models_expose_agent_execution_modes(routing):
    cfg = get_config().model_copy(deep=True)
    _declare_agent_aliases(cfg)
    cfg.clients["caller-a"] = cfg.clients["caller-a"].model_copy(update={
        "models": ["agent-sandbox", "agent-host"],
        "places": ["isolated", "host"],
    })
    save_config(cfg)
    _install_agent_drivers(routing.service.config)
    response = routing.client.get("/v1/models")
    assert response.status_code == 200
    assert "agent_execution" not in response.json()["data"][0]
    agents = {item["id"]: item["agent"] for item in response.json()["data"]}
    assert agents["agent-sandbox"] == {
        "place": "isolated", "surface": "write", "provider": "claude",
        "driver": "claude.agent-sdk", "enforced": True,
    }
    assert agents["agent-host"] is None
    assert "inactive-native" not in agents


def test_models_execution_mode_is_disabled_or_caller_scoped(routing, monkeypatch):
    cfg = get_config().model_copy(deep=True)
    _declare_agent_aliases(cfg)
    cfg.clients["caller-a"] = cfg.clients["caller-a"].model_copy(update={
        "models": ["agent-sandbox", "agent-host"],
    })
    cfg.clients["caller-b"] = cfg.clients["caller-b"].model_copy(update={"models": []})
    save_config(cfg)
    settings = routing.service.config
    _install_agent_drivers(settings)
    response = routing.client.get("/v1/models")
    agents = {item["id"]: item["agent"] for item in response.json()["data"]}
    assert agents["agent-sandbox"]["place"] == "isolated"
    assert "agent-host" not in agents  # Caller A の places は isolated だけ。
    other = routing.client.get("/v1/models", headers={"Authorization": "Bearer kys_alias-token-b"})
    assert other.json()["data"] == []
    admin = routing.client.get("/admin/models?client=caller-a")
    assert admin.json()["data"]
    listed = {item["id"]: item["agent"] for item in admin.json()["data"]}
    assert listed["agent-sandbox"]["place"] == "isolated"
    assert all(item["agent"] is None for item in routing.client.get("/admin/models?client=caller-b").json()["data"])
    monkeypatch.setattr(routing.client.app.state, "execution_service", None)
    disabled = routing.client.get("/v1/models")
    assert disabled.status_code == 200
    assert all(item["agent"] is None for item in disabled.json()["data"])
    assert all(item["agent"] is None for item in routing.client.get("/admin/models").json()["data"])


def test_compatibility_json_merges_and_duplicate_names_fail_at_load(routing, monkeypatch):
    h = routing
    migration = h.path.parent / "compatibility.json"
    profile = get_config().models["native-chat"].native.profile().model_dump(mode="json")
    migration.write_text(json.dumps({"schema_version": "1", "callers": {
        "caller-a": {"migrated": profile},
        "caller-b": {"migrated": {**profile, "instructions": "Other Caller policy"}},
    }}))
    monkeypatch.setenv("KOTOYOSE_COMPATIBILITY_CONFIG", str(migration))
    cfg = reload_config().model_copy(deep=True)
    client_a = cfg.clients["caller-a"]
    cfg.clients["caller-a"] = client_a.model_copy(update={
        "models": [*client_a.models, "migrated"],
    })
    save_config(cfg)
    assert cfg.compatibility_config.profile("caller-b", "migrated").instructions == "Other Caller policy"
    assert {item["id"] for item in h.client.get("/v1/models").json()["data"]} == {
        "native-chat", "migrated",
    }
    assert h.client.post("/v1/chat/completions", json=body("migrated")).status_code == 200
    assert h.dispatched[-1][3] == "migrated"
    save_config(cfg)
    assert "migrated" not in yaml.safe_load(h.path.read_text())["models"]
    saved = h.path.read_text()
    response = h.client.post("/admin/models", json={"model_id": "migrated", "preset": {}})
    assert response.status_code == 422
    assert "migrated" in response.text
    assert h.path.read_text() == saved
    migration.write_text(json.dumps({"schema_version": "1", "callers": {
        "caller-b": {"legacy-chat": profile, "native-chat": profile},
    }}))
    current = get_config()
    with pytest.raises(ValueError, match="Duplicate model aliases.*legacy-chat, native-chat"):
        reload_config()
    assert get_config() is current
    assert h.client.post("/admin/reload").status_code == 422


@pytest.mark.parametrize("invalid,expected", [
    ({"expires_at": "2026-10-12T00:00:00"}, "expires_at must include a timezone"),
    ({"callers": None}, "callers"),
    ({"callers": []}, "callers"),
    ({"callers": [" "]}, "callers"),
    ({"workspace_path": "relative/workspace"}, "workspace_path must be absolute"),
    ({"timeout": {"schema_version": "1", "kind": "timeout_policy", "soft_seconds": 20,
                  "hard_seconds": 10, "grace_seconds": 1}}, "soft threshold must precede"),
    ({"cpus": 0}, "cpus"),
])
def test_admin_rejects_invalid_native_settings_without_saving(routing, invalid, expected):
    h = routing
    native = {**h.native, **invalid}
    if native.get("callers") is None:
        del native["callers"]
    saved = h.path.read_text()
    current = get_config()
    for execution in ("native", "direct"):
        preset = {"execution": execution, "native": native}
        for response in (
            h.client.put("/admin/models/legacy-chat", json=preset),
            h.client.post("/admin/models", json={"model_id": "new-alias", "preset": preset}),
        ):
            assert response.status_code == 422, response.text
            assert expected in response.json()["error"]["message"]
    missing = h.client.put("/admin/models/legacy-chat", json={"execution": "native"})
    assert missing.status_code == 422
    assert "native settings are required" in missing.text
    assert h.path.read_text() == saved
    assert get_config() is current


def test_admin_save_and_reload_immediately_refresh_alias_routing(routing):
    h = routing
    assert h.client.get("/v1/models").status_code == 200
    cfg = get_config().model_copy(deep=True)
    client_a = cfg.clients["caller-a"]
    cfg.clients["caller-a"] = client_a.model_copy(update={
        "models": [*client_a.models, "legacy-chat", "created-native"],
    })
    save_config(cfg)
    old_adapter = app.state.compatibility_adapter
    native = {**h.native, "instructions": "Updated instruction"}
    response = h.client.put("/admin/models/legacy-chat", json={"execution": "native", "native": native})
    assert response.status_code == 200, response.text
    persisted = yaml.safe_load(h.path.read_text())["models"]["legacy-chat"]
    assert persisted["execution"] == "native"
    assert persisted["native"]["workspace_path"] == native["workspace_path"]
    assert persisted["native"]["expires_at"] == native["expires_at"].replace("+00:00", "Z")
    returned = h.client.get("/admin/config").json()["models"]["legacy-chat"]
    assert returned["execution"] == "native"
    assert returned["native"] == persisted["native"]
    schema = h.client.get("/openapi.json").json()["components"]["schemas"]
    assert {"execution", "native"} <= schema["AdminModelPreset"]["properties"].keys()
    assert "callers" in schema["NativeAliasSettings"]["required"]
    assert h.client.post("/v1/chat/completions", json=body()).status_code == 200
    assert h.dispatched[-1][0] is not old_adapter
    assert h.dispatched[-1][1].instructions == "Updated instruction"
    assert h.client.post("/admin/models", json={
        "model_id": "created-native", "preset": {"execution": "native", "native": native},
    }).status_code == 200
    assert h.client.post("/v1/chat/completions", json=body("created-native")).status_code == 200
    old_adapter = h.dispatched[-1][0]
    data = yaml.safe_load(h.path.read_text())
    data["models"]["legacy-chat"]["native"]["instructions"] = "Reloaded instruction"
    h.path.write_text(yaml.safe_dump(data))
    assert h.client.post("/admin/reload").status_code == 200
    assert h.client.post("/v1/chat/completions", json=body()).status_code == 200
    assert h.dispatched[-1][0] is not old_adapter
    assert h.dispatched[-1][1].instructions == "Reloaded instruction"
    assert h.client.put("/admin/models/legacy-chat", json={"execution": "direct", "native": native}).status_code == 200
    assert get_config().models["legacy-chat"].native.instructions == native["instructions"]
    assert h.client.post("/v1/chat/completions", json=body()).json()["choices"][0]["message"]["content"] == "legacy provider output"
    h.legacy.assert_awaited_once()
    assert h.client.delete("/admin/models/created-native").status_code == 200
    assert "created-native" not in {item["id"] for item in h.client.get("/v1/models").json()["data"]}
    assert not app.state.compatibility_adapter.config.has_model("created-native")


def test_native_disabled_preserves_configured_models_and_rejects_unknown(routing, monkeypatch):
    h = routing
    monkeypatch.setattr(app.state, "execution_service", None)
    config = get_config()
    config.clients["caller-a"].models = ["*"]
    save_config(config)
    response = h.client.get("/v1/models")
    assert [item["id"] for item in response.json()["data"]] == list(get_config().models)
    for model in ("legacy-chat", "native-chat"):
        response = h.client.post("/v1/chat/completions", json=body(model))
        assert response.status_code == 200, response.text
        assert response.json()["choices"][0]["message"]["content"] == "legacy provider output"
    response = h.client.post("/v1/chat/completions", json=body("unknown-model"))
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "model_not_found"
    assert h.legacy.await_count == 2
    assert not h.dispatched
    save_config(HarnessConfig(clients=config.clients))
    assert h.client.get("/v1/models").json()["data"] == []


@pytest.mark.parametrize("model,status", [
    ("legacy-chat", 200), ("inactive-native", 200), ("native-chat", 501), ("other-native", 403), (None, 200),
])
def test_conversation_event_stream_uses_alias_execution(routing, monkeypatch, model, status):
    interaction = {"id": 12, "status": "completed", "model": model} if model is not None else None
    monkeypatch.setattr(chat, "get_interaction_for_conversation", AsyncMock(return_value=interaction))
    observed = []

    async def stream_activity(interaction_id, after_seq, *, live):
        observed.append((interaction_id, after_seq, live))
        yield "event: activity\ndata: legacy event\n\n"

    monkeypatch.setattr(chat, "stream_activity", stream_activity)
    response = routing.client.get("/v1/conversations/key/events/stream?after_seq=5")
    assert response.status_code == status, response.text
    if model is None:
        # 最初の interaction 前は Native 無効時と同じ空の stream。
        assert response.text == ""
        assert not observed
    elif status == 200:
        assert "legacy event" in response.text
        assert observed == [(12, 5, False)]
    else:
        assert not observed
