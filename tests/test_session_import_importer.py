"""取り込みの保存境界、増分状態遷移、CLI 契約を検証する。"""

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from kotoyose import cli, db
from kotoyose.session_import import (
    FileLedgerEntry, ImportedTurn, ImportOptions, ImportSummary, PathSecurityError,
    SessionImportStore, build_interaction_row, compute_request_id, list_import_runs,
    list_import_sources, parse_since, run_import,
)
from kotoyose.session_import.models import MAX_TEXT_BYTES
from kotoyose.session_import.paths import default_source_roots

UTC = timezone.utc
STAMP = datetime(2026, 1, 1, tzinfo=UTC)
SECRET = "ghp_" + "a" * 36


def pair(kind, index=0, prompt="question"):
    start = (STAMP + timedelta(days=index)).isoformat()
    end = (STAMP + timedelta(days=index, seconds=1)).isoformat()
    if kind == "claude":
        return [
            {"type": "user", "sessionId": "c", "timestamp": start, "message": {"content": prompt}},
            {"type": "assistant", "timestamp": end, "message": {"content": "answer", "model": "claude-test"}},
        ]
    if kind == "codex":
        return [
            {"type": "event_msg", "timestamp": start, "payload": {"type": "user_message", "message": prompt}},
            {"type": "event_msg", "timestamp": end, "payload": {"type": "agent_message", "message": "answer"}},
        ]
    return [
        {"type": "USER_INPUT", "created_at": start, "content": prompt},
        {"type": "PLANNER_RESPONSE", "created_at": end, "content": "answer"},
    ]


def write(path, lines, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode) as stream:
        for line in lines:
            stream.write(json.dumps(line) + "\n")
    return path


@pytest.fixture
def sources(tmp_path):
    roots = default_source_roots(tmp_path)
    files = {}
    for kind, root in roots.items():
        relative = "s/.system_generated/logs/transcript.jsonl" if kind == "antigravity" else "s.jsonl"
        files[kind] = write(root / relative, pair(kind, prompt=SECRET))
    return tmp_path, files


def turn(kind="claude", **kwargs):
    return ImportedTurn(kind, "session", 0, STAMP, STAMP, "question", "answer", **kwargs)


def test_request_id_is_canonical_and_distinguishes_identity():
    value = compute_request_id("claude", "host", "s", 0, STAMP)
    assert value == compute_request_id("claude", "host", "s", 0, STAMP.astimezone(timezone(timedelta(hours=9))))
    assert value.startswith("import:claude:") and len(value.split(":")[-1]) == 32
    for args in [("codex", "host", "s", 0, STAMP), ("claude", "other", "s", 0, STAMP),
                 ("claude", "host", "s2", 0, STAMP), ("claude", "host", "s", 1, STAMP),
                 ("claude", "host", "s", 0, STAMP + timedelta(seconds=1))]:
        assert compute_request_id(*args) != value


@pytest.mark.parametrize("kind", ["claude", "codex", "antigravity"])
def test_row_mapping(kind):
    item = turn(kind, cwd_basename="project", tokens_in=0, has_tool_calls=True, timestamp_fallback=True)
    item.prompt = SECRET
    item.response = "あ" * MAX_TEXT_BYTES
    row, redacted, truncated = build_interaction_row(
        item, source_machine="host", source_path_hash="sha256:test", origin_hint="work", imported_at=STAMP,
    )
    assert redacted and truncated
    assert row["claude_session_id"] == ("session" if kind == "claude" else None)
    assert row["runtime_session_id"] == "session"
    assert row["conversation_key"] == f"import:{kind}:session"
    assert row["provider"] == kind and row["model"] == "unknown"
    assert row["source"] == f"import:{kind}"
    assert row["status"] == "completed" and row["streamed"] is False
    assert row["received_at"] == row["completed_at"] == STAMP
    assert row["tokens_in"] == 0 and row["tokens_out"] is None and row["tokens_source"] == "import"
    assert row["prompt"] == "[REDACTED]" and SECRET not in json.dumps(row, default=str)
    assert row["prompt_chars"] == len(row["prompt"]) and row["response_chars"] == len(row["response"])
    assert len(row["response"].encode()) <= MAX_TEXT_BYTES
    assert row["metadata"] == {
        "imported": True, "import_version": 1, "source_kind": kind, "source_machine": "host",
        "source_path_hash": "sha256:test", "project": "project", "origin_hint": "work", "turn_index": 0,
        "has_tool_calls": True, "truncated": True, "redacted": True,
        "imported_at": "2026-01-01T00:00:00Z", "timestamp_fallback": True,
    }
    clean, redacted, truncated = build_interaction_row(turn(kind), source_machine="h", source_path_hash="hash")
    assert not redacted and not truncated
    assert clean["tokens_source"] is None and "timestamp_fallback" not in clean["metadata"]


