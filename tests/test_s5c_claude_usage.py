"""Tests for S5c: Claude config_dir subscriptions, /usage CLI probing, quota_from, and runtime auth."""

from __future__ import annotations

import asyncio
import builtins
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from kotoyose import auth as auth_module
from kotoyose import db as db_module
from kotoyose.agent_route import _resolve_claude
from kotoyose.app import app
from kotoyose.config import (
    ClaudeSubscriptionAuthError,
    Connection,
    HarnessConfig,
    build_options,
    reload_config,
    resolve_claude_subscription_token_sync,
    save_config,
)
from kotoyose.connection_state_store import ConnectionStateStore, set_connection_state_store
from kotoyose.connections_probe import (
    _CLI_VERSION_CACHE,
    parse_claude_usage_output,
    probe_all_connections,
    probe_claude_connection,
)
from kotoyose.execution_resources import AliasRuntimeProfile, resolve_runtime_profile
from kotoyose.runtime.claude_auth import build_auth_env
from kotoyose.secret_store import SecretStore, set_secret_store

SAMPLE_USAGE_TEXT = """You are currently using your subscription to power your Claude Code usage

Current session: 4% used · resets Nov 15, 3am (Asia/Tokyo)
Current week (all models): 55% used · resets Nov 19, 3am (Asia/Tokyo)
Current week (Fable): 11% used · resets Nov 19, 3am (Asia/Tokyo)

What's contributing to your usage?
  ...
"""

SAMPLE_FIXED_NOW = datetime(2026, 11, 14, 13, 41, 0, tzinfo=timezone.utc)


def _write_fake_claude(
    bin_dir: Path,
    *,
    mode: str = "ok",
    log_path: Path | None = None,
    sleep_seconds: float = 0.0,
    version_text: str = "2.1.62 (Claude Code)",
) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / "claude"
    script = textwrap.dedent(
        f"""\
        #!{sys.executable}
        import json
        import os
        import sys
        import time

        MODE = {mode!r}
        LOG_PATH = {str(log_path) if log_path is not None else ""!r}
        SLEEP_SECONDS = {sleep_seconds!r}
        VERSION_TEXT = {version_text!r}
        SAMPLE_USAGE_TEXT = {SAMPLE_USAGE_TEXT!r}

        def main():
            argv = sys.argv[1:]
            if argv == ["--version"]:
                print(VERSION_TEXT)
                return
            if LOG_PATH:
                with open(LOG_PATH, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({{
                        "argv": argv,
                        "cwd": os.getcwd(),
                        "env": dict(os.environ),
                    }}) + "\\n")
            if SLEEP_SECONDS > 0:
                time.sleep(SLEEP_SECONDS)
            if MODE == "ok":
                print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": SAMPLE_USAGE_TEXT}}))
            elif MODE == "setup_token_only":
                print(json.dumps({{
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "Total cost: $0.00\\nUsage: 0 input, 0 output",
                }}))
            elif MODE == "login_required":
                print(json.dumps({{
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "result": "Not logged in · Please run /login (secret-token-should-not-leak)",
                }}))
                sys.exit(1)
            elif MODE == "login_expired":
                print(json.dumps({{
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "result": "Session login expired; please log in again",
                }}))
                sys.exit(1)
            elif MODE == "garbage":
                print("not json at all! RAW_GARBAGE_SENTINEL_XYZ")
            else:
                print(json.dumps({{"result": "unexpected " + ("X" * 300)}}))

        if __name__ == "__main__":
            main()
        """
    )
    exe.write_text(script, encoding="utf-8")
    exe.chmod(0o755)
    return exe


