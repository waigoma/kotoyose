"""Subagent visibility defaults and explicit audit access."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kotoyose import interactions
from kotoyose.auth import require_basic_auth
from kotoyose.routes import observability, session_export


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["list_interactions", "list_sessions", "export_interactions"])
@pytest.mark.parametrize("include", [None, False, True])
async def test_query_visibility(monkeypatch, name, include):
    pool = MagicMock(fetch=AsyncMock(return_value=[]), fetchval=AsyncMock(return_value=0))
    monkeypatch.setattr(interactions, "_get_pool", lambda: pool)
    kwargs = {} if include is None else {"include_subagents": include}
    await getattr(interactions, name)(**kwargs)
    calls = [*pool.fetch.await_args_list, *pool.fetchval.await_args_list]
    assert len(calls) == (1 if name == "export_interactions" else 2)
    for call in calls:
        sql = call.args[0]
        alias = "i." if name == "list_interactions" else ""
        expected = (
            f"({alias}conversation_key IS NULL OR ({alias}conversation_key NOT LIKE 'delegate:%'"
            f" AND {alias}conversation_key NOT LIKE 'delegation:%'))"
            f" AND COALESCE(({alias}metadata->>'is_subagent')::boolean, FALSE) = FALSE"
        )
        assert (expected in sql) is (include is not True)
        if include is True:
            assert "delegation:%" not in sql
            assert "is_subagent" not in sql
        if name == "list_interactions":
            assert "FROM kotoyose.interactions i" in sql


@pytest.mark.parametrize("path,module,name,result,kwargs", [
    ("/admin/interactions", observability, "list_interactions",
     {"items": [], "total": 0}, {
         "limit": 50, "offset": 0, "model": None, "source": None, "status": None,
         "conversation_key": None, "since": None, "until": None,
     }),
    ("/admin/sessions", observability, "list_sessions",
     {"items": [], "total": 0}, {"limit": 50, "offset": 0}),
    ("/v1/sessions/export", session_export, "export_interactions",
     [], {"after_completed_at": None, "after_id": 0, "limit": 101}),
])
@pytest.mark.parametrize("include", [None, False, True])
def test_api_visibility(monkeypatch, path, module, name, result, kwargs, include):
    app = FastAPI()
    app.include_router(observability.router)
    app.include_router(session_export.router)
    app.dependency_overrides[require_basic_auth] = lambda: None
    app.dependency_overrides[session_export.authorize_session_export] = lambda: None
    mock = AsyncMock(return_value=result)
    monkeypatch.setattr(module, name, mock)
    params = {} if include is None else {"include_subagents": str(include).lower()}
    with TestClient(app) as client:
        response = client.get(path, params=params)
    assert response.status_code == 200
    mock.assert_awaited_once_with(
        **kwargs, **({"include_subagents": True} if include else {}),
    )
