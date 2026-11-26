"""Tests for S5d: Claude CLI PTY login, setup-token, auth status probing, and console UI."""

from __future__ import annotations

import asyncio
import builtins
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import pytest

from kotoyose import auth as auth_module
from kotoyose import claude_login as claude_login_mod
from kotoyose import db as db_module
from kotoyose.app import app
from kotoyose.claude_login import (
    DEFAULT_CLAUDE_LOGIN_TTL_SECONDS,
    ClaudeLoginSession,
    extract_claude_oauth_url,
    extract_setup_token,
    shutdown_claude_login_sessions,
    strip_ansi,
    validate_claude_code,
)
from kotoyose.config import Connection, HarnessConfig, reload_config, save_config
from kotoyose.connection_state_store import ConnectionStateStore, set_connection_state_store
from kotoyose.connections_probe import _CLI_VERSION_CACHE, probe_claude_connection
from kotoyose.secret_store import SecretStore, set_secret_store

AUTH_USER = "admin"
AUTH_PASS = "test-s5d-admin-secret"
BASIC_AUTH = (AUTH_USER, AUTH_PASS)

FAKE_OAUTH_URL = (
    "https://claude.com/cai/oauth/authorize"
    "?code=true&client_id=test-client&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
    "&scope=user%3Aprofile&code_challenge=SECRET_CHALLENGE_XYZ&state=SECRET_STATE_XYZ"
)
FAKE_SETUP_TOKEN = "sk\x2dant-oat01-FAKE_SETUP_TOKEN_S5D_9999"
FAKE_GOOD_CODE = "good-code"
FAKE_EMAIL = "owner@example.invalid"
FAKE_ORG = "Owner Org"

SAMPLE_USAGE_TEXT = (
    "Current session: 12% used · resets Nov 15, 3am (Asia/Tokyo)\n"
    "Current week (all models): 34% used · resets Nov 19, 3am (Asia/Tokyo)\n"
)


