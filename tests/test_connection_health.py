"""Tests for S4b connection health: probes, expiry transitions, runtime evidence, APIs, and Cursor route connection."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import textwrap
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import pytest

from kotoyose import auth as auth_module
from kotoyose import db as db_module
from kotoyose.app import app
from kotoyose.chat_completion import CompletionPorts, TurnContext
from kotoyose.chat_streaming import StreamContext, StreamingPorts, stream_chat_response
from kotoyose.config import Connection, HarnessConfig, reload_config, save_config
from kotoyose.connection_state_store import ConnectionStateStore, set_connection_state_store
from kotoyose.connections_probe import (
    classify_expiry_auth,
    connection_probe_loop,
    probe_all_connections,
    probe_codex_connection,
    record_claude_rate_limit,
)
from kotoyose.execution_agent import _ProgressWriter
from kotoyose.execution_limits import ProviderUsageLimit
from kotoyose.execution_service import ExecutionService
from kotoyose.model_pools import ModelPools
from kotoyose.runtime.base import RuntimeEvent, RuntimeResult, RuntimeTurnError
from kotoyose.runtime.claude import ClaudeRuntimeSession
from kotoyose.runtime.cursor import CursorRuntimeSession, build_cursor_session
from kotoyose.secret_store import SecretStore, set_secret_store


def _write_fake_auth_file(path: Path, exp: float, *, token_sentinel: str = "fake-token-sentinel-secret") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp, "sentinel": token_sentinel}).encode()
    ).decode().rstrip("=")
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode().rstrip("=")
    jwt_with_sentinel = f"{header}.{payload}.{token_sentinel}"
    body = {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": jwt_with_sentinel,
            "account_id": f"acct_{token_sentinel}",
        },
    }
    path.write_text(json.dumps(body), encoding="utf-8")
    path.chmod(0o600)


def _write_fake_codex_script(
    path: Path,
    *,
    mode: str = "ok",
    log_path: Path | None = None,
    sleep_seconds: float = 0.0,
) -> Path:
    script = textwrap.dedent(
        f"""\
        #!{sys.executable}
        import json
        import os
        import sys
        import time

        LOG_PATH = {str(log_path)!r}
        MODE = {mode!r}
        SLEEP_SECONDS = {sleep_seconds!r}

        def record(entry):
            if LOG_PATH:
                with open(LOG_PATH, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry) + "\\n")

        if "--version" in sys.argv[1:]:
            print("codex-cli 0.99.0-fake")
            sys.exit(0)

        record({{"event": "start", "argv": sys.argv[1:], "env_keys": sorted(os.environ.keys()), "codex_home": os.environ.get("CODEX_HOME")}})

        if SLEEP_SECONDS > 0:
            time.sleep(SLEEP_SECONDS)

        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            msg = json.loads(raw)
            method = msg.get("method")
            req_id = msg.get("id")
            params = msg.get("params")
            record({{"method": method, "params": params}})

            if req_id is None:
                continue

            if method == "initialize":
                resp = {{"userAgent": "fake-codex/0.99.0"}}
            elif method == "account/read":
                if isinstance(params, dict) and params.get("refreshToken") is True:
                    sys.stdout.write(json.dumps({{"id": req_id, "error": {{"code": -32602, "message": "refreshToken forbidden"}}}}) + "\\n")
                    sys.stdout.flush()
                    continue
                if MODE == "logged_out":
                    resp = {{"account": None, "requiresOpenaiAuth": True}}
                else:
                    resp = {{
                        "account": {{
                            "type": "chatgpt",
                            "email": "fake-user@example.invalid",
                            "planType": "pro",
                        }},
                        "requiresOpenaiAuth": False,
                    }}
            elif method == "account/rateLimits/read":
                resp = {{
                    "rateLimits": {{
                        "limitId": "codex",
                        "limitName": "Codex",
                        "primary": {{
                            "usedPercent": 35,
                            "windowDurationMins": 300,
                            "resetsAt": 1900000000,
                        }},
                        "secondary": {{
                            "usedPercent": 80,
                            "windowDurationMins": 10080,
                            "resetsAt": 1900500000,
                        }},
                        "planType": "pro",
                    }},
                    "rateLimitsByLimitId": None,
                }}
            elif method == "model/list":
                resp = {{
                    "data": [
                        {{
                            "id": "gpt-5.4",
                            "model": "gpt-5.4",
                            "displayName": "GPT-5.4",
                            "description": "Fake model",
                            "hidden": False,
                            "isDefault": True,
                            "defaultReasoningEffort": "medium",
                            "supportedReasoningEfforts": [
                                {{"reasoningEffort": "medium", "description": "Medium"}}
                            ],
                        }}
                    ],
                    "nextCursor": None,
                }}
            else:
                resp = {{}}

            sys.stdout.write(json.dumps({{"id": req_id, "result": resp}}) + "\\n")
            sys.stdout.flush()
        """
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def isolated_connection_env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(cfg_path))
    # app の lifespan は structlog を cache_logger_on_first_use=True で設定する。ここで走らせると
    # 共有の logger が本番の処理系で cache され、後の試験の capture_logs が何も拾えなくなる。
    monkeypatch.setattr("kotoyose.app.configure_logging", lambda: None)
    monkeypatch.setattr(auth_module, "API_KEY", None)
    monkeypatch.setattr(db_module, "_pool", None)
    store = ConnectionStateStore(None)
    set_connection_state_store(store)
    reload_config()
    yield tmp_path, store
    set_connection_state_store(None)
    reload_config()


@pytest.mark.asyncio
async def test_codex_probe_reads_account_rate_limits_models_and_hides_tokens(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-work"
    auth_file = codex_home / "auth.json"
    sentinel = "FAKE_SECRET_DO_NOT_LEAK_123"
    future_exp = time.time() + 7 * 86400
    _write_fake_auth_file(auth_file, future_exp, token_sentinel=sentinel)

    log_path = tmp_path / "rpc.log"
    fake_bin = _write_fake_codex_script(tmp_path / "fake-codex", mode="ok", log_path=log_path)
    # Plant a secret env var in parent env to verify minimal env stripping
    monkeypatch.setenv("LEAKED_PARENT_SECRET", "must-not-appear-in-probe")

    store = ConnectionStateStore(None)
    conn = Connection(provider="codex", kind="subscription", data_policy="no_training", auth_file=auth_file)
    state = await probe_codex_connection(
        "codex-work",
        conn,
        cli_binary=fake_bin,
        store=store,
        timeout_seconds=10.0,
    )

    assert state["connection"] == "codex-work"
    assert state["auth"] == "ok"
    assert state["plan"] == "pro"
    assert state["quota_source"] == "official_rpc"
    assert state["cli_version"] == "codex-cli 0.99.0-fake"
    assert state["expires_at"] is not None
    assert state["last_error"] is None
    assert [q["window"] for q in state["quota"]] == ["5h", "7d"]
    assert state["quota"][0]["used_percent"] == 35.0
    assert state["quota"][1]["used_percent"] == 80.0
    assert state["quota"][0]["source"] == "official_rpc"

    # Never leak token sentinel anywhere in serialized state
    dumped = json.dumps(state)
    assert sentinel not in dumped
    assert "access_token" not in dumped

    # Verify exact RPC calls and minimal isolated env
    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    start_evt = next(e for e in events if e.get("event") == "start")
    assert start_evt["codex_home"] == str(codex_home)
    assert "LEAKED_PARENT_SECRET" not in start_evt["env_keys"]

    methods = [e.get("method") for e in events if "method" in e]
    assert "account/read" in methods
    assert "account/rateLimits/read" in methods
    assert "model/list" in methods
    acct_call = next(e for e in events if e.get("method") == "account/read")
    assert acct_call["params"] == {"refreshToken": False}


def test_expiry_state_transitions():
    now = datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc)
    assert classify_expiry_auth(now + timedelta(days=5), logged_in=True, now=now) == "ok"
    assert classify_expiry_auth(now + timedelta(hours=24), logged_in=True, now=now) == "expiring"
    assert classify_expiry_auth(now - timedelta(seconds=10), logged_in=True, now=now) == "expired"
    assert classify_expiry_auth(None, logged_in=False, now=now) == "logged_out"
    assert classify_expiry_auth(now - timedelta(seconds=1), logged_in=False, now=now) == "expired"


@pytest.mark.asyncio
async def test_codex_probe_expiring_expired_and_logged_out(tmp_path):
    store = ConnectionStateStore(None)
    fake_ok_bin = _write_fake_codex_script(tmp_path / "fake-ok", mode="ok")
    fake_out_bin = _write_fake_codex_script(tmp_path / "fake-out", mode="logged_out")

    # Expiring (< 48h)
    expiring_auth = tmp_path / "expiring" / "auth.json"
    _write_fake_auth_file(expiring_auth, time.time() + 12 * 3600)
    st_expiring = await probe_codex_connection(
        "c-expiring",
        Connection(provider="codex", kind="subscription", data_policy="no_training", auth_file=expiring_auth),
        cli_binary=fake_ok_bin,
        store=store,
    )
    assert st_expiring["auth"] == "expiring"

    # Expired (past)
    expired_auth = tmp_path / "expired" / "auth.json"
    _write_fake_auth_file(expired_auth, time.time() - 600)
    st_expired = await probe_codex_connection(
        "c-expired",
        Connection(provider="codex", kind="subscription", data_policy="no_training", auth_file=expired_auth),
        cli_binary=fake_ok_bin,
        store=store,
    )
    assert st_expired["auth"] == "expired"

    # Logged out (official account/read returns null account)
    logged_out_dir = tmp_path / "logged_out"
    logged_out_dir.mkdir()
    st_out = await probe_codex_connection(
        "c-out",
        Connection(provider="codex", kind="subscription", data_policy="no_training", home=str(logged_out_dir)),
        cli_binary=fake_out_bin,
        store=store,
    )
    assert st_out["auth"] == "logged_out"


@pytest.mark.asyncio
async def test_codex_probe_timeout_records_error_cleanly(tmp_path):
    store = ConnectionStateStore(None)
    auth_file = tmp_path / "hang" / "auth.json"
    _write_fake_auth_file(auth_file, time.time() + 5 * 86400)
    hang_bin = _write_fake_codex_script(tmp_path / "fake-hang", mode="ok", sleep_seconds=5.0)

    state = await probe_codex_connection(
        "c-hang",
        Connection(provider="codex", kind="subscription", data_policy="no_training", auth_file=auth_file),
        cli_binary=hang_bin,
        store=store,
        timeout_seconds=0.3,
    )
    assert state["last_error"] is not None
    assert "timed out" in state["last_error"]


@pytest.mark.asyncio
async def test_claude_never_actively_probed_and_captures_rate_limit_events(isolated_connection_env):
    tmp_path, store = isolated_connection_env
    cfg = HarnessConfig.model_validate(
        {
            "connections": {
                "claude-sub": {"provider": "claude", "kind": "subscription", "data_policy": "no_training", "secret": "claude-oauth"},
            },
            "routes": {
                "claude-r": {"driver": "claude.agent-sdk", "model": "claude-sonnet-4-6", "connection": "claude-sub"},
            },
            "models": {
                "claude-alias": {"route": "claude-r"},
            },
        }
    )
    save_config(cfg)

    fake_claude_bin = tmp_path / "fake-claude"
    fake_claude_bin.write_text("#!/bin/sh\necho '2.1.50 (Claude Code)'\n", encoding="utf-8")
    fake_claude_bin.chmod(0o755)
    fake_exec_service = SimpleNamespace(
        config=SimpleNamespace(drivers={"claude.agent-sdk": SimpleNamespace(cli_binary=fake_claude_bin)})
    )

    with patch("kotoyose.connections_probe.probe_codex_connection", new=AsyncMock()) as codex_probe_mock:
        results = await probe_all_connections(cfg=cfg, execution_service=fake_exec_service, store=store)
    codex_probe_mock.assert_not_awaited()
    assert results["claude-sub"]["cli_version"] == "2.1.50 (Claude Code)"
    assert results["claude-sub"]["auth"] == "unknown"

    # Simulate Claude SDK RateLimitEvent arriving during ClaudeRuntimeSession stream
    @dataclass
    class _FakeRateInfo:
        status: str
        rate_limit_type: str | None = None
        resets_at: int | None = None
        overage_resets_at: int | None = None
        utilization: float | None = None
        overage_status: str | None = None

    @dataclass
    class _FakeRateLimitEvent:
        rate_limit_info: _FakeRateInfo

    class _FakeSDKClient:
        async def query(self, prompt):
            return None

        async def disconnect(self):
            return None

        async def receive_response(self):
            from claude_agent_sdk import ResultMessage
            yield _FakeRateLimitEvent(
                rate_limit_info=_FakeRateInfo(
                    status="allowed_warning",
                    rate_limit_type="five_hour",
                    resets_at=1900000000,
                    utilization=0.62,
                )
            )
            yield ResultMessage(
                subtype="success",
                duration_ms=10,
                duration_api_ms=5,
                is_error=False,
                num_turns=1,
                session_id="sess-1",
                usage={"input_tokens": 10, "output_tokens": 5},
                result="done",
            )

    with patch("kotoyose.runtime.claude.OBSERVATION_MESSAGES", (_FakeRateLimitEvent,)):
        with patch(
            "kotoyose.runtime.claude.claude_observation",
            side_effect=lambda msg, session_id=None: RuntimeEvent(
                kind="rate_limit",
                summary="Claude rate limit",
                payload={
                    "source": "claude_sdk",
                    "status": msg.rate_limit_info.status,
                    "rate_limit_type": msg.rate_limit_info.rate_limit_type,
                    "resets_at": msg.rate_limit_info.resets_at,
                    "utilization": msg.rate_limit_info.utilization,
                },
            ),
        ):
            session = ClaudeRuntimeSession(client=_FakeSDKClient(), connection_id="claude-sub")
            res = await session.ask("hi")
            assert res.provider == "claude"

    stored = await store.get("claude-sub")
    assert stored is not None
    assert stored["auth"] == "ok"
    assert stored["quota_source"] == "official_stream"
    assert stored["quota"][0]["window"] == "5h"
    assert stored["quota"][0]["used_percent"] == 62.0
    assert stored["quota"][0]["source"] == "official_stream"


@pytest.mark.asyncio
async def test_auth_failure_and_usage_limit_update_connection_state(isolated_connection_env):
    _, store = isolated_connection_env
    cfg = HarnessConfig.model_validate(
        {
            "connections": {
                "claude-sub": {"provider": "claude", "kind": "subscription", "data_policy": "no_training", "secret": "claude-oauth"},
            },
            "routes": {
                "claude-r": {"driver": "claude.agent-sdk", "model": "claude-sonnet-4-6", "connection": "claude-sub"},
            },
            "models": {
                "claude-alias": {"route": "claude-r"},
            },
        }
    )
    save_config(cfg)

    await store.upsert("claude-sub", auth="ok")

    # 1) Streaming chat RuntimeTurnError with authentication_failed marks expired
    async def failing_stream(_client, _prompt):
        raise RuntimeTurnError(
            "auth failed",
            partial_result=RuntimeResult(text="", provider="claude"),
            diagnostics={"assistant_error": "authentication_failed"},
        )
        yield ""

    ctx = StreamContext(
        turn=TurnContext(
            client=object(),
            latest_text="hi",
            messages=[],
            model="claude-alias",
            provider="claude",
            interaction_id=1,
            session_event="created",
            conversation_key="c1",
            lookup_key="c1",
            session_identity="id",
        ),
        prompt="hi",
    )
    ports = StreamingPorts(
        stream_runtime=failing_stream,
        start_event_writer=lambda *_a: asyncio.Queue(),
        record_failed=AsyncMock(),
        disconnect_client=AsyncMock(),
        completion=CompletionPorts(
            record_completed=AsyncMock(),
            backfill_conversation_key=AsyncMock(),
            rotate_hash_key=AsyncMock(),
            upsert_session=AsyncMock(),
            extract_tokens=lambda _: (0, 0),
        ),
    )
    chunks = [chunk async for chunk in stream_chat_response(ctx, ports)]
    assert any("provider_error" in c for c in chunks)
    st = await store.get("claude-sub")
    assert st is not None
    assert st["auth"] == "expired"

    # 2) Live RateLimitEvent recovery clears expired -> ok and records quota window
    await record_claude_rate_limit(
        "claude-sub",
        {"status": "allowed", "rate_limit_type": "seven_day", "utilization": 0.45, "resets_at": 1900500000},
        store=store,
    )
    st = await store.get("claude-sub")
    assert st["auth"] == "ok"
    assert st["quota"][0]["window"] == "7d"
    assert st["quota"][0]["used_percent"] == 45.0

    # 3) ExecutionService._observe_pool records both ProviderUsageLimit and authentication_failed
    dummy_service = object.__new__(ExecutionService)
    dummy_service.store = SimpleNamespace(model_pools=ModelPools(cfg))
    limit = ProviderUsageLimit(
        provider="claude",
        limit_window="five_hour",
        detected_at=datetime.now(timezone.utc),
        reset_at=datetime.now(timezone.utc) + timedelta(hours=3),
        resume="unavailable",
        unavailable_reason="no_session",
    )
    await dummy_service._observe_pool({"alias": "claude-alias"}, "failed", limit, reason="provider_usage_limit")
    st = await store.get("claude-sub")
    windows = {item["window"]: item for item in st["quota"]}
    assert windows["5h"]["used_percent"] == 100.0
    assert windows["7d"]["used_percent"] == 45.0

    await dummy_service._observe_pool({"alias": "claude-alias"}, "failed", None, reason="authentication_failed")
    st = await store.get("claude-sub")
    assert st["auth"] == "expired"


@pytest.mark.asyncio
async def test_connection_probe_loop_starts_recovers_and_stops():
    calls = 0
    second_call = asyncio.Event()

    async def fake_probe(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient probe failure")
        second_call.set()

    task = asyncio.create_task(connection_probe_loop(interval_seconds=0.02, probe_fn=fake_probe))
    try:
        await asyncio.wait_for(second_call.wait(), timeout=2.0)
        assert calls >= 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_admin_connections_api_includes_state_and_probe_endpoint(isolated_connection_env):
    tmp_path, store = isolated_connection_env
    codex_home = tmp_path / "codex-api"
    auth_file = codex_home / "auth.json"
    sentinel = "API_SECRET_SENTINEL_999"
    _write_fake_auth_file(auth_file, time.time() + 10 * 86400, token_sentinel=sentinel)
    fake_bin = _write_fake_codex_script(tmp_path / "fake-codex-api", mode="ok")

    cfg = HarnessConfig.model_validate(
        {
            "connections": {
                "codex-sub": {
                    "provider": "codex",
                    "kind": "subscription", "data_policy": "no_training",
                    "auth_file": str(auth_file),
                },
            },
            "routes": {
                "codex-r": {
                    "driver": "codex.app-server",
                    "model": "gpt-5.4",
                    "connection": "codex-sub",
                },
            },
            "models": {
                "codex-alias": {"route": "codex-r"},
            },
        }
    )
    save_config(cfg)

    fake_exec_service = SimpleNamespace(
        config=SimpleNamespace(drivers={"codex.app-server": SimpleNamespace(cli_binary=fake_bin)}),
        close=AsyncMock(),
    )

    with patch("kotoyose.app.start_execution_service", new=AsyncMock(return_value=fake_exec_service)):
        with patch("kotoyose.app.connection_probe_loop", new=AsyncMock()):
            with TestClient(app) as client:
                list_resp = client.get("/admin/connections")
                assert list_resp.status_code == 200
                items = list_resp.json()["connections"]
                assert len(items) == 1
                assert items[0]["id"] == "codex-sub"
                assert items[0]["state"]["auth"] == "unknown"

                probe_resp = client.post("/admin/connections/codex-sub/probe")
                assert probe_resp.status_code == 200
                body = probe_resp.json()
                assert body["status"] == "ok"
                assert body["state"]["auth"] == "ok"
                assert body["state"]["plan"] == "pro"
                assert body["state"]["cli_version"] == "codex-cli 0.99.0-fake"
                assert len(body["state"]["quota"]) == 2
                assert sentinel not in probe_resp.text

                list_after = client.get("/admin/connections")
                assert list_after.status_code == 200
                joined = list_after.json()["connections"][0]["state"]
                assert joined["auth"] == "ok"
                assert joined["plan"] == "pro"
                assert sentinel not in list_after.text

                missing_resp = client.post("/admin/connections/does-not-exist/probe")
                assert missing_resp.status_code == 404


@pytest.mark.asyncio
async def test_cursor_runtime_uses_route_connection_with_discovery_fallback(isolated_connection_env):
    _, _ = isolated_connection_env
    master_key = b"k" * 32
    secret_store = SecretStore(master_key=master_key)
    await secret_store.set("cursor-sec-a", "fake-cursor-token-primary")
    await secret_store.set("cursor-sec-b", "fake-cursor-token-route")
    set_secret_store(secret_store)

    try:
        cfg = HarnessConfig.model_validate(
            {
                "connections": {
                    "cursor-a": {"provider": "cursor", "kind": "api_key", "data_policy": "no_training", "secret": "cursor-sec-a"},
                    "cursor-b": {"provider": "cursor", "kind": "api_key", "data_policy": "no_training", "secret": "cursor-sec-b"},
                },
                "routes": {
                    "cursor-route-b": {"driver": "cursor.sdk", "model": "auto", "connection": "cursor-b"},
                },
                "models": {
                    "cursor-alias": {"route": "cursor-route-b"},
                },
            }
        )
        save_config(cfg)

        captured_api_keys: list[str] = []

        async def fake_create(options, *, client=None, api_key=None):
            captured_api_keys.append(api_key or options.api_key)
            return SimpleNamespace(close=AsyncMock())

        fake_bridge = AsyncMock(return_value=SimpleNamespace(aclose=AsyncMock()))
        preset = cfg.preset_for("cursor-alias")
        with (
            patch("kotoyose.runtime.cursor.AsyncClient.launch_bridge", new=fake_bridge),
            patch("kotoyose.runtime.cursor.AsyncAgent.create", side_effect=fake_create),
        ):
            session = build_cursor_session(preset, cwd_override="/tmp", session_id=None)
            assert session.connection == "cursor-b"
            await session._ensure_agent()
            await session.disconnect()

            # Fallback when no route connection is attached (e.g. model discovery)
            fallback_session = CursorRuntimeSession(model="auto", cwd="/tmp", connection=None)
            await fallback_session._ensure_agent()
            await fallback_session.disconnect()

        assert captured_api_keys == ["fake-cursor-token-route", "fake-cursor-token-primary"]
    finally:
        set_secret_store(None)


@pytest.mark.asyncio
async def test_progress_writer_records_claude_rate_limit_and_auth_failure(isolated_connection_env):
    _, store = isolated_connection_env
    fake_exec_store = SimpleNamespace(append_event=AsyncMock())
    writer = _ProgressWriter(fake_exec_store, "attempt-1", connection_id="claude-sub")

    writer.emit(
        RuntimeEvent(
            kind="rate_limit",
            summary="Claude rate limit",
            payload={"status": "allowed_warning", "rate_limit_type": "five_hour", "utilization": 0.91, "resets_at": 1900000000},
        )
    )
    await writer.queue.join()
    st = await store.get("claude-sub")
    assert st is not None
    assert st["quota_source"] == "official_stream"
    assert st["quota"][0]["used_percent"] == 91.0

    writer.emit(
        RuntimeEvent(
            kind="provider_error",
            summary="Auth failure",
            payload={"assistant_error": "authentication_failed"},
        )
    )
    await writer.finish()
    st = await store.get("claude-sub")
    assert st["auth"] == "expired"


def test_app_lifespan_starts_and_stops_connection_probe_task(isolated_connection_env):
    started = False
    cancelled = False

    async def fake_loop(**_kwargs):
        nonlocal started, cancelled
        started = True
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled = True
            raise

    with patch("kotoyose.app.start_execution_service", new=AsyncMock(return_value=None)):
        with patch("kotoyose.app.connection_probe_loop", side_effect=fake_loop):
            with TestClient(app):
                assert started is True
    assert cancelled is True