@pytest.fixture
def isolated_s5c_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(cfg_path))
    monkeypatch.setattr("kotoyose.app.configure_logging", lambda: None)
    monkeypatch.setattr(auth_module, "API_KEY", None)
    monkeypatch.setattr(auth_module, "BASIC_AUTH_USER", None)
    monkeypatch.setattr(db_module, "_pool", None)
    _CLI_VERSION_CACHE.clear()
    store = ConnectionStateStore(pool=None)
    set_connection_state_store(store)
    reload_config()
    yield tmp_path, store
    set_connection_state_store(None)
    set_secret_store(None)
    reload_config()


# ── 1. Connection & config validation ────────────────────────────────────────


def test_connection_validation_claude_config_dir_and_secret_rules(tmp_path: Path):
    cfg_dir = tmp_path / "claude-host"
    # Valid config_dir on claude and anthropic subscriptions
    c_claude = Connection(provider="claude", kind="subscription", data_policy="no_training", config_dir=cfg_dir)
    assert c_claude.config_dir == cfg_dir
    c_anth = Connection(provider="anthropic", kind="subscription", data_policy="no_training", config_dir=cfg_dir)
    assert c_anth.config_dir == cfg_dir

    # Must have exactly one of secret or config_dir
    with pytest.raises(ValidationError):
        Connection(provider="claude", kind="subscription", data_policy="no_training")
    with pytest.raises(ValidationError):
        Connection(provider="claude", kind="subscription", data_policy="no_training", secret="claude-oauth", config_dir=cfg_dir)

    # Relative config_dir rejected
    with pytest.raises(ValidationError):
        Connection(provider="claude", kind="subscription", data_policy="no_training", config_dir=Path("relative/claude-host"))

    # auth_file still disallowed for Claude subscription
    with pytest.raises(ValidationError):
        Connection(
            provider="claude",
            kind="subscription", data_policy="no_training",
            config_dir=cfg_dir,
            auth_file=tmp_path / "auth.json",
        )

    # Disallowed on codex subscription, api_key, endpoint
    with pytest.raises(ValidationError):
        Connection(provider="codex", kind="subscription", data_policy="no_training", config_dir=cfg_dir)
    with pytest.raises(ValidationError):
        Connection(provider="claude", kind="api_key", data_policy="no_training", secret="sk", config_dir=cfg_dir)
    with pytest.raises(ValidationError):
        Connection(
            provider="claude",
            kind="endpoint", data_policy="no_training",
            endpoint="https://example.invalid/v1",
            secret="sk",
            config_dir=cfg_dir,
        )

    # Codex existing rules unchanged
    codex_sub = Connection(provider="codex", kind="subscription", data_policy="no_training", auth_file=tmp_path / "auth.json")
    assert codex_sub.config_dir is None


def test_quota_from_validation_rules(tmp_path: Path):
    cfg_dir = tmp_path / "claude-host"

    # Self reference rejected
    with pytest.raises(ValidationError, match="itself"):
        HarnessConfig.model_validate(
            {
                "connections": {
                    "claude-host": {
                        "provider": "claude",
                        "kind": "subscription", "data_policy": "no_training",
                        "config_dir": str(cfg_dir),
                        "quota_from": "claude-host",
                    }
                }
            }
        )

    # Unknown target rejected
    with pytest.raises(ValidationError, match="unknown"):
        HarnessConfig.model_validate(
            {
                "connections": {
                    "claude-oauth": {
                        "provider": "claude",
                        "kind": "subscription", "data_policy": "no_training",
                        "secret": "claude-oauth",
                        "quota_from": "missing-conn",
                    }
                }
            }
        )

    # Chains rejected (c -> b -> a)
    with pytest.raises(ValidationError, match="chain"):
        HarnessConfig.model_validate(
            {
                "connections": {
                    "a": {"provider": "claude", "kind": "subscription", "data_policy": "no_training", "config_dir": str(cfg_dir)},
                    "b": {"provider": "claude", "kind": "subscription", "data_policy": "no_training", "secret": "s1", "quota_from": "a"},
                    "c": {"provider": "claude", "kind": "subscription", "data_policy": "no_training", "secret": "s2", "quota_from": "b"},
                }
            }
        )

    # Valid quota_from accepted on subscription and api_key
    valid = HarnessConfig.model_validate(
        {
            "connections": {
                "claude-host": {"provider": "claude", "kind": "subscription", "data_policy": "no_training", "config_dir": str(cfg_dir)},
                "claude-oauth": {
                    "provider": "claude",
                    "kind": "subscription", "data_policy": "no_training",
                    "secret": "claude-oauth",
                    "quota_from": "claude-host",
                },
            }
        }
    )
    assert valid.connections["claude-oauth"].quota_from == "claude-host"


