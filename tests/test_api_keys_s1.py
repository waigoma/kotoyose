"""API-key lifecycle, authentication and isolation in memory."""
import hashlib
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from kotoyose import auth
from kotoyose.api_key_store import ApiKeyStore
from kotoyose.config import Client, HarnessConfig
from kotoyose.execution_http import authorize_execution
from kotoyose.execution_resources import ExecutionError, NativeExecutionConfig
from kotoyose.routes import api_keys


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(auth, "API_KEY", "admin-password")
    monkeypatch.setattr(auth, "BASIC_AUTH_USER", "admin")
    monkeypatch.setattr(
        "kotoyose.config._config",
        HarnessConfig(models={"kotoyose-chat": {}}, clients={"a": Client(models=["*"])}),
    )
    app = FastAPI()
    app.state.api_key_store = ApiKeyStore()
    app.state.execution_service = None
    app.include_router(api_keys.router)
    app.include_router(api_keys.client_router)

    @app.exception_handler(ExecutionError)
    async def execution_error(request, exc):
        return JSONResponse({"error": exc.code}, status_code=exc.status)

    @app.get("/v1/test", dependencies=[Depends(authorize_execution)])
    async def check(request: Request):
        return {"name": request.state.api_key_name, "caller": request.state.api_key_caller,
                "principal": request.state.execution_principal}

    @app.post("/v1/native", dependencies=[Depends(authorize_execution)])
    async def native(request: Request):
        return {"principal": request.state.execution_principal}

    return app


@pytest.mark.asyncio
async def test_key_lifecycle_show_once_and_admin_auth(app, caplog):
    config = NativeExecutionConfig.model_construct()
    app.state.execution_service = SimpleNamespace(
        config=config, store=SimpleNamespace(runtime_store_id="00000000-0000-0000-0000-000000000001"),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        missing_caller = await client.post("/admin/api-keys", auth=("admin", "admin-password"), json={"name": "no-caller"})
        assert missing_caller.status_code == 422
        response = await client.post("/admin/api-keys", auth=("admin", "admin-password"), json={"name": "hub", "client": "a"})
        assert response.status_code == 200
        key = response.json()["key"]
        assert key.startswith("kys_") and len(key) == 47
        assert response.headers["cache-control"] == "no-store"
        row = app.state.api_key_store._rows["hub"]
        assert row["key_hash"] == hashlib.sha256(key.encode()).hexdigest()
        assert not any(key in str(value) for value in row.values())
        assert not (key in caplog.text)
        listing = await client.get("/admin/api-keys", auth=("admin", "admin-password"))
        assert not (key in listing.text)
        assert "key_hash" not in listing.text
        assert (await client.get("/admin/api-keys", headers={"Authorization": f"Bearer {key}"})).status_code == 401
        assert (await client.get("/v1/test", headers={"Authorization": f"Bearer {key}"})).json()["name"] == "hub"
        assert (await client.delete("/admin/api-keys/hub", auth=("admin", "admin-password"))).status_code == 409
        assert (await client.post("/admin/api-keys/hub/revoke", auth=("admin", "admin-password"))).status_code == 200
        assert (await client.get("/v1/test", headers={"Authorization": f"Bearer {key}"})).status_code == 401
        assert (await client.delete("/admin/api-keys/hub", auth=("admin", "admin-password"))).status_code == 200


@pytest.mark.asyncio
async def test_last_used_throttling_and_isolation():
    store = ApiKeyStore()
    row, key = await store.create("one")
    assert row["last_used_at"] is None
    first = await store.authenticate(key)
    second = await store.authenticate(key)
    assert first["last_used_at"] == second["last_used_at"]
    store._rows["one"]["last_used_at"] -= timedelta(minutes=2)
    third = await store.authenticate(key)
    assert third["last_used_at"] > second["last_used_at"]
    assert await ApiKeyStore().authenticate(key) is None

    legacy_key = "ahs_" + key.removeprefix("kys_")
    store._rows["one"]["key_hash"] = hashlib.sha256(legacy_key.encode()).hexdigest()
    assert await store.authenticate(legacy_key) is None


@pytest.mark.asyncio
async def test_client_binding_and_obsolete_credentials(app, monkeypatch):
    monkeypatch.setenv("CALLER_A_TOKEN", "old-caller-token")
    monkeypatch.setattr(
        "kotoyose.config._config",
        HarnessConfig(clients={
            "a": Client(models=["*"]),
            "b": Client(models=["*"]),
        }),
    )
    config = NativeExecutionConfig.model_construct()
    app.state.execution_service = SimpleNamespace(config=config, store=SimpleNamespace(runtime_store_id="00000000-0000-0000-0000-000000000001"))
    store = app.state.api_key_store
    _, key_a = await store.create("key-a", "a")
    _, chat_key = await store.create("chat")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        def headers(key):
            return {"Authorization": "Bearer " + key}
        assert (await client.post("/v1/native", headers=headers(key_a))).json()["principal"] == "a"
        assert (await client.post("/v1/native", headers={**headers(key_a), "X-Kotoyose-Caller-Namespace": "b", "X-Kotoyose-Runtime-Store-Id": "00000000-0000-0000-0000-000000000001"})).status_code != 200
        assert (await client.post("/v1/native", headers=headers(chat_key))).status_code == 401
        unbound = await client.get("/v1/test", headers=headers(chat_key))
        assert unbound.status_code == 401
        assert "re-issue" in unbound.text.lower()
        for obsolete in ("old-caller-token", "admin-password"):
            assert (await client.post("/v1/native", headers=headers(obsolete))).status_code == 401
            assert (await client.get("/v1/test", headers=headers(obsolete))).status_code == 401


@pytest.mark.asyncio
async def test_sessions_pass_authenticated_scope(app, monkeypatch):
    seen = []
    async def scoped(key, name, session_id, provider):
        seen.append((key, name, session_id, provider))
        return [{"owner": name}]
    monkeypatch.setattr(api_keys, "get_scoped_session_turns", scoped)
    store = app.state.api_key_store
    _, first = await store.create("first", "a")
    _, second = await store.create("second", "a")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for key, owner in [(first, "first"), (second, "second")]:
            response = await client.get("/v1/sessions/project/test?provider=claude", headers={"Authorization": "Bearer " + key})
            assert response.json()["turns"] == [{"owner": owner}]
        assert [item[1] for item in seen] == ["first", "second"]


@pytest.mark.asyncio
async def test_scoped_session_query_filters_before_return(monkeypatch):
    from kotoyose import interactions
    pool = SimpleNamespace(fetch=AsyncMock(return_value=[]))
    monkeypatch.setattr(interactions, "_get_pool", lambda: pool)
    await interactions.get_scoped_session_turns("same", "owner")
    sql, *args = pool.fetch.await_args.args
    assert "api_key_name IS NOT DISTINCT FROM $2" in sql
    assert args[:2] == ["same", "owner"]


@pytest.mark.asyncio
async def test_revoked_key_in_development_mode(app, monkeypatch):
    monkeypatch.setattr(auth, "API_KEY", None)
    _, key = await app.state.api_key_store.create("development", "a")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"Authorization": "Bearer " + key}
        assert (await client.get("/v1/test", headers=headers)).status_code == 200
        await app.state.api_key_store.revoke("development")
        assert (await client.get("/v1/test", headers=headers)).status_code == 401
        assert (await client.post("/v1/native", headers=headers)).status_code == 401
        assert (await client.get("/v1/test")).status_code == 401


