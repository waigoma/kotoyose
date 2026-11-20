"""Tests for P1b: Print-CLI PTY login (cli_login.py), agy login home probing/discovery, and console UI."""

from __future__ import annotations

import asyncio
import builtins
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from kotoyose import auth as auth_module
from kotoyose import cli_login as cli_login_mod
from kotoyose import db as db_module
from kotoyose.app import app
from kotoyose.cli_login import (
    PtyLoginSession,
    extract_matching_url,
    shutdown_cli_login_sessions,
    strip_ansi,
    validate_login_code,
)
from kotoyose.config import Connection, HarnessConfig, reload_config, save_config
from kotoyose.connection_state_store import ConnectionStateStore, set_connection_state_store
from kotoyose.connections_probe import _CLI_VERSION_CACHE, probe_print_cli_connection
from kotoyose.model_discovery import clear_connection_discovery_cache, discover_models
from kotoyose.print_cli_profiles import AGY_PROFILE
from tests.test_s5e_agy_quota import SAMPLE_AGY_MODELS_TEXT, SAMPLE_AGY_USAGE_PAYLOAD

AUTH_USER = "admin"
AUTH_PASS = "test-p1b-admin-secret"
BASIC_AUTH = (AUTH_USER, AUTH_PASS)

FAKE_GOOGLE_URL = (
    "https://accounts.google.com/o/oauth2/auth"
    "?access_type=offline&client_id=test-agy-client"
    "&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback"
    "&response_type=code&code_challenge=SECRET_AGY_CHALLENGE&state=SECRET_AGY_STATE"
)
FAKE_GOOD_CODE = "good-code"