def test_config_dir_rejects_isolated_and_jail_routes(tmp_path: Path):
    cfg_dir = tmp_path / "claude-host"
    fake_cli = tmp_path / "fake-claude"
    fake_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_cli.chmod(0o755)
    base = {
        "connections": {
            "claude-host": {"provider": "claude", "kind": "subscription", "data_policy": "no_training", "config_dir": str(cfg_dir)},
        },
        "routes": {
            "r-host": {"driver": "claude.agent-sdk", "model": "claude-sonnet-4-6", "connection": "claude-host"},
        },
    }

    # Host chat preset passes
    host_cfg = HarnessConfig.model_validate(
        {**base, "models": {"chat": {"route": "r-host", "cwd": str(tmp_path)}}}
    )
    assert host_cfg.preset_for("chat").execution == "direct"

    # execution: native rejected
    with pytest.raises(ValidationError, match="host placement only"):
        HarnessConfig.model_validate(
            {
                **base,
                "models": {
                    "chat": {
                        "route": "r-host",
                        "execution": "native",
                        "native": {
                            "workspace_path": str(tmp_path),
                            "caller_workspace_ref": "ws",
                            "policy_ref": "pol",
                            "instructions": "test",
                            "callers": ["caller-a"],
                            "expires_after_seconds": 60,
                        },
                        "cwd": str(tmp_path),
                    }
                },
            }
        )

    # execution_environment: isolated rejected
    with pytest.raises(ValidationError):
        HarnessConfig.model_validate(
            {
                **base,
                "models": {"chat": {"route": "r-host", "execution_environment": "isolated", "cwd": str(tmp_path)}},
            }
        )

    # agent place=isolated rejected
    with pytest.raises(ValidationError, match="host placement only"):
        HarnessConfig.model_validate(
            {
                **base,
                "models": {
                    "agent-alias": {
                        "route": "r-host",
                        "agent": {"surface": "read", "place": "isolated"},
                        "cwd": str(tmp_path),
                    }
                },
            }
        )

    # Belt-and-suspenders runtime guards (resolve_runtime_profile / _resolve_claude)
    with pytest.raises(ValueError, match="host placement only"):
        resolve_runtime_profile(AliasRuntimeProfile(alias="chat", cli_binary=fake_cli), config=host_cfg)

    with pytest.raises(ValueError, match="host placement only"):
        _resolve_claude(
            "chat",
            host_cfg.preset_for("chat"),
            SimpleNamespace(),
            host_cfg,
            host_cfg.routes["r-host"],
            SimpleNamespace(cli_binary=fake_cli),
        )


# ── 2. Runtime auth env for config_dir vs secret ─────────────────────────────


