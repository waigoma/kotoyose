"""管理インポート API の認証、パス境界、dry-run、台帳、ルート優先順位の契約。"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Match

from kotoyose import auth, db
from kotoyose.api_key_store import ApiKeyStore
from kotoyose.config import Client, HarnessConfig
from kotoyose.routes import admin_imports, api_keys, session_export

STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)
ADMIN = ("admin", "admin-password")


def claude_lines(prompt="question"):
    return [
        {"type": "user", "sessionId": "claude-session", "timestamp": STAMP.isoformat(),
         "message": {"content": prompt}},
        {"type": "assistant", "timestamp": STAMP.replace(second=1).isoformat(),
         "message": {"content": "answer", "model": "claude-test"}},
    ]


def write_claude_file(home: Path, name: str = "s.jsonl", prompt: str = "question") -> Path:
    path = home / ".claude" / "projects" / "proj" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as stream:
        for line in claude_lines(prompt):
            stream.write(json.dumps(line) + "\n")
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    """既定ソースと許可ルートを試験用ホームへ向ける。"""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.delenv("KOTOYOSE_IMPORT_ALLOWED_ROOTS", raising=False)
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig())
    monkeypatch.setattr(db, "_pool", None)
    return home_dir


@pytest.fixture
def app(home, monkeypatch):
    monkeypatch.setattr(auth, "API_KEY", "admin-password")
    monkeypatch.setattr(auth, "BASIC_AUTH_USER", "admin")
    app = FastAPI()
    app.include_router(admin_imports.router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_all_endpoints_require_basic_auth(client, home):
    write_claude_file(home)
    assert client.get("/admin/imports/sources").status_code == 401
    assert client.get("/admin/imports/runs").status_code == 401
    assert client.post("/admin/imports/run", json={}).status_code == 401
    assert client.get("/admin/imports/sources", auth=("admin", "wrong")).status_code == 401
    response = client.get("/admin/imports/runs")
    assert response.headers["www-authenticate"].startswith("Basic")


def test_sources_reports_detected_files_and_allowed_roots(client, home):
    write_claude_file(home)
    response = client.get("/admin/imports/sources", auth=ADMIN)
    assert response.status_code == 200
    body = response.json()
    sources = {source["source_kind"]: source for source in body["sources"]}
    assert set(sources) == {"claude", "codex", "antigravity", "cursor", "grok"}
    assert sources["claude"]["exists"] is True
    assert sources["claude"]["file_count"] == 1
    assert sources["codex"]["exists"] is False
    assert sources["codex"]["file_count"] == 0
    assert str(home / ".claude" / "projects") in body["allowed_roots"]
    assert str(home / ".codex" / "sessions") in body["allowed_roots"]


def test_dry_run_reports_summary_without_persisting(client, home):
    write_claude_file(home)
    response = client.post("/admin/imports/run", auth=ADMIN, json={"source_kind": "claude", "dry_run": True})
    assert response.status_code == 200
    summary = response.json()
    assert summary["dry_run"] is True
    assert summary["files_scanned"] == 1
    assert summary["turns_extracted"] == 1
    assert summary["turns_imported"] == 1
    assert summary["sessions_detected"] == 1
    assert client.get("/admin/imports/runs", auth=ADMIN).json() == {"runs": []}

    # dry-run は台帳を書かないので、続く本実行は同じターンを取り込める。
    real = client.post("/admin/imports/run", auth=ADMIN, json={"source_kind": "claude"}).json()
    assert real["turns_imported"] == 1


def test_real_run_imports_records_run_and_second_run_skips_unchanged(client, home):
    write_claude_file(home)
    first = client.post("/admin/imports/run", auth=ADMIN, json={"source_kind": "claude"})
    assert first.status_code == 200
    assert first.json()["dry_run"] is False
    assert first.json()["turns_imported"] == 1

    runs = client.get("/admin/imports/runs", auth=ADMIN, params={"limit": 5}).json()["runs"]
    assert [run["run_id"] for run in runs] == [first.json()["run_id"]]

    second = client.post("/admin/imports/run", auth=ADMIN, json={"source_kind": "claude"}).json()
    assert second["files_skipped_unchanged"] == 1
    assert second["files_scanned"] == 0
    assert second["turns_imported"] == 0

    runs = client.get("/admin/imports/runs", auth=ADMIN).json()["runs"]
    assert len(runs) == 2

    sources = client.get("/admin/imports/sources", auth=ADMIN).json()["sources"]
    claude = next(source for source in sources if source["source_kind"] == "claude")
    assert claude["imported_turns"] == 1
    assert claude["imported_files"] == 1


def test_runs_limit_is_validated(client):
    assert client.get("/admin/imports/runs", auth=ADMIN, params={"limit": 0}).status_code == 422
    assert client.get("/admin/imports/runs", auth=ADMIN, params={"limit": 201}).status_code == 422


def test_path_outside_allowed_roots_is_forbidden(client, home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.jsonl").write_text(json.dumps(claude_lines()[0]) + "\n")
    response = client.post(
        "/admin/imports/run", auth=ADMIN,
        json={"source_kind": "claude", "path": str(outside), "dry_run": True},
    )
    assert response.status_code == 403
    assert client.get("/admin/imports/runs", auth=ADMIN).json() == {"runs": []}


def test_path_inside_allowed_root_is_accepted(client, home):
    path = write_claude_file(home)
    response = client.post(
        "/admin/imports/run", auth=ADMIN,
        json={"source_kind": "claude", "path": str(path.parent), "dry_run": True},
    )
    assert response.status_code == 200
    assert response.json()["files_scanned"] == 1


@pytest.mark.parametrize("body", [
    {"source_kind": "bogus"},
    {"source_kind": "claude", "origin_hint": "public"},
    {"source_kind": "claude", "since": "not-a-date"},
    {"source_kind": "claude", "max_files": 0},
    {"source_kind": "claude", "unexpected": True},
])
def test_invalid_run_requests_are_unprocessable(client, home, body):
    write_claude_file(home)
    response = client.post("/admin/imports/run", auth=ADMIN, json=body)
    assert response.status_code == 422
    assert client.get("/admin/imports/runs", auth=ADMIN).json() == {"runs": []}


def test_in_memory_store_is_shared_across_requests(app, client, home):
    write_claude_file(home)
    client.post("/admin/imports/run", auth=ADMIN, json={"source_kind": "claude"})
    assert app.state.session_import_store is not None
    assert len(client.get("/admin/imports/runs", auth=ADMIN).json()["runs"]) == 1


# --- /v1/sessions/export と /v1/sessions/{conversation_key} のルート優先順位 ---


@pytest.fixture
def real_app(monkeypatch):
    from kotoyose.app import app as kotoyose_app

    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(clients={
        "exporter": Client(models=[], scopes=["sessions:export"]),
        "runner": Client(models=["*"]),
    }))
    monkeypatch.setattr(auth, "API_KEY", None)
    store = ApiKeyStore()
    monkeypatch.setattr(kotoyose_app.state, "api_key_store", store)
    monkeypatch.setattr(session_export, "export_interactions", AsyncMock(return_value=[]))
    monkeypatch.setattr(api_keys, "get_scoped_session_turns", AsyncMock(return_value=[]))
    return kotoyose_app, store


def _first_matching_route(app, path):
    scope = {"type": "http", "path": path, "method": "GET", "root_path": "", "headers": []}
    for route in app.router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return route
    raise AssertionError(f"no route matches {path}")


@pytest.mark.asyncio
async def test_export_route_precedes_client_session_route(real_app):
    app, store = real_app
    _, exporter_token = await store.create("exporter-key", "exporter")
    _, runner_token = await store.create("runner-key", "runner")

    export_route = _first_matching_route(app, "/v1/sessions/export")
    assert export_route.endpoint is session_export.export_sessions
    detail_route = _first_matching_route(app, "/v1/sessions/my-conv-key")
    assert detail_route.endpoint is api_keys.session_detail

    client = TestClient(app)
    denied = client.get("/v1/sessions/export", headers={"Authorization": f"Bearer {runner_token}"})
    assert denied.status_code == 403

    allowed = client.get("/v1/sessions/export", headers={"Authorization": f"Bearer {exporter_token}"})
    assert allowed.status_code == 200
    assert allowed.json()["schema_version"] == "1"

    detail = client.get("/v1/sessions/my-conv-key", headers={"Authorization": f"Bearer {runner_token}"})
    assert detail.status_code == 200
    assert detail.json()["conversation_key"] == "my-conv-key"