@pytest.mark.parametrize("value", ["2026-01-01", "2026-01-01T00:00:00Z", "2026-01-01T09:00:00+09:00", STAMP, STAMP.replace(tzinfo=None)])
def test_parse_since(value):
    assert parse_since(value) == STAMP


def test_parse_since_invalid():
    assert parse_since(None) is None
    for value in ("invalid", "2026-13-01", "", 123):
        with pytest.raises(ValueError):
            parse_since(value)


async def test_incremental_import_and_discovery(sources):
    home, files = sources
    store = SessionImportStore(pool=None)
    options = ImportOptions(source_machine="host")
    first = await run_import(options, home=home, store=store)
    assert (first.files_scanned, first.sessions_detected, first.turns_extracted, first.turns_imported) == (3, 3, 3, 3)
    assert first.redactions_count == 3 and not first.errors
    second = await run_import(options, home=home, store=store)
    assert second.files_skipped_unchanged == 3 and second.turns_imported == second.files_scanned == 0
    write(files["claude"], pair("claude", 1), mode="a")
    third = await run_import(options, home=home, store=store)
    assert (third.files_scanned, third.files_skipped_unchanged, third.turns_imported, third.turns_skipped_duplicate) == (1, 2, 1, 1)
    statuses = await list_import_sources(home=home, store=store)
    assert len(statuses) == 5
    by_kind = {s.source_kind: s for s in statuses}
    for kind in ("claude", "codex", "antigravity"):
        assert by_kind[kind].exists and by_kind[kind].file_count == by_kind[kind].imported_files == 1 and by_kind[kind].last_imported_at
    assert {s.source_kind: s.imported_turns for s in statuses} == {
        "claude": 2, "codex": 1, "antigravity": 1, "cursor": 0, "grok": 0,
    }
    assert (await store.source_ledger_stats("missing")) == {}
    runs = await list_import_runs(store=store, limit=2)
    assert [r["run_id"] for r in runs] == [third.run_id, second.run_id]
    assert await list_import_runs(store=store, limit=0) == []


async def test_dry_run_only_reads_and_counts_duplicates(sources):
    home, _ = sources
    store = SessionImportStore(pool=None)
    preview = await run_import(ImportOptions(dry_run=True, source_machine="h"), home=home, store=store)
    assert (preview.sessions_detected, preview.turns_extracted, preview.turns_imported, preview.redactions_count) == (3, 3, 3, 3)
    assert not store._files and not store._interactions and not store._runs
    await run_import(ImportOptions(source_machine="h"), home=home, store=store)
    snapshot = (dict(store._files), dict(store._interactions), list(store._runs))
    store.insert_interaction = AsyncMock(side_effect=AssertionError("dry run wrote interactions"))
    store.upsert_file_ledger = AsyncMock(side_effect=AssertionError("dry run wrote ledger"))
    store.record_run = AsyncMock(side_effect=AssertionError("dry run wrote run"))
    store.get_file_ledger = AsyncMock(side_effect=AssertionError("dry run read ledger"))
    preview = await run_import(ImportOptions(dry_run=True, source_machine="h"), home=home, store=store)
    assert preview.turns_imported == 0 and preview.turns_skipped_duplicate == 3
    assert preview.files_scanned == 3 and preview.files_skipped_unchanged == 0
    assert snapshot == (store._files, store._interactions, store._runs)