def _write_fake_claude_cli(
    bin_dir: Path,
    *,
    log_path: Path | None = None,
    pid_path: Path | None = None,
    url: str = FAKE_OAUTH_URL,
    setup_token: str = FAKE_SETUP_TOKEN,
    logged_in_after_login: bool = True,
    setup_token_prints_token: bool = True,
    ansi_wrap: bool = True,
    hang_on_code: bool = False,
    status_override: dict | None = None,
) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / "claude"
    script = textwrap.dedent(
        f"""\
        #!{sys.executable}
        import json
        import os
        from pathlib import Path
        import sys
        import time

        LOG_PATH = {str(log_path) if log_path is not None else ""!r}
        PID_PATH = {str(pid_path) if pid_path is not None else ""!r}
        URL = {url!r}
        SETUP_TOKEN = {setup_token!r}
        LOGGED_IN_AFTER_LOGIN = {logged_in_after_login!r}
        SETUP_TOKEN_PRINTS_TOKEN = {setup_token_prints_token!r}
        ANSI_WRAP = {ansi_wrap!r}
        HANG_ON_CODE = {hang_on_code!r}
        STATUS_OVERRIDE = {status_override!r}
        USAGE_TEXT = {SAMPLE_USAGE_TEXT!r}

        def record(argv):
            if not LOG_PATH:
                return
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({{
                    "argv": argv,
                    "cwd": os.getcwd(),
                    "env": dict(os.environ),
                    "isatty": sys.stdin.isatty() and sys.stdout.isatty(),
                }}) + "\\n")

        def marker_file():
            cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR")
            if not cfg_dir:
                return None
            return Path(cfg_dir) / ".fake-logged-in"

        def main():
            argv = sys.argv[1:]
            if argv == ["--version"]:
                print("2.1.323 (Claude Code)")
                return

            record(argv)
            if PID_PATH:
                Path(PID_PATH).write_text(str(os.getpid()), encoding="utf-8")

            if argv == ["auth", "status", "--json"]:
                if STATUS_OVERRIDE is not None:
                    print(json.dumps(STATUS_OVERRIDE))
                    sys.exit(0 if STATUS_OVERRIDE.get("loggedIn") else 1)
                mf = marker_file()
                is_logged_in = bool(mf and mf.exists())
                if is_logged_in:
                    print(json.dumps({{
                        "loggedIn": True,
                        "authMethod": "claudeai",
                        "loginExpiresAt": "2026-12-15T00:00:00Z",
                        "email": {FAKE_EMAIL!r},
                        "orgName": {FAKE_ORG!r},
                        "subscriptionType": "max",
                        "planTier": "default_claude_max_20x",
                    }}))
                else:
                    print(json.dumps({{
                        "loggedIn": False,
                        "authMethod": "none",
                        "loginExpiresAt": None,
                    }}))
                    sys.exit(1)  # 本物の CLI (2.1.323) は未ログインで終了コード 1 を返す
                return

            if argv[:2] == ["-p", "/usage"]:
                print(json.dumps({{
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": USAGE_TEXT,
                }}))
                return

            if argv == ["auth", "login", "--claudeai"] or argv == ["setup-token"]:
                if ANSI_WRAP:
                    sys.stdout.write("\\x1b[36mOpening browser to sign in…\\x1b[0m\\r\\n")
                    sys.stdout.write(
                        f"If the browser didn't open, visit: \\x1b[4m\\x1b]8;;{{URL}}\\x07{{URL}}\\x1b]8;;\\x07\\x1b[0m\\r\\n"
                    )
                    sys.stdout.write("Paste code here if prompted > ")
                else:
                    sys.stdout.write("Opening browser to sign in…\\n")
                    sys.stdout.write(f"If the browser didn't open, visit: {{URL}}\\n")
                    sys.stdout.write("Paste code here if prompted > ")
                sys.stdout.flush()

                code = sys.stdin.readline()
                if not code:
                    sys.exit(1)
                code = code.rstrip("\\r\\n")
                if HANG_ON_CODE:
                    time.sleep(30)
                    return

                if code != "good-code":
                    sys.stdout.write("\\x1b[31mError: invalid authorization code\\x1b[0m\\r\\n")
                    sys.stdout.flush()
                    sys.exit(1)

                if argv == ["auth", "login", "--claudeai"]:
                    if LOGGED_IN_AFTER_LOGIN:
                        mf = marker_file()
                        if mf is not None:
                            mf.parent.mkdir(parents=True, exist_ok=True)
                            mf.write_text("ok", encoding="utf-8")
                    sys.stdout.write("Login successful!\\r\\n")
                    sys.stdout.flush()
                    return

                # setup-token
                if SETUP_TOKEN_PRINTS_TOKEN:
                    sys.stdout.write("\\r\\nYour long-lived OAuth token:\\r\\n")
                    if ANSI_WRAP:
                        sys.stdout.write(f"\\x1b[32m{{SETUP_TOKEN}}\\x1b[0m\\r\\n")
                    else:
                        sys.stdout.write(f"{{SETUP_TOKEN}}\\r\\n")
                    sys.stdout.flush()
                else:
                    sys.stdout.write("Setup finished without token\\r\\n")
                    sys.stdout.flush()
                return

            sys.exit(2)

        if __name__ == "__main__":
            main()
        """
    )
    exe.write_text(script, encoding="utf-8")
    exe.chmod(0o755)
    return exe


@pytest.fixture
def isolated_s5d_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text("{}\n", encoding="utf-8")
    key_file = tmp_path / "master.key"
    key_file.write_bytes(os.urandom(32))
    os.chmod(key_file, 0o600)

    monkeypatch.setenv("KOTOYOSE_CONFIG", str(cfg_path))
    monkeypatch.setenv("KOTOYOSE_MASTER_KEY_FILE", str(key_file))
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setattr(auth_module, "API_KEY", AUTH_PASS)
    monkeypatch.setattr(auth_module, "BASIC_AUTH_USER", AUTH_USER)
    monkeypatch.setattr(db_module, "_pool", None)
    _CLI_VERSION_CACHE.clear()

    state_store = ConnectionStateStore(pool=None)
    set_connection_state_store(state_store)
    secret_store = SecretStore(master_key=key_file.read_bytes())
    set_secret_store(secret_store)
    reload_config()

    yield tmp_path, cfg_path, state_store, secret_store

    asyncio.run(shutdown_claude_login_sessions())
    set_connection_state_store(None)
    set_secret_store(None)
    reload_config()