def test_build_auth_env_and_build_options_with_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_dir = tmp_path / "claude-host"
    cfg = HarnessConfig.model_validate(
        {
            "connections": {
                "claude-host": {"provider": "claude", "kind": "subscription", "data_policy": "no_training", "config_dir": str(cfg_dir)},
            },
            "routes": {
                "r-host": {"driver": "claude.agent-sdk", "model": "claude-sonnet-4-6", "connection": "claude-host"},
            },
            "models": {
                "claude-chat": {"route": "r-host", "cwd": str(tmp_path), "bare": False},
            },
        }
    )

    # Inherited token env vars must be stripped/blanked and never forwarded from Kotoyose
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "inherited-oauth-sentinel")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited-api-sentinel")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "inherited-auth-sentinel")

    def boom():
        raise AssertionError("oauth_token_provider must not be called for config_dir")

    preset = cfg.preset_for("claude-chat")
    env = build_auth_env(preset, cfg, boom)
    assert env["CLAUDE_CONFIG_DIR"] == str(cfg_dir)
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == ""
    assert env.get("ANTHROPIC_API_KEY") == ""
    assert env.get("ANTHROPIC_AUTH_TOKEN") == ""

    # When inherited env has no token vars, they are absent from env altogether
    for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    clean_env = build_auth_env(preset, cfg, boom)
    assert clean_env == {"CLAUDE_CONFIG_DIR": str(cfg_dir)}

    # End-to-end via build_options()
    import kotoyose.config as cfg_mod

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "inherited-again")
    monkeypatch.setattr(cfg_mod, "_config", cfg)
    opts = build_options("claude-chat")
    assert opts.env["CLAUDE_CONFIG_DIR"] == str(cfg_dir)
    assert opts.env.get("CLAUDE_CODE_OAUTH_TOKEN") == ""
    assert "ANTHROPIC_API_KEY" not in opts.env
    assert "ANTHROPIC_AUTH_TOKEN" not in opts.env

    # Direct sync resolver rejects when require_configured=True, returns None when False
    assert resolve_claude_subscription_token_sync(cfg, preset, require_configured=False) is None
    with pytest.raises(ClaudeSubscriptionAuthError, match="config_dir"):
        resolve_claude_subscription_token_sync(cfg, preset, require_configured=True)


def test_secret_path_remains_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    store = SecretStore(master_key=b"\x02" * 32)
    asyncio.run(store.set("claude-oauth-sec", "sk\x2dant-oat-secret-only"))
    set_secret_store(store)
    try:
        cfg = HarnessConfig.model_validate(
            {
                "connections": {
                    "claude-host": {"provider": "claude", "kind": "subscription", "data_policy": "no_training", "config_dir": str(tmp_path / "host")},
                    "claude-oauth": {
                        "provider": "claude",
                        "kind": "subscription", "data_policy": "no_training",
                        "secret": "claude-oauth-sec",
                        "quota_from": "claude-host",
                    },
                },
                "routes": {
                    "r-oauth": {"driver": "claude.agent-sdk", "model": "claude-sonnet-4-6", "connection": "claude-oauth"},
                },
                "models": {
                    "chat": {"route": "r-oauth", "cwd": str(tmp_path)},
                },
            }
        )
        preset = cfg.preset_for("chat")
        env = build_auth_env(preset, cfg, lambda: resolve_claude_subscription_token_sync(cfg, preset))
        assert env == {"CLAUDE_CODE_OAUTH_TOKEN": "sk\x2dant-oat-secret-only"}
    finally:
        set_secret_store(None)


# ── 3. /usage parser ─────────────────────────────────────────────────────────


def test_parse_claude_usage_sample_including_fable_and_tokyo_resets():
    raw = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": SAMPLE_USAGE_TEXT})
    parsed = parse_claude_usage_output(raw, now=SAMPLE_FIXED_NOW)
    assert parsed["auth"] == "ok"
    assert parsed["quota_source"] == "claude_cli_usage"
    assert parsed["last_error"] is None
    quota = parsed["quota"]
    assert [w["label"] for w in quota] == [
        "Current session",
        "Current week (all models)",
        "Current week (Fable)",
    ]
    assert [w["used_percent"] for w in quota] == [4.0, 55.0, 11.0]
    assert quota[0]["resets_text"] == "Nov 15, 3am (Asia/Tokyo)"
    # Nov 15, 03:00 Asia/Tokyo (+09:00) == Nov 14, 18:00 UTC
    assert datetime.fromisoformat(quota[0]["resets_at"]) == datetime(2026, 11, 14, 18, 0, tzinfo=timezone.utc)
    assert datetime.fromisoformat(quota[1]["resets_at"]) == datetime(2026, 11, 18, 18, 0, tzinfo=timezone.utc)
    assert datetime.fromisoformat(quota[2]["resets_at"]) == datetime(2026, 11, 18, 18, 0, tzinfo=timezone.utc)