async def test_since_boundary_and_max_files(sources):
    home, files = sources
    write(files["claude"], pair("claude", 1), mode="a")
    summary = await run_import(ImportOptions(source="claude", dry_run=True, since=STAMP + timedelta(days=1, seconds=1)), home=home)
    assert summary.turns_skipped_since == 1 and summary.turns_extracted == summary.turns_imported == 1
    assert summary.sessions_detected == 1
    summary = await run_import(ImportOptions(dry_run=True, max_files=1), home=home)
    assert summary.files_scanned == 1
    summary = await run_import(ImportOptions(dry_run=True, max_files=0), home=home)
    assert summary.files_scanned == 0


async def test_validation_and_path_security(tmp_path):
    with pytest.raises(PathSecurityError):
        await run_import(ImportOptions(source="claude", path=tmp_path, allowed_roots=()), home=tmp_path)
    for options in [ImportOptions(path=tmp_path), ImportOptions(source="bad"), ImportOptions(origin_hint="bad"), ImportOptions(max_files=-1)]:
        with pytest.raises(ValueError):
            await run_import(options, home=tmp_path)


async def test_parser_error_does_not_leak_or_mark_ledger(sources, monkeypatch):
    from kotoyose.session_import import importer
    home, _ = sources
    parser = type("Broken", (), {"parse_file": lambda self, path: (_ for _ in ()).throw(OSError(SECRET))})()
    monkeypatch.setattr(importer, "get_parser", lambda kind: parser)
    store = SessionImportStore(pool=None)
    summary = await run_import(ImportOptions(), home=home, store=store)
    assert len(summary.errors) == 3 and SECRET not in str(summary.errors)
    assert not store._files and len(store._runs) == 1


class FakePool:
    def __init__(self):
        self.execute = AsyncMock()
        self.fetchrow = AsyncMock()
        self.fetch = AsyncMock(return_value=[])


async def test_postgres_store_sql_paths():
    pool = FakePool()
    store = SessionImportStore(pool)
    entry = FileLedgerEntry("claude", "h", "hash", 100, 200, last_imported_at=STAMP)
    pool.fetchrow.return_value = asdict(entry)
    assert await store.get_file_ledger("claude", "h", "hash") == entry
    assert "source_kind = $1 AND source_machine = $2 AND path_hash = $3" in pool.fetchrow.call_args.args[0]
    await store.upsert_file_ledger(entry)
    sql, *values = pool.execute.call_args.args
    assert "INSERT INTO kotoyose.session_import_files" in sql
    assert "ON CONFLICT (source_kind, source_machine, path_hash) DO UPDATE" in sql
    assert values == list(asdict(entry).values())
    row, _, _ = build_interaction_row(turn(), source_machine="h", source_path_hash="hash")
    pool.fetchrow.return_value = {"id": 1}
    assert await store.insert_interaction(row)
    sql, *values = pool.fetchrow.call_args.args
    assert "ON CONFLICT (request_id) DO NOTHING RETURNING id" in sql
    assert json.loads(values[-1]) == row["metadata"]
    pool.fetchrow.return_value = None
    assert not await store.insert_interaction(row)
    pool.fetch.return_value = [{"request_id": row["request_id"]}]
    assert await store.find_existing_request_ids([row["request_id"]]) == {row["request_id"]}
    assert "ANY($1::text[])" in pool.fetch.call_args.args[0]
    summary = ImportSummary(run_id="run", completed_at=STAMP)
    await store.record_run(summary)
    assert "INSERT INTO kotoyose.session_import_runs" in pool.execute.call_args.args[0]
    pool.fetch.return_value = [dict(asdict(summary), errors='[]')]
    assert (await store.list_runs())[0]["completed_at"] == "2026-01-01T00:00:00Z"
    assert "ORDER BY started_at DESC LIMIT $1" in pool.fetch.call_args.args[0]
    pool.fetch.return_value = [{"source_kind": "claude", "last_imported_at": STAMP, "imported_files": 1, "imported_turns": 3}]
    assert (await store.source_ledger_stats("h"))["claude"]["imported_turns"] == 3
    assert pool.fetch.call_args.args[1] == "h"