def _install_fake_sdk_and_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bin_dir: Path,
    cfg_dir: Path,
    secret_name: str = "claude-oauth-token",
) -> None:
    sdk = tmp_path / "fake-sdk"
    sdk.mkdir()
    (sdk / "_bundled").symlink_to(bin_dir, target_is_directory=True)
    monkeypatch.setattr("kotoyose.claude_cli.find_spec", lambda _: SimpleNamespace(origin=sdk / "__init__.py"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LANG", "C.UTF-8")
    # Pollute env with forbidden credentials that must be stripped from CLI PTY env.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk\x2dant-api-FORBIDDEN")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "forbidden-auth-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://forbidden.example.invalid")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk\x2dant-oat-FORBIDDEN")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("UNRELATED_SECRET", "top-secret-env")

    cfg = HarnessConfig.model_validate(
        {
            "connections": {
                "claude-host": {
                    "provider": "claude",
                    "kind": "subscription", "data_policy": "no_training",
                    "config_dir": str(cfg_dir),
                },
                "claude-oauth": {
                    "provider": "claude",
                    "kind": "subscription", "data_policy": "no_training",
                    "secret": secret_name,
                },
                "claude-env": {
                    "provider": "claude",
                    "kind": "subscription", "data_policy": "no_training",
                    "secret": "env:CLAUDE_CODE_OAUTH_TOKEN",
                },
            }
        }
    )
    save_config(cfg)


@pytest.mark.asyncio
async def test_claude_cli_defaults_to_bundle_and_reports_its_version(
    isolated_s5d_env, monkeypatch: pytest.MonkeyPatch,
):
    from kotoyose.execution_resources import NativeExecutionConfig

    tmp_path, _, state_store, _ = isolated_s5d_env
    sdk = tmp_path / "sdk"
    bundled = sdk / "_bundled" / "claude"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("#!/bin/sh\nprintf '%s\\n' '2.1.456 (Claude Code)'\n", encoding="utf-8")
    bundled.chmod(0o755)
    competing = tmp_path / "claude"
    competing.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    competing.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr("kotoyose.claude_cli.find_spec", lambda _: SimpleNamespace(origin=sdk / "__init__.py"))
    settings = dict(schema_version="1", image_id="sha256:" + "0" * 64)
    conn = Connection(kind="subscription", provider="claude", data_policy="no_training", secret="env:FAKE")
    cfg = HarnessConfig(connections={"claude-test": conn})

    execution = NativeExecutionConfig(**settings, drivers={"claude.agent-sdk": {}})
    assert execution.drivers["claude.agent-sdk"].cli_binary == bundled
    assert claude_login_mod._resolve_claude_cli(cfg, "claude-test", conn) == bundled
    state = await probe_claude_connection("claude-test", conn, cli_binary=None, store=state_store)
    assert state["cli_version"] == "2.1.456 (Claude Code)"

    bundled.unlink()
    with pytest.raises(ValueError, match="bundled CLI is missing"):
        NativeExecutionConfig(**settings, drivers={"claude.agent-sdk": {}})
    with pytest.raises(claude_login_mod.ClaudeLoginNotSupportedError, match="bundled CLI is missing"):
        claude_login_mod._resolve_claude_cli(cfg, "claude-test", conn)
    state = await probe_claude_connection("claude-test", conn, cli_binary=None, store=state_store)
    assert "bundled CLI is missing" in state["last_error"]

    explicit = NativeExecutionConfig(**settings, drivers={"claude.agent-sdk": {"cli_binary": competing}})
    assert explicit.drivers["claude.agent-sdk"].cli_binary == competing


# ── 1. Unit helpers: ANSI stripping, URL extraction, code validation, 4KB buffer ─


def test_ansi_url_token_and_code_validation_helpers():
    raw_ansi = (
        "\x1b[?25l\x1b[36mOpening...\x1b[0m\r\n"
        "Visit: \x1b]8;;https://platform.claude.com/oauth/authorize?state=abc&code_challenge=def\x07"
        "https://platform.claude.com/oauth/authorize?state=abc&code_challenge=def\x1b]8;;\x07\r\n"
    )
    assert (
        extract_claude_oauth_url(raw_ansi)
        == "https://platform.claude.com/oauth/authorize?state=abc&code_challenge=def"
    )
    assert (
        extract_claude_oauth_url("visit https://claude.ai/oauth/authorize?x=1\n")
        == "https://claude.ai/oauth/authorize?x=1"
    )
    assert (
        extract_claude_oauth_url("visit https://claude.com/cai/oauth/authorize?x=2 ")
        == "https://claude.com/cai/oauth/authorize?x=2"
    )
    assert extract_claude_oauth_url("visit https://evil.example.com/oauth\n") is None
    assert strip_ansi("\x1b[31merr\x1b[0m\r\n") == "err\n"

    assert extract_setup_token("prefix\n\x1b[32msk\x2dant-oat01-ABC_def-123\x1b[0m\nsuffix\n") == "sk\x2dant-oat01-ABC_def-123"
    assert extract_setup_token("no token here") is None

    assert validate_claude_code("good-code#123") == "good-code#123"
    for bad in ("", "x" * 513, "has space", "has\nnewline", "has\rreturn", "has\ttab", "ctrl\x03char", "esc\x1b[31m"):
        with pytest.raises(ValueError):
            validate_claude_code(bad)

    sess = ClaudeLoginSession(
        session_id="s1",
        connection_id="c1",
        kind="login",
        url="https://claude.com/x",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        cli_binary=Path("claude"),
        config_dir=Path("/tmp/c1"),
    )
    sess.append_output_bytes(b"A" * 5000)
    assert len(sess._output_buf) == 4096
    sess.wipe_buffers()
    assert sess._output_buf == ""


# ── 2. Claude login (config_dir): start, code submit, status, no shell/file reads/logs ─


def test_claude_login_full_flow_env_no_shell_no_config_open_and_audit_logs(
    isolated_s5d_env, monkeypatch: pytest.MonkeyPatch
):
    tmp_path, _, state_store, _ = isolated_s5d_env
    cfg_dir = tmp_path / "missing-claude-host"
    assert not cfg_dir.exists()

    bin_dir = tmp_path / "bin"
    log_path = tmp_path / "cli-calls.jsonl"
    _write_fake_claude_cli(bin_dir, log_path=log_path)
    _install_fake_sdk_and_config(tmp_path, monkeypatch, bin_dir=bin_dir, cfg_dir=cfg_dir)

    audit_events: list[tuple[str, dict]] = []

    class _RecordingLog:
        def info(self, event: str, **kwargs):
            audit_events.append((event, kwargs))

        def warning(self, event: str, **kwargs):
            audit_events.append((event, kwargs))

    monkeypatch.setattr(claude_login_mod, "log", _RecordingLog())

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

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)

    with (
        patch("asyncio.create_subprocess_shell", side_effect=AssertionError("shell must never be used")),
        patch("kotoyose.app.start_execution_service", new=AsyncMock(return_value=None)),
        patch("kotoyose.app.connection_probe_loop", new=AsyncMock()),
    ):
        with TestClient(app) as client:
            # Auth required
            assert client.post("/admin/connections/claude-host/claude-login").status_code == 401
            assert (
                client.post(
                    "/admin/connections/claude-host/claude-login",
                    headers={"Authorization": f"Bearer {AUTH_PASS}"},
                ).status_code
                == 401
            )

            # Wrong connection kind rejected
            assert (
                client.post("/admin/connections/claude-oauth/claude-login", auth=BASIC_AUTH).status_code
                == 400
            )

            # Start login
            started = client.post("/admin/connections/claude-host/claude-login", auth=BASIC_AUTH)
            assert started.status_code == 200
            start_body = started.json()
            assert start_body["url"] == FAKE_OAUTH_URL
            assert start_body["session_id"]
            assert start_body["expires_at"]
            sid = start_body["session_id"]

            # Missing config_dir was created with 0700
            assert cfg_dir.is_dir()
            assert (cfg_dir.stat().st_mode & 0o777) == 0o700

            # One session per connection
            conflict = client.post("/admin/connections/claude-host/claude-login", auth=BASIC_AUTH)
            assert conflict.status_code == 409

            # Poll waiting status
            polled = client.get(f"/admin/connections/claude-host/claude-login/{sid}", auth=BASIC_AUTH)
            assert polled.status_code == 200
            assert polled.json()["state"] == "waiting_for_code"
            assert polled.json()["url"] == FAKE_OAUTH_URL

            # Code validation rejects whitespace / empty / oversize / control chars
            for bad_code in ("", "bad code", "bad\ncode", "x" * 513, "bad\x03code"):
                bad_req = client.post(
                    f"/admin/connections/claude-host/claude-login/{sid}/code",
                    auth=BASIC_AUTH,
                    json={"code": bad_code},
                )
                assert bad_req.status_code == 422

            # Submit good-code
            completed = client.post(
                f"/admin/connections/claude-host/claude-login/{sid}/code",
                auth=BASIC_AUTH,
                json={"code": FAKE_GOOD_CODE},
            )
            assert completed.status_code == 200
            comp_body = completed.json()
            assert comp_body == {
                "state": "completed",
                "logged_in": True,
                "login_expires_at": "2026-12-15T00:00:00+00:00",
                "plan": "max",
                "email": FAKE_EMAIL,
            }

            # Connection state recorded without email/org
            conn_state = asyncio.run(app.state.connection_state_store.get("claude-host"))
            assert conn_state is not None
            assert conn_state["auth"] == "ok"
            assert conn_state["plan"] == "max"
            assert conn_state["expires_at"] == "2026-12-15T00:00:00+00:00"
            assert len(conn_state["quota"]) == 2
            assert FAKE_EMAIL not in json.dumps(conn_state)
            assert FAKE_ORG not in json.dumps(conn_state)

    # Verify CLI calls, minimal stripped env, PTY isatty
    recorded = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    pty_call = recorded[0]
    assert pty_call["argv"] == ["auth", "login", "--claudeai"]
    assert pty_call["isatty"] is True
    assert pty_call["env"]["BROWSER"] == "true"
    assert pty_call["env"]["CLAUDE_CONFIG_DIR"] == str(cfg_dir)
    for key in pty_call["env"]:
        assert not key.startswith("ANTHROPIC_")
        assert not key.startswith("CLAUDE_CODE_")
    assert "UNRELATED_SECRET" not in pty_call["env"]

    # Audit events logged ONLY claude_login.started and claude_login.completed with connection & session_id
    assert audit_events == [
        ("claude_login.started", {"connection": "claude-host", "session_id": sid}),
        ("claude_login.completed", {"connection": "claude-host", "session_id": sid}),
    ]
    serialized_logs = json.dumps(audit_events)
    assert "SECRET_STATE_XYZ" not in serialized_logs
    assert "SECRET_CHALLENGE_XYZ" not in serialized_logs
    assert FAKE_GOOD_CODE not in serialized_logs


# ── 3. Bad code -> failed, cancel, expiry kills process ──────────────────────


def test_claude_login_bad_code_cancel_and_expiry_kills_process(
    isolated_s5d_env, monkeypatch: pytest.MonkeyPatch
):
    tmp_path, _, _, _ = isolated_s5d_env
    cfg_dir = tmp_path / "claude-host"
    cfg_dir.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    pid_path = tmp_path / "cli.pid"
    _write_fake_claude_cli(bin_dir, pid_path=pid_path)
    _install_fake_sdk_and_config(tmp_path, monkeypatch, bin_dir=bin_dir, cfg_dir=cfg_dir)

    audit_events: list[tuple[str, dict]] = []

    class _RecordingLog:
        def info(self, event: str, **kwargs):
            audit_events.append((event, kwargs))

        def warning(self, event: str, **kwargs):
            audit_events.append((event, kwargs))

    monkeypatch.setattr(claude_login_mod, "log", _RecordingLog())

    with (
        patch("kotoyose.app.start_execution_service", new=AsyncMock(return_value=None)),
        patch("kotoyose.app.connection_probe_loop", new=AsyncMock()),
    ):
        with TestClient(app) as client:
            # 1) Bad code -> failed
            s1 = client.post("/admin/connections/claude-host/claude-login", auth=BASIC_AUTH).json()
            sid1 = s1["session_id"]
            bad_resp = client.post(
                f"/admin/connections/claude-host/claude-login/{sid1}/code",
                auth=BASIC_AUTH,
                json={"code": "wrong-code"},
            )
            assert bad_resp.status_code == 200
            bad_body = bad_resp.json()
            assert bad_body["state"] == "failed"
            assert "invalid authorization code" in bad_body["error"].lower()
            assert "wrong-code" not in json.dumps(bad_body)

            # 2) Cancel kills process
            s2 = client.post("/admin/connections/claude-host/claude-login", auth=BASIC_AUTH).json()
            sid2 = s2["session_id"]
            pid2 = int(pid_path.read_text(encoding="utf-8"))
            del_resp = client.delete(f"/admin/connections/claude-host/claude-login/{sid2}", auth=BASIC_AUTH)
            assert del_resp.status_code == 200
            assert del_resp.json()["state"] == "cancelled"
            with pytest.raises(ProcessLookupError):
                os.kill(pid2, 0)

            # 3) Expiry (10m constant monkeypatched short) kills process
            monkeypatch.setattr(claude_login_mod, "DEFAULT_CLAUDE_LOGIN_TTL_SECONDS", 0.15)
            s3 = client.post("/admin/connections/claude-host/claude-login", auth=BASIC_AUTH).json()
            sid3 = s3["session_id"]
            pid3 = int(pid_path.read_text(encoding="utf-8"))
            import time

            time.sleep(0.28)
            polled3 = client.get(f"/admin/connections/claude-host/claude-login/{sid3}", auth=BASIC_AUTH)
            assert polled3.status_code == 200
            assert polled3.json()["state"] == "expired"
            with pytest.raises(ProcessLookupError):
                os.kill(pid3, 0)

    assert ("claude_login.failed", {"connection": "claude-host", "session_id": sid1}) in audit_events
    assert ("claude_login.cancelled", {"connection": "claude-host", "session_id": sid2}) in audit_events
    assert ("claude_login.failed", {"connection": "claude-host", "session_id": sid3}) in audit_events
    assert DEFAULT_CLAUDE_LOGIN_TTL_SECONDS == 600


# ── 4. Setup-token issue: stores in SecretStore, wipes buffers, never leaks ──


def test_claude_setup_token_stores_secret_cleans_tempdir_and_never_leaks(
    isolated_s5d_env, monkeypatch: pytest.MonkeyPatch, capsys
):
    tmp_path, _, state_store, _ = isolated_s5d_env
    cfg_dir = tmp_path / "claude-host"
    cfg_dir.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    log_path = tmp_path / "setup-calls.jsonl"
    _write_fake_claude_cli(bin_dir, log_path=log_path)
    _install_fake_sdk_and_config(tmp_path, monkeypatch, bin_dir=bin_dir, cfg_dir=cfg_dir)

    with (
        patch("kotoyose.app.start_execution_service", new=AsyncMock(return_value=None)),
        patch("kotoyose.app.connection_probe_loop", new=AsyncMock()),
    ):
        with TestClient(app) as client:
            # Incompatible connections rejected
            assert (
                client.post("/admin/connections/claude-host/claude-setup-token", auth=BASIC_AUTH).status_code
                == 400
            )
            assert (
                client.post("/admin/connections/claude-env/claude-setup-token", auth=BASIC_AUTH).status_code
                == 400
            )

            started = client.post("/admin/connections/claude-oauth/claude-setup-token", auth=BASIC_AUTH)
            assert started.status_code == 200
            start_body = started.json()
            sid = start_body["session_id"]
            assert start_body["url"] == FAKE_OAUTH_URL

            completed = client.post(
                f"/admin/connections/claude-oauth/claude-setup-token/{sid}/code",
                auth=BASIC_AUTH,
                json={"code": FAKE_GOOD_CODE},
            )
            assert completed.status_code == 200
            comp_body = completed.json()
            assert comp_body["state"] == "completed"
            assert comp_body["secret"] == "claude-oauth-token"
            assert comp_body["updated_at"]
            assert FAKE_SETUP_TOKEN not in completed.text

            # Polled status also never reveals the token
            polled = client.get(
                f"/admin/connections/claude-oauth/claude-setup-token/{sid}",
                auth=BASIC_AUTH,
            )
            assert polled.status_code == 200
            assert FAKE_SETUP_TOKEN not in polled.text

            # Stored in active secret store
            active_secret_store = app.state.secret_store
            assert asyncio.run(active_secret_store.get("claude-oauth-token")) == FAKE_SETUP_TOKEN

            # Session buffer wiped
            session_obj = claude_login_mod._SESSIONS[sid]
            assert session_obj._output_buf == ""

            # Connection state cleared to ok
            conn_state = asyncio.run(app.state.connection_state_store.get("claude-oauth"))
            assert conn_state is not None
            assert conn_state["auth"] == "ok"

    # Temp CLAUDE_CONFIG_DIR used by setup-token was deleted afterwards and env was stripped
    recorded = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(recorded) == 1
    call = recorded[0]
    assert call["argv"] == ["setup-token"]
    temp_cfg_dir = Path(call["env"]["CLAUDE_CONFIG_DIR"])
    assert temp_cfg_dir != cfg_dir
    assert not temp_cfg_dir.exists()
    assert call["env"]["BROWSER"] == "true"
    for key in call["env"]:
        assert not key.startswith("ANTHROPIC_")
        assert not key.startswith("CLAUDE_CODE_")

    # Captured stdout/logs never contain token, code, or OAuth state/challenge
    captured_out = capsys.readouterr().out
    assert FAKE_SETUP_TOKEN not in captured_out
    assert FAKE_GOOD_CODE not in captured_out
    assert "SECRET_STATE_XYZ" not in captured_out
    assert "SECRET_CHALLENGE_XYZ" not in captured_out
    assert "claude_login.started" in captured_out
    assert "claude_login.completed" in captured_out


def test_claude_setup_token_missing_token_returns_failed(
    isolated_s5d_env, monkeypatch: pytest.MonkeyPatch
):
    tmp_path, _, _, _ = isolated_s5d_env
    cfg_dir = tmp_path / "claude-host"
    cfg_dir.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    _write_fake_claude_cli(bin_dir, setup_token_prints_token=False)
    _install_fake_sdk_and_config(tmp_path, monkeypatch, bin_dir=bin_dir, cfg_dir=cfg_dir)

    with (
        patch("kotoyose.app.start_execution_service", new=AsyncMock(return_value=None)),
        patch("kotoyose.app.connection_probe_loop", new=AsyncMock()),
    ):
        with TestClient(app) as client:
            sid = client.post("/admin/connections/claude-oauth/claude-setup-token", auth=BASIC_AUTH).json()["session_id"]
            res = client.post(
                f"/admin/connections/claude-oauth/claude-setup-token/{sid}/code",
                auth=BASIC_AUTH,
                json={"code": FAKE_GOOD_CODE},
            )
            assert res.status_code == 200
            assert res.json()["state"] == "failed"
            assert "No setup-token" in res.json()["error"]


# ── 5. Status detection in S5c probe & Console UI strings ────────────────────


@pytest.mark.asyncio
async def test_s5c_probe_records_claude_auth_status_without_email_or_org(
    isolated_s5d_env, monkeypatch: pytest.MonkeyPatch
):
    tmp_path, _, state_store, _ = isolated_s5d_env
    cfg_dir = tmp_path / "claude-host"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / ".fake-logged-in").write_text("ok", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    cli_bin = _write_fake_claude_cli(
        bin_dir,
        status_override={
            "loggedIn": True,
            "authMethod": "claudeai",
            "loginExpiresAt": "2026-11-15T12:00:00Z",
            "email": FAKE_EMAIL,
            "orgName": FAKE_ORG,
            "subscriptionType": "pro",
            "planTier": "pro",
        },
    )
    conn = Connection(provider="claude", kind="subscription", data_policy="no_training", config_dir=cfg_dir)
    fixed_now = datetime(2026, 11, 14, 18, 0, 0, tzinfo=timezone.utc)

    state = await probe_claude_connection(
        "claude-host",
        conn,
        cli_binary=cli_bin,
        store=state_store,
        now=fixed_now,
    )
    # Within 48h -> expiring
    assert state["auth"] == "expiring"
    assert state["plan"] == "pro"
    assert state["expires_at"] == "2026-11-15T12:00:00+00:00"
    assert len(state["quota"]) == 2
    assert FAKE_EMAIL not in json.dumps(state)
    assert FAKE_ORG not in json.dumps(state)

    # Logged out case
    _CLI_VERSION_CACHE.clear()
    cli_logged_out = _write_fake_claude_cli(
        tmp_path / "bin-out",
        status_override={"loggedIn": False, "loginExpiresAt": None},
    )
    state_out = await probe_claude_connection(
        "claude-host",
        conn,
        cli_binary=cli_logged_out,
        store=state_store,
        now=fixed_now,
    )
    assert state_out["auth"] == "logged_out"
    assert state_out["quota"] == []

    # config_dir がまだ無い: CLI を走らせず logged_out。dir も作らない (作るのはログインの流れで 0700)。
    missing_dir = tmp_path / "never-logged-in"
    missing_conn = conn.model_copy(update={"config_dir": missing_dir})
    log_path = tmp_path / "missing-calls.jsonl"
    cli_missing = _write_fake_claude_cli(tmp_path / "bin-missing", log_path=log_path)
    state_missing = await probe_claude_connection(
        "claude-host",
        missing_conn,
        cli_binary=cli_missing,
        store=state_store,
        now=fixed_now,
    )
    assert state_missing["auth"] == "logged_out"
    assert not missing_dir.exists()
    calls = [json.loads(line)["argv"] for line in log_path.read_text().splitlines()] if log_path.exists() else []
    assert ["auth", "status", "--json"] not in calls


def test_console_providers_ui_contains_s5d_login_and_issue_token_controls(isolated_s5d_env):
    with (
        patch("kotoyose.app.start_execution_service", new=AsyncMock(return_value=None)),
        patch("kotoyose.app.connection_probe_loop", new=AsyncMock()),
    ):
        with TestClient(app) as client:
            ui_html = client.get("/admin/ui", auth=BASIC_AUTH).text
            ver_match = re.search(r"/admin/ui/console/([0-9a-f]{12})/", ui_html)
            assert ver_match is not None
            prefix = f"/admin/ui/console/{ver_match.group(1)}"

            providers_js = client.get(f"{prefix}/js/pages/providers.js", auth=BASIC_AUTH)
            assert providers_js.status_code == 200
            text = providers_js.text
            assert "Log in" in text
            assert "Issue token" in text
            assert "Open sign-in page" in text
            assert "Submit" in text
            assert "Cancel" in text
            assert "Log in from a terminal:" not in text
            assert ".innerHTML" not in text
            tools = client.get("/admin/tools", auth=BASIC_AUTH)
            assert tools.status_code == 200
            assert tools.json()["connections"]["claude"]["terminal"] == {
                "env_paths": {"CLAUDE_CONFIG_DIR": "."}, "env": {}, "argv": ["claude"], "follow_up": "/login",
            }

            api_js = client.get(f"{prefix}/js/api.js", auth=BASIC_AUTH)
            assert api_js.status_code == 200
            assert "claude-login" in api_js.text
            assert "claude-setup-token" in api_js.text


def test_claude_login_app_shutdown_kills_active_pty_process(
    isolated_s5d_env, monkeypatch: pytest.MonkeyPatch
):
    tmp_path, _, _, _ = isolated_s5d_env
    cfg_dir = tmp_path / "claude-host"
    cfg_dir.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    pid_path = tmp_path / "shutdown-cli.pid"
    _write_fake_claude_cli(bin_dir, pid_path=pid_path)
    _install_fake_sdk_and_config(tmp_path, monkeypatch, bin_dir=bin_dir, cfg_dir=cfg_dir)

    with (
        patch("kotoyose.app.start_execution_service", new=AsyncMock(return_value=None)),
        patch("kotoyose.app.connection_probe_loop", new=AsyncMock()),
    ):
        with TestClient(app) as client:
            started = client.post("/admin/connections/claude-host/claude-login", auth=BASIC_AUTH)
            assert started.status_code == 200
            pid = int(pid_path.read_text(encoding="utf-8"))
            os.kill(pid, 0)

        # Exiting TestClient runs lifespan shutdown -> shutdown_claude_login_sessions()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