def test_parse_claude_usage_unparseable_resets_and_year_rollover():
    sample = "\n".join(
        [
            "Current session: 20% used · resets Jan 2, 4am (Asia/Tokyo)",
            "Bonus bucket: 7% used · resets sometime later (Mars/Olympus)",
            "Local window: 3% used · resets soon",
        ]
    )
    dec_now = datetime(2026, 12, 30, 12, 0, tzinfo=timezone.utc)
    parsed = parse_claude_usage_output(json.dumps({"result": sample}), now=dec_now)
    assert parsed["auth"] == "ok"
    w0, w1, w2 = parsed["quota"]
    assert datetime.fromisoformat(w0["resets_at"]) == datetime(2027, 1, 1, 19, 0, tzinfo=timezone.utc)
    assert w1["resets_at"] is None
    assert w1["resets_text"] == "sometime later (Mars/Olympus)"
    assert w2["resets_at"] is None
    assert w2["resets_text"] == "soon"


def test_parse_claude_usage_states_and_no_raw_output():
    setup_only = json.dumps({"result": "Total cost: $0.00\nUsage: 0 input, 0 output tokens\nRAW_MARKER"})
    res_setup = parse_claude_usage_output(setup_only)
    assert res_setup["auth"] == "unknown"
    assert res_setup["quota"] == []
    assert res_setup["quota_source"] == "claude_cli_usage"
    assert res_setup["last_error"] == "quota unavailable from this login"
    assert "RAW_MARKER" not in res_setup["last_error"]

    not_logged = json.dumps({"is_error": True, "result": "Not logged in · Please run /login SENSITIVE_DETAIL"})
    res_login = parse_claude_usage_output(not_logged)
    assert res_login["auth"] == "login_required"
    assert res_login["quota"] == []
    assert "SENSITIVE_DETAIL" not in (res_login["last_error"] or "")

    garbage = parse_claude_usage_output("corrupted garbage SECRET_LEAK")
    assert garbage["auth"] == "unknown"
    assert garbage["last_error"] == "quota unavailable from this login"


# ── 4. Fake CLI subprocess probe, no shell, no config_dir file reads ─────────