def _write_fake_agy_cli(
    bin_dir: Path,
    *,
    log_path: Path | None = None,
    pid_path: Path | None = None,
    url: str = FAKE_GOOGLE_URL,
    ansi_wrap: bool = True,
    cli_idle_timeout: float = 30.0,
    timeout_exit_code: int = 1,
    timeout_message: str = "Timed out waiting for authentication",
    non_success_json: bool = False,
) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / "agy"
    script = textwrap.dedent(
        f"""\
        #!{sys.executable}
        import json
        import os
        from pathlib import Path
        import select
        import sys
        import time

        LOG_PATH = {str(log_path) if log_path is not None else ""!r}
        PID_PATH = {str(pid_path) if pid_path is not None else ""!r}
        URL = {url!r}
        ANSI_WRAP = {ansi_wrap!r}
        CLI_IDLE_TIMEOUT = {cli_idle_timeout!r}
        TIMEOUT_EXIT_CODE = {timeout_exit_code!r}
        TIMEOUT_MESSAGE = {timeout_message!r}
        NON_SUCCESS_JSON = {non_success_json!r}
        USAGE_PAYLOAD = {SAMPLE_AGY_USAGE_PAYLOAD!r}
        MODELS_TEXT = {SAMPLE_AGY_MODELS_TEXT!r}

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
            home = os.environ.get("HOME")
            if not home:
                return None
            return Path(home) / ".agy-fake-logged-in"

        def main():
            argv = sys.argv[1:]
            if argv == ["--version"]:
                print("1.5.9")
                return

            record(argv)
            if PID_PATH:
                Path(PID_PATH).write_text(str(os.getpid()), encoding="utf-8")

            if argv == ["models"]:
                mf = marker_file()
                if not (mf and mf.exists()):
                    sys.stderr.write("Authentication required\\n")
                    sys.exit(1)
                sys.stdout.write(MODELS_TEXT)
                return

            if argv == ["-p", "/usage", "--output-format", "json"]:
                mf = marker_file()
                if mf and mf.exists():
                    print(json.dumps(USAGE_PAYLOAD))
                    return

                if ANSI_WRAP:
                    sys.stdout.write("Authentication required. Please visit the URL to log in:\\r\\n")
                    sys.stdout.write(
                        f"  \\x1b[4m\\x1b]8;;{{URL}}\\x07{{URL}}\\x1b]8;;\\x07\\x1b[0m\\r\\n\\r\\n"
                    )
                else:
                    sys.stdout.write("Authentication required. Please visit the URL to log in:\\n")
                    sys.stdout.write(f"  {{URL}}\\n\\n")
                sys.stdout.write("Waiting for authentication (timeout 60s)...\\r\\n")
                sys.stdout.write("Or, paste the authorization code here and press Enter: ")
                sys.stdout.flush()

                ready, _, _ = select.select([sys.stdin], [], [], CLI_IDLE_TIMEOUT)
                if not ready:
                    sys.stdout.write(f"\\r\\n{{TIMEOUT_MESSAGE}}\\r\\n")
                    sys.stdout.flush()
                    sys.exit(TIMEOUT_EXIT_CODE)

                code = sys.stdin.readline()
                if not code:
                    sys.exit(1)
                code = code.rstrip("\\r\\n")
                if code != "good-code":
                    sys.stdout.write("\\x1b[31mError: invalid authorization code\\x1b[0m\\r\\n")
                    sys.stdout.flush()
                    sys.exit(1)

                if NON_SUCCESS_JSON:
                    print(json.dumps({{"status": "ERROR", "error": "verification failed"}}))
                    return

                if mf is not None:
                    mf.parent.mkdir(parents=True, exist_ok=True)
                    mf.write_text("ok", encoding="utf-8")
                print(json.dumps(USAGE_PAYLOAD))
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
def isolated_p1b_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(cfg_path))
    monkeypatch.setattr(auth_module, "API_KEY", AUTH_PASS)
    monkeypatch.setattr(auth_module, "BASIC_AUTH_USER", AUTH_USER)
    monkeypatch.setattr(db_module, "_pool", None)
    _CLI_VERSION_CACHE.clear()
    clear_connection_discovery_cache()

    state_store = ConnectionStateStore(pool=None)
    set_connection_state_store(state_store)
    reload_config()

    yield tmp_path, cfg_path, state_store

    asyncio.run(shutdown_cli_login_sessions())
    set_connection_state_store(None)
    clear_connection_discovery_cache()
    reload_config()


def _install_agy_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bin_dir: Path,
    cfg_dir: Path,
) -> None:
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "process-home"))
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk\x2dant-api-FORBIDDEN")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk\x2dant-oat-FORBIDDEN")
    monkeypatch.setenv("UNRELATED_SECRET", "top-secret-env")

    cfg = HarnessConfig.model_validate(
        {
            "connections": {
                "agy-dedicated": {
                    "provider": "antigravity",
                    "profile": "agy",
                    "kind": "subscription", "data_policy": "no_training",
                    "config_dir": str(cfg_dir),
                },
                "agy-host": {
                    "provider": "antigravity",
                    "profile": "agy",
                    "kind": "subscription", "data_policy": "no_training",
                },
                "codex-sub": {
                    "provider": "codex",
                    "kind": "subscription", "data_policy": "no_training",
                },
            }
        }
    )
    save_config(cfg)


# ── 1. Helpers & config_dir tilde expansion ──────────────────────────────────


def test_cli_login_helpers_and_config_dir_tilde_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert AGY_PROFILE.login is not None
    raw_ansi = (
        "Authentication required. Please visit the URL to log in:\r\n"
        f"  \x1b[4m\x1b]8;;{FAKE_GOOGLE_URL}\x07{FAKE_GOOGLE_URL}\x1b]8;;\x07\x1b[0m\r\n"
    )
    assert extract_matching_url(raw_ansi, AGY_PROFILE.login.url_pattern) == FAKE_GOOGLE_URL
    assert extract_matching_url("visit https://evil.example.com/oauth\n", AGY_PROFILE.login.url_pattern) is None
    assert strip_ansi("\x1b[31merr\x1b[0m\r\n") == "err\n"

    assert validate_login_code("good-code#1") == "good-code#1"
    for bad in ("", "x" * 513, "bad code", "bad\ncode", "bad\x03code"):
        with pytest.raises(ValueError):
            validate_login_code(bad)

    sess = PtyLoginSession(
        session_id="s1",
        connection_id="agy-1",
        url=FAKE_GOOGLE_URL,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
        cli_binary=Path("agy"),
        login_home=tmp_path / "home",
    )
    sess.append_output_bytes(b"B" * 5000)
    assert len(sess._output_buf) == 4096
    sess.wipe_buffers()
    assert sess._output_buf == ""

    fake_home = tmp_path / "user-home"
    monkeypatch.setenv("HOME", str(fake_home))
    conn = Connection(
        provider="antigravity",
        kind="subscription", data_policy="no_training",
        config_dir=Path("~/.local/share/kotoyose/agy-home"),
    )
    assert conn.config_dir == fake_home / ".local/share/kotoyose/agy-home"
    assert conn.config_dir.is_absolute()

    with pytest.raises(ValidationError):
        Connection(provider="antigravity", kind="subscription", data_policy="no_training", config_dir=Path("relative/agy-home"))


# ── 2. Full cli-login flow: URL, 0700 home, no file open, env, probe rerun ───


def test_cli_login_full_flow_env_no_shell_no_home_open_and_probe_rerun(
    isolated_p1b_env, monkeypatch: pytest.MonkeyPatch, capsys
):
    tmp_path, _, _ = isolated_p1b_env
    cfg_dir = tmp_path / "missing-agy-home"
    assert not cfg_dir.exists()

    bin_dir = tmp_path / "bin"
    log_path = tmp_path / "agy-calls.jsonl"
    _write_fake_agy_cli(bin_dir, log_path=log_path)
    _install_agy_env(tmp_path, monkeypatch, bin_dir=bin_dir, cfg_dir=cfg_dir)

    audit_events: list[tuple[str, dict]] = []

    class _RecordingLog:
        def info(self, event: str, **kwargs):
            audit_events.append((event, kwargs))

        def warning(self, event: str, **kwargs):
            audit_events.append((event, kwargs))

    monkeypatch.setattr(cli_login_mod, "log", _RecordingLog())

    real_open = builtins.open
    real_os_open = os.open

    def guarded_open(file, *args, **kwargs):
        try:
            resolved = Path(os.fspath(file)).resolve()
        except (TypeError, OSError):
            resolved = None
        if resolved is not None and (resolved == cfg_dir.resolve() or cfg_dir.resolve() in resolved.parents):
            raise AssertionError(f"Kotoyose must never open files inside login home: {resolved}")
        return real_open(file, *args, **kwargs)

    def guarded_os_open(path, flags, *args, **kwargs):
        try:
            resolved = Path(os.fspath(path)).resolve()
        except (TypeError, OSError):
            resolved = None
        if resolved is not None and (resolved == cfg_dir.resolve() or cfg_dir.resolve() in resolved.parents):
            raise AssertionError(f"Kotoyose must never os.open files inside login home: {resolved}")
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
            assert client.post("/admin/connections/agy-dedicated/cli-login").status_code == 401
            assert (
                client.post(
                    "/admin/connections/agy-dedicated/cli-login",
                    headers={"Authorization": f"Bearer {AUTH_PASS}"},
                ).status_code
                == 401
            )

            # Unsupported / missing config_dir / unknown rejected
            assert client.post("/admin/connections/agy-host/cli-login", auth=BASIC_AUTH).status_code == 400
            assert client.post("/admin/connections/codex-sub/cli-login", auth=BASIC_AUTH).status_code == 400
            assert client.post("/admin/connections/unknown-conn/cli-login", auth=BASIC_AUTH).status_code == 404

            # Start login
            started = client.post("/admin/connections/agy-dedicated/cli-login", auth=BASIC_AUTH)
            assert started.status_code == 200
            start_body = started.json()
            assert start_body["url"] == FAKE_GOOGLE_URL
            assert start_body["session_id"]
            assert start_body["expires_at"]
            sid = start_body["session_id"]
            exp_dt = datetime.fromisoformat(start_body["expires_at"])
            remaining_s = (exp_dt - datetime.now(timezone.utc)).total_seconds()
            assert 45.0 <= remaining_s <= 61.0

            # Missing login home created with mode 0700
            assert cfg_dir.is_dir()
            assert (cfg_dir.stat().st_mode & 0o777) == 0o700

            # One session per connection
            conflict = client.post("/admin/connections/agy-dedicated/cli-login", auth=BASIC_AUTH)
            assert conflict.status_code == 409

            # Poll waiting status
            polled = client.get(f"/admin/connections/agy-dedicated/cli-login/{sid}", auth=BASIC_AUTH)
            assert polled.status_code == 200
            assert polled.json()["state"] == "waiting_for_code"
            assert polled.json()["url"] == FAKE_GOOGLE_URL

            # Invalid code rejected with 422
            bad_req = client.post(
                f"/admin/connections/agy-dedicated/cli-login/{sid}/code",
                auth=BASIC_AUTH,
                json={"code": "bad code"},
            )
            assert bad_req.status_code == 422

            # Submit good-code -> returns {state: "completed"} and re-runs probe
            completed = client.post(
                f"/admin/connections/agy-dedicated/cli-login/{sid}/code",
                auth=BASIC_AUTH,
                json={"code": FAKE_GOOD_CODE},
            )
            assert completed.status_code == 200
            assert completed.json() == {"state": "completed"}

            # Session buffer wiped
            session_obj = cli_login_mod._SESSIONS[sid]
            assert session_obj._output_buf == ""

            # Probe re-ran and updated state store
            conn_state = asyncio.run(app.state.connection_state_store.get("agy-dedicated"))
            assert conn_state is not None
            assert conn_state["auth"] == "ok"
            assert conn_state["quota_source"] == "agy_cli_usage"
            assert len(conn_state["quota"]) == 5

    # Verify PTY call + probe call env
    recorded = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(recorded) == 2  # 1) PTY login, 2) probe_connection re-run
    pty_call = recorded[0]
    assert pty_call["argv"] == ["-p", "/usage", "--output-format", "json"]
    assert pty_call["isatty"] is True
    assert pty_call["env"]["HOME"] == str(cfg_dir)
    assert pty_call["env"]["BROWSER"] == "true"
    for key in pty_call["env"]:
        assert not key.startswith("ANTHROPIC_")
        assert not key.startswith("CLAUDE_CODE_")
    assert "UNRELATED_SECRET" not in pty_call["env"]

    probe_call = recorded[1]
    assert probe_call["argv"] == ["-p", "/usage", "--output-format", "json"]
    assert probe_call["env"]["HOME"] == str(cfg_dir)
    assert "UNRELATED_SECRET" not in probe_call["env"]

    # Audit events logged ONLY cli_login.started and cli_login.completed
    assert audit_events == [
        ("cli_login.started", {"connection": "agy-dedicated", "session_id": sid}),
        ("cli_login.completed", {"connection": "agy-dedicated", "session_id": sid}),
    ]
    serialized_logs = json.dumps(audit_events)
    assert "SECRET_AGY_STATE" not in serialized_logs
    assert "SECRET_AGY_CHALLENGE" not in serialized_logs
    assert FAKE_GOOD_CODE not in serialized_logs

    captured_out = capsys.readouterr().out
    assert FAKE_GOOD_CODE not in captured_out
    assert "SECRET_AGY_STATE" not in captured_out
    assert "SECRET_AGY_CHALLENGE" not in captured_out


# ── 3. Bad code, cancel, CLI timeout -> expired, TTL expiry, shutdown ────────


def test_cli_login_bad_code_cancel_cli_timeout_and_non_success(
    isolated_p1b_env, monkeypatch: pytest.MonkeyPatch
):
    tmp_path, _, _ = isolated_p1b_env
    cfg_dir = tmp_path / "agy-home"
    cfg_dir.mkdir(parents=True, mode=0o700)
    bin_dir = tmp_path / "bin"
    pid_path = tmp_path / "cli.pid"
    _write_fake_agy_cli(bin_dir, pid_path=pid_path)
    _install_agy_env(tmp_path, monkeypatch, bin_dir=bin_dir, cfg_dir=cfg_dir)

    audit_events: list[tuple[str, dict]] = []

    class _RecordingLog:
        def info(self, event: str, **kwargs):
            audit_events.append((event, kwargs))

        def warning(self, event: str, **kwargs):
            audit_events.append((event, kwargs))

    monkeypatch.setattr(cli_login_mod, "log", _RecordingLog())

    with (
        patch("kotoyose.app.start_execution_service", new=AsyncMock(return_value=None)),
        patch("kotoyose.app.connection_probe_loop", new=AsyncMock()),
    ):
        with TestClient(app) as client:
            # 1) Bad code -> failed
            s1 = client.post("/admin/connections/agy-dedicated/cli-login", auth=BASIC_AUTH).json()
            sid1 = s1["session_id"]
            bad_resp = client.post(
                f"/admin/connections/agy-dedicated/cli-login/{sid1}/code",
                auth=BASIC_AUTH,
                json={"code": "wrong-code"},
            )
            assert bad_resp.status_code == 200
            bad_body = bad_resp.json()
            assert bad_body["state"] == "failed"
            assert "invalid authorization code" in bad_body["error"].lower()
            assert "wrong-code" not in json.dumps(bad_body)

            # 2) Cancel kills process
            s2 = client.post("/admin/connections/agy-dedicated/cli-login", auth=BASIC_AUTH).json()
            sid2 = s2["session_id"]
            pid2 = int(pid_path.read_text(encoding="utf-8"))
            del_resp = client.delete(f"/admin/connections/agy-dedicated/cli-login/{sid2}", auth=BASIC_AUTH)
            assert del_resp.status_code == 200
            assert del_resp.json()["state"] == "cancelled"
            with pytest.raises(ProcessLookupError):
                os.kill(pid2, 0)

            # 3) CLI-side timeout (fake agy exits after 0.15s with no input) -> expired
            _write_fake_agy_cli(bin_dir, pid_path=pid_path, cli_idle_timeout=0.15)
            s3 = client.post("/admin/connections/agy-dedicated/cli-login", auth=BASIC_AUTH).json()
            sid3 = s3["session_id"]
            pid3 = int(pid_path.read_text(encoding="utf-8"))
            import time

            time.sleep(0.35)
            polled3 = client.get(f"/admin/connections/agy-dedicated/cli-login/{sid3}", auth=BASIC_AUTH)
            assert polled3.status_code == 200
            assert polled3.json()["state"] == "expired"
            with pytest.raises(ProcessLookupError):
                os.kill(pid3, 0)

            # Can start again immediately after expiry
            _write_fake_agy_cli(bin_dir, pid_path=pid_path, non_success_json=True)
            s4 = client.post("/admin/connections/agy-dedicated/cli-login", auth=BASIC_AUTH)
            assert s4.status_code == 200
            sid4 = s4.json()["session_id"]
            # Exit 0 but JSON status != "SUCCESS" -> failed
            res4 = client.post(
                f"/admin/connections/agy-dedicated/cli-login/{sid4}/code",
                auth=BASIC_AUTH,
                json={"code": FAKE_GOOD_CODE},
            )
            assert res4.status_code == 200
            assert res4.json()["state"] == "failed"

    assert ("cli_login.failed", {"connection": "agy-dedicated", "session_id": sid1}) in audit_events
    assert ("cli_login.cancelled", {"connection": "agy-dedicated", "session_id": sid2}) in audit_events
    assert ("cli_login.failed", {"connection": "agy-dedicated", "session_id": sid3}) in audit_events
    assert ("cli_login.failed", {"connection": "agy-dedicated", "session_id": sid4}) in audit_events


def test_cli_login_app_shutdown_kills_active_pty_process(
    isolated_p1b_env, monkeypatch: pytest.MonkeyPatch
):
    tmp_path, _, _ = isolated_p1b_env
    cfg_dir = tmp_path / "agy-home"
    cfg_dir.mkdir(parents=True, mode=0o700)
    bin_dir = tmp_path / "bin"
    pid_path = tmp_path / "shutdown-agy.pid"
    _write_fake_agy_cli(bin_dir, pid_path=pid_path)
    _install_agy_env(tmp_path, monkeypatch, bin_dir=bin_dir, cfg_dir=cfg_dir)

    with (
        patch("kotoyose.app.start_execution_service", new=AsyncMock(return_value=None)),
        patch("kotoyose.app.connection_probe_loop", new=AsyncMock()),
    ):
        with TestClient(app) as client:
            started = client.post("/admin/connections/agy-dedicated/cli-login", auth=BASIC_AUTH)
            assert started.status_code == 200
            pid = int(pid_path.read_text(encoding="utf-8"))

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# ── 4. Probe & model discovery with config_dir ───────────────────────────────


@pytest.mark.asyncio
async def test_agy_probe_and_discovery_use_config_dir_home_and_skip_missing_dir(
    isolated_p1b_env, monkeypatch: pytest.MonkeyPatch
):
    tmp_path, _, state_store = isolated_p1b_env
    bin_dir = tmp_path / "bin"
    log_path = tmp_path / "probe-calls.jsonl"
    cli_bin = _write_fake_agy_cli(bin_dir, log_path=log_path)

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "process-home"))
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "forbidden-anthropic")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "forbidden-claude")
    monkeypatch.setenv("UNRELATED_SECRET", "forbidden-secret")

    # Missing config_dir -> logged_out without running agy and without creating dir
    missing_dir = tmp_path / "never-logged-in-agy"
    missing_conn = Connection(provider="antigravity", kind="subscription", profile="agy", data_policy="no_training", config_dir=missing_dir)
    st_missing = await probe_print_cli_connection(
        "agy-missing",
        missing_conn,
        cli_binary=cli_bin,
        store=state_store,
    )
    assert st_missing["auth"] == "logged_out"
    assert st_missing["quota"] == []
    assert not missing_dir.exists()
    assert not log_path.exists()

    # Discovery on missing config_dir -> bundled fallback without running agy
    cfg_missing = HarnessConfig.model_validate(
        {
            "connections": {
                "agy-missing": {"provider": "antigravity", "kind": "subscription", "profile": "agy", "data_policy": "no_training", "config_dir": str(missing_dir)},
            }
        }
    )
    save_config(cfg_missing)
    disc_missing = await discover_models("antigravity", connection="agy-missing")
    assert disc_missing["source"] == "bundled_catalog"
    assert not missing_dir.exists()
    assert not log_path.exists()

    # Existing logged-in config_dir -> runs with HOME=<config_dir> and allowlisted env
    clear_connection_discovery_cache()
    cfg_dir = tmp_path / "logged-in-agy"
    cfg_dir.mkdir(mode=0o700)
    (cfg_dir / ".agy-fake-logged-in").write_text("ok", encoding="utf-8")

    conn = Connection(provider="antigravity", kind="subscription", profile="agy", data_policy="no_training", config_dir=cfg_dir)
    st_ok = await probe_print_cli_connection(
        "agy-ok",
        conn,
        cli_binary=cli_bin,
        store=state_store,
    )
    assert st_ok["auth"] == "ok"
    assert len(st_ok["quota"]) == 5

    cfg_ok = HarnessConfig.model_validate(
        {
            "connections": {
                "agy-ok": {"provider": "antigravity", "kind": "subscription", "profile": "agy", "data_policy": "no_training", "config_dir": str(cfg_dir)},
            }
        }
    )
    save_config(cfg_ok)
    disc_ok = await discover_models("antigravity", connection="agy-ok")
    assert disc_ok["source"] == "agy_cli_models"
    assert disc_ok["models"] == ["gemini-4-hydrogen-high", "gemini-3.8-flash", "claude-opus-5-5"]

    recorded = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [r["argv"] for r in recorded] == [
        ["-p", "/usage", "--output-format", "json"],
        ["models"],
    ]
    for call in recorded:
        assert call["env"]["HOME"] == str(cfg_dir)
        assert "ANTHROPIC_API_KEY" not in call["env"]
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in call["env"]
        assert "UNRELATED_SECRET" not in call["env"]


# ── 5. Console UI strings, server-side tilde expansion via API, & Node DOM ───


def test_console_ui_and_tilde_expansion_api_and_node_dom(
    isolated_p1b_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake_home = tmp_path / "owner-home"
    monkeypatch.setenv("HOME", str(fake_home))

    with (
        patch("kotoyose.app.start_execution_service", new=AsyncMock(return_value=None)),
        patch("kotoyose.app.connection_probe_loop", new=AsyncMock()),
    ):
        with TestClient(app) as client:
            # Create antigravity subscription with tilde config_dir -> expanded server-side
            created = client.post(
                "/admin/connections/agy-new",
                auth=BASIC_AUTH,
                json={
                    "provider": "antigravity",
                    "profile": "agy",
                    "kind": "subscription", "data_policy": "no_training",
                    "config_dir": "~/.local/share/kotoyose/agy-home",
                },
            )
            assert created.status_code == 200
            conn_json = created.json()["connection"]
            assert conn_json["config_dir"] == str(fake_home / ".local/share/kotoyose/agy-home")

            ui_html = client.get("/admin/ui", auth=BASIC_AUTH).text
            ver_match = re.search(r"/admin/ui/console/([0-9a-f]{12})/", ui_html)
            assert ver_match is not None
            prefix = f"/admin/ui/console/{ver_match.group(1)}"

            providers_js = client.get(f"{prefix}/js/pages/providers.js", auth=BASIC_AUTH)
            assert providers_js.status_code == 200
            text = providers_js.text
            assert "Expires in " in text
            assert "Start again" in text
            assert "~/.local/share/kotoyose/agy-home" in text
            assert ".innerHTML" not in text

            api_js = client.get(f"{prefix}/js/api.js", auth=BASIC_AUTH)
            assert api_js.status_code == 200
            assert "cli-login" in api_js.text

    if shutil.which("node") is None:
        pytest.skip("node not installed")

    console_dir = Path(__file__).resolve().parents[1] / "src/kotoyose/static/console"
    script = f"""
import assert from 'node:assert/strict';

class FakeNode {{
  constructor(tag) {{
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.listeners = {{}};
    this.attributes = {{}};
    this.style = {{}};
    this.className = '';
    this.hidden = false;
    this.open = false;
    this.disabled = false;
    this.required = false;
    this.checked = false;
    this._text = '';
    this._value = '';
    this.classList = {{
      contains: (v) => this.className.split(/\\s+/).includes(v),
      add: (...vs) => {{ this.className = [...new Set(this.className.split(/\\s+/).filter(Boolean).concat(vs))].join(' '); }},
      remove: (...vs) => {{ this.className = this.className.split(/\\s+/).filter((v) => !vs.includes(v)).join(' '); }},
      toggle: (v, force) => {{
        const has = this.classList.contains(v);
        const on = force === undefined ? !has : Boolean(force);
        if (on) this.classList.add(v);
        else this.classList.remove(v);
        return on;
      }},
    }};
  }}
  get value() {{ return this._value; }}
  set value(v) {{ this._value = String(v ?? ''); }}
  get isConnected() {{
    let cur = this;
    while (cur) {{
      if (cur === doc.body) return true;
      cur = cur.parentNode;
    }}
    return false;
  }}
  append(...items) {{
    for (const item of items) {{
      if (item === null || item === undefined || item === false) continue;
      if (typeof item === 'string' || typeof item === 'number') {{
        const tn = new FakeNode('#text');
        tn._text = String(item);
        tn.parentNode = this;
        this.children.push(tn);
      }} else {{
        if (item.parentNode) item.parentNode.removeChild(item);
        item.parentNode = this;
        this.children.push(item);
      }}
    }}
  }}
  removeChild(child) {{
    const idx = this.children.indexOf(child);
    if (idx >= 0) this.children.splice(idx, 1);
    child.parentNode = null;
  }}
  remove() {{
    if (this.parentNode) this.parentNode.removeChild(this);
  }}
  replaceChildren(...items) {{
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this.append(...items);
  }}
  set textContent(v) {{
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this._text = String(v ?? '');
  }}
  get textContent() {{
    return this._text + this.children.map((c) => c.textContent).join('');
  }}
  setAttribute(k, v) {{
    this.attributes[k] = String(v);
    if (k === 'open') this.open = true;
    if (k === 'hidden') this.hidden = true;
  }}
  getAttribute(k) {{ return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null; }}
  addEventListener(name, fn) {{ (this.listeners[name] ||= []).push(fn); }}
  removeEventListener(name, fn) {{
    if (!this.listeners[name]) return;
    this.listeners[name] = this.listeners[name].filter((f) => f !== fn);
  }}
  dispatch(name, ev = {{}}) {{
    const event = {{ target: this, preventDefault() {{}}, stopPropagation() {{}}, ...ev }};
    for (const fn of [...(this.listeners[name] || [])]) fn(event);
  }}
  closest() {{ return null; }}
  focus() {{ doc.activeElement = this; }}
  querySelectorAll() {{ return []; }}
}}

const doc = {{
  body: new FakeNode('body'),
  documentElement: new FakeNode('html'),
  activeElement: null,
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (_ns, tag) => new FakeNode(tag),
  createTextNode: (t) => {{ const n = new FakeNode('#text'); n._text = String(t); return n; }},
  getElementById: () => null,
  addEventListener: () => {{}},
  removeEventListener: () => {{}},
}};
const mainEl = new FakeNode('main');
doc.body.append(mainEl);

globalThis.document = doc;
globalThis.window = {{ matchMedia: () => ({{ matches: true }}), confirm: () => true, localStorage: {{ getItem: () => null, setItem: () => {{}} }} }};
globalThis.Node = FakeNode;
globalThis.location = {{ hash: '#/providers/connection/new?family=google&provider=antigravity', pathname: '/admin/ui', search: '', origin: 'http://localhost' }};
globalThis.requestAnimationFrame = (cb) => cb();

let pollCount = 0;
let startCount = 0;
let loginFlow = 'paste_code';
let copied = null;
Object.defineProperty(globalThis, 'navigator', {{
  configurable: true,
  value: {{ clipboard: {{ writeText: async (value) => {{ copied = value; }} }} }},
}});
const apiResponses = {{
  '/admin/config': {{ catalog: {{ drivers: {{}} }}, connections: {{}}, routes: {{}}, route_sanctions: {{}} }},
  '/admin/machine': {{ enabled: false, drivers: {{}} }},
  '/admin/connections': {{
    connections: [
      {{
        id: 'agy-google',
        provider: 'antigravity',
        kind: 'subscription',
        data_policy: 'no_training',
        config_dir: '/srv/operator/.local/share/kotoyose/agy-home',
        routes: [],
        state: {{ auth: 'logged_out', quota: [] }},
      }},
    ],
  }},
  '/admin/secrets': {{ secrets: [] }},
}};

globalThis.fetch = async (url, init = {{}}) => {{
  const key = new URL(String(url), 'http://localhost').pathname;
  if (key === '/admin/connections/agy-google/cli-login' && init.method === 'POST') {{
    startCount += 1;
    return {{
      ok: true,
      status: 200,
      headers: {{ get: () => 'application/json' }},
      text: async () => JSON.stringify({{
        session_id: 'sid-' + startCount,
        flow: loginFlow,
        state: loginFlow === 'paste_code' ? 'waiting_for_code' : 'waiting_for_browser',
        ...(loginFlow === 'device_code' ? {{ user_code: 'DEMO-123' }} : {{}}),
        url: {FAKE_GOOGLE_URL!r},
        expires_at: new Date(Date.now() + 60000).toISOString(),
      }}),
    }};
  }}
  if (key.startsWith('/admin/connections/agy-google/cli-login/')) {{
    pollCount += 1;
    return {{
      ok: true,
      status: 200,
      headers: {{ get: () => 'application/json' }},
      text: async () => JSON.stringify({{
        session_id: 'sid-1',
        connection_id: 'agy-google',
        state: 'expired',
        url: null,
        expires_at: new Date(Date.now() - 1000).toISOString(),
        error: 'Login session expired',
      }}),
    }};
  }}
  return {{
    ok: true,
    status: 200,
    headers: {{ get: () => 'application/json' }},
    text: async () => JSON.stringify(apiResponses[key] || {{}}),
  }};
}};

function findAll(root, pred, out = []) {{
  if (pred(root)) out.push(root);
  for (const c of root.children || []) findAll(c, pred, out);
  return out;
}}

const {{ renderProviders, connectionBody }} = await import({repr((console_dir / "js/pages/providers.js").as_uri())});
const {{ destroyPeek }} = await import({repr((console_dir / "js/peek.js").as_uri())});

// 1. New connection form for antigravity subscription prefills ~/.local/share/kotoyose/agy-home
await renderProviders(mainEl, {{
  config: apiResponses['/admin/config'],
  rest: ['connection', 'new'],
  query: new URLSearchParams('family=google&provider=antigravity'),
  refresh: async () => {{}},
}});
const configDirInput = findAll(doc.body, (n) => n.getAttribute && n.getAttribute('aria-label') === 'Config dir')[0];
assert.ok(configDirInput);
assert.equal(configDirInput.value, '~/.local/share/kotoyose/agy-home');
assert.deepEqual(
  connectionBody({{ provider: 'antigravity', kind: 'subscription', configDir: configDirInput.value, home: '', showQuota: true, dataPolicy: 'no_training' }}),
  {{ provider: 'antigravity', kind: 'subscription', profile: 'agy', config_dir: '~/.local/share/kotoyose/agy-home', show_quota: true, data_policy: 'no_training' }},
);

// 2. Peek for agy-google shows Log in, 60s countdown, and Start again after expiry
destroyPeek();
mainEl.textContent = '';
doc.body.children = [mainEl];
await renderProviders(mainEl, {{
  config: apiResponses['/admin/config'],
  rest: ['connection', 'agy-google'],
  query: new URLSearchParams(''),
  refresh: async () => {{}},
}});
const loginBtns = findAll(doc.body, (n) => n.tagName === 'BUTTON' && n.textContent === 'Log in');
// The list rows no longer carry actions (2026-11-19 accordion); the peek owns Log in.
assert.ok(loginBtns.length >= 1);
const peekLoginBtn = loginBtns.find((b) => b.classList.contains('btn-primary'));
assert.ok(peekLoginBtn);
peekLoginBtn.dispatch('click');
await new Promise((r) => setTimeout(r, 20));
assert.equal(startCount, 1);

const signInLinks = findAll(doc.body, (n) => n.tagName === 'A' && n.textContent === 'Open sign-in page');
assert.equal(signInLinks.length, 1);
assert.equal(signInLinks[0].getAttribute('href'), {FAKE_GOOGLE_URL!r});

const timerNodes = findAll(doc.body, (n) => n.getAttribute && n.getAttribute('role') === 'timer');
assert.equal(timerNodes.length, 1);
assert.match(timerNodes[0].textContent, /^Expires in (?:59|60)s$/);

// Wait for first poll (1.5s) to see expired state + Start again button
await new Promise((r) => setTimeout(r, 1650));
assert.ok(pollCount >= 1);
const startAgainBtns = findAll(doc.body, (n) => n.tagName === 'BUTTON' && n.textContent === 'Start again');
assert.equal(startAgainBtns.length, 1);
startAgainBtns[0].dispatch('click');
await new Promise((r) => setTimeout(r, 20));
assert.equal(startCount, 2);

// Clean up timers by cancelling
const cancelBtns = findAll(doc.body, (n) => n.tagName === 'BUTTON' && n.textContent === 'Cancel');
assert.equal(cancelBtns.length, 1);
cancelBtns[0].dispatch('click');
await new Promise((r) => setTimeout(r, 20));

for (const flow of ['browser_wait', 'device_code']) {{
  loginFlow = flow;
  peekLoginBtn.dispatch('click');
  await new Promise((r) => setTimeout(r, 20));
  assert.match(doc.body.textContent, /Waiting for browser sign-in/);
  assert.equal(findAll(doc.body, (n) => n.getAttribute?.('aria-label') === 'Authorization code').length, 0);
  assert.equal(findAll(doc.body, (n) => n.tagName === 'A' && n.textContent === 'Open sign-in page').length, 1);
  const copyBtns = findAll(doc.body, (n) => n.tagName === 'BUTTON' && n.textContent === 'Copy code');
  if (flow === 'device_code') {{
    assert.match(doc.body.textContent, /DEMO-123/);
    assert.equal(copyBtns.length, 1);
    copyBtns[0].dispatch('click');
    await new Promise((r) => setTimeout(r, 20));
    assert.equal(copied, 'DEMO-123');
  }} else {{
    assert.equal(copyBtns.length, 0);
  }}
  findAll(doc.body, (n) => n.tagName === 'BUTTON' && n.textContent === 'Cancel')[0].dispatch('click');
  await new Promise((r) => setTimeout(r, 20));
}}
"""
    script_path = tmp_path / "verify_p1b_console.mjs"
    script_path.write_text(script, encoding="utf-8")
    res = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=15)
    assert res.returncode == 0, res.stdout + res.stderr
