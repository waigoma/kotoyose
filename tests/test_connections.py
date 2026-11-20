"""Tests for S4a Connections entity, Admin API, pool state sharing, and conversion script."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from kotoyose.app import app
from kotoyose.config import AgentRoute, Connection, HarnessConfig, HarnessSettings, reload_config, save_config
from kotoyose.execution_limits import ProviderUsageLimit
from kotoyose.execution_resources import DriverInstall
from kotoyose.model_pools import ModelPools, pool_name

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)

client = TestClient(app)


def _old_route_digest(driver: str, model: str, endpoint: str | None, credential: str, model_provider) -> str:
    if credential.startswith("secret:"):
        cred_kind = "secret"
    elif credential.startswith("env:"):
        cred_kind = "env"
    else:
        cred_kind = "named"
    payload = {
        "driver": driver,
        "model": model,
        "endpoint": endpoint,
        "credential_kind": cred_kind,
        "model_provider": model_provider,
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class _InMemoryPoolStateStore:
    """Minimal PoolStateStore double for verifying shared connection pool limits."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.last: dict[tuple[str, str], datetime] = {}

    async def states(self, names: list[str]) -> dict[str, dict]:
        return {name: self.rows[name] for name in names if name in self.rows}

    async def last_selected(self, pool_aliases: list[str]) -> dict[tuple[str, str], datetime]:
        return {k: v for k, v in self.last.items() if k[0] in pool_aliases}

    async def observe(self, name: str, state: str, until: datetime | None, at: datetime) -> None:
        prev = self.rows.get(name)
        if prev is not None and prev["at"] > at:
            return
        self.rows[name] = {"state": state, "until": until, "at": at}

    async def select(self, pool_alias: str, names: list[str], choose) -> list[str]:
        states = await self.states(names)
        last = {alias: dt for (pa, alias), dt in self.last.items() if pa == pool_alias}
        return choose(states, last)

    async def record_selection(self, pool_alias: str, alias: str) -> None:
        self.last[(pool_alias, alias)] = NOW


# ── Validation & replaced-key errors ───────────────────────────────────────


def test_replaced_keys_fail_loudly_naming_replacements():
    with pytest.raises(ValidationError, match="credential.*connection"):
        AgentRoute(driver="claude.agent-sdk", model="claude-sonnet-4-6", connection="c", credential="env:X")
    with pytest.raises(ValidationError, match="endpoint.*connection"):
        AgentRoute(driver="claude.agent-sdk", model="claude-sonnet-4-6", connection="c", endpoint="https://x.invalid/v1")
    with pytest.raises(ValidationError, match="pool.*connection"):
        AgentRoute(driver="claude.agent-sdk", model="claude-sonnet-4-6", connection="c", pool="p")
    with pytest.raises(ValidationError, match="credentials.*connections"):
        DriverInstall(cli_binary=Path("/usr/bin/true"), credentials={"claude-oauth": {"mode": "runtime_managed"}})
    with pytest.raises(ValidationError, match="cursor_api_key.*connections"):
        HarnessSettings(cursor_api_key="fake-cursor-value")


def test_unknown_connection_and_provider_mismatch_rejected():
    with pytest.raises(ValidationError, match="不明な connection"):
        HarnessConfig(
            connections={},
            routes={"r": AgentRoute(driver="claude.agent-sdk", model="claude-sonnet-4-6", connection="missing")},
        )
    with pytest.raises(ValidationError, match="provider"):
        HarnessConfig(
            connections={"codex-conn": Connection(provider="codex", kind="subscription", data_policy="no_training")},
            routes={"r": AgentRoute(driver="claude.agent-sdk", model="claude-sonnet-4-6", connection="codex-conn")},
        )


def test_subscription_connection_default_home():
    conn = Connection(provider="claude", kind="subscription", data_policy="no_training", secret="claude-oauth")
    assert conn.home == "default"
    assert conn.secret_name == "claude-oauth"
    with pytest.raises(ValidationError, match="secret"):
        Connection(provider="claude", kind="subscription", data_policy="no_training")
    with pytest.raises(ValidationError, match="secret"):
        Connection(provider="anthropic", kind="subscription", data_policy="no_training")
    with pytest.raises(ValidationError, match="secret"):
        Connection(provider="codex", kind="subscription", data_policy="no_training", secret="codex-secret")
    with pytest.raises(ValidationError, match="endpoint"):
        Connection(provider="claude", kind="endpoint", data_policy="no_training", secret="env:TOKEN")


# ── Admin API (/admin/connections & /admin/machine) ────────────────────────