@pytest.mark.asyncio
async def test_probe_claude_connection_uses_minimal_env_temp_cwd_and_never_opens_config_dir(
    isolated_s5c_env, monkeypatch: pytest.MonkeyPatch
):
    tmp_path, store = isolated_s5c_env
    cfg_dir = tmp_path / "claude-host-creds"
    cfg_dir.mkdir(parents=True)
    secret_marker = cfg_dir / ".credentials.json"
    secret_marker.write_text('{"should_never_be_opened": true}', encoding="utf-8")

    bin_dir = tmp_path / "bin"
    log_path = tmp_path / "cli-calls.jsonl"
    _write_fake_claude(bin_dir, mode="ok", log_path=log_path)

    # Pollute inherited env with secrets + token vars to verify probe isolates them
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "should-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "should-not-leak")
    monkeypatch.setenv("UNRELATED_SECRET", "top-secret-value")

    # Guard: fail if Kotoyose Python process opens ANY path under cfg_dir
    real_open = builtins.open
    real_os_open = os.open

    def guarded_open(file, *args, **kwargs):
        try:
            resolved = Path(os.fspath(file)).resolve()
        except (TypeError, OSError):
            resolved = None
        if resolved is not None and (resolved == cfg_dir.resolve() or cfg_dir.resolve() in resolved.parents):
            raise AssertionError(f"Kotoyose must never open files inside config_dir: {resolved}")
        return real_open(file, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        try:
            resolved = Path(os.fspath(path)).resolve()
        except (TypeError, OSError):
            resolved = None
        if resolved is not None and (resolved == cfg_dir.resolve() or cfg_dir.resolve() in resolved.parents):
            raise AssertionError(f"Kotoyose must never os.open files inside config_dir: {resolved}")
        return real_os_open(path, flags, *args, **kwargs)

    cfg = HarnessConfig.model_validate(
        {
            "connections": {
                "claude-host": {"provider": "claude", "kind": "subscription", "data_policy": "no_training", "config_dir": str(cfg_dir)},
            }
        }
    )

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    with patch("asyncio.create_subprocess_shell", side_effect=AssertionError("shell must never be used")):
        results = await probe_all_connections(cfg=cfg, execution_service=None, store=store)

    state = results["claude-host"]
    assert state["auth"] == "ok"
    assert state["quota_source"] == "claude_cli_usage"
    assert state["cli_version"] == "2.1.62 (Claude Code)"
    assert state["last_error"] is None
    assert len(state["quota"]) == 3
    assert [w["window"] for w in state["quota"]] == [
        "Current session",
        "Current week (all models)",
        "Current week (Fable)",
    ]

    recorded = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [entry["argv"] for entry in recorded] == [
        ["auth", "status", "--json"],
        ["-p", "/usage", "--output-format", "json"],
    ]
    for call in recorded:
        assert call["cwd"] != str(cfg_dir)
        assert call["cwd"] != str(Path.cwd())
        assert call["env"]["CLAUDE_CONFIG_DIR"] == str(cfg_dir)
        assert set(call["env"].keys()) <= {"PATH", "HOME", "LANG", "CLAUDE_CONFIG_DIR", "__PYVENV_LAUNCHER__", "LC_CTYPE"}
        for forbidden_env in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "UNRELATED_SECRET"):
            assert forbidden_env not in call["env"]


@pytest.mark.asyncio
async def test_probe_claude_connection_modes_timeout_setup_token_login_required_garbage(
    isolated_s5c_env, monkeypatch: pytest.MonkeyPatch
):
    tmp_path, store = isolated_s5c_env
    cfg_dir = tmp_path / "claude-host"
    cfg_dir.mkdir(parents=True)
    conn = Connection(provider="claude", kind="subscription", data_policy="no_training", config_dir=cfg_dir)

    for mode, expected_auth, expected_err in (
        ("setup_token_only", "unknown", "quota unavailable from this login"),
        ("login_required", "login_required", None),
        ("login_expired", "login_required", None),
        ("garbage", "unknown", "quota unavailable from this login"),
    ):
        _CLI_VERSION_CACHE.clear()
        cli_bin = _write_fake_claude(tmp_path / f"bin-{mode}", mode=mode)
        st = await probe_claude_connection(f"conn-{mode}", conn, cli_binary=cli_bin, store=store)
        assert st["auth"] == expected_auth
        assert st["quota"] == []
        assert st["quota_source"] == "claude_cli_usage"
        assert st["cli_version"] == "2.1.62 (Claude Code)"
        if expected_err is not None:
            assert st["last_error"] == expected_err
        assert st["last_error"] is None or len(st["last_error"]) <= 200
        assert "RAW_GARBAGE_SENTINEL_XYZ" not in json.dumps(st)

    # Timeout case
    _CLI_VERSION_CACHE.clear()
    hang_bin = _write_fake_claude(tmp_path / "bin-hang", mode="ok", sleep_seconds=5.0)
    st_timeout = await probe_claude_connection(
        "conn-timeout",
        conn,
        cli_binary=hang_bin,
        store=store,
        timeout_seconds=0.25,
    )
    assert st_timeout["auth"] == "unknown"
    assert st_timeout["quota"] == []
    assert st_timeout["last_error"] is not None
    assert "timed out" in st_timeout["last_error"]
    assert len(st_timeout["last_error"]) <= 200