async def test_init_db_adds_idempotent_ddl(monkeypatch):
    from kotoyose.api_key_store import ApiKeyStore
    from kotoyose.connection_state_store import ConnectionStateStore
    from kotoyose.secret_store import SecretStore
    pool = FakePool()

    class Acquisition:
        async def __aenter__(self):
            return pool

        async def __aexit__(self, *args):
            pass

    pool.acquire = Acquisition
    monkeypatch.setattr(db, "DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setattr(db.asyncpg, "create_pool", AsyncMock(return_value=pool))
    for cls in (ApiKeyStore, ConnectionStateStore, SecretStore):
        monkeypatch.setattr(cls, "initialize", AsyncMock())
    await db.init_db()
    sql = " ".join(call.args[0] for call in pool.execute.call_args_list)
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_interactions_completed_id",
        "ON kotoyose.interactions (completed_at ASC, id ASC)", "WHERE completed_at IS NOT NULL",
        "CREATE TABLE IF NOT EXISTS kotoyose.session_import_files",
        "PRIMARY KEY (source_kind, source_machine, path_hash)",
        "CREATE TABLE IF NOT EXISTS kotoyose.session_import_runs",
        "CREATE INDEX IF NOT EXISTS idx_session_import_runs_started_at",
    ):
        assert statement in sql


def test_cli_dry_run_json(sources, monkeypatch, capsys):
    home, _ = sources
    from kotoyose.session_import import importer
    monkeypatch.setattr(db, "DATABASE_URL", None)
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setattr(importer, "default_source_roots", lambda _: default_source_roots(home))
    monkeypatch.setattr(importer, "configured_allowed_roots", lambda **_: tuple(default_source_roots(home).values()))
    assert cli.main(["import-sessions", "--dry-run", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] and result["sessions_detected"] == result["turns_imported"] == 3
    assert cli.main(["import-sessions"]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err
    assert cli.main(["import-sessions", "--dry-run", "--since", "bad"]) == 2
    assert "ISO" in capsys.readouterr().err
    assert cli.main(["import-sessions", "--dry-run", "--source", "claude", "--path", str(home.parent)]) == 2
    assert "allowed import roots" in capsys.readouterr().err


def test_cli_db_lifecycle(monkeypatch, capsys):
    ro_pool = FakePool()
    ro_pool.close = AsyncMock()
    monkeypatch.setattr(db, "DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(db, "init_db", AsyncMock())
    monkeypatch.setattr(db, "close_db", AsyncMock())
    monkeypatch.setattr(cli.asyncpg, "create_pool", AsyncMock(return_value=ro_pool))
    monkeypatch.setattr(cli, "run_import", AsyncMock(return_value=ImportSummary()))
    assert cli.main(["import-sessions", "--dry-run"]) == 0
    db.init_db.assert_not_awaited()
    ro_pool.close.assert_awaited_once()
    assert cli.main(["import-sessions"]) == 0
    db.init_db.assert_awaited_once()
    db.close_db.assert_awaited_once()
    monkeypatch.setattr(cli, "run_import", AsyncMock(side_effect=ValueError("bad path")))
    assert cli.main(["import-sessions"]) == 2
    assert db.close_db.await_count == 2


async def test_since_run_does_not_poison_ledger_for_full_run(sources):
    home, _ = sources
    store = SessionImportStore(pool=None)
    filtered = await run_import(
        ImportOptions(source="all", since=parse_since("2026-11-24T01:30:00Z")),
        home=home,
        store=store,
    )
    assert filtered.turns_skipped_since >= 1
    full = await run_import(ImportOptions(source="all"), home=home, store=store)
    assert full.turns_imported == filtered.turns_skipped_since
    assert len(store._interactions) == 3


async def test_postgres_dry_run_performs_only_duplicate_lookup(sources):
    home, _ = sources
    pool = FakePool()
    pool.fetch.return_value = []
    summary = await run_import(ImportOptions(dry_run=True), home=home, store=SessionImportStore(pool))
    assert summary.turns_imported == 3
    assert pool.fetch.await_count == 6
    assert all(
        "SELECT request_id" in call.args[0] or "SELECT DISTINCT COALESCE(runtime_session_id" in call.args[0]
        for call in pool.fetch.call_args_list
    )
    pool.execute.assert_not_awaited()
    pool.fetchrow.assert_not_awaited()


async def test_ledger_db_failure_propagates(sources):
    home, _ = sources
    pool = FakePool()
    pool.fetchrow.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        await run_import(ImportOptions(), home=home, store=SessionImportStore(pool))
