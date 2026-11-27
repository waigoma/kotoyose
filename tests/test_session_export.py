"""Export の認可、カーソル、サイズ境界とインポート統計分離の契約。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from kotoyose import interactions
from kotoyose.api_key_store import ApiKeyStore
from kotoyose.config import Client, HarnessConfig
from kotoyose.execution_resources import agent_profile_allowed
from kotoyose.routes import observability, session_export
from kotoyose.session_import.models import EXPORT_MAX_BODY_BYTES

NOW = datetime(2026, 11, 27, tzinfo=timezone.utc)


def row(identifier=1, **changes):
    return {
        "id": identifier, "request_id": f"req-{identifier}", "conversation_key": "session",
        "provider": "codex", "runtime_session_id": "thread", "model": "test", "source": "chat",
        "received_at": NOW, "completed_at": NOW, "prompt": "質問", "response": "応答",
        "deleted_at": None, "metadata": None, **changes,
    }


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(clients={
        "exporter": Client(models=[], scopes=["sessions:export"]), "runner": Client(models=["*"]),
    }))
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    app = FastAPI()
    app.state.api_key_store = ApiKeyStore()
    app.include_router(session_export.router)
    app.include_router(observability.router)
    monkeypatch.setattr(session_export, "export_interactions", AsyncMock(return_value=[]))
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


async def headers(app, caller="exporter"):
    _, token = await app.state.api_key_store.create("key", caller)
    return {"Authorization": f"Bearer {token}"}


def test_config_validation():
    policy = Client(scopes=["sessions:export", "sessions:export"])
    assert policy.scopes == ["sessions:export"]
    assert "scopes" not in Client().model_dump()
    assert not agent_profile_allowed("any-model", policy)
    with pytest.raises(ValidationError):
        Client(scopes=["sessions:write"])
    assert HarnessConfig(session_import_allowed_roots=["/tmp"]).session_import_allowed_roots == [Path("/tmp")]
    assert "session_import_allowed_roots" not in HarnessConfig().model_dump()
    with pytest.raises(ValidationError):
        HarnessConfig(session_import_allowed_roots=["relative"])


@pytest.mark.asyncio
async def test_authentication(client, app):
    for auth in ({}, {"Authorization": "Bearer kys_invalid"}, {"Authorization": "Basic abc"}):
        result = client.get("/v1/sessions/export", headers=auth)
        assert result.status_code == 401
        assert result.headers["www-authenticate"] == "Bearer"
    auth = await headers(app)
    assert client.get("/v1/sessions/export", headers=auth).status_code == 200
    await app.state.api_key_store.revoke("key")
    assert client.get("/v1/sessions/export", headers=auth).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("caller,status", [("runner", 403), (None, 401), ("unknown", 401)])
async def test_client_binding_and_scope(client, app, caller, status):
    result = client.get("/v1/sessions/export", headers=await headers(app, caller))
    assert result.status_code == status
    if status == 403:
        assert result.json()["detail"] == "API key lacks 'sessions:export' scope"
    else:
        assert result.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_cursor_pagination(client, app, monkeypatch):
    records = [row(1), row(2), row(3, completed_at=NOW + timedelta(seconds=1))]

    async def fetch(*, after_completed_at, after_id, limit):
        eligible = [r for r in records if after_completed_at is None or
                    (r["completed_at"], r["id"]) > (after_completed_at, after_id)]
        return eligible[:limit]

    mock = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(session_export, "export_interactions", mock)
    auth = await headers(app)
    first = client.get("/v1/sessions/export?limit=1", headers=auth).json()
    assert first["schema_version"] == "1"
    assert first["has_more"] is True
    assert first["next_cursor"] == {"completed_at": "2026-11-27T00:00:00Z", "id": 1}
    second = client.get("/v1/sessions/export", params={
        "after_completed_at": first["next_cursor"]["completed_at"], "after_id": 1, "limit": 1,
    }, headers=auth).json()
    assert [r["id"] for r in second["items"]] == [2]
    third = client.get("/v1/sessions/export", params={
        "after_completed_at": second["next_cursor"]["completed_at"], "after_id": 2,
    }, headers=auth).json()
    assert [r["id"] for r in third["items"]] == [3]
    assert third["has_more"] is False
    empty = client.get("/v1/sessions/export", params={
        "after_completed_at": third["next_cursor"]["completed_at"], "after_id": 3,
    }, headers=auth).json()
    assert empty["items"] == [] and empty["next_cursor"] is None and not empty["has_more"]
    mock.assert_any_await(after_completed_at=NOW, after_id=1, limit=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["after_id=1", "limit=101", "limit=0", "after_id=-1"])
async def test_invalid_queries(client, app, query):
    assert client.get(f"/v1/sessions/export?{query}", headers=await headers(app)).status_code == 422


@pytest.mark.asyncio
async def test_serialization_and_utc(client, app, monkeypatch):
    mock = AsyncMock(return_value=[row(1, deleted_at=NOW, metadata='{"imported":true}'), row(2)])
    monkeypatch.setattr(session_export, "export_interactions", mock)
    result = client.get("/v1/sessions/export", headers=await headers(app), params={
        "after_completed_at": "2026-11-27T09:00:00+09:00",
    })
    assert result.status_code == 200
    deleted, live = result.json()["items"]
    assert deleted["deleted"] and deleted["prompt"] is None and deleted["response"] is None
    assert deleted["metadata"] == {"imported": True}
    assert live["prompt"] == "質問" and live["metadata"] == {} and not live["deleted"]
    mock.assert_awaited_once_with(after_completed_at=NOW, after_id=0, limit=101)


@pytest.mark.asyncio
async def test_body_cap(client, app, monkeypatch):
    monkeypatch.setattr(session_export, "export_interactions", AsyncMock(return_value=[
        row(i, response="界" * 250_000) for i in range(1, 5)
    ]))
    result = client.get("/v1/sessions/export", headers=await headers(app))
    assert len(result.content) <= EXPORT_MAX_BODY_BYTES
    data = result.json()
    assert [r["id"] for r in data["items"]] == [1, 2]
    assert data["has_more"] is True
    assert data["next_cursor"] == {"completed_at": data["items"][-1]["completed_at"], "id": 2}


@pytest.mark.asyncio
async def test_oversized_first_item_progresses(client, app, monkeypatch):
    monkeypatch.setattr(session_export, "export_interactions", AsyncMock(return_value=[
        row(1, response="x" * EXPORT_MAX_BODY_BYTES), row(2),
    ]))
    data = client.get("/v1/sessions/export", headers=await headers(app)).json()
    assert len(data["items"]) == 1 and data["has_more"]
    assert data["next_cursor"]["id"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("include", [False, True])
async def test_stats_import_filter(monkeypatch, include):
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={
        "total": 0, "completed": 0, "failed": 0, "tokens_in": 0, "tokens_out": 0, "avg_duration_ms": 0,
    })
    pool.fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(interactions, "_get_pool", lambda: pool)
    kwargs = {"include_imported": True} if include else {}
    stats = await interactions.compute_stats(**kwargs)
    assert set(stats) == {"past_24h", "past_7d"}
    await interactions.compute_hourly_stats(**kwargs)
    for call in [*pool.fetch.await_args_list, *pool.fetchrow.await_args_list]:
        assert ("AND (source IS NULL OR source NOT LIKE 'import:%')" in call.args[0]) is not include


@pytest.mark.parametrize("path,attribute,kwargs", [
    ("/admin/stats", "compute_stats", {}),
    ("/admin/stats/hourly", "compute_hourly_stats", {"hours": 24}),
])
@pytest.mark.parametrize("include", [False, True])
def test_stats_api(client, monkeypatch, path, attribute, kwargs, include):
    mock = AsyncMock(return_value={})
    monkeypatch.setattr(observability, attribute, mock)
    result = client.get(path, params={"include_imported": str(include).lower()})
    assert result.status_code == 200
    mock.assert_awaited_once_with(**kwargs, **({"include_imported": True} if include else {}))


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [None, NOW])
async def test_export_query(monkeypatch, cursor):
    pool = MagicMock(fetch=AsyncMock(return_value=[row()]))
    monkeypatch.setattr(interactions, "_get_pool", lambda: pool)
    assert await interactions.export_interactions(after_completed_at=cursor, after_id=2, limit=3) == [row()]
    sql, *args = pool.fetch.await_args.args
    assert "WHERE completed_at IS NOT NULL" in sql
    assert "ORDER BY completed_at ASC, id ASC" in sql
    assert "deleted_at IS NULL" not in sql
    if cursor is None:
        assert args == [3] and "LIMIT $1" in sql
    else:
        assert args == [NOW, 2, 3] and "LIMIT $3" in sql
        assert "AND (completed_at, id) > ($1, $2)" in sql


@pytest.mark.asyncio
async def test_export_no_database(monkeypatch):
    monkeypatch.setattr(interactions, "_get_pool", lambda: None)
    assert await interactions.export_interactions() == []


@pytest.mark.asyncio
async def test_body_cap_exact_boundary(client, app, monkeypatch):
    records = [row(1), row(2)]
    monkeypatch.setattr(session_export, "export_interactions", AsyncMock(return_value=records))
    auth = await headers(app)
    full = client.get("/v1/sessions/export", headers=auth)
    monkeypatch.setattr(session_export, "EXPORT_MAX_BODY_BYTES", len(full.content))
    assert client.get("/v1/sessions/export", headers=auth).json() == full.json()
    monkeypatch.setattr(session_export, "EXPORT_MAX_BODY_BYTES", len(full.content) - 1)
    split = client.get("/v1/sessions/export", headers=auth).json()
    assert [item["id"] for item in split["items"]] == [1]
    assert split["has_more"] and split["next_cursor"]["id"] == 1


def test_client_payload_scopes():
    from kotoyose.routes.admin_clients import _client_payload
    from kotoyose.routes.api_keys import _client_policies

    for scopes in ([], ["sessions:export"]):
        policy = Client(scopes=scopes)
        assert _client_payload("exporter", policy, [])["scopes"] == scopes
        assert _client_policies({"exporter": policy})[0]["scopes"] == scopes
