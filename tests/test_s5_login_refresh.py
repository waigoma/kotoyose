"""S5 tests: Codex keep-alive refresh, concurrent rewrite retry, device-code login, and Claude secret-store auth."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import textwrap
import threading
import time

from fastapi.testclient import TestClient
import pytest

from kotoyose import auth as auth_module, db as db_module
from kotoyose.app import app
from kotoyose.codex_login import shutdown_login_sessions
from kotoyose.codex_subscription import SubscriptionCredential, read_subscription
from kotoyose.config import (
    Connection,
    build_options,
    reload_config,
)
from kotoyose.connection_state_store import ConnectionStateStore, set_connection_state_store
from kotoyose.connections_probe import probe_codex_connection, reset_codex_refresh_history
from kotoyose.execution_agent import resolve_runtime_credential
from kotoyose.execution_resources import ExecutionError
from kotoyose.secret_store import SecretStore, set_secret_store


def _make_fake_jwt(exp: float, sentinel: str = "FAKE_TOK_S5") -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp, "sub": sentinel}).encode()
    ).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(sentinel.encode()).decode().rstrip("=")
    return f"{header}.{payload}.{sig}"


def _write_fake_auth_file(path: Path, exp: float, sentinel: str = "FAKE_TOK_S5") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": _make_fake_jwt(exp, sentinel=sentinel),
                "account_id": "acct_s5_fake_001",
            },
        }),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _write_s5_fake_codex(
    path: Path,
    *,
    log_path: Path,
    auth_file: Path,
    refreshed_exp: float,
    sentinel: str = "FAKE_REFRESH_SENTINEL",
    login_outcome: str = "success",
    login_delay: float = 0.08,
) -> Path:
    """Write a fake `codex app-server` script supporting keep-alive refresh and device-code login."""
    python_exe = sys.executable
    script = textwrap.dedent(
        f"""\
        #!{python_exe}
        import base64, json, os, sys, threading, time
        from pathlib import Path

        LOG_PATH = Path({str(log_path)!r})
        AUTH_FILE = Path({str(auth_file)!r})
        REFRESHED_EXP = {refreshed_exp!r}
        SENTINEL = {sentinel!r}
        LOGIN_OUTCOME = {login_outcome!r}
        LOGIN_DELAY = {login_delay!r}

        if len(sys.argv) >= 2 and sys.argv[1] == "--version":
            print("codex-cli 0.99.0-s5-fake")
            sys.exit(0)

        if len(sys.argv) < 2 or sys.argv[1] != "app-server":
            sys.exit(2)

        def log_evt(entry):
            with LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\\n")

        def write_auth(exp_ts):
            hdr = base64.urlsafe_b64encode(b'{{"alg":"RS256","typ":"JWT"}}').decode().rstrip("=")
            pl = base64.urlsafe_b64encode(json.dumps({{"exp": exp_ts}}).encode()).decode().rstrip("=")
            sig = base64.urlsafe_b64encode(SENTINEL.encode()).decode().rstrip("=")
            AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
            AUTH_FILE.write_text(json.dumps({{
                "auth_mode": "chatgpt",
                "OPENAI_API_KEY": None,
                "tokens": {{"access_token": f"{{hdr}}.{{pl}}.{{sig}}", "account_id": "acct_s5_fake_001"}},
            }}), encoding="utf-8")
            AUTH_FILE.chmod(0o600)

        out_lock = threading.Lock()
        def send(obj):
            with out_lock:
                sys.stdout.write(json.dumps(obj) + "\\n")
                sys.stdout.flush()

        cancelled_ids = set()

        def emit_login_completed(login_id):
            time.sleep(LOGIN_DELAY)
            if login_id in cancelled_ids:
                return
            if LOGIN_OUTCOME == "success":
                write_auth(REFRESHED_EXP)
                send({{
                    "method": "account/login/completed",
                    "params": {{"loginId": login_id, "success": True, "error": None}},
                }})
            elif LOGIN_OUTCOME == "failed":
                send({{
                    "method": "account/login/completed",
                    "params": {{"loginId": login_id, "success": False, "error": "denied_by_user"}},
                }})

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            method = msg.get("method")
            req_id = msg.get("id")
            params = msg.get("params")
            if method:
                log_evt({{"method": method, "params": params}})
            if req_id is None:
                continue

            if method == "initialize":
                resp = {{"userAgent": "fake-codex-s5/0.99.0"}}
            elif method == "account/read":
                if isinstance(params, dict) and params.get("refreshToken") is True:
                    write_auth(REFRESHED_EXP)
                resp = {{
                    "account": {{"type": "chatgpt", "email": "fake@example.invalid", "planType": "pro"}},
                    "requiresOpenaiAuth": False,
                }}
            elif method == "account/rateLimits/read":
                resp = {{
                    "rateLimits": {{
                        "limitId": "codex",
                        "limitName": "Codex",
                        "primary": {{"usedPercent": 15, "windowDurationMins": 300, "resetsAt": 1900000000}},
                        "secondary": {{"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1900500000}},
                        "planType": "pro",
                    }},
                    "rateLimitsByLimitId": None,
                }}
            elif method == "model/list":
                resp = {{"data": [], "nextCursor": None}}
            elif method == "account/login/start":
                login_id = "login-s5-001"
                resp = {{
                    "type": "chatgptDeviceCode",
                    "loginId": login_id,
                    "verificationUrl": "https://auth.example.invalid/device",
                    "userCode": "S5CD-9876",
                }}
                send({{"id": req_id, "result": resp}})
                if LOGIN_OUTCOME != "hang":
                    threading.Thread(target=emit_login_completed, args=(login_id,), daemon=True).start()
                continue
            elif method == "account/login/cancel":
                login_id = params.get("loginId") if isinstance(params, dict) else None
                if login_id:
                    cancelled_ids.add(login_id)
                resp = {{"status": "canceled"}}
            else:
                resp = {{}}
            send({{"id": req_id, "result": resp}})
        """
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def isolated_s5_env(tmp_path, monkeypatch):
    reset_codex_refresh_history()
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(cfg_path))
    monkeypatch.setattr("kotoyose.app.configure_logging", lambda: None)
    monkeypatch.setattr(auth_module, "API_KEY", None)
    monkeypatch.setattr(db_module, "_pool", None)
    store = ConnectionStateStore(None)
    set_connection_state_store(store)
    secret_store = SecretStore(master_key=b"\x02" * 32)
    set_secret_store(secret_store)
    reload_config()
    yield tmp_path, cfg_path, store, secret_store
    asyncio.run(shutdown_login_sessions())
    reset_codex_refresh_history()
    set_secret_store(None)
    set_connection_state_store(None)
    reload_config()


# ── 1. Codex keep-alive scheduling ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_codex_keep_alive_refresh_under_3d_cooldown_12h_and_manual(tmp_path):
    reset_codex_refresh_history()
    t0 = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
    auth_file = tmp_path / "codex" / "auth.json"
    sentinel = "FAKE_KEEPALIVE_TOKEN"
    # Starts < 3 days from t0 (36h remaining -> triggers refresh)
    _write_fake_auth_file(auth_file, t0.timestamp() + 36 * 3600, sentinel=sentinel)
    # After refresh, fake server still bumps exp to t0 + 50h (< 3 days) so cooldown is exercised
    bump_exp = t0.timestamp() + 50 * 3600
    log_path = tmp_path / "rpc.log"
    fake_bin = _write_s5_fake_codex(
        tmp_path / "fake-codex",
        log_path=log_path,
        auth_file=auth_file,
        refreshed_exp=bump_exp,
        sentinel=sentinel,
    )
    conn = Connection(provider="codex", kind="subscription", data_policy="no_training", auth_file=auth_file)
    store = ConnectionStateStore(None)

    try:
        # 1) < 3 days remaining & never refreshed -> sends refreshToken: true and re-reads bumped expiry
        state1 = await probe_codex_connection("codex-main", conn, cli_binary=fake_bin, store=store, now=t0)
        assert state1["auth"] == "ok"  # bumped from 36h (expiring) to 50h (ok, > 48h)
        assert abs(datetime.fromisoformat(state1["expires_at"]).timestamp() - bump_exp) < 2.0
        assert sentinel not in json.dumps(state1)

        # 2) +2h later (< 12h cooldown), still < 3 days -> must NOT refresh again
        t_plus_2h = t0 + timedelta(hours=2)
        await probe_codex_connection("codex-main", conn, cli_binary=fake_bin, store=store, now=t_plus_2h)

        # 3) +13h later (>= 12h cooldown), still < 3 days -> refreshes again
        t_plus_13h = t0 + timedelta(hours=13)
        await probe_codex_connection("codex-main", conn, cli_binary=fake_bin, store=store, now=t_plus_13h)

        # 4) Manual force_refresh within 12h cooldown -> overrides cooldown and refreshes
        await probe_codex_connection(
            "codex-main", conn, cli_binary=fake_bin, store=store, now=t_plus_13h + timedelta(minutes=5), force_refresh=True
        )

        acct_calls = [
            json.loads(line)["params"]["refreshToken"]
            for line in log_path.read_text(encoding="utf-8").splitlines()
            if json.loads(line).get("method") == "account/read"
        ]
        assert acct_calls == [True, False, True, True]
    finally:
        reset_codex_refresh_history()


# ── 2. read_subscription single 100 ms parse retry ──────────────────────────


def test_read_subscription_retries_once_on_concurrent_parse_error(tmp_path):
    auth_file = tmp_path / "auth.json"
    # Start in a truncated mid-write JSON state
    auth_file.write_bytes(b'{"auth_mode": "chat')
    auth_file.chmod(0o600)

    exp = time.time() + 3600
    good_payload = json.dumps({
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "access_token": _make_fake_jwt(exp, sentinel="RETRY_OK_TOKEN"),
            "account_id": "acct_retry_ok",
        },
    }).encode("utf-8")

    def rewrite_after_delay():
        time.sleep(0.03)
        tmp_atomic = tmp_path / "auth.json.tmp"
        tmp_atomic.write_bytes(good_payload)
        tmp_atomic.chmod(0o600)
        os.replace(tmp_atomic, auth_file)

    writer = threading.Thread(target=rewrite_after_delay, daemon=True)
    writer.start()
    cred = read_subscription(auth_file)
    writer.join(timeout=2.0)
    assert isinstance(cred, SubscriptionCredential)
    assert cred.access_token.startswith("eyJ")


def test_read_subscription_fails_after_single_retry_if_still_corrupt(tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_bytes(b'{"auth_mode": "chat')
    auth_file.chmod(0o600)
    t0 = time.monotonic()
    with pytest.raises(ValueError, match="authentication_failed"):
        read_subscription(auth_file)
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.09


# ── 3. Codex device-code login flow & API endpoints ─────────────────────────


def _setup_codex_app_env(isolated_s5_env, tmp_path, *, outcome="success", delay=0.08):
    _, cfg_path, store, _ = isolated_s5_env
    auth_file = tmp_path / "codex-login" / "auth.json"
    log_path = tmp_path / "login-rpc.log"
    sentinel = "FAKE_LOGIN_TOKEN_SECRET"
    fake_bin = _write_s5_fake_codex(
        tmp_path / "fake-codex-login",
        log_path=log_path,
        auth_file=auth_file,
        refreshed_exp=time.time() + 7 * 86400,
        sentinel=sentinel,
        login_outcome=outcome,
        login_delay=delay,
    )
    exe_path = fake_bin
    cfg_path.write_text(
        f"""
connections:
  codex-chatgpt:
    provider: codex
    kind: subscription
    data_policy: no_training
    auth_file: {auth_file}
  claude-oauth:
    provider: claude
    kind: subscription
    data_policy: no_training
    secret: claude-oauth
routes:
  codex-default:
    driver: codex.app-server
    model: gpt-5.4
    connection: codex-chatgpt
  claude-default:
    driver: claude.agent-sdk
    model: claude-sonnet-5
    connection: claude-oauth
""",
        encoding="utf-8",
    )
    return cfg_path, exe_path, auth_file, log_path, sentinel, store


def test_codex_device_code_login_complete_conflict_and_no_token_leak(isolated_s5_env, tmp_path, monkeypatch):
    _, fake_bin, auth_file, log_path, sentinel, _ = _setup_codex_app_env(
        isolated_s5_env, tmp_path, outcome="success", delay=0.12
    )
    monkeypatch.delenv("KOTOYOSE_EXECUTION_CONFIG", raising=False)
    reload_config()

    class _ExecCfg:
        class _Drv:
            def __init__(self, b):
                self.cli_binary = b

        def __init__(self, b):
            self.drivers = {"codex.app-server": self._Drv(b)}

    class _FakeExecSvc:
        def __init__(self, b):
            self.config = _ExecCfg(b)

        async def close(self):
            pass

    with TestClient(app) as http_client:
        app.state.execution_service = _FakeExecSvc(fake_bin)

        started = http_client.post("/admin/connections/codex-chatgpt/login")
        assert started.status_code == 200
        body = started.json()
        assert body["verification_url"] == "https://auth.example.invalid/device"
        assert body["user_code"] == "S5CD-9876"
        assert body["expires_at"]
        session_id = body["session_id"]

        # Only one login at a time per connection
        conflict = http_client.post("/admin/connections/codex-chatgpt/login")
        assert conflict.status_code == 409

        # Claude connections must reject login flow
        claude_reject = http_client.post("/admin/connections/claude-oauth/login")
        assert claude_reject.status_code == 400

        # Poll until completed (auto-runs probe afterward)
        final_status = None
        for _ in range(40):
            polled = http_client.get(f"/admin/connections/codex-chatgpt/login/{session_id}")
            assert polled.status_code == 200
            assert sentinel not in polled.text
            assert "access_token" not in polled.text
            final_status = polled.json()
            if final_status["status"] != "waiting":
                break
            time.sleep(0.05)

        assert final_status is not None
        assert final_status["status"] == "completed"
        assert final_status["state"]["auth"] == "ok"
        assert final_status["state"]["plan"] == "pro"
        assert sentinel not in json.dumps(final_status)

        # Manual refresh endpoint works too and hides tokens
        refreshed = http_client.post("/admin/connections/codex-chatgpt/refresh")
        assert refreshed.status_code == 200
        assert sentinel not in refreshed.text

        rpc_events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        login_req = next(e for e in rpc_events if e.get("method") == "account/login/start")
        assert login_req["params"] == {"type": "chatgptDeviceCode"}
        acct_refresh_calls = [
            e["params"]["refreshToken"]
            for e in rpc_events
            if e.get("method") == "account/read"
        ]
        assert True in acct_refresh_calls


def test_codex_device_code_login_cancel_failed_and_expired(isolated_s5_env, tmp_path, monkeypatch):
    _, fake_bin, _, log_path, _, _ = _setup_codex_app_env(
        isolated_s5_env, tmp_path, outcome="hang"
    )
    monkeypatch.delenv("KOTOYOSE_EXECUTION_CONFIG", raising=False)
    reload_config()

    class _FakeExecSvc:
        class _Cfg:
            class _Drv:
                def __init__(self, b):
                    self.cli_binary = b

            def __init__(self, b):
                self.drivers = {"codex.app-server": self._Drv(b)}

        def __init__(self, b):
            self.config = self._Cfg(b)

        async def close(self):
            pass

    with TestClient(app) as http_client:
        app.state.execution_service = _FakeExecSvc(fake_bin)

        # 1) Start then DELETE cancels via account/login/cancel
        res = http_client.post("/admin/connections/codex-chatgpt/login")
        assert res.status_code == 200
        sid = res.json()["session_id"]

        deleted = http_client.delete(f"/admin/connections/codex-chatgpt/login/{sid}")
        assert deleted.status_code == 200
        assert deleted.json()["status"] == "failed"

        rpc_methods = [json.loads(l).get("method") for l in log_path.read_text().splitlines()]
        assert "account/login/cancel" in rpc_methods

    # 2) Expired flow via short TTL
    import kotoyose.codex_login as cl_mod

    monkeypatch.setattr(cl_mod, "DEFAULT_LOGIN_TTL_SECONDS", 0.05)
    with TestClient(app) as http_client:
        app.state.execution_service = _FakeExecSvc(fake_bin)
        res2 = http_client.post("/admin/connections/codex-chatgpt/login")
        assert res2.status_code == 200
        sid2 = res2.json()["session_id"]
        time.sleep(0.12)
        polled2 = http_client.get(f"/admin/connections/codex-chatgpt/login/{sid2}")
        assert polled2.status_code == 200
        assert polled2.json()["status"] == "expired"

    # 3) Server returns failed completion notification
    monkeypatch.setattr(cl_mod, "DEFAULT_LOGIN_TTL_SECONDS", 60.0)
    auth_file_fail = tmp_path / "codex-fail" / "auth.json"
    fail_log = tmp_path / "fail-rpc.log"
    fail_bin = _write_s5_fake_codex(
        tmp_path / "fake-codex-fail",
        log_path=fail_log,
        auth_file=auth_file_fail,
        refreshed_exp=time.time() + 3600,
        login_outcome="failed",
        login_delay=0.05,
    )
    with TestClient(app) as http_client:
        app.state.execution_service = _FakeExecSvc(fail_bin)
        res3 = http_client.post("/admin/connections/codex-chatgpt/login")
        assert res3.status_code == 200
        sid3 = res3.json()["session_id"]
        for _ in range(30):
            polled3 = http_client.get(f"/admin/connections/codex-chatgpt/login/{sid3}").json()
            if polled3["status"] != "waiting":
                break
            time.sleep(0.03)
        assert polled3["status"] == "failed"
        assert polled3["error"] == "denied_by_user"


# ── 4. Claude setup-token end-to-end (secret store, env override, missing -> authentication_failed) ─


@pytest.mark.asyncio
async def test_claude_secret_store_execution_and_missing_secret_error(isolated_s5_env, tmp_path, monkeypatch):
    _, cfg_path, state_store, secret_store = isolated_s5_env
    for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    cfg_path.write_text(
        f"""
connections:
  claude-oauth:
    provider: claude
    kind: subscription
    data_policy: no_training
    secret: claude-oauth
routes:
  claude-default:
    driver: claude.agent-sdk
    model: claude-sonnet-5
    connection: claude-oauth
models:
  kotoyose-chat:
    route: claude-default
    bare: false
    cwd: {tmp_path}
""",
        encoding="utf-8",
    )
    await secret_store.set("claude-oauth", "synthetic-config-validation-token")
    reload_config()
    await secret_store.delete("claude-oauth")

    snapshot = {
        "alias": "kotoyose-chat",
        "credential_mode": "runtime_managed",
        "credential_env": "CLAUDE_CODE_OAUTH_TOKEN",
        "credential_secret": None,
    }

    # Missing secret -> ExecutionError authentication_failed mentioning claude setup-token & secret name
    with pytest.raises(ExecutionError) as exc:
        await resolve_runtime_credential(snapshot)
    assert exc.value.code == "authentication_failed"
    assert "claude setup-token" in str(exc.value)
    assert "claude-oauth" in str(exc.value)

    # Env override wins even if secret is missing in store
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-override-win-token")
    cred_env = await resolve_runtime_credential(snapshot)
    assert cred_env == "env-override-win-token"

    # Saved secret in store works when env var unset
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    await secret_store.set("claude-oauth", "stored-setup-token-xyz")
    cred_secret = await resolve_runtime_credential(snapshot)
    assert cred_secret == "stored-setup-token-xyz"

    # build_options injects it as CLAUDE_CODE_OAUTH_TOKEN
    opts = build_options("kotoyose-chat")
    assert opts.env["CLAUDE_CODE_OAUTH_TOKEN"] == "stored-setup-token-xyz"

    # Admin connections endpoint NEVER leaks token value
    with TestClient(app) as http_client:
        set_secret_store(secret_store)
        resp = http_client.get("/admin/connections")
        assert resp.status_code == 200
        assert "stored-setup-token-xyz" not in resp.text


def test_chat_missing_claude_secret_returns_401_and_records_connection_error(isolated_s5_env, tmp_path, monkeypatch, client_api_key):
    from tests.fixtures.api_keys import TEST_API_HEADERS
    _, cfg_path, state_store, _ = isolated_s5_env
    for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    cfg_path.write_text(
        f"""
connections:
  claude-oauth:
    provider: claude
    kind: subscription
    data_policy: no_training
    secret: claude-oauth
routes:
  claude-default:
    driver: claude.agent-sdk
    model: claude-sonnet-5
    connection: claude-oauth
models:
  kotoyose-chat:
    route: claude-default
    bare: false
    cwd: {tmp_path}
""",
        encoding="utf-8",
    )
    reload_config()

    with TestClient(app) as http_client:
        res = http_client.post(
            "/v1/chat/completions", headers=TEST_API_HEADERS,
            json={
                "model": "kotoyose-chat",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert res.status_code == 401
        err = res.json()["error"]
        assert err["type"] == "authentication_failed"
        assert "claude setup-token" in err["message"]
        assert "claude-oauth" in err["message"]

        conns = http_client.get("/admin/connections").json()["connections"]
        claude_conn = next(c for c in conns if c["id"] == "claude-oauth")
        assert claude_conn["state"]["auth"] == "expired"
        assert "claude setup-token" in (claude_conn["state"]["last_error"] or "")
