"""Tests for Clients in config.yml, admin CRUD + live reload, legacy bearer, and migration script (ADR-0011)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
import yaml

from kotoyose import auth
from kotoyose.api_key_store import ApiKeyStore
from kotoyose.app import app
from kotoyose.config import Client, HarnessConfig, get_client, get_clients, reload_config
from kotoyose.execution_resources import NativeExecutionConfig
from kotoyose.routes import chat

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text("models:\n  chat-a: {}\n  chat-b: {}\n", encoding="utf-8")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(cfg_path))
    monkeypatch.delenv("KOTOYOSE_EXECUTION_CONFIG", raising=False)
    monkeypatch.delenv("KOTOYOSE_COMPATIBILITY_CONFIG", raising=False)
    monkeypatch.setattr(auth, "API_KEY", "admin-password")
    monkeypatch.setattr(auth, "BASIC_AUTH_USER", "admin")
    reload_config()

    prev_store = getattr(app.state, "api_key_store", None)
    prev_service = getattr(app.state, "execution_service", None)
    prev_compat = getattr(app.state, "compatibility_adapter", None)
    store = ApiKeyStore()
    app.state.api_key_store = store
    app.state.execution_service = None
    app.state.compatibility_adapter = None

    monkeypatch.setattr(chat, "get_or_resume_client", AsyncMock(return_value=(AsyncMock(), False)))
    monkeypatch.setattr(chat, "ask_claude", AsyncMock(return_value=("chat response", None, "sess-1")))
    monkeypatch.setattr(chat, "_cache_identity", lambda *args: ("claude", "alias", "digest"))
    for name in (
        "record_received",
        "record_completed",
        "record_failed",
        "_safe_upsert_session",
        "_rotate_hash_key",
        "backfill_conversation_key",
        "_safe_disconnect_client",
    ):
        monkeypatch.setattr(chat, name, AsyncMock())
    monkeypatch.setattr(chat, "record_received", AsyncMock(return_value=None))
    monkeypatch.setattr(chat, "start_event_writer", lambda *args: asyncio.Queue())

    try:
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, path=cfg_path, store=store, tmp_path=tmp_path)
    finally:
        app.state.api_key_store = prev_store
        app.state.execution_service = prev_service
        app.state.compatibility_adapter = prev_compat
        reload_config()


# ── Config & Validation ───────────────────────────────────────────────────────


@pytest.mark.parametrize("valid_name", ["a", "taskhub", "task-hub-1", "a" + ("0" * 63)])
def test_client_name_accepts_valid_identifiers(valid_name):
    cfg = HarnessConfig(clients={valid_name: Client()})
    assert get_client(valid_name, cfg) is cfg.clients[valid_name]
    assert valid_name in get_clients(cfg)


@pytest.mark.parametrize("bad_name", ["", "TaskHub", "task_hub", "-leading", "a" * 65, "has space", "dot.name"])
def test_client_name_rejects_invalid_identifiers(bad_name):
    with pytest.raises(ValidationError, match="Invalid client name"):
        HarnessConfig(clients={bad_name: Client()})


def test_client_field_validation_and_single_accessor(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    c = Client(
        models=["chat-a"],
        places=["isolated", "host"],
        max_surface="read",
        workspace_roots=[ws],

        external_mcps=["memory"],
        allowed_env_vars=["FOO"],
        allowed_provenance=["trusted"],
        max_run_seconds=120,
        max_memory_bytes=256 * 1024 * 1024,
        max_processes=16,
        max_cpus=1.5,
        max_concurrent_runs=2,
    )
    assert c.agent_models == ["chat-a"]
    assert c.mcp_servers == ["memory"]
    assert c.env_keys == ["FOO"]
    assert c.provenance == ["trusted"]
    assert c.max_seconds == 120
    assert c.max_pids == 16
    assert c.max_concurrent == 2

    cfg = HarnessConfig(clients={"worker-1": c})
    assert get_clients(cfg) == {"worker-1": c}
    assert get_client("worker-1", cfg) == c
    assert get_client("missing", cfg) is None

    with pytest.raises(ValidationError, match="workspace_roots entries must be absolute"):
        Client(workspace_roots=[Path("relative/dir")])

    with pytest.raises(ValidationError):
        Client(places=["container"])

    with pytest.raises(ValidationError):
        Client(max_surface="sudo")

    with pytest.raises(ValidationError):
        Client(unknown_field="nope")


def test_execution_json_with_callers_is_rejected():
    with pytest.raises(ValidationError, match="callers moved to config\\.yml clients \\(ADR-0011\\)"):
        NativeExecutionConfig.model_validate({
            "schema_version": "1",
            "image_id": "sha256:" + ("a" * 64),
            "callers": {"taskhub": {}},
        })


# ── Legacy bearer transition & chat-only client scope ─────────────────────────




# ── Admin CRUD, 409 on bound keys & live reload ───────────────────────────────


def test_admin_clients_crud_live_reload_and_conflict_on_delete(isolated_env):
    c = isolated_env.client
    admin_auth = ("admin", "admin-password")
    ws_dir = str(isolated_env.tmp_path / "ws")

    # Initially empty
    listed = c.get("/admin/clients", auth=admin_auth)
    assert listed.status_code == 200
    assert listed.json() == {"clients": []}

    # Reject invalid name and invalid relative workspace_roots
    assert c.put(
        "/admin/clients/BadName",
        auth=admin_auth,
        json={"models": ["chat-a"], "places": ["isolated"]},
    ).status_code == 422
    assert c.put(
        "/admin/clients/good-name",
        auth=admin_auth,
        json={"models": ["chat-a"], "workspace_roots": ["relative/path"]},
    ).status_code == 422
    assert get_clients() == {}

    # PUT create -> persisted & live reloaded without restart
    created = c.put(
        "/admin/clients/taskhub",
        auth=admin_auth,
        json={
            "models": ["chat-a"],
            "places": ["isolated"],
            "max_surface": "read",
            "workspace_roots": [ws_dir],

        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["created"] is True
    assert created.json()["client"]["models"] == ["chat-a"]
    assert created.json()["client"]["workspace_roots"] == [ws_dir]
    assert "taskhub" in get_clients()
    persisted = yaml.safe_load(isolated_env.path.read_text(encoding="utf-8"))
    assert persisted["clients"]["taskhub"]["models"] == ["chat-a"]

    # Bind two active API keys to the newly created client (no restart)
    k1 = c.post("/admin/api-keys", auth=admin_auth, json={"name": "alpha-key", "client": "taskhub"})
    k2 = c.post("/admin/api-keys", auth=admin_auth, json={"name": "beta-key", "client": "taskhub"})
    assert k1.status_code == 200 and k2.status_code == 200
    token1 = k1.json()["key"]

    # Live check: token1 works on chat-a and is blocked on chat-b
    assert c.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token1}"},
        json={"model": "chat-a", "messages": [{"role": "user", "content": "hi"}]},
    ).status_code == 200
    assert c.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token1}"},
        json={"model": "chat-b", "messages": [{"role": "user", "content": "hi"}]},
    ).status_code == 403

    # PUT update expands models to chat-b and takes effect immediately without restart
    updated = c.put(
        "/admin/clients/taskhub",
        auth=admin_auth,
        json={
            "models": ["chat-a", "chat-b"],
            "places": ["isolated", "host"],
            "max_surface": "write",
            "workspace_roots": [],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["created"] is False
    assert c.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token1}"},
        json={"model": "chat-b", "messages": [{"role": "user", "content": "hi"}]},
    ).status_code == 200

    # GET /admin/clients reports bound keys
    clients_list = c.get("/admin/clients", auth=admin_auth).json()["clients"]
    assert len(clients_list) == 1
    assert clients_list[0]["active_keys"] == ["alpha-key", "beta-key"]

    # DELETE blocked with 409 listing bound key names
    conflict = c.delete("/admin/clients/taskhub", auth=admin_auth)
    assert conflict.status_code == 409
    assert conflict.json()["keys"] == ["alpha-key", "beta-key"]
    assert "alpha-key" in conflict.json()["error"]["message"]
    assert "beta-key" in conflict.json()["error"]["message"]
    assert "taskhub" in get_clients()

    # Revoke both keys -> DELETE succeeds
    assert c.post("/admin/api-keys/alpha-key/revoke", auth=admin_auth).status_code == 200
    assert c.delete("/admin/clients/taskhub", auth=admin_auth).status_code == 409
    assert c.post("/admin/api-keys/beta-key/revoke", auth=admin_auth).status_code == 200
    deleted = c.delete("/admin/clients/taskhub", auth=admin_auth)
    assert deleted.status_code == 200
    assert "taskhub" not in get_clients()
    assert c.delete("/admin/clients/taskhub", auth=admin_auth).status_code == 404


# ── One-shot migration script ─────────────────────────────────────────────────