@pytest.mark.asyncio
async def test_postgres_store_uses_only_digest_and_throttles_updates():
    calls = []
    async def fetchrow(sql, *args):
        calls.append((sql, args))
        if sql.startswith("INSERT"):
            return {"id": 1, "name": args[0], "caller": args[1], "key_hash": args[2],
                    "prefix": args[3], "last4": args[4]}
        if sql.startswith("UPDATE"):
            return None
        return {"id": 1, "name": "client", "caller": None, "key_hash": args[0]}
    store = ApiKeyStore(SimpleNamespace(fetchrow=fetchrow))
    row, key = await store.create("client")
    assert "key_hash" not in row
    assert calls[0][1][2] == hashlib.sha256(key.encode()).hexdigest()
    assert not any(key in str(part) for _, args in calls for part in args)
    authenticated = await store.authenticate(key)
    assert authenticated["name"] == "client"
    assert "last_used_at < now() - interval '1 minute'" in calls[1][0]
    assert calls[1][1][0] == hashlib.sha256(key.encode()).hexdigest()
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_chat_records_authenticated_key_name(app, monkeypatch):
    from kotoyose.routes import chat
    app.include_router(chat.router)
    _, key = await app.state.api_key_store.create("chat-client", "a")
    received = AsyncMock(return_value=42)
    monkeypatch.setattr(chat, "record_received", received)
    monkeypatch.setattr(chat, "record_completed", AsyncMock())
    monkeypatch.setattr(chat, "record_failed", AsyncMock())
    monkeypatch.setattr(chat, "get_or_resume_client", AsyncMock(return_value=(AsyncMock(), False)))
    monkeypatch.setattr(chat, "ask_claude", AsyncMock(return_value=("ok", {}, "session")))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key},
                                     json={"model": "kotoyose-chat", "messages": [{"role": "user", "content": "hello"}]})
    assert response.status_code == 200
    assert received.await_args.kwargs["api_key_name"] == "chat-client"
