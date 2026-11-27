"""Child imports: auth, policy failures, classification and persistence."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kotoyose import auth, db
from kotoyose.routes import admin_imports
from kotoyose.session_import import SessionImportStore
from kotoyose.session_import.models import FileLedgerEntry

ADMIN = ("admin", "password")
BASE = "/admin/imports/children"


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(auth, "API_KEY", "password")
    monkeypatch.setattr(auth, "BASIC_AUTH_USER", "admin")
    monkeypatch.setattr(db, "_pool", None)
    app = FastAPI()
    app.include_router(admin_imports.router)
    store = SessionImportStore(None)
    app.state.session_import_store = store
    hub = SimpleNamespace(
        session_collect_children=Mock(return_value=[{"name": "laptop"}, {"name": "empty"}]),
        registry=SimpleNamespace(set_origin_hint=Mock()),
        collect_sessions=AsyncMock(return_value=([], {}, None)),
    )
    app.state.child_hub = hub
    return app, TestClient(app), hub, store


def test_auth_and_no_hub(setup):
    app, client, _, _ = setup
    for method, path in [("get", BASE), ("put", BASE + "/laptop/origin-hint"), ("post", BASE + "/laptop/collect")]:
        assert getattr(client, method)(path).status_code == 401
    app.state.child_hub = None
    assert client.get(BASE, auth=ADMIN).json() == {"children": []}
    for method, suffix in [("put", "origin-hint"), ("post", "collect")]:
        response = getattr(client, method)(f"{BASE}/laptop/{suffix}", auth=ADMIN, json={})
        assert response.status_code == 404
        assert response.json()["detail"] == "Child hub is not enabled"


@pytest.mark.asyncio
async def test_children_ledger_totals(setup):
    _, client, hub, store = setup
    for kind, machine, turns, day in [("claude", "laptop", 2, 1), ("codex", "laptop", 3, 2), ("codex", "other", 99, 3)]:
        await store.upsert_file_ledger(FileLedgerEntry(
            kind, machine, kind, 1, 1, imported_count=turns,
            last_imported_at=datetime(2026, 1, day, tzinfo=timezone.utc),
        ))
    response = client.get(BASE, auth=ADMIN)
    assert response.status_code == 200
    assert response.json()["children"] == [
        {"name": "laptop", "imported_files": 2, "imported_turns": 5, "last_imported_at": "2026-01-02T00:00:00Z"},
        {"name": "empty", "imported_files": 0, "imported_turns": 0, "last_imported_at": None},
    ]
    assert hub.session_collect_children.return_value == [{"name": "laptop"}, {"name": "empty"}]


@pytest.mark.parametrize("hint", [None, "personal", "work"])
def test_set_origin(setup, hint):
    _, client, hub, _ = setup
    response = client.put(BASE + "/laptop/origin-hint", auth=ADMIN, json={"origin_hint": hint})
    assert response.status_code == 200
    assert response.json() == {"name": "laptop", "origin_hint": hint}
    hub.registry.set_origin_hint.assert_called_once_with("laptop", hint)


@pytest.mark.parametrize("error,code", [(KeyError("unknown"), 404), (ValueError("invalid"), 422)])
def test_set_origin_errors(setup, error, code):
    _, client, hub, _ = setup
    hub.registry.set_origin_hint.side_effect = error
    assert client.put(BASE + "/unknown/origin-hint", auth=ADMIN, json={}).status_code == code


@pytest.mark.parametrize("suffix,body", [
    ("origin-hint", {"origin_hint": "public"}), ("origin-hint", {"extra": 1}),
    ("collect", {"origin_hint": "public"}), ("collect", {"extra": 1}),
    ("collect", {"since": "bad"}), ("collect", {"max_files": 0}),
    ("collect", {"max_files": 2001}), ("collect", {"max_turns": 0}),
    ("collect", {"max_turns": 20001}),
])
def test_validation(setup, suffix, body):
    _, client, hub, _ = setup
    method = client.put if suffix == "origin-hint" else client.post
    assert method(f"{BASE}/laptop/{suffix}", auth=ADMIN, json=body).status_code == 422
    hub.collect_sessions.assert_not_called()
    hub.registry.set_origin_hint.assert_not_called()


@pytest.mark.parametrize("error,code", [
    (KeyError("unknown"), 404), (PermissionError("policy"), 403),
    (ValueError("source"), 422), (RuntimeError("offline"), 503), (TimeoutError("timeout"), 503),
])
def test_collect_errors(setup, error, code):
    _, client, hub, _ = setup
    hub.collect_sessions.side_effect = error
    assert client.post(BASE + "/laptop/collect", auth=ADMIN, json={}).status_code == code


@pytest.mark.asyncio
@pytest.mark.parametrize("hub_hint,request_hint,child_hint", [
    ("work", "personal", None), ("personal", "work", None), (None, "personal", "work"),
])
async def test_collect_dry_then_real(setup, hub_hint, request_hint, child_hint):
    _, client, hub, store = setup
    batch = {"source_kind": "claude", "source_path_hash": "hash", "file_mtime_ns": 1,
             "file_size": 10, "last_turn_index": 0, "turns": [{
                 "session_id": "session", "turn_index": 0, "prompt": "hello", "response": "hi",
                 "received_at": "2026-01-02T00:00:00Z", "completed_at": "2026-01-02T00:00:01Z",
             }]}
    hub.collect_sessions.return_value = ([batch], {"child_origin_hint": child_hint}, hub_hint)
    body = {"origin_hint": request_hint, "since": "2026-01-01T09:00:00+09:00", "dry_run": True,
            "max_files": 2000, "max_turns": 20000}
    for dry in [True, False]:
        body["dry_run"] = dry
        response = client.post(BASE + "/laptop/collect", auth=ADMIN, json=body)
        assert response.status_code == 200
        result = response.json()
        assert result["turns_imported"] == 1
        assert result["origin_hint"] == "work"
        assert result["source_machine"] == "laptop"
        assert result["dry_run"] is dry
        hub.collect_sessions.assert_awaited_with(
            "laptop", source_kind="all", since="2026-01-01T00:00:00Z", max_files=2000, max_turns=20000,
        )
        runs = client.get("/admin/imports/runs", auth=ADMIN).json()["runs"]
        ledger = await store.get_file_ledger("claude", "laptop", "hash")
        if dry:
            assert runs == []
            assert ledger is None
            assert store._interactions == {}
        else:
            assert len(runs) == 1
            assert runs[0]["run_id"] == result["run_id"]
            assert ledger.imported_count == 1
            row = next(iter(store._interactions.values()))
            assert row["metadata"]["origin_hint"] == "work"
            assert row["metadata"]["source_machine"] == "laptop"