# ── 5. Admin API quota_from projection & Console UI ──────────────────────────


def test_admin_connections_quota_from_and_console_assets(isolated_s5c_env):
    tmp_path, store = isolated_s5c_env
    cfg_dir = tmp_path / "claude-host"
    cfg_dir.mkdir(parents=True)
    cli_bin = _write_fake_claude(tmp_path / "bin-api", mode="ok")

    cfg = HarnessConfig.model_validate(
        {
            "connections": {
                "claude-host": {"provider": "claude", "kind": "subscription", "data_policy": "no_training", "config_dir": str(cfg_dir)},
                "claude-oauth": {
                    "provider": "claude",
                    "kind": "subscription", "data_policy": "no_training",
                    "secret": "claude-oauth",
                    "quota_from": "claude-host",
                },
            },
            "routes": {
                "claude-r": {"driver": "claude.agent-sdk", "model": "claude-sonnet-4-6", "connection": "claude-oauth"},
            },
            "models": {
                "claude-alias": {"route": "claude-r", "cwd": str(tmp_path)},
            },
        }
    )
    save_config(cfg)

    fake_exec_service = SimpleNamespace(
        config=SimpleNamespace(drivers={"claude.agent-sdk": SimpleNamespace(cli_binary=cli_bin)}),
        close=AsyncMock(),
    )

    with (
        patch("kotoyose.app.start_execution_service", new=AsyncMock(return_value=fake_exec_service)),
        patch("kotoyose.app.connection_probe_loop", new=AsyncMock()),
    ):
        with TestClient(app) as client:
            probe_resp = client.post("/admin/connections/claude-host/probe")
            assert probe_resp.status_code == 200
            assert probe_resp.json()["state"]["auth"] == "ok"

            list_resp = client.get("/admin/connections")
            assert list_resp.status_code == 200
            by_id = {c["id"]: c for c in list_resp.json()["connections"]}

            host_view = by_id["claude-host"]
            assert host_view["config_dir"] == str(cfg_dir)
            assert host_view["state"]["auth"] == "ok"
            assert host_view["state"]["quota_source"] == "claude_cli_usage"
            assert len(host_view["state"]["quota"]) == 3

            oauth_view = by_id["claude-oauth"]
            assert oauth_view["quota_from"] == "claude-host"
            assert oauth_view["state"]["quota_via"] == "claude-host"
            assert oauth_view["state"]["quota_source"] == "claude_cli_usage"
            assert [w["label"] for w in oauth_view["state"]["quota"]] == [
                "Current session",
                "Current week (all models)",
                "Current week (Fable)",
            ]
            # Own auth state stays independent from host quota source
            assert oauth_view["state"]["auth"] == "unknown"

            # Deleting referenced quota_from target is blocked cleanly (409)
            del_resp = client.delete("/admin/connections/claude-host")
            assert del_resp.status_code == 409
            assert "claude-oauth" in del_resp.text

            # Console JS assets load and reflect S5c controls without innerHTML
            import re

            ui_html = client.get("/admin/ui").text
            ver_match = re.search(r"/admin/ui/console/([0-9a-f]{12})/", ui_html)
            assert ver_match is not None
            prefix = f"/admin/ui/console/{ver_match.group(1)}"

            providers_js = client.get(f"{prefix}/js/pages/providers.js")
            assert providers_js.status_code == 200
            assert "Config dir" in providers_js.text
            assert "Quota from" in providers_js.text
            assert "CLAUDE_CONFIG_DIR=" in providers_js.text
            assert "via ${via}" in providers_js.text
            assert ".innerHTML" not in providers_js.text

            format_js = client.get(f"{prefix}/js/format.js")
            assert format_js.status_code == 200
            assert "login_required" in format_js.text
            assert "login required" in format_js.text