@pytest.fixture
def isolated_admin_config(tmp_path, monkeypatch):
    path = tmp_path / "config.yml"
    exe_path = tmp_path / "execution.json"
    exe_path.write_text(
        json.dumps({
            "schema_version": "1",
            "image_id": "sha256:" + "a" * 64,
            "drivers": {
                "claude.agent-sdk": {"cli_binary": "/usr/bin/true"},
                "codex.app-server": {"cli_binary": "/usr/bin/false"},
            },
            "callers": {
            "caller-a": {

                "workspace_roots": [str(tmp_path)],
                "agent_models": [],
                "places": ["isolated"],
                "max_seconds": 60,
                "max_memory_bytes": 128 * 1024 * 1024,
                "max_pids": 32,
                "max_cpus": 1,
                "max_concurrent": 1,
            }
        },
        }),
        encoding="utf-8",
    )
    path.write_text(
        yaml.safe_dump({
            "connections": {
                "claude-oauth": {
                    "provider": "claude",
                    "kind": "subscription", "data_policy": "no_training",
                    "secret": "env:CLAUDE_CODE_OAUTH_TOKEN",
                },
                "nine-router": {
                    "provider": "claude",
                    "kind": "endpoint", "data_policy": "no_training",
                    "endpoint": "https://router.example.invalid/v1",
                    "secret": "env:NINE_ROUTER_KEY",
                },
            },
            "routes": {
                "claude-main": {"driver": "claude.agent-sdk", "model": "claude-sonnet-4-6", "connection": "claude-oauth"},
                "claude-router": {"driver": "claude.agent-sdk", "model": "cc/claude-sonnet-5-5", "connection": "nine-router"},
            },
            "models": {
                "chat-a": {"route": "claude-main"},
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    monkeypatch.setenv("KOTOYOSE_EXECUTION_CONFIG", str(exe_path))
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    reload_config()
    yield path
    reload_config()


def test_admin_connections_crud_and_delete_refused_while_referenced(isolated_admin_config):
    listed = client.get("/admin/connections")
    assert listed.status_code == 200
    by_id = {row["id"]: row for row in listed.json()["connections"]}
    assert by_id["claude-oauth"]["home"] == "default"
    assert by_id["claude-oauth"]["routes"] == ["claude-main"]
    assert by_id["nine-router"]["routes"] == ["claude-router"]

    created = client.post(
        "/admin/connections/cursor-key",
        json={"provider": "cursor", "kind": "api_key", "data_policy": "no_training", "secret": "env:CURSOR_KEY_TEST"},
    )
    assert created.status_code == 200
    assert created.json()["connection"]["id"] == "cursor-key"

    updated = client.put(
        "/admin/connections/cursor-key",
        json={"provider": "cursor", "kind": "api_key", "data_policy": "no_training", "secret": "env:CURSOR_KEY_UPDATED"},
    )
    assert updated.status_code == 200
    assert updated.json()["connection"]["secret"] == "env:CURSOR_KEY_UPDATED"

    # Referenced connection cannot be deleted (409 listing referencing routes).
    blocked = client.delete("/admin/connections/nine-router")
    assert blocked.status_code == 409
    assert "claude-router" in blocked.json()["error"]["message"]

    # Delete route first, then deleting connection succeeds.
    assert client.delete("/admin/routes/claude-router").status_code == 200
    assert client.delete("/admin/connections/nine-router").status_code == 200
    remaining = {row["id"] for row in client.get("/admin/connections").json()["connections"]}
    assert "nine-router" not in remaining and "cursor-key" in remaining


def test_admin_machine_no_longer_lists_credentials(isolated_admin_config):
    resp = client.get("/admin/machine")
    assert resp.status_code == 200
    for driver in resp.json()["drivers"]:
        assert "credentials" not in driver


# ── Pool state shared across routes on one connection ──────────────────────


@pytest.mark.asyncio
async def test_pool_state_key_is_connection_and_shared_between_routes():
    config = HarnessConfig.model_validate({
        "connections": {
            "shared-account": {"provider": "claude", "kind": "api_key", "data_policy": "no_training", "secret": "env:SHARED_KEY"},
            "backup-account": {"provider": "claude", "kind": "api_key", "data_policy": "no_training", "secret": "env:BACKUP_KEY"},
        },
        "routes": {
            "route-sonnet": {"driver": "claude.agent-sdk", "model": "claude-sonnet-4-6", "connection": "shared-account"},
            "route-opus": {"driver": "claude.agent-sdk", "model": "claude-opus-4-6", "connection": "shared-account"},
            "route-backup": {"driver": "claude.agent-sdk", "model": "claude-haiku-4-5", "connection": "backup-account"},
        },
        "models": {
            "sonnet": {"route": "route-sonnet", "agent": {"place": "isolated", "surface": "write"}},
            "opus": {"route": "route-opus", "agent": {"place": "isolated", "surface": "write"}},
            "backup": {"route": "route-backup", "agent": {"place": "isolated", "surface": "write"}},
            "coding": {"candidates": ["opus", "backup"], "policy": "first_available"},
        },
    })
    assert pool_name(config, "sonnet") == "shared-account"
    assert pool_name(config, "opus") == "shared-account"
    assert pool_name(config, "backup") == "backup-account"

    store = _InMemoryPoolStateStore()
    pools = ModelPools(store, clock=lambda: NOW)
    # Observe a usage limit on `sonnet` (using `route-sonnet` -> `shared-account`).
    limit = ProviderUsageLimit(
        provider="claude", limit_window="five_hour", reset_at=NOW + timedelta(hours=1),
        detected_at=NOW, resume="unavailable", unavailable_reason="no_session",
    )
    await pools.observe_attempt(config, "sonnet", "failed", limit)
    # Because `opus` uses the same connection (`shared-account`), selecting from `coding` skips `opus`!
    ordered = await pools.select(config, "coding", refuse_needs_ack=False)
    assert ordered == ["backup"]


# ── Conversion script & digest rewriting ───────────────────────────────────






def test_connection_show_quota_config_roundtrip(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yml"
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(cfg_path))

    cfg = HarnessConfig.model_validate({
        "connections": {
            "claude-default": {
                "provider": "claude",
                "kind": "subscription", "data_policy": "no_training",
                "secret": "env:CLAUDE_TOKEN",
            },
            "codex-hidden": {
                "provider": "codex",
                "kind": "subscription", "data_policy": "no_training",
                "show_quota": False,
            },
            "cursor-hidden": {
                "provider": "cursor",
                "kind": "api_key", "data_policy": "no_training",
                "secret": "env:CURSOR_KEY",
                "show_quota": False,
            },
            "router-hidden": {
                "provider": "claude",
                "kind": "endpoint", "data_policy": "no_training",
                "endpoint": "https://router.example.invalid/v1",
                "secret": "env:ROUTER_KEY",
                "show_quota": False,
            },
        },
    })

    assert cfg.connections["claude-default"].show_quota is True
    assert cfg.connections["codex-hidden"].show_quota is False
    assert cfg.connections["cursor-hidden"].show_quota is False
    assert cfg.connections["router-hidden"].show_quota is False

    dumped = cfg.model_dump(mode="json")
    assert "show_quota" not in dumped["connections"]["claude-default"]
    assert dumped["connections"]["codex-hidden"]["show_quota"] is False
    assert dumped["connections"]["cursor-hidden"]["show_quota"] is False
    assert dumped["connections"]["router-hidden"]["show_quota"] is False

    save_config(cfg)
    raw_yaml = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert "show_quota" not in raw_yaml["connections"]["claude-default"]
    assert raw_yaml["connections"]["codex-hidden"]["show_quota"] is False
    assert raw_yaml["connections"]["cursor-hidden"]["show_quota"] is False
    assert raw_yaml["connections"]["router-hidden"]["show_quota"] is False

    reloaded = reload_config()
    assert reloaded.connections["claude-default"].show_quota is True
    assert reloaded.connections["codex-hidden"].show_quota is False
    assert reloaded.connections["cursor-hidden"].show_quota is False
    assert reloaded.connections["router-hidden"].show_quota is False
    reload_config()


def test_admin_connections_show_quota_api(isolated_admin_config):
    listed = client.get("/admin/connections")
    assert listed.status_code == 200
    by_id = {row["id"]: row for row in listed.json()["connections"]}
    assert by_id["claude-oauth"]["show_quota"] is True
    assert by_id["nine-router"]["show_quota"] is True

    created = client.post(
        "/admin/connections/agy-quiet",
        json={"provider": "antigravity", "kind": "subscription", "data_policy": "no_training", "show_quota": False},
    )
    assert created.status_code == 200
    assert created.json()["connection"]["show_quota"] is False

    listed_after_create = {
        row["id"]: row for row in client.get("/admin/connections").json()["connections"]
    }
    assert listed_after_create["agy-quiet"]["show_quota"] is False
    raw_after_create = yaml.safe_load(isolated_admin_config.read_text(encoding="utf-8"))
    assert raw_after_create["connections"]["agy-quiet"]["show_quota"] is False

    updated = client.put(
        "/admin/connections/agy-quiet",
        json={"provider": "antigravity", "kind": "subscription", "data_policy": "no_training", "show_quota": True},
    )
    assert updated.status_code == 200
    assert updated.json()["connection"]["show_quota"] is True

    listed_after_update = {
        row["id"]: row for row in client.get("/admin/connections").json()["connections"]
    }
    assert listed_after_update["agy-quiet"]["show_quota"] is True
    raw_after_update = yaml.safe_load(isolated_admin_config.read_text(encoding="utf-8"))
    assert "show_quota" not in raw_after_update["connections"]["agy-quiet"]
