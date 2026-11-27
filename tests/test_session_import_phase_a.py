"""Phase A discovery, metadata, live deduplication, and CLI contracts."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kotoyose import cli, db
from kotoyose.session_import import importer
from kotoyose.session_import.models import ImportedTurn, ImportOptions, ImportSummary
from kotoyose.session_import.paths import PathSecurityError, default_source_roots, iter_safe_source_files

STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_turn(session_id="sess-live", **kwargs):
    return ImportedTurn("claude", session_id, 0, STAMP, STAMP, "question", "answer", **kwargs)


def test_default_roots(tmp_path):
    assert default_source_roots(tmp_path) == {
        "claude": tmp_path / ".claude/projects",
        "codex": tmp_path / ".codex/sessions",
        "antigravity": tmp_path / ".gemini/antigravity-cli/brain",
        "cursor": tmp_path / ".cursor/chats",
        "grok": tmp_path / ".grok/sessions",
    }


@pytest.mark.parametrize(("kind", "filename"), [("cursor", "store.db"), ("grok", "chat_history.jsonl")])
def test_discovery_and_symlink_boundaries(tmp_path, kind, filename):
    root = default_source_roots(tmp_path)[kind]
    nested = root / "session"
    nested.mkdir(parents=True)
    target = nested / filename
    target.touch()
    wrong = root / "unrelated.jsonl"
    wrong.touch()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / filename).touch()
    (root / filename).symlink_to(outside / filename)
    (root / "linked-dir").symlink_to(outside, target_is_directory=True)
    assert iter_safe_source_files(kind, root, [root]) == [target]
    assert iter_safe_source_files(kind, target, [root]) == [target]
    assert iter_safe_source_files(kind, wrong, [root]) == []
    with pytest.raises(PathSecurityError):
        iter_safe_source_files(kind, root / filename, [root])
    with pytest.raises(PathSecurityError):
        iter_safe_source_files(kind, root / "linked-dir", [root])


@pytest.mark.parametrize("is_subagent", [False, True])
@pytest.mark.parametrize("agent_origin", [None, "", "parent-agent"])
def test_subagent_metadata(is_subagent, agent_origin):
    row, _, _ = importer.build_interaction_row(
        make_turn(is_subagent=is_subagent, agent_origin=agent_origin),
        source_machine="host", source_path_hash="hash",
    )
    metadata = row["metadata"]
    assert ("is_subagent" in metadata) == is_subagent
    if is_subagent:
        assert metadata["is_subagent"] is True
    assert ("agent_origin" in metadata) == (agent_origin is not None)
    if agent_origin is not None:
        assert metadata["agent_origin"] == agent_origin


class FakePool:
    def __init__(self):
        self.fetch = AsyncMock(side_effect=self.select)
        self.fetchrow = AsyncMock(side_effect=self.select_row)
        self.execute = AsyncMock()
        self.inserted = []

    async def select(self, query, *args):
        if "COALESCE(runtime_session_id, claude_session_id)" in query:
            assert "provider = $1" in query
            assert "ANY($2::text[])" in query
            assert "source IS NULL OR source NOT LIKE 'import:%'" in query
            assert "deleted_at IS NULL" in query
            assert args == ("claude", ["sess-live", "sess-new"])
            return [{"session_id": "sess-live"}]
        assert "SELECT request_id" in query
        return []

    async def select_row(self, query, *args):
        if "INSERT INTO kotoyose.interactions" in query:
            self.inserted.append(dict(zip(importer._INTERACTION_COLUMNS, args)))
            return {"id": len(self.inserted)}
        return None


@pytest.mark.parametrize("postgres", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("skip_live", [False, True])
async def test_live_dedup_run(tmp_path, monkeypatch, postgres, dry_run, skip_live):
    path = tmp_path / "history.jsonl"
    path.touch()
    turns = [make_turn(), make_turn("sess-new")]
    monkeypatch.setattr(importer, "get_parser", lambda _: SimpleNamespace(parse_file=lambda _: turns))
    pool = FakePool() if postgres else None
    store = importer.SessionImportStore(pool)
    live = {"provider": "claude", "runtime_session_id": "sess-live", "source": "chat"}
    store._interactions["live"] = live
    summary = await importer.run_import(
        ImportOptions(source="claude", path=path, allowed_roots=(tmp_path,),
                      dry_run=dry_run, skip_live_duplicates=skip_live),
        store=store,
    )
    assert not summary.errors
    assert summary.sessions_detected == summary.turns_extracted == 2
    assert summary.turns_skipped_duplicate == int(skip_live)
    assert summary.turns_imported == 2 - int(skip_live)
    inserted = pool.inserted if postgres else [row for key, row in store._interactions.items() if key != "live"]
    assert len(inserted) == (0 if dry_run else summary.turns_imported)
    if skip_live:
        assert all(row["runtime_session_id"] != "sess-live" for row in inserted)
    if dry_run:
        assert not store._files and not store._runs
        if postgres:
            pool.execute.assert_not_awaited()
            pool.fetchrow.assert_not_awaited()


async def test_memory_lookup_filters_and_fallback():
    store = importer.SessionImportStore(None)
    rows = [
        {"runtime_session_id": "runtime", "claude_session_id": "ignored", "source": "chat"},
        {"runtime_session_id": None, "claude_session_id": "legacy", "source": None},
        {"runtime_session_id": "imported", "source": "import:claude"},
        {"runtime_session_id": "deleted", "source": "chat", "deleted_at": STAMP},
        {"runtime_session_id": "other", "source": "chat", "provider": "codex"},
        {"runtime_session_id": "", "claude_session_id": "ignored", "source": "chat"},
    ]
    store._interactions = {str(i): {"provider": "claude", **row} for i, row in enumerate(rows)}
    assert await store.find_live_session_ids("claude", []) == set()
    assert await store.find_live_session_ids(
        "claude", ["runtime", "legacy", "imported", "deleted", "other", "ignored", ""],
    ) == {"runtime", "legacy", ""}


async def test_postgres_lookup_empty_and_missing_table():
    pool = SimpleNamespace(fetch=AsyncMock())
    store = importer.SessionImportStore(pool)
    assert await store.find_live_session_ids("claude", []) == set()
    pool.fetch.assert_not_awaited()

    class MissingTable(Exception):
        sqlstate = "42P01"

    pool.fetch.side_effect = MissingTable()
    assert await store.find_live_session_ids("claude", ["session"]) == set()
    pool.fetch.side_effect = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        await store.find_live_session_ids("claude", ["session"])


@pytest.mark.parametrize("source", ["cursor", "grok"])
@pytest.mark.parametrize("include_live", [False, True])
def test_cli_source_and_live_override(monkeypatch, source, include_live, capsys):
    mocked = AsyncMock(return_value=ImportSummary(dry_run=True))
    monkeypatch.setattr(cli, "run_import", mocked)
    monkeypatch.setattr(db, "DATABASE_URL", None)
    args = ["import-sessions", "--source", source, "--dry-run"]
    if include_live:
        args.append("--include-live-duplicates")
    assert cli.main(args) == 0
    options = mocked.call_args.args[0]
    assert options.source == source
    assert options.skip_live_duplicates is not include_live
    capsys.readouterr()
