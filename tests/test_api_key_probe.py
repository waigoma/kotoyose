"""Tests for api_key and endpoint connection health probes (no network; HTTP/SDK mocked)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import cursor_sdk
from fastapi.testclient import TestClient
import httpx
import pytest
from structlog.testing import capture_logs

from kotoyose import auth as auth_module
from kotoyose import db as db_module
from kotoyose.app import app
from kotoyose.config import Connection, HarnessConfig, reload_config, save_config
from kotoyose.connection_state_store import ConnectionStateStore, set_connection_state_store
from kotoyose.connections_probe import (
    ANTHROPIC_MODELS_URL,
    API_KEY_PROBE_TIMEOUT_SECONDS,
    OPENAI_MODELS_URL,
    _require_https_url,
    _sanitize_probe_error,
    probe_all_connections,
    probe_api_key_connection,
    probe_connection,
    probe_endpoint_connection,
)
from kotoyose.secret_store import SecretStore, set_secret_store


@pytest.fixture
def isolated_probe_env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(cfg_path))
    monkeypatch.setattr("kotoyose.app.configure_logging", lambda: None)
    monkeypatch.setattr(auth_module, "API_KEY", None)
    monkeypatch.setattr(db_module, "_pool", None)
    store = ConnectionStateStore(None)
    set_connection_state_store(store)
    secret_store = SecretStore(master_key=b"s" * 32)
    set_secret_store(secret_store)
    reload_config()
    yield tmp_path, store, secret_store
    set_secret_store(None)
    set_connection_state_store(None)
    reload_config()


def _install_mock_http(monkeypatch, handler):
    """Route `connections_probe._probe_http_get` through an `httpx.MockTransport` without network."""
    recorded: list[dict] = []
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(**kwargs):
        recorded.append(
            {
                "follow_redirects": kwargs.get("follow_redirects"),
                "timeout": kwargs.get("timeout"),
                "trust_env": kwargs.get("trust_env"),
            }
        )
        kwargs["transport"] = transport
        return real_async_client(**kwargs)

    monkeypatch.setattr("kotoyose.connections_probe.httpx.AsyncClient", fake_async_client)
    return recorded


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "anthropic"])
async def test_claude_api_key_probe_status_mappings(isolated_probe_env, monkeypatch, provider):
    _, store, secret_store = isolated_probe_env
    key = "sk\x2dant-synthetic-test-key-001"
    await secret_store.set("claude-key", key)
    conn = Connection(provider=provider, kind="api_key", data_policy="no_training", secret="claude-key")

    seen_requests: list[httpx.Request] = []
    mode = "200"

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        if mode == "200":
            return httpx.Response(200, json={"data": [{"id": "claude-sonnet-4-6"}], "has_more": True})
        if mode == "401":
            return httpx.Response(401, json={"error": {"message": f"invalid key {key}"}})
        if mode == "403":
            return httpx.Response(403, json={"error": {"message": "forbidden"}})
        if mode == "429":
            return httpx.Response(429, json={"error": {"message": "too many requests"}})
        if mode == "500":
            return httpx.Response(500, text="server error")
        if mode == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if mode == "connect_error":
            raise httpx.ConnectError("connection refused", request=request)
        raise AssertionError(f"unexpected mode: {mode}")

    client_opts = _install_mock_http(monkeypatch, handler)

    # 200
    st = await probe_api_key_connection("c-claude", conn, store=store)
    assert len(seen_requests) == 1
    assert str(seen_requests[0].url) == ANTHROPIC_MODELS_URL
    assert seen_requests[0].method == "GET"
    assert seen_requests[0].headers["x-api-key"] == key
    assert seen_requests[0].headers["anthropic-version"] == "2023-06-01"
    assert client_opts[0]["follow_redirects"] is False
    assert client_opts[0]["timeout"].read == API_KEY_PROBE_TIMEOUT_SECONDS
    assert st["auth"] == "ok"
    assert st["plan"] == "1 model"
    assert st["quota"] == []
    assert st["quota_source"] is None
    assert st["checked_at"] is not None
    assert st["last_error"] is None

    # 401
    mode = "401"
    st = await probe_api_key_connection("c-claude", conn, store=store)
    assert st["auth"] == "logged_out"
    assert st["last_error"] == "API key rejected (HTTP 401)"
    assert st["plan"] is None
    assert key not in json.dumps(st)

    # 403
    mode = "403"
    st = await probe_api_key_connection("c-claude", conn, store=store)
    assert st["auth"] == "logged_out"
    assert st["last_error"] == "API key rejected (HTTP 403)"

    # 429
    mode = "429"
    st = await probe_api_key_connection("c-claude", conn, store=store)
    assert st["auth"] == "ok"
    assert st["last_error"] == "rate limited"

    # 500
    mode = "500"
    st = await probe_api_key_connection("c-claude", conn, store=store)
    assert st["auth"] == "unknown"
    assert st["last_error"] == "HTTP 500"

    # timeout
    mode = "timeout"
    st = await probe_api_key_connection("c-claude", conn, store=store)
    assert st["auth"] == "unknown"
    assert "timed out" in st["last_error"]

    # connect error
    mode = "connect_error"
    st = await probe_api_key_connection("c-claude", conn, store=store)
    assert st["auth"] == "unknown"
    assert "network error" in st["last_error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "openai"])
async def test_openai_api_key_probe_status_mappings(isolated_probe_env, monkeypatch, provider):
    _, store, _ = isolated_probe_env
    key = "sk\x2dopenai-synthetic-test-key-002"
    monkeypatch.setenv("TEST_OPENAI_API_KEY", key)
    conn = Connection(provider=provider, kind="api_key", data_policy="no_training", secret="env:TEST_OPENAI_API_KEY")

    seen_requests: list[httpx.Request] = []
    mode = "200"

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        if mode == "200":
            return httpx.Response(200, json={"data": [{"id": f"gpt-m-{i}"} for i in range(12)]})
        if mode == "401":
            return httpx.Response(401, json={"error": {"message": "Incorrect API key"}})
        if mode == "403":
            return httpx.Response(403)
        if mode == "429":
            return httpx.Response(429)
        if mode == "timeout":
            raise httpx.ConnectTimeout("connect timed out", request=request)
        if mode == "connect_error":
            raise httpx.ConnectError("dns failure", request=request)
        raise AssertionError(f"unexpected mode: {mode}")

    _install_mock_http(monkeypatch, handler)

    st = await probe_api_key_connection("c-openai", conn, store=store)
    assert str(seen_requests[0].url) == OPENAI_MODELS_URL
    assert seen_requests[0].method == "GET"
    assert seen_requests[0].headers["authorization"] == f"Bearer {key}"
    assert st["auth"] == "ok"
    assert st["plan"] == "12 models"
    assert st["quota"] == []
    assert st["quota_source"] is None
    assert st["checked_at"] is not None
    assert st["last_error"] is None

    for err_mode, expected_auth, expected_sub in (
        ("401", "logged_out", "API key rejected (HTTP 401)"),
        ("403", "logged_out", "API key rejected (HTTP 403)"),
        ("429", "ok", "rate limited"),
        ("timeout", "unknown", "timed out"),
        ("connect_error", "unknown", "network error"),
    ):
        mode = err_mode
        st = await probe_api_key_connection("c-openai", conn, store=store)
        assert st["auth"] == expected_auth
        assert expected_sub in st["last_error"]
        assert st["quota"] == []
        assert st["quota_source"] is None
        assert key not in json.dumps(st)


@pytest.mark.asyncio
async def test_endpoint_probe_with_and_without_secret_and_v1_fallback(isolated_probe_env, monkeypatch):
    _, store, secret_store = isolated_probe_env
    gw_secret = "gw-synthetic-bearer-999"
    await secret_store.set("gw-key", gw_secret)

    seen: list[httpx.Request] = []
    mode = "ok_v1_fallback"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if mode == "ok_v1_fallback":
            if str(request.url).endswith("/v1/models"):
                return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}]})
            return httpx.Response(404)
        if mode == "ok_direct":
            return httpx.Response(200, json={"models": ["local-a", "local-b"]})
        if mode == "401":
            return httpx.Response(401)
        if mode == "403":
            return httpx.Response(403)
        if mode == "429":
            return httpx.Response(429)
        if mode == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if mode == "connect_error":
            raise httpx.ConnectError("refused", request=request)
        raise AssertionError(f"unexpected mode: {mode}")

    _install_mock_http(monkeypatch, handler)

    # 1. Endpoint WITH secret and base URL lacking /v1 -> tries /models (404) then /v1/models (200)
    conn_with_secret = Connection(
        provider="openai",
        kind="endpoint", data_policy="no_training",
        endpoint="https://gateway.example",
        secret="gw-key",
    )
    st = await probe_endpoint_connection("ep-with-secret", conn_with_secret, store=store)
    assert [str(r.url) for r in seen] == [
        "https://gateway.example/models",
        "https://gateway.example/v1/models",
    ]
    assert seen[-1].headers["authorization"] == f"Bearer {gw_secret}"
    assert st["auth"] == "ok"
    assert st["plan"] == "3 models"
    assert st["quota"] == []
    assert st["quota_source"] is None
    assert st["last_error"] is None

    # 2. Endpoint WITHOUT secret (e.g. local unauthenticated gateway) -> no Authorization header
    seen.clear()
    mode = "ok_direct"
    conn_no_secret = Connection.model_construct(
        provider="openai",
        kind="endpoint",
        endpoint="http://127.0.0.1:8080/v1",
        secret=None,
    )
    st_no_secret = await probe_endpoint_connection("ep-no-secret", conn_no_secret, store=store)
    assert [str(r.url) for r in seen] == ["http://127.0.0.1:8080/v1/models"]
    assert "authorization" not in seen[0].headers
    assert st_no_secret["auth"] == "ok"
    assert st_no_secret["plan"] == "2 models"
    assert st_no_secret["last_error"] is None

    # 3. 401 / 403 / 429 / unreachable (timeout + connect error)
    conn_ep = Connection(
        provider="claude",
        kind="endpoint", data_policy="no_training",
        endpoint="https://gateway.example/v1",
        secret="gw-key",
    )
    for err_mode, expected_auth, expected_err in (
        ("401", "logged_out", "API key rejected (HTTP 401)"),
        ("403", "logged_out", "API key rejected (HTTP 403)"),
        ("429", "ok", "rate limited"),
        ("timeout", "unknown", "endpoint unreachable"),
        ("connect_error", "unknown", "endpoint unreachable"),
    ):
        seen.clear()
        mode = err_mode
        st_err = await probe_endpoint_connection("ep-err", conn_ep, store=store)
        assert st_err["auth"] == expected_auth
        assert st_err["last_error"] == expected_err
        assert len(seen) == 1
        assert gw_secret not in json.dumps(st_err)


@pytest.mark.asyncio
async def test_missing_secret_store_and_env_returns_logged_out_without_network(
    isolated_probe_env, monkeypatch
):
    _, store, _ = isolated_probe_env
    monkeypatch.delenv("MISSING_ENV_API_KEY", raising=False)

    def fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"HTTP must not be called when secret is missing: {request.url}")

    _install_mock_http(monkeypatch, fail_if_called)

    # Secret store missing
    conn_store = Connection(provider="claude", kind="api_key", data_policy="no_training", secret="nonexistent-secret")
    st1 = await probe_api_key_connection("c-missing-store", conn_store, store=store)
    assert st1["auth"] == "logged_out"
    assert st1["last_error"] == "secret 'nonexistent-secret' is not set"
    assert st1["checked_at"] is not None
    assert st1["quota"] == []
    assert st1["quota_source"] is None

    # Env var missing
    conn_env = Connection(provider="openai", kind="api_key", data_policy="no_training", secret="env:MISSING_ENV_API_KEY")
    st2 = await probe_api_key_connection("c-missing-env", conn_env, store=store)
    assert st2["auth"] == "logged_out"
    assert st2["last_error"] == "secret 'MISSING_ENV_API_KEY' is not set"

    # Endpoint with missing secret
    conn_ep = Connection(
        provider="codex",
        kind="endpoint", data_policy="no_training",
        endpoint="https://gw.example/v1",
        secret="missing-gw-secret",
    )
    st3 = await probe_endpoint_connection("ep-missing-secret", conn_ep, store=store)
    assert st3["auth"] == "logged_out"
    assert st3["last_error"] == "secret 'missing-gw-secret' is not set"


@pytest.mark.asyncio
async def test_no_redirect_following_and_https_only_and_error_cap(isolated_probe_env, monkeypatch):
    _, store, secret_store = isolated_probe_env
    secret_key = "sk\x2ddo-not-forward-on-redirect"
    await secret_store.set("redirect-key", secret_key)
    conn = Connection(provider="openai", kind="api_key", data_policy="no_training", secret="redirect-key")

    visited_urls: list[str] = []

    def redirect_handler(request: httpx.Request) -> httpx.Response:
        visited_urls.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "https://attacker.example/steal"},
            request=request,
        )

    _install_mock_http(monkeypatch, redirect_handler)

    st = await probe_api_key_connection("c-redirect", conn, store=store)
    assert visited_urls == [OPENAI_MODELS_URL]
    assert st["auth"] == "unknown"
    assert "302" in st["last_error"]
    assert secret_key not in json.dumps(st)

    # Fixed vendor URLs must be HTTPS
    assert ANTHROPIC_MODELS_URL.startswith("https://")
    assert OPENAI_MODELS_URL.startswith("https://")
    with pytest.raises(ValueError, match="HTTPS"):
        _require_https_url("http://api.openai.com/v1/models")

    # Error text capped at 200 chars and never contains secret
    capped = _sanitize_probe_error(f"prefix {secret_key} " + ("x" * 300), secret_key)
    assert capped is not None
    assert len(capped) == 200
    assert secret_key not in capped


@pytest.mark.asyncio
async def test_cursor_api_key_probe_via_fake_sdk_bridge(isolated_probe_env, monkeypatch):
    _, store, secret_store = isolated_probe_env
    cursor_key = "crsr-synthetic-key-777"
    await secret_store.set("cursor-sec", cursor_key)
    conn = Connection(provider="cursor", kind="api_key", data_policy="no_training", secret="cursor-sec")

    closed_count = 0

    class FakeClient:
        async def aclose(self):
            nonlocal closed_count
            closed_count += 1

    async def fake_launch_bridge():
        return FakeClient()

    mode = "ok"
    captured_keys: list[str | None] = []

    async def fake_models_list(*, client, api_key=None):
        captured_keys.append(api_key)
        if mode == "ok":
            return [SimpleNamespace(id="auto"), SimpleNamespace(id="claude-sonnet"), SimpleNamespace(id="gpt-5")]
        if mode == "auth_error":
            raise cursor_sdk.AuthenticationError(f"invalid api key {cursor_key}")
        if mode == "perm_error":
            raise cursor_sdk.PermissionDeniedError("403 forbidden")
        if mode == "rate_limit":
            raise cursor_sdk.RateLimitError("rate limited")
        if mode == "timeout":
            raise TimeoutError("bridge timed out")
        raise RuntimeError("bridge crashed")

    monkeypatch.setattr("kotoyose.model_discovery.AsyncClient.launch_bridge", fake_launch_bridge)
    monkeypatch.setattr("kotoyose.model_discovery.AsyncCursor.models.list", fake_models_list)

    # Success
    st_ok = await probe_api_key_connection("cursor-1", conn, store=store)
    assert captured_keys[-1] == cursor_key
    assert closed_count == 1
    assert st_ok["auth"] == "ok"
    assert st_ok["plan"] == "3 models"
    assert st_ok["quota"] == []
    assert st_ok["quota_source"] is None
    assert st_ok["last_error"] is None
    assert cursor_key not in json.dumps(st_ok)

    # Auth errors -> logged_out
    for err_mode in ("auth_error", "perm_error"):
        mode = err_mode
        st_auth = await probe_api_key_connection("cursor-1", conn, store=store)
        assert st_auth["auth"] == "logged_out"
        assert st_auth["last_error"] == "API key rejected"
        assert cursor_key not in json.dumps(st_auth)

    # Rate limit -> ok with rate limited
    mode = "rate_limit"
    st_rl = await probe_api_key_connection("cursor-1", conn, store=store)
    assert st_rl["auth"] == "ok"
    assert st_rl["last_error"] == "rate limited"

    # Timeout -> unknown
    mode = "timeout"
    st_to = await probe_api_key_connection("cursor-1", conn, store=store)
    assert st_to["auth"] == "unknown"
    assert "timed out" in st_to["last_error"]

    # Bridge error -> unknown
    mode = "crash"
    st_cr = await probe_api_key_connection("cursor-1", conn, store=store)
    assert st_cr["auth"] == "unknown"
    assert st_cr["last_error"] == "Cursor SDK probe failed"


@pytest.mark.asyncio
async def test_key_never_appears_in_logs_stored_state_or_admin_api_responses(
    isolated_probe_env, monkeypatch, caplog
):
    _, store, secret_store = isolated_probe_env
    claude_secret = "sk\x2dant-top-secret-sentinel-alpha"
    openai_secret = "sk\x2dproj-top-secret-sentinel-beta"
    await secret_store.set("sec-claude", claude_secret)
    monkeypatch.setenv("TEST_EP_SECRET_BETA", openai_secret)
    monkeypatch.delenv("MISSING_API_KEY_BETA", raising=False)

    cfg = HarnessConfig.model_validate(
        {
            "connections": {
                "claude-api": {"provider": "claude", "kind": "api_key", "data_policy": "no_training", "secret": "sec-claude"},
                "gw-ep": {
                    "provider": "openai",
                    "kind": "endpoint", "data_policy": "no_training",
                    "endpoint": "https://gw.example/v1",
                    "secret": "env:TEST_EP_SECRET_BETA",
                },
                "missing-api": {"provider": "openai", "kind": "api_key", "data_policy": "no_training", "secret": "env:MISSING_API_KEY_BETA"},
            }
        }
    )
    save_config(cfg)

    def handler(request: httpx.Request) -> httpx.Response:
        if "anthropic.com" in str(request.url):
            return httpx.Response(200, json={"data": [{"id": "claude-opus-4-6"}]})
        return httpx.Response(401, json={"error": f"rejected {openai_secret}"})

    _install_mock_http(monkeypatch, handler)

    with capture_logs() as struct_logs:
        all_results = await probe_all_connections(cfg=cfg, store=store)
        assert set(all_results.keys()) == {"claude-api", "gw-ep", "missing-api"}
        assert all_results["claude-api"]["auth"] == "ok"
        assert all_results["claude-api"]["plan"] == "1 model"
        assert all_results["gw-ep"]["auth"] == "logged_out"
        assert all_results["gw-ep"]["last_error"] == "API key rejected (HTTP 401)"
        assert all_results["missing-api"]["auth"] == "logged_out"
        assert all_results["missing-api"]["last_error"] == "secret 'MISSING_API_KEY_BETA' is not set"

        fake_exec = SimpleNamespace(config=SimpleNamespace(drivers={}), close=AsyncMock())
        with patch("kotoyose.app.start_execution_service", new=AsyncMock(return_value=fake_exec)):
            with patch("kotoyose.app.connection_probe_loop", new=AsyncMock()):
                with TestClient(app) as client:
                    list_resp = client.get("/admin/connections")
                    assert list_resp.status_code == 200
                    probe_resp = client.post("/admin/connections/claude-api/probe")
                    assert probe_resp.status_code == 200
                    probe_ep = client.post("/admin/connections/gw-ep/probe")
                    assert probe_ep.status_code == 200

                    for payload_text in (list_resp.text, probe_resp.text, probe_ep.text):
                        assert claude_secret not in payload_text
                        assert openai_secret not in payload_text

    stored_dump = json.dumps(await store.list())
    assert claude_secret not in stored_dump
    assert openai_secret not in stored_dump

    struct_dump = json.dumps(struct_logs)
    assert claude_secret not in struct_dump
    assert openai_secret not in struct_dump
    assert claude_secret not in caplog.text
    assert openai_secret not in caplog.text


@pytest.mark.asyncio
async def test_probe_connection_dispatches_api_key_and_endpoint(isolated_probe_env, monkeypatch):
    _, store, secret_store = isolated_probe_env
    await secret_store.set("oai-k", "sk-oai-test")
    cfg = HarnessConfig.model_validate(
        {
            "connections": {
                "oai": {"provider": "openai", "kind": "api_key", "data_policy": "no_training", "secret": "oai-k"},
                "ep": {"provider": "codex", "kind": "endpoint", "data_policy": "no_training", "endpoint": "https://gw.example/v1", "secret": "oai-k"},
            }
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]})

    _install_mock_http(monkeypatch, handler)

    st_oai = await probe_connection("oai", cfg=cfg, store=store)
    assert st_oai["auth"] == "ok"
    assert st_oai["plan"] == "2 models"
    datetime.fromisoformat(st_oai["checked_at"].replace("Z", "+00:00"))

    st_ep = await probe_connection("ep", cfg=cfg, store=store)
    assert st_ep["auth"] == "ok"
    assert st_ep["plan"] == "2 models"
