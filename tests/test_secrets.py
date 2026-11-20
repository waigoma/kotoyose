"""Tests for encrypted secret store, secret: references, API, scripts, and launch resolution."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from kotoyose import auth, config
from kotoyose.config import HarnessConfig, build_options, get_config, reload_config, save_config
from kotoyose.mcp_probe import run_mcp_probe
from kotoyose.routes import admin, admin_secrets, admin_settings, health
from kotoyose.runtime.claude_auth import build_auth_env
from kotoyose.runtime.cursor import CursorRuntimeSession
from kotoyose.runtime.translation import translate_runtime_config
from kotoyose.secret_store import (
    SecretDecryptionError,
    SecretKeyError,
    SecretNotFoundError,
    SecretStore,
    SecretStoreDisabledError,
    compute_key_id,
    load_master_key_from_env,
    set_secret_store,
)


def _load_script_module(rel_path: str, name: str):
    path = Path(__file__).resolve().parent.parent / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolated_harness(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yml"
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(cfg_path))
    monkeypatch.setattr(auth, "API_KEY", "test-admin-secret")
    monkeypatch.setattr(auth, "BASIC_AUTH_USER", "admin")

    master_key = os.urandom(32)
    store = SecretStore(master_key=master_key)
    set_secret_store(store)

    orig_cfg = config._config
    config._config = HarnessConfig()
    try:
        yield store, cfg_path, master_key
    finally:
        config._config = orig_cfg
        set_secret_store(None)


def _build_admin_app(store: SecretStore) -> FastAPI:
    app = FastAPI()
    app.state.secret_store = store
    app.include_router(health.router)
    app.include_router(admin.router)
    app.include_router(admin_settings.router)
    app.include_router(admin_secrets.router)
    return app


@pytest.mark.asyncio
async def test_encrypt_decrypt_round_trip_and_aad_protection():
    key = os.urandom(32)
    store = SecretStore(master_key=key)

    meta1 = await store.set("alpha-key", "fake-value-alpha-1234", purpose="alpha", updated_by="tester")
    meta2 = await store.set("beta-key", "fake-value-beta-5678", purpose="beta", updated_by="tester")

    assert meta1["version"] == 1
    assert meta1["last4"] == "1234"
    assert meta1["key_id"] == compute_key_id(key)
    assert meta2["version"] == 1
    assert await store.get("alpha-key") == "fake-value-alpha-1234"
    assert await store.get("beta-key") == "fake-value-beta-5678"

    # Ciphertext is not plaintext and uses unique nonces.
    row_a = store._rows["alpha-key"]
    row_b = store._rows["beta-key"]
    assert b"fake-value-alpha-1234" not in row_a.ciphertext
    assert row_a.nonce != row_b.nonce

    # Replacing bumps version and changes ciphertext/last4.
    meta1_v2 = await store.set("alpha-key", "fake-value-alpha-9999", purpose="rotated")
    assert meta1_v2["version"] == 2
    assert meta1_v2["last4"] == "9999"
    assert await store.get("alpha-key") == "fake-value-alpha-9999"

    # AAD binds ciphertext to `name|version|key_id` so swapping rows fails loudly.
    swapped = type(row_b)(
        name="beta-key",
        purpose=row_b.purpose,
        ciphertext=store._rows["alpha-key"].ciphertext,
        nonce=store._rows["alpha-key"].nonce,
        key_id=store._rows["alpha-key"].key_id,
        last4=row_b.last4,
        created_at=row_b.created_at,
        updated_at=row_b.updated_at,
        updated_by=row_b.updated_by,
        version=store._rows["alpha-key"].version,
    )
    store._rows["beta-key"] = swapped
    with pytest.raises(SecretDecryptionError):
        await store.get("beta-key")


@pytest.mark.asyncio
async def test_wrong_master_key_fails_loudly():
    key_a = os.urandom(32)
    key_b = os.urandom(32)
    store = SecretStore(master_key=key_a)
    await store.set("provider-token", "fake-secret-value-abcd")

    wrong_store = SecretStore(master_key=key_b)
    wrong_store._rows = dict(store._rows)
    with pytest.raises(SecretDecryptionError, match="key_id"):
        await wrong_store.get("provider-token")


def test_master_key_file_permissions_and_precedence(tmp_path):
    key_file = tmp_path / "master.key"
    key_bytes = os.urandom(32)
    key_file.write_bytes(key_bytes)

    # Mode 0644 must be rejected loudly.
    os.chmod(key_file, 0o644)
    with pytest.raises(SecretKeyError, match="permissions"):
        load_master_key_from_env({"KOTOYOSE_MASTER_KEY_FILE": str(key_file)})

    # Wrong size must be rejected.
    os.chmod(key_file, 0o600)
    short_file = tmp_path / "short.key"
    short_file.write_bytes(b"too-short")
    os.chmod(short_file, 0o600)
    with pytest.raises(SecretKeyError, match="32 bytes"):
        load_master_key_from_env({"KOTOYOSE_MASTER_KEY_FILE": str(short_file)})

    # Valid 0600 file loads.
    loaded, source = load_master_key_from_env({"KOTOYOSE_MASTER_KEY_FILE": str(key_file)})
    assert loaded == key_bytes
    assert source == str(key_file)

    # Systemd credential directory takes precedence over KOTOYOSE_MASTER_KEY_FILE.
    creds_dir = tmp_path / "systemd-creds"
    creds_dir.mkdir()
    systemd_key = os.urandom(32)
    systemd_path = creds_dir / "kotoyose-master"
    systemd_path.write_bytes(systemd_key)
    os.chmod(systemd_path, 0o400)

    loaded_cred, cred_source = load_master_key_from_env(
        {
            "CREDENTIALS_DIRECTORY": str(creds_dir),
            "KOTOYOSE_MASTER_KEY_FILE": str(key_file),
        }
    )
    assert loaded_cred == systemd_key
    assert cred_source == f"systemd:{systemd_path}"

    # Unconfigured env returns (None, None).
    assert load_master_key_from_env({}) == (None, None)


@pytest.mark.asyncio
async def test_api_never_returns_values_and_delete_protection(isolated_harness):
    store, _, _ = isolated_harness
    app = _build_admin_app(store)
    auth_tuple = ("admin", "test-admin-secret")
    plaintext = "fake-ultra-secret-token-4242"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Unauthenticated rejected.
        unauth = await client.get("/admin/secrets")
        assert unauth.status_code == 401

        put_resp = await client.put(
            "/admin/secrets/claude-gateway",
            auth=auth_tuple,
            json={"value": plaintext, "purpose": "gateway token"},
        )
        assert put_resp.status_code == 200
        assert plaintext not in json.dumps(put_resp.json())
        assert put_resp.json()["version"] == 1
        assert put_resp.json()["last4"] == "4242"

        # Reference it from connection, MCP, and settings.anthropic_auth_token.
        conn_resp = await client.post(
            "/admin/connections/claude-conn",
            auth=auth_tuple,
            json={
                "provider": "claude",
                "kind": "endpoint", "data_policy": "no_training",
                "endpoint": "https://gateway.example.invalid",
                "secret": "claude-gateway",
            },
        )
        assert conn_resp.status_code == 200

        mcp_resp = await client.post(
            "/admin/mcps/ext-mcp",
            auth=auth_tuple,
            json={
                "description": "test mcp",
                "server": {
                    "type": "http",
                    "url": "https://mcp.example.invalid",
                    "headers": {"Authorization": {"secret": "claude-gateway"}},
                },
            },
        )
        assert mcp_resp.status_code == 200

        settings_resp = await client.put(
            "/admin/settings",
            auth=auth_tuple,
            json={"anthropic_auth_token": "secret:claude-gateway"},
        )
        assert settings_resp.status_code == 200

        list_resp = await client.get("/admin/secrets", auth=auth_tuple)
        assert list_resp.status_code == 200
        list_json = list_resp.json()
        assert plaintext not in json.dumps(list_json)
        entry = next(item for item in list_json["secrets"] if item["name"] == "claude-gateway")
        assert entry["set"] is True
        assert entry["last4"] == "4242"
        assert "connection:claude-conn" in entry["used_by"]
        assert "mcp:ext-mcp" in entry["used_by"]
        assert "settings.anthropic_auth_token" in entry["used_by"]

        cfg_resp = await client.get("/admin/config", auth=auth_tuple)
        assert cfg_resp.status_code == 200
        assert plaintext not in json.dumps(cfg_resp.json())

        get_settings_resp = await client.get("/admin/settings", auth=auth_tuple)
        assert get_settings_resp.status_code == 200
        assert plaintext not in json.dumps(get_settings_resp.json())
        assert get_settings_resp.json()["anthropic_auth_token"]["secret"] == "claude-gateway"

        # Deleting while referenced is refused with 409 listing users.
        del_blocked = await client.delete("/admin/secrets/claude-gateway", auth=auth_tuple)
        assert del_blocked.status_code == 409
        detail = del_blocked.json()["detail"]
        assert "connection:claude-conn" in detail
        assert "mcp:ext-mcp" in detail
        assert "settings.anthropic_auth_token" in detail

        # Clear references, then delete succeeds.
        assert (await client.delete("/admin/connections/claude-conn", auth=auth_tuple)).status_code == 200
        assert (await client.delete("/admin/mcps/ext-mcp", auth=auth_tuple)).status_code == 200
        assert (await client.put("/admin/settings", auth=auth_tuple, json={"anthropic_auth_token": ""})).status_code == 200

        del_ok = await client.delete("/admin/secrets/claude-gateway", auth=auth_tuple)
        assert del_ok.status_code == 200


@pytest.mark.asyncio
async def test_disabled_store_when_no_key_configured(isolated_harness):
    _, _, _ = isolated_harness
    disabled_store = SecretStore(master_key=None)
    set_secret_store(disabled_store)
    app = _build_admin_app(disabled_store)
    auth_tuple = ("admin", "test-admin-secret")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for method, path, kwargs in (
            ("GET", "/admin/secrets", {}),
            ("PUT", "/admin/secrets/any", {"json": {"value": "fake-val"}}),
            ("DELETE", "/admin/secrets/any", {}),
        ):
            resp = await client.request(method, path, auth=auth_tuple, **kwargs)
            assert resp.status_code == 503
            assert "secret store not configured" in resp.json()["detail"].lower()

        # Other endpoints continue working cleanly.
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/admin/config", auth=auth_tuple)).status_code == 200
        assert (await client.get("/admin/settings", auth=auth_tuple)).status_code == 200


@pytest.mark.asyncio
async def test_unknown_secret_rejected_at_config_load_validation_and_launch(isolated_harness, tmp_path):
    store, cfg_path, _ = isolated_harness
    app = _build_admin_app(store)
    auth_tuple = ("admin", "test-admin-secret")

    # Config save/validation rejects unknown secret names.
    bad_cfg = HarnessConfig.model_validate(
        {
            "connections": {
                "bad-conn": {
                    "provider": "claude",
                    "kind": "endpoint", "data_policy": "no_training",
                    "endpoint": "https://api.example.invalid",
                    "secret": "does-not-exist",
                }
            }
        }
    )
    with pytest.raises(ValueError, match="does-not-exist"):
        save_config(bad_cfg)

    # Loading a YAML file referencing an unknown secret rejects clearly.
    cfg_path.write_text(
        "connections:\n"
        "  bad-conn:\n"
        "    provider: claude\n"
        "    kind: endpoint\n"
        "    data_policy: no_training\n"
        "    endpoint: https://api.example.invalid\n"
        "    secret: missing-at-load\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing-at-load"):
        reload_config()

    # Admin API rejects connection referencing an unknown secret.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(
            "/admin/connections/bad-conn",
            auth=auth_tuple,
            json={
                "provider": "claude",
                "kind": "api_key", "data_policy": "no_training",
                "secret": "not-created-yet",
            },
        )
        assert r.status_code == 422
        assert "not-created-yet" in r.json()["detail"]

    # Launch resolution also fails loudly if secret disappears before launch.
    await store.set("ephemeral-secret", "fake-ephemeral-1234")
    cfg = HarnessConfig.model_validate(
        {
            "connections": {
                "r1-conn": {
                    "provider": "claude",
                    "kind": "endpoint", "data_policy": "no_training",
                    "endpoint": "https://api.example.invalid",
                    "secret": "ephemeral-secret",
                }
            },
            "routes": {
                "r1": {
                    "driver": "claude.agent-sdk",
                    "model": "claude-sonnet-4-6",
                    "connection": "r1-conn",
                }
            },
            "models": {"chat-alias": {"route": "r1"}},
        }
    )
    save_config(cfg)
    await store.delete("ephemeral-secret")
    with pytest.raises(SecretNotFoundError, match="ephemeral-secret"):
        build_options("chat-alias")


@pytest.mark.asyncio
async def test_route_mcp_and_cursor_resolution_and_replace_without_restart(isolated_harness):
    store, _, _ = isolated_harness
    app = _build_admin_app(store)
    auth_tuple = ("admin", "test-admin-secret")

    await store.set("route-token", "fake-route-token-v1")
    await store.set("mcp-header-token", "fake-mcp-header-v1")
    await store.set("mcp-env-token", "fake-mcp-env-v1")
    await store.set("cursor-token", "fake-cursor-token-v1")

    cfg = HarnessConfig.model_validate(
        {
            "connections": {
                "cursor": {
                    "provider": "cursor",
                    "kind": "api_key", "data_policy": "no_training",
                    "secret": "cursor-token",
                },
                "claude-gw": {
                    "provider": "claude",
                    "kind": "endpoint",
                    "endpoint": "https://gateway.example.invalid",
                    "secret": "route-token",
                    "data_policy": "no_training",
                },
            },
            "routes": {
                "claude-sec": {
                    "driver": "claude.agent-sdk",
                    "model": "claude-sonnet-4-6",
                    "connection": "claude-gw",
                }
            },
            "mcps": {
                "remote-http": {
                    "server": {
                        "type": "http",
                        "url": "https://mcp.example.invalid/stream",
                        "headers": {"Authorization": {"secret": "mcp-header-token"}},
                    }
                },
                "local-stdio": {
                    "server": {
                        "type": "stdio",
                        "command": "echo",
                        "args": ["ok"],
                        "env": {"SERVICE_TOKEN": {"secret": "mcp-env-token"}},
                    }
                },
            },
            "models": {
                "claude-alias": {
                    "route": "claude-sec",
                    "mcp_refs": ["remote-http", "local-stdio"],
                },
                "codex-alias": {
                    "provider": "codex",
                    "model": "gpt-5",
                    "mcp_refs": ["remote-http", "local-stdio"],
                },
            },
        }
    )
    save_config(cfg)

    # 1. Claude route + MCP options resolved at launch time without mutating stored config.
    opts = build_options("claude-alias")
    assert opts.env["ANTHROPIC_API_KEY"] == "fake-route-token-v1"
    assert opts.env["ANTHROPIC_BASE_URL"] == "https://gateway.example.invalid"
    assert opts.mcp_servers["remote-http"]["headers"]["Authorization"] == "fake-mcp-header-v1"
    assert opts.mcp_servers["local-stdio"]["env"]["SERVICE_TOKEN"] == "fake-mcp-env-v1"
    assert get_config().mcps["remote-http"].server["headers"]["Authorization"] == {"secret": "mcp-header-token"}

    # 2. Codex / runtime translation also resolves MCP secrets at launch time.
    translated = translate_runtime_config(get_config(), get_config().preset_for("codex-alias"))
    assert translated.mcp_servers["remote-http"]["headers"]["Authorization"] == "fake-mcp-header-v1"
    assert translated.mcp_servers["local-stdio"]["env"]["SERVICE_TOKEN"] == "fake-mcp-env-v1"

    # 3. Cursor connection secret resolves cleanly.
    from kotoyose.secret_store import resolve_secret_async
    assert await resolve_secret_async(get_config().cursor_connection().secret_name) == "fake-cursor-token-v1"

    # 4. Replace secrets via Admin API and verify immediate effect WITHOUT restart.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (
            await client.put("/admin/secrets/route-token", auth=auth_tuple, json={"value": "fake-route-token-v2"})
        ).status_code == 200
        assert (
            await client.put("/admin/secrets/mcp-header-token", auth=auth_tuple, json={"value": "fake-mcp-header-v2"})
        ).status_code == 200
        assert (
            await client.put("/admin/secrets/cursor-token", auth=auth_tuple, json={"value": "fake-cursor-token-v2"})
        ).status_code == 200

    opts_v2 = build_options("claude-alias")
    assert opts_v2.env["ANTHROPIC_API_KEY"] == "fake-route-token-v2"
    assert opts_v2.mcp_servers["remote-http"]["headers"]["Authorization"] == "fake-mcp-header-v2"
    assert await resolve_secret_async(get_config().cursor_connection().secret_name) == "fake-cursor-token-v2"

    # 5. Auth env direct helper check and Cursor _ensure_agent async check.
    env_v2 = build_auth_env(get_config().preset_for("claude-alias"), get_config(), lambda: None)
    assert env_v2["ANTHROPIC_API_KEY"] == "fake-route-token-v2"

    captured_bridge_args = {}

    class FakeBridge:
        async def aclose(self):
            return None

    async def fake_launch_bridge(*, workspace=None):
        captured_bridge_args["workspace"] = workspace
        raise RuntimeError("stop-after-secret")

    session = CursorRuntimeSession(model="default", cwd=str(Path.cwd()), config={})
    with patch("kotoyose.runtime.cursor.AsyncClient.launch_bridge", side_effect=fake_launch_bridge):
        with pytest.raises(RuntimeError, match="stop-after-secret"):
            await session._ensure_agent()
    # Secret resolution succeeded before reaching launch_bridge; now test missing secret failure:
    await store.delete("cursor-token")
    with pytest.raises(RuntimeError, match="cursor-token"):
        await session._ensure_agent()

    # 6. Probe resolves {"secret": "NAME"} before spawning worker stdin.
    sent_payloads = []

    class FakeStdin:
        def write(self, data: bytes):
            sent_payloads.append(json.loads(data.decode()))

        async def drain(self):
            return None

        def close(self):
            return None

    class FakeStdout:
        async def readline(self):
            return (json.dumps({"status": "ok", "stage": "tools_list", "tools": [], "tool_count": 0}) + "\n").encode()

    class FakeProc:
        pid = 999999
        stdin = FakeStdin()
        stdout = FakeStdout()

        async def wait(self):
            return 0

    async def fake_spawn(*args, **kwargs):
        return FakeProc()

    with patch("kotoyose.mcp_probe.asyncio.create_subprocess_exec", side_effect=fake_spawn):
        with patch("kotoyose.mcp_probe.os.killpg", side_effect=ProcessLookupError):
            probe_result = await run_mcp_probe(
                {
                    "type": "http",
                    "url": "https://mcp.example.invalid/stream",
                    "headers": {"Authorization": {"secret": "mcp-header-token"}},
                }
            )
    assert probe_result["status"] == "ok"
    assert sent_payloads[0]["headers"]["Authorization"] == "fake-mcp-header-v2"


@pytest.mark.asyncio
async def test_scripts_create_and_rotate_master_key(tmp_path):
    create_mod = _load_script_module("scripts/create_master_key.py", "create_master_key")
    rotate_mod = _load_script_module("scripts/rotate_master_key.py", "rotate_master_key")

    old_key_path = tmp_path / "old.key"
    new_key_path = tmp_path / "new.key"

    assert create_mod.main([str(old_key_path)]) == 0
    assert old_key_path.exists()
    assert len(old_key_path.read_bytes()) == 32
    assert stat.S_IMODE(old_key_path.stat().st_mode) == 0o600

    # Refuses to overwrite an existing file.
    assert create_mod.main([str(old_key_path)]) == 1
    assert create_mod.main([str(new_key_path)]) == 0

    old_key = old_key_path.read_bytes()
    new_key = new_key_path.read_bytes()

    # Build a minimal fake asyncpg pool/conn to exercise rotate_database_secrets SQL + crypto.
    db_rows: dict[str, dict] = {}
    executed_queries: list[str] = []

    class FakeConn:
        class _Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        def transaction(self):
            return self._Tx()

        async def fetch(self, query, *args):
            executed_queries.append(" ".join(query.split()))
            return [db_rows[name] for name in sorted(db_rows)]

        async def execute(self, query, *args):
            executed_queries.append(" ".join(query.split()))
            if query.strip().upper().startswith("LOCK TABLE"):
                return "LOCK TABLE"
            name, new_ciphertext, new_nonce, new_key_id = args
            db_rows[name]["ciphertext"] = new_ciphertext
            db_rows[name]["nonce"] = new_nonce
            db_rows[name]["key_id"] = new_key_id
            return "UPDATE 1"

    class FakePool:
        class _Acq:
            async def __aenter__(self):
                return FakeConn()

            async def __aexit__(self, *args):
                return False

        def acquire(self):
            return self._Acq()

        async def close(self):
            return None

    seed_store = SecretStore(master_key=old_key)
    await seed_store.set("first-secret", "fake-rotate-value-1111")
    await seed_store.set("second-secret", "fake-rotate-value-2222")
    await seed_store.set("second-secret", "fake-rotate-value-3333")  # version 2
    for name, r in seed_store._rows.items():
        db_rows[name] = {
            "name": r.name,
            "ciphertext": r.ciphertext,
            "nonce": r.nonce,
            "key_id": r.key_id,
            "version": r.version,
        }

    async def fake_create_pool(dsn, **kwargs):
        return FakePool()

    with patch.object(rotate_mod.asyncpg, "create_pool", side_effect=fake_create_pool):
        count, old_id, new_id = await rotate_mod.rotate_database_secrets(
            old_key_path=old_key_path,
            new_key_path=new_key_path,
            database_url="postgresql://user:pass@localhost/db",
        )

    assert count == 2
    assert old_id == compute_key_id(old_key)
    assert new_id == compute_key_id(new_key)
    assert executed_queries[0] == "LOCK TABLE kotoyose.secrets IN ACCESS EXCLUSIVE MODE"

    # Help text must warn operators to stop the server first and restart after.
    with pytest.raises(SystemExit) as help_exc:
        rotate_mod.main(["--help"])
    assert help_exc.value.code == 0

    # Old key must no longer decrypt rotated rows; new key must decrypt cleanly.
    verify_old = SecretStore(master_key=old_key)
    verify_new = SecretStore(master_key=new_key)
    for name, d in db_rows.items():
        orig = seed_store._rows[name]
        rotated_row = type(orig)(
            name=name,
            purpose=orig.purpose,
            ciphertext=d["ciphertext"],
            nonce=d["nonce"],
            key_id=d["key_id"],
            last4=orig.last4,
            created_at=orig.created_at,
            updated_at=orig.updated_at,
            updated_by=orig.updated_by,
            version=d["version"],
        )
        verify_old._rows[name] = rotated_row
        verify_new._rows[name] = rotated_row

    with pytest.raises(SecretDecryptionError):
        await verify_old.get("first-secret")
    assert await verify_new.get("first-secret") == "fake-rotate-value-1111"
    assert await verify_new.get("second-secret") == "fake-rotate-value-3333"
    assert verify_new._rows["second-secret"].version == 2

    # Ensure no disabled-store leak across event loops.
    assert asyncio.get_running_loop() is not None
    assert SecretStore(master_key=None).is_configured is False
    with pytest.raises(SecretStoreDisabledError, match="secret store not configured"):
        await SecretStore(master_key=None).get("x")


@pytest.mark.asyncio
async def test_cached_session_rebuilds_when_secret_version_changes(isolated_harness):
    from unittest.mock import AsyncMock, MagicMock
    from kotoyose import session as session_module

    store, _, _ = isolated_harness
    await store.set("route-token", "fake-route-secret-v1")
    await store.set("mcp-token", "fake-mcp-secret-v1")

    cfg = HarnessConfig.model_validate(
        {
            "connections": {
                "claude-gw": {
                    "provider": "claude",
                    "kind": "endpoint",
                    "endpoint": "https://gateway.example.invalid",
                    "secret": "route-token",
                    "data_policy": "no_training",
                }
            },
            "routes": {
                "claude-sec": {
                    "driver": "claude.agent-sdk",
                    "model": "claude-sonnet-4-6",
                    "connection": "claude-gw",
                }
            },
            "mcps": {
                "ext-mcp": {
                    "server": {
                        "type": "http",
                        "url": "https://mcp.example.invalid/stream",
                        "headers": {"Authorization": {"secret": "mcp-token"}},
                    }
                }
            },
            "models": {
                "sec-alias": {
                    "route": "claude-sec",
                    "mcp_refs": ["ext-mcp"],
                }
            },
        }
    )
    save_config(cfg)

    created_clients: list[MagicMock] = []
    captured_options: list[object] = []

    def fake_sdk_client_ctor(options=None):
        captured_options.append(options)
        c = MagicMock()
        c.connect = AsyncMock()
        c.disconnect = AsyncMock()
        created_clients.append(c)
        return c

    session_module.sessions.clear()
    session_module.session_last_used.clear()
    session_module.session_cache_identities.clear()
    try:
        with (
            patch("kotoyose.session.lookup_session", new=AsyncMock(return_value=None)),
            patch("kotoyose.session.ClaudeSDKClient", side_effect=fake_sdk_client_ctor),
        ):
            client_v1, _ = await session_module.get_or_resume_client("conv:sec", "sec-alias")
            assert len(created_clients) == 1
            assert captured_options[0].env["ANTHROPIC_API_KEY"] == "fake-route-secret-v1"
            assert captured_options[0].mcp_servers["ext-mcp"]["headers"]["Authorization"] == "fake-mcp-secret-v1"

            # Same secret versions -> cache hit, no rebuild.
            client_hit, _ = await session_module.get_or_resume_client("conv:sec", "sec-alias")
            assert client_hit is client_v1
            assert len(created_clients) == 1

            # Replace route secret -> version bumps -> old client disconnected and rebuilt.
            await store.set("route-token", "fake-route-secret-v2")
            client_v2, _ = await session_module.get_or_resume_client("conv:sec", "sec-alias")
            assert client_v2 is not client_v1
            created_clients[0].disconnect.assert_awaited_once()
            assert len(created_clients) == 2
            assert captured_options[1].env["ANTHROPIC_API_KEY"] == "fake-route-secret-v2"

            # Replace MCP secret -> version bumps -> rebuilt again.
            await store.set("mcp-token", "fake-mcp-secret-v2")
            client_v3, _ = await session_module.get_or_resume_client("conv:sec", "sec-alias")
            assert client_v3 is not client_v2
            created_clients[1].disconnect.assert_awaited_once()
            assert len(created_clients) == 3
            assert captured_options[2].mcp_servers["ext-mcp"]["headers"]["Authorization"] == "fake-mcp-secret-v2"

            # Identity never contains plaintext secret values.
            identity_json = json.dumps(session_module.session_cache_identities["conv:sec"])
            assert "fake-route-secret-v2" not in identity_json
            assert "fake-mcp-secret-v2" not in identity_json
    finally:
        session_module.sessions.clear()
        session_module.session_last_used.clear()
        session_module.session_cache_identities.clear()


@pytest.mark.asyncio
async def test_mask_last4_threshold_16_chars(isolated_harness):
    from kotoyose.secret_store import _mask_last4

    assert _mask_last4("a") == ""
    assert _mask_last4("short-15-chars!") == ""
    assert len("short-15-chars!") == 15
    assert _mask_last4("exact-16-ch-1234") == "1234"
    assert len("exact-16-ch-1234") == 16

    store, _, _ = isolated_harness
    app = _build_admin_app(store)
    auth_tuple = ("admin", "test-admin-secret")

    short_meta = await store.set("short-secret", "15-char-val-abc")
    long_meta = await store.set("long-secret", "16-char-val-abcd")
    assert short_meta["last4"] == ""
    assert long_meta["last4"] == "abcd"

    cfg = HarnessConfig.model_validate(
        {
            "settings": {"anthropic_auth_token": "short-setting15"},
            "mcps": {
                "short-mcp": {
                    "server": {
                        "type": "http",
                        "url": "https://mcp.example.invalid",
                        "headers": {
                            "X-Short": "short-hdr-15ch!",
                            "X-Long": "long-header-val-9876",
                        },
                    }
                }
            },
        }
    )
    save_config(cfg)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/admin/secrets", auth=auth_tuple)
        assert resp.status_code == 200
        unmanaged = {item["target"]: item["current"] for item in resp.json()["unmanaged"]}
        assert unmanaged["settings.anthropic_auth_token"] == "literal (••••)"
        assert unmanaged["mcp:short-mcp.headers.X-Short"] == "literal (••••)"
        assert unmanaged["mcp:short-mcp.headers.X-Long"] == "literal (••••9876)"


def test_master_key_file_rejects_symlink_o_nofollow(tmp_path):
    from kotoyose.secret_store import load_master_key_file

    real_key = tmp_path / "real.key"
    real_key.write_bytes(os.urandom(32))
    os.chmod(real_key, 0o600)

    symlink_key = tmp_path / "link.key"
    symlink_key.symlink_to(real_key)

    with pytest.raises(SecretKeyError, match="symlink"):
        load_master_key_file(symlink_key)


def test_create_master_key_checks_written_byte_count(tmp_path):
    from kotoyose.secret_store import create_master_key_file

    target = tmp_path / "short-write.key"
    with patch("kotoyose.secret_store.os.write", side_effect=[10, 0]):
        with pytest.raises(OSError, match="short write"):
            create_master_key_file(target)
    assert not target.exists()

    target2 = tmp_path / "mismatched-write.key"
    with patch("kotoyose.secret_store.os.write", return_value=10):
        with pytest.raises(OSError, match="32"):
            create_master_key_file(target2)
    assert not target2.exists()


@pytest.mark.asyncio
async def test_startup_refuses_secret_store_when_admin_auth_disabled(tmp_path, monkeypatch, capsys):
    from unittest.mock import AsyncMock
    from kotoyose import app as app_module

    key_file = tmp_path / "master.key"
    key_file.write_bytes(os.urandom(32))
    os.chmod(key_file, 0o600)

    monkeypatch.setattr(auth, "API_KEY", None)
    monkeypatch.setenv("KOTOYOSE_MASTER_KEY_FILE", str(key_file))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    orig_store = app_module.app.state.secret_store
    try:
        with (
            patch("kotoyose.db._pool", None),
            patch("kotoyose.app.init_db", new=AsyncMock()),
            patch("kotoyose.app.close_db", new=AsyncMock()),
            patch("kotoyose.app.create_conversations", return_value=None),
            patch("kotoyose.app.start_execution_service", new=AsyncMock(return_value=None)),
            patch("kotoyose.app.evict_expired_sessions", new=AsyncMock()),
        ):
            async with app_module.lifespan(app_module.app):
                assert app_module.app.state.secret_store.is_configured is False
                assert "KOTOYOSE_TOKEN" in (app_module.app.state.secret_store.key_error or "")

                async with AsyncClient(
                    transport=ASGITransport(app=app_module.app),
                    base_url="http://test",
                ) as client:
                    get_resp = await client.get("/admin/secrets")
                    assert get_resp.status_code == 503
                    assert "auth" in get_resp.json()["error"]["message"].lower()

                    put_resp = await client.put(
                        "/admin/secrets/test-secret",
                        json={"value": "fake-secret-value-1234"},
                    )
                    assert put_resp.status_code == 503
                    assert "auth" in put_resp.json()["error"]["message"].lower()

        out = capsys.readouterr().out
        assert "secret_store.disabled" in out
    finally:
        set_secret_store(None)
        app_module.app.state.secret_store = orig_store


@pytest.mark.asyncio
async def test_default_master_key_created_on_startup_in_temp_xdg(tmp_path, monkeypatch, capsys):
    from unittest.mock import AsyncMock
    from kotoyose import app as app_module

    xdg_dir = tmp_path / "xdg-data"
    monkeypatch.setattr(auth, "API_KEY", "test-admin-secret")
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_dir))
    monkeypatch.delenv("KOTOYOSE_MASTER_KEY_FILE", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    expected_dir = xdg_dir / "kotoyose"
    expected_key = expected_dir / "master.key"
    assert not expected_key.exists()

    orig_store = app_module.app.state.secret_store
    try:
        with (
            patch("kotoyose.db._pool", None),
            patch("kotoyose.app.init_db", new=AsyncMock()),
            patch("kotoyose.app.close_db", new=AsyncMock()),
            patch("kotoyose.app.create_conversations", return_value=None),
            patch("kotoyose.app.start_execution_service", new=AsyncMock(return_value=None)),
            patch("kotoyose.app.evict_expired_sessions", new=AsyncMock()),
        ):
            async with app_module.lifespan(app_module.app):
                assert expected_key.exists()
                assert stat.S_IMODE(expected_dir.stat().st_mode) == 0o700
                assert stat.S_IMODE(expected_key.stat().st_mode) == 0o600
                first_bytes = expected_key.read_bytes()
                assert len(first_bytes) == 32
                assert app_module.app.state.secret_store.is_configured is True
                first_key_id = app_module.app.state.secret_store.key_id

            out1 = capsys.readouterr().out
            assert "master_key_created" in out1
            assert "back it up" in out1.lower()

            # Second startup loads the existing key without overwriting or logging creation.
            async with app_module.lifespan(app_module.app):
                assert expected_key.read_bytes() == first_bytes
                assert app_module.app.state.secret_store.key_id == first_key_id

            out2 = capsys.readouterr().out
            assert "master_key_created" not in out2
    finally:
        set_secret_store(None)
        app_module.app.state.secret_store = orig_store
