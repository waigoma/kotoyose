"""Tests for ADR-0010 build step 1: process environment secret scrubbing."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from claude_agent_sdk import ClaudeSDKClient
import pytest

from kotoyose import app as app_module
from kotoyose import config as config_module
from kotoyose import execution_agent, process_env
from kotoyose.artifact_storage_config import LocalArtifactStorageConfig, S3ArtifactStorageConfig
from kotoyose.config import (
    AgentRoute,
    Client,
    Connection,
    HarnessConfig,
    ModelPreset,
    _build_database_url,
    resolve_claude_subscription_token_async,
    resolve_claude_subscription_token_sync,
)
from kotoyose.connections_probe import _resolve_connection_secret_for_probe
from kotoyose.execution_resources import NativeExecutionConfig
from kotoyose.mcp_contract import ExternalMcpServerConfig, McpToolCeiling
from kotoyose.model_discovery import _resolve_connection_secret
from kotoyose.runtime.antigravity import AntigravityRuntimeSession, _cli_env_for
from kotoyose.runtime.antigravity_probe import probe_antigravity_cli
from kotoyose.runtime.claude_auth import build_auth_env
from kotoyose.runtime.claude_options import build_claude_options
from kotoyose.runtime.codex import CodexRuntimeSession
from kotoyose.runtime.cursor import CursorRunError, CursorRuntimeSession
from kotoyose.s3_artifact_store import S3ArtifactStore
from kotoyose.secret_store import SecretStore, create_master_key_file


@pytest.fixture(autouse=True)
def _restore_process_env():
    """Keep process_env and os.environ clean across tests."""
    snapshot = dict(os.environ)
    process_env._reset_for_tests(restore_environ=False)
    try:
        yield
    finally:
        process_env._reset_for_tests(restore_environ=False)
        for key in list(os.environ.keys()):
            if key not in snapshot:
                os.environ.pop(key, None)
        for key, value in snapshot.items():
            os.environ[key] = value


def _write_env_dump_cli(path: Path, dump_path: Path, body: str = "") -> Path:
    """Create an executable script that writes its os.environ as JSON to dump_path."""
    script = (
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        f"pathlib.Path({str(dump_path)!r}).write_text(json.dumps(dict(os.environ)), encoding='utf-8')\n"
        f"{body}\n"
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_scrub_moves_secrets_and_keeps_allowlist(monkeypatch):
    monkeypatch.setenv("KOTOYOSE_TEST_SENTINEL_TOKEN", "sentinel-token-value-123")
    monkeypatch.setenv("POSTGRES_PASSWORD", "sentinel-db-pass-456")
    monkeypatch.setenv("CUSTOM_API_KEY", "sentinel-api-key-789")
    monkeypatch.setenv("ACCESS_KEY_ALT", "sentinel-access-key")
    monkeypatch.setenv("MY_PRIVATE_SIGNING_KEY", "sentinel-private-key")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/tmp/fake-creds-dir")
    monkeypatch.setenv("KOTOYOSE_MASTER_KEY_FILE", "/tmp/fake-master.key")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/fake-ssh-agent.sock")
    monkeypatch.setenv("KOTOYOSE_CONFIG", "/tmp/fake-config.yml")
    monkeypatch.setenv("KOTOYOSE_EXECUTION_CONFIG", "/tmp/fake-exec.json")
    monkeypatch.setenv("SESSION_TTL_SECONDS", "1800")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("XDG_DATA_HOME", "/tmp/xdg-data")
    monkeypatch.setenv("PYTHONUNBUFFERED", "1")
    monkeypatch.setenv("KOTOYOSE_BASIC_USER", "owner")

    captured_logs: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        process_env.log,
        "info",
        lambda event, **kw: captured_logs.append((event, kw)),
    )

    process_env.scrub()

    for secret_name in (
        "KOTOYOSE_TEST_SENTINEL_TOKEN",
        "POSTGRES_PASSWORD",
        "CUSTOM_API_KEY",
        "ACCESS_KEY_ALT",
        "MY_PRIVATE_SIGNING_KEY",
        "CREDENTIALS_DIRECTORY",
        "KOTOYOSE_MASTER_KEY_FILE",
        "SSH_AUTH_SOCK",
    ):
        assert secret_name not in os.environ
        assert process_env.get(secret_name) is not None
        assert secret_name in process_env.names()

    assert process_env.require("KOTOYOSE_TEST_SENTINEL_TOKEN") == "sentinel-token-value-123"
    with pytest.raises(KeyError):
        process_env.require("KOTOYOSE_DEFINITELY_NOT_SET_VAR")

    for allowed_name in (
        "PATH",
        "KOTOYOSE_CONFIG",
        "KOTOYOSE_EXECUTION_CONFIG",
        "SESSION_TTL_SECONDS",
        "LC_ALL",
        "XDG_DATA_HOME",
        "PYTHONUNBUFFERED",
        "KOTOYOSE_BASIC_USER",
    ):
        if allowed_name in os.environ:
            assert allowed_name not in process_env.names()

    assert "SESSION_TTL_SECONDS" in os.environ
    assert "KOTOYOSE_CONFIG" in os.environ
    assert "SSH_AUTH_SOCK" not in os.environ

    scrub_events = [kw for event, kw in captured_logs if event == "process_env.scrubbed"]
    assert len(scrub_events) == 1
    payload = scrub_events[0]
    assert payload["count"] == len(payload["names"])
    assert payload["names"] == sorted(payload["names"])
    assert "KOTOYOSE_TEST_SENTINEL_TOKEN" in payload["names"]
    assert "SSH_AUTH_SOCK" in payload["names"]

    serialized = json.dumps(captured_logs)
    for leaked_value in (
        "sentinel-token-value-123",
        "sentinel-db-pass-456",
        "sentinel-api-key-789",
        "/tmp/fake-ssh-agent.sock",
        "/tmp/fake-master.key",
    ):
        assert leaked_value not in serialized


def test_scrub_moves_innocent_names_referenced_by_configs(monkeypatch, tmp_path):
    monkeypatch.setenv("INNOCENT_CONN_VAR", "conn-val")
    monkeypatch.setenv("INNOCENT_ANTHROPIC_VAR", "anthropic-val")
    monkeypatch.setenv("INNOCENT_AGY_VAR", "agy-val")
    monkeypatch.setenv("INNOCENT_MCP_VAR", "mcp-val")
    monkeypatch.setenv("INNOCENT_S3_ACCESS", "s3-access-val")
    monkeypatch.setenv("INNOCENT_S3_SECRET", "s3-secret-val")
    monkeypatch.setenv("UNRELATED_ORDINARY_VAR", "keep-me")

    harness_cfg = HarnessConfig(
        connections={"c1": Connection(provider="cursor", kind="api_key", data_policy="no_training", secret="env:INNOCENT_CONN_VAR")},
        models={
            "m1": ModelPreset(
                provider="claude",
                anthropic_auth_token_env="INNOCENT_ANTHROPIC_VAR",
                antigravity={"api_key_env": "INNOCENT_AGY_VAR"},
            ),
        },
        clients={
            "c": Client(
                workspace_roots=[tmp_path],
                places=["host"],
                max_run_seconds=60,
                max_memory_bytes=256 * 1024 * 1024,
                max_processes=32,
                max_cpus=1.0,
                max_concurrent_runs=1,
            ),
        },
    )
    exec_cfg = NativeExecutionConfig(
        schema_version="1",
        image_id="sha256:" + ("a" * 64),
        mcp_servers={
            "mcp1": ExternalMcpServerConfig(
                endpoint="https://example.invalid/mcp",
                server_name="mcp1",
                server_version="1.0",
                trust_profile_ref="trust@1",
                credential_env="INNOCENT_MCP_VAR",
                tools={"ping": McpToolCeiling(effect="read")},
            )
        },
        artifact_storage={
            "default_backend": "s3",
            "backends": {
                "local": LocalArtifactStorageConfig(kind="local_fs", root=tmp_path / "artifacts"),
                "s3": S3ArtifactStorageConfig(
                    kind="s3",
                    endpoint="https://s3.example.invalid",
                    bucket="bucket",
                    access_key_env="INNOCENT_S3_ACCESS",
                    secret_key_env="INNOCENT_S3_SECRET",
                ),
            },
        },
    )

    refs = process_env.collect_config_env_names(harness_cfg, exec_cfg)
    assert set(refs) == {
        "INNOCENT_CONN_VAR",
        "INNOCENT_ANTHROPIC_VAR",
        "INNOCENT_AGY_VAR",
        "INNOCENT_MCP_VAR",
        "INNOCENT_S3_ACCESS",
        "INNOCENT_S3_SECRET",
    }

    process_env.scrub(refs)

    for name in refs:
        assert name not in os.environ
        assert process_env.get(name) is not None
    assert os.environ.get("UNRELATED_ORDINARY_VAR") == "keep-me"


async def test_app_lifespan_scrubs_at_startup(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(cfg_path))
    config_module.reload_config()
    exec_cfg_path = tmp_path / "exec.json"
    exec_cfg_path.write_text(
        json.dumps(
            {
                "mcp_servers": {"m": {"credential_env": "INNOCENT_EXEC_MCP"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KOTOYOSE_EXECUTION_CONFIG", str(exec_cfg_path))
    monkeypatch.setenv("KOTOYOSE_TEST_SENTINEL_TOKEN", "top-secret-app-token")
    monkeypatch.setenv("INNOCENT_EXEC_MCP", "mcp-secret")

    scrubbed_during_init: dict[str, bool] = {}

    async def fake_init_db():
        scrubbed_during_init["sentinel_in_environ"] = "KOTOYOSE_TEST_SENTINEL_TOKEN" in os.environ
        scrubbed_during_init["mcp_in_environ"] = "INNOCENT_EXEC_MCP" in os.environ

    monkeypatch.setattr(app_module, "configure_logging", lambda: None)
    monkeypatch.setattr(app_module, "init_db", fake_init_db)
    monkeypatch.setattr(app_module, "close_db", AsyncMock())
    monkeypatch.setattr(app_module, "create_conversations", lambda: None)
    monkeypatch.setattr(app_module, "start_execution_service", AsyncMock(return_value=None))
    monkeypatch.setattr(app_module, "shutdown_claude_login_sessions", AsyncMock())
    monkeypatch.setattr(app_module, "shutdown_cli_login_sessions", AsyncMock())
    monkeypatch.setattr(app_module, "shutdown_login_sessions", AsyncMock())
    monkeypatch.setattr(app_module, "connection_probe_loop", AsyncMock())
    monkeypatch.setattr(app_module, "_ttl_loop", AsyncMock())
    monkeypatch.setattr(app_module.auth, "API_KEY", "test-api-key")

    async with app_module.lifespan(app_module.app):
        assert scrubbed_during_init == {
            "sentinel_in_environ": False,
            "mcp_in_environ": False,
        }
        assert process_env.get("KOTOYOSE_TEST_SENTINEL_TOKEN") == "top-secret-app-token"
        assert process_env.get("INNOCENT_EXEC_MCP") == "mcp-secret"


async def test_claude_child_process_receives_no_scrubbed_secrets(monkeypatch, tmp_path):
    dump_path = tmp_path / "claude_env.json"
    fake_cli = _write_env_dump_cli(
        tmp_path / "fake-claude",
        dump_path,
        body=(
            "if '-v' in sys.argv or '--version' in sys.argv:\n"
            "    print('99.0.0')\n"
            "    sys.exit(0)\n"
            "sys.stderr.write('not logged in\\n')\n"
            "sys.exit(1)"
        ),
    )
    monkeypatch.setenv("KOTOYOSE_TEST_SENTINEL_TOKEN", "sentinel-claude-secret")
    monkeypatch.setenv("INNOCENT_CONFIG_REF_VAR", "innocent-config-secret")

    cfg = HarnessConfig(
        connections={"ext": Connection(provider="cursor", kind="api_key", data_policy="no_training", secret="env:INNOCENT_CONFIG_REF_VAR")},
        models={
            "claude-test": ModelPreset(
                provider="claude",
                model="claude-sonnet-4-5",
                cwd=str(tmp_path),
            )
        },
    )
    monkeypatch.setattr(config_module, "_config", cfg)
    monkeypatch.setattr(config_module, "_get_oauth_token", lambda: "explicit-oauth-token")

    process_env.scrub(process_env.collect_config_env_names(cfg))

    preset = cfg.preset_for("claude-test")
    options = build_claude_options(
        cfg,
        preset,
        model_name="claude-test",
        cwd_override=None,
        request_headers={},
        env=build_auth_env(preset, cfg, lambda: "explicit-oauth-token"),
        session_store=None,
    )
    options = replace(options, cli_path=str(fake_cli))
    client = ClaudeSDKClient(options=options)
    try:
        try:
            await client.connect()
        except Exception:
            pass
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    assert dump_path.exists()
    child_env = json.loads(dump_path.read_text(encoding="utf-8"))
    assert "KOTOYOSE_TEST_SENTINEL_TOKEN" not in child_env
    assert "INNOCENT_CONFIG_REF_VAR" not in child_env


async def test_codex_child_process_receives_no_scrubbed_secrets(monkeypatch, tmp_path):
    dump_path = tmp_path / "codex_env.json"
    fake_codex = _write_env_dump_cli(
        tmp_path / "fake-codex",
        dump_path,
        body="sys.exit(1)",
    )
    monkeypatch.setenv("KOTOYOSE_TEST_SENTINEL_TOKEN", "sentinel-codex-secret")
    monkeypatch.setenv("INNOCENT_CONFIG_REF_VAR", "innocent-config-secret")

    cfg = HarnessConfig(
        connections={"ext": Connection(provider="cursor", kind="api_key", data_policy="no_training", secret="env:INNOCENT_CONFIG_REF_VAR")}
    )
    process_env.scrub(process_env.collect_config_env_names(cfg))

    session = CodexRuntimeSession(
        model="gpt-5.1-codex",
        cwd=str(tmp_path),
        config={"codex_bin": str(fake_codex)},
    )
    with pytest.raises(Exception):
        async for _ in session.stream("hello"):
            pass

    assert dump_path.exists()
    child_env = json.loads(dump_path.read_text(encoding="utf-8"))
    assert "KOTOYOSE_TEST_SENTINEL_TOKEN" not in child_env
    assert "INNOCENT_CONFIG_REF_VAR" not in child_env


async def test_cursor_bridge_child_receives_no_scrubbed_secrets_and_passes_api_key(monkeypatch, tmp_path):
    dump_path = tmp_path / "cursor_bridge_env.json"
    fake_bridge = _write_env_dump_cli(
        tmp_path / "fake-cursor-bridge",
        dump_path,
        body=(
            "import time\n"
            "print('cursor-sdk-bridge ready ' + json.dumps("
            "{'schemaVersion':1,'transport':'tcp','protocol':'connect','url':'http://127.0.0.1:1','authToken':'tok'}"
            "), file=sys.stderr, flush=True)\n"
            "time.sleep(5)"
        ),
    )
    monkeypatch.setenv("KOTOYOSE_TEST_SENTINEL_TOKEN", "sentinel-cursor-secret")
    monkeypatch.setenv("INNOCENT_CONFIG_REF_VAR", "innocent-cursor-api-key")

    cfg = HarnessConfig(
        connections={
            "cursor-env": Connection(provider="cursor", kind="api_key", data_policy="no_training", secret="env:INNOCENT_CONFIG_REF_VAR"),
        }
    )
    monkeypatch.setattr("kotoyose.runtime.cursor.get_config", lambda: cfg)
    monkeypatch.setattr("cursor_sdk._bridge.resolve_bridge_path", lambda: str(fake_bridge))
    monkeypatch.setattr("cursor_sdk._async_bridge.resolve_bridge_path", lambda: str(fake_bridge))

    captured_api_key: dict[str, str | None] = {}

    class _FakeAgent:
        agent_id = "cursor-agent-1"

        async def send(self, prompt):
            raise RuntimeError("stop-after-create")

        async def close(self):
            return None

    async def fake_create(options, *, client, api_key=None):
        captured_api_key["value"] = api_key
        return _FakeAgent()

    monkeypatch.setattr("kotoyose.runtime.cursor.AsyncAgent.create", fake_create)
    process_env.scrub(process_env.collect_config_env_names(cfg))

    session = CursorRuntimeSession(model="cursor-model", cwd=str(tmp_path), connection="cursor-env")
    with pytest.raises(CursorRunError):
        async for _ in session.stream("hello"):
            pass

    assert captured_api_key["value"] == "innocent-cursor-api-key"
    assert dump_path.exists()
    child_env = json.loads(dump_path.read_text(encoding="utf-8"))
    assert "KOTOYOSE_TEST_SENTINEL_TOKEN" not in child_env
    assert "INNOCENT_CONFIG_REF_VAR" not in child_env


async def test_agy_child_process_receives_no_scrubbed_secrets(monkeypatch, tmp_path):
    probe_dump = tmp_path / "agy_probe_env.json"
    fake_agy_probe = _write_env_dump_cli(
        tmp_path / "fake-agy-probe",
        probe_dump,
        body="print('1.2.0')\nsys.exit(0)",
    )
    run_dump = tmp_path / "agy_run_env.json"
    fake_agy_run = _write_env_dump_cli(
        tmp_path / "fake-agy-run",
        run_dump,
        body=(
            "if '--version' in sys.argv:\n"
            "    print('1.2.0')\n"
            "else:\n"
            "    print(json.dumps({'event':'result','status':'SUCCESS','response':'ok'}))\n"
            "sys.exit(0)"
        ),
    )
    login_home = tmp_path / "agy-home"
    login_home.mkdir()

    monkeypatch.setenv("KOTOYOSE_TEST_SENTINEL_TOKEN", "sentinel-agy-secret")
    monkeypatch.setenv("INNOCENT_CONFIG_REF_VAR", "innocent-config-secret")

    cfg = HarnessConfig(
        connections={
            "ext": Connection(provider="cursor", kind="api_key", data_policy="no_training", secret="env:INNOCENT_CONFIG_REF_VAR"),
            "agy": Connection(provider="antigravity", kind="subscription", data_policy="no_training", config_dir=login_home),
        },
        routes={
            "r-agy": AgentRoute(driver="antigravity.agy", model="gemini-3-pro", connection="agy"),
        },
    )
    monkeypatch.setattr("kotoyose.config.get_config", lambda: cfg)
    process_env.scrub(process_env.collect_config_env_names(cfg))

    diag = await probe_antigravity_cli((str(fake_agy_probe), "--version"))
    assert diag.compatible is True
    assert probe_dump.exists()
    probe_env = json.loads(probe_dump.read_text(encoding="utf-8"))
    assert "KOTOYOSE_TEST_SENTINEL_TOKEN" not in probe_env
    assert "INNOCENT_CONFIG_REF_VAR" not in probe_env

    session = AntigravityRuntimeSession(
        model="gemini-3-pro",
        cwd=str(tmp_path),
        config={"command": str(fake_agy_run)},
        env=_cli_env_for(ModelPreset(route="r-agy")),
    )
    chunks = [item async for item in session.stream("hello")]
    assert chunks

    assert run_dump.exists()
    run_env = json.loads(run_dump.read_text(encoding="utf-8"))
    assert "KOTOYOSE_TEST_SENTINEL_TOKEN" not in run_env
    assert "INNOCENT_CONFIG_REF_VAR" not in run_env


async def test_scrubbed_credentials_still_work_end_to_end(monkeypatch, tmp_path):
    master_file = tmp_path / "master.key"
    create_master_key_file(master_file)
    monkeypatch.setenv("KOTOYOSE_MASTER_KEY_FILE", str(master_file))
    monkeypatch.setenv("POSTGRES_URL", "localhost")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_USER", "kotoyose_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "scrubbed-pg-pass")
    monkeypatch.setenv("POSTGRES_DB", "kotoyose_db")
    monkeypatch.setenv("INNOCENT_MCP_VAR", "mcp-bearer-secret")
    monkeypatch.setenv("INNOCENT_CONN_VAR", "conn-env-secret")
    monkeypatch.setenv("INNOCENT_S3_ACCESS", "s3-access-id")
    monkeypatch.setenv("INNOCENT_S3_SECRET", "s3-secret-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-override-secret")

    cfg = HarnessConfig(
        connections={
            "c-env": Connection(provider="cursor", kind="api_key", data_policy="no_training", secret="env:INNOCENT_CONN_VAR"),
            "c-oauth": Connection(provider="claude", kind="subscription", data_policy="no_training", secret="claude-oauth-prod"),
        },
        routes={
            "r-claude": AgentRoute(driver="claude.agent-sdk", model="claude-sonnet-4-5", connection="c-oauth"),
        },
        models={"chat": ModelPreset(route="r-claude")},
        clients={
            "caller1": Client(
                workspace_roots=[tmp_path],
                places=["isolated"],
                max_run_seconds=60,
                max_memory_bytes=256 * 1024 * 1024,
                max_processes=32,
                max_cpus=1.0,
                max_concurrent_runs=1,
            ),
        },
    )
    monkeypatch.setattr(config_module, "_config", cfg)
    exec_cfg = NativeExecutionConfig(
        schema_version="1",
        image_id="sha256:" + ("b" * 64),

        mcp_servers={
            "srv1": ExternalMcpServerConfig(
                endpoint="https://mcp.example.invalid/mcp",
                server_name="srv1",
                server_version="1.0",
                trust_profile_ref="trust@1",
                credential_env="INNOCENT_MCP_VAR",
                tools={"ping": McpToolCeiling(effect="read")},
            )
        },
        artifact_storage={
            "default_backend": "s3",
            "backends": {
                "s3": S3ArtifactStorageConfig(
                    kind="s3",
                    endpoint="https://s3.example.invalid",
                    bucket="bkt",
                    access_key_env="INNOCENT_S3_ACCESS",
                    secret_key_env="INNOCENT_S3_SECRET",
                )
            },
        },
    )

    process_env.scrub(process_env.collect_config_env_names(cfg, exec_cfg))

    # 1. Client resource and MCP credential validation
    exec_cfg.validate_client_resources()

    # 2. DB URL resolution
    assert _build_database_url() == "postgresql://kotoyose_user:scrubbed-pg-pass@localhost:5432/kotoyose_db"

    # 3. Connection env: secret resolution (probe + model discovery)
    probe_tok, probe_err = await _resolve_connection_secret_for_probe(cfg.connections["c-env"])
    assert (probe_tok, probe_err) == ("conn-env-secret", None)
    disc_tok, disc_err = await _resolve_connection_secret(cfg.connections["c-env"])
    assert (disc_tok, disc_err) == ("conn-env-secret", None)

    # 4. Claude CLAUDE_CODE_OAUTH_TOKEN env override + subscription env: resolution
    monkeypatch.setattr(config_module, "_get_oauth_token", lambda: None)
    auth_env = build_auth_env(cfg.preset_for("chat"), cfg, lambda: None)
    assert auth_env == {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-override-secret"}

    sub_env_cfg = HarnessConfig(
        connections={
            "c-sub-env": Connection(provider="claude", kind="subscription", data_policy="no_training", secret="env:INNOCENT_CONN_VAR"),
        },
        routes={
            "r-claude": AgentRoute(driver="claude.agent-sdk", model="claude-sonnet-4-5", connection="c-sub-env"),
        },
        models={"chat": ModelPreset(route="r-claude")},
    )
    assert resolve_claude_subscription_token_sync(sub_env_cfg, sub_env_cfg.preset_for("chat")) == "conn-env-secret"
    assert await resolve_claude_subscription_token_async(sub_env_cfg, sub_env_cfg.preset_for("chat")) == "conn-env-secret"

    resolved_exec_cred = await execution_agent.resolve_runtime_credential(
        {
            "alias": "chat",
            "credential_mode": "runtime_managed",
            "credential_env": None,
            "credential_secret": None,
        }
    )
    assert resolved_exec_cred == "oauth-override-secret"

    # 5. Secret store master key resolution from scrubbed KOTOYOSE_MASTER_KEY_FILE
    store = SecretStore.from_env(pool=None)
    assert store.is_configured

    # 6. S3 client receives explicit credentials from scrubbed env
    captured_boto: dict[str, object] = {}

    class _FakeBotoModule:
        @staticmethod
        def client(service_name, **kwargs):
            captured_boto["service"] = service_name
            captured_boto["kwargs"] = kwargs
            return SimpleNamespace()

    monkeypatch.setitem(sys.modules, "boto3", _FakeBotoModule())
    monkeypatch.setitem(sys.modules, "botocore.config", SimpleNamespace(Config=SimpleNamespace))
    S3ArtifactStore._client(exec_cfg.artifact_storage.backends["s3"])
    assert captured_boto["service"] == "s3"
    assert captured_boto["kwargs"]["aws_access_key_id"] == "s3-access-id"
    assert captured_boto["kwargs"]["aws_secret_access_key"] == "s3-secret-key"
    assert captured_boto["kwargs"]["config"].signature_version == "s3v4"
