"""Container GC must not erase the evidence needed to confirm a sandbox stop."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from kotoyose.execution_store import ExecutionStore
from tests.test_local_worker import _reaper_case, _reaper_entry
from tests.test_execution_store import _admit_ready, store as postgres_store


class _Pool:
    """Exercise the store's SQL joins/predicates with only unnest adapted for SQLite.

    This is an offline query test, not a PostgreSQL or Docker integration test.
    SQLite integer booleans are normalized to asyncpg's bool result type.
    """

    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            ATTACH DATABASE ':memory:' AS kotoyose;
            CREATE TABLE candidates (id TEXT);
            CREATE TABLE kotoyose.execution_attempts (
                id TEXT PRIMARY KEY, generation INTEGER, finished_at TEXT,
                stop_confirmation TEXT
            );
            CREATE TABLE kotoyose.execution_operations (
                id TEXT PRIMARY KEY, attempt_id TEXT, kind TEXT, state TEXT
            );
        """)

    def attempt(self, attempt_id="parent", *, generation=0, finished=True, stop="confirmed"):
        self.connection.execute(
            "INSERT INTO kotoyose.execution_attempts VALUES (?, ?, ?, ?)",
            (attempt_id, generation, "2026-10-01T00:00:00Z" if finished else None, stop),
        )

    def operation(self, operation_id="sandbox", *, attempt_id="parent", kind="sandbox_command", state="confirmed"):
        self.connection.execute(
            "INSERT INTO kotoyose.execution_operations VALUES (?, ?, ?, ?)",
            (operation_id, attempt_id, kind, state),
        )

    async def fetch(self, query, ids):
        assert "FROM unnest($1::text[]) AS candidate(id) " in query
        self.connection.execute("DELETE FROM candidates")
        self.connection.executemany("INSERT INTO candidates VALUES (?)", [(value,) for value in ids])
        query = query.replace("FROM unnest($1::text[]) AS candidate(id) ", "FROM candidates AS candidate ")
        rows = [dict(row) for row in self.connection.execute(query)]
        for row in rows:
            if row.get("cleanup_ready") is not None:
                row["cleanup_ready"] = bool(row["cleanup_ready"])
        return rows


@pytest.fixture
def pool():
    value = _Pool()
    yield value
    value.connection.close()


def _store(pool):
    store = ExecutionStore.__new__(ExecutionStore)
    store.pool = pool
    return store


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["prepared", "sent", "unknown", "unconfirmed", "Confirmed", "", None])
async def test_gc_retains_terminal_sandbox_without_exact_confirmed_operation(pool, state):
    pool.attempt()
    pool.operation(state=state)
    assert await _store(pool).container_attempt_owners(["sandbox"]) == {"sandbox": ("parent", False, 0)}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["provider_process", "local_execution", "client_tool", "", None])
async def test_gc_retains_operation_with_wrong_or_malformed_kind(pool, kind):
    pool.attempt()
    pool.operation(kind=kind)
    assert await _store(pool).container_attempt_owners(["sandbox"]) == {"sandbox": ("parent", False, 0)}


@pytest.mark.asyncio
@pytest.mark.parametrize("finished,stop", [(False, "confirmed"), (True, "unconfirmed"), (True, ""), (True, None)])
async def test_gc_retains_confirmed_sandbox_until_parent_terminal_stop_is_persisted(pool, finished, stop):
    pool.attempt(finished=finished, stop=stop)
    pool.operation()
    assert await _store(pool).container_attempt_owners(["sandbox"]) == {"sandbox": ("parent", False, 0)}


@pytest.mark.asyncio
async def test_gc_missing_exact_operation_cannot_borrow_another_operations_confirmation(pool):
    pool.attempt()
    pool.operation("other-operation")
    assert await _store(pool).container_attempt_owners(["sandbox"]) == {}


@pytest.mark.asyncio
async def test_gc_missing_parent_cannot_authorize_confirmed_operation(pool):
    pool.operation()
    assert await _store(pool).container_attempt_owners(["sandbox"]) == {}


@pytest.mark.asyncio
async def test_gc_ambiguous_attempt_and_operation_identity_is_retained(pool):
    pool.attempt()
    pool.attempt("sandbox")
    pool.operation()
    assert await _store(pool).container_attempt_owners(["sandbox"]) == {"sandbox": ("parent", False, 0)}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["confirmed", "unknown", "sent", None])
async def test_gc_direct_command_requires_its_exact_local_execution_evidence(pool, state):
    pool.attempt()
    pool.operation("execution:parent", kind="local_execution", state=state)
    assert await _store(pool).container_attempt_owners(["parent"]) == {"parent": ("parent", state == "confirmed", 0)}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation_id,attempt_id,kind", [
    ("other", "parent", "local_execution"),
    ("execution:parent", "other-parent", "local_execution"),
    ("execution:parent", "parent", "sandbox_command"),
])
async def test_gc_direct_command_cannot_borrow_mismatched_local_evidence(pool, operation_id, attempt_id, kind):
    pool.attempt()
    pool.operation(operation_id, attempt_id=attempt_id, kind=kind)
    assert await _store(pool).container_attempt_owners(["parent"]) == {"parent": ("parent", False, 0)}


@pytest.mark.asyncio
async def test_gc_direct_command_missing_operation_is_retained(pool):
    pool.attempt()
    assert await _store(pool).container_attempt_owners(["parent"]) == {"parent": ("parent", False, 0)}


@pytest.mark.asyncio
async def test_gc_waits_for_durable_sandbox_confirmation_then_removes_exact_container(pool):
    pool.attempt()
    pool.operation(state="unknown")
    entry = _reaper_entry("sandbox", finished_at=datetime.now(timezone.utc) - timedelta(days=30))
    service, calls, fake = _reaper_case([entry], {})
    service.store = _store(pool)
    with patch("asyncio.create_subprocess_exec", new=fake):
        assert await service._reap_containers_once() == 0
        assert not [call for call in calls if call[1] == "rm"]
        pool.connection.execute("UPDATE kotoyose.execution_operations SET state='confirmed' WHERE id='sandbox'")
        assert await service._reap_containers_once() == 1
    assert [call[2] for call in calls if call[1] == "rm"] == ["id-sandbox"]


@pytest.mark.asyncio
@pytest.mark.parametrize("generation,active,expected", [(0, (), 1), (1, (), 0), (0, ("parent",), 0)])
async def test_gc_requires_matching_generation_and_no_active_parent(pool, generation, active, expected):
    pool.attempt(generation=generation)
    pool.operation()
    service, calls, fake = _reaper_case([_reaper_entry("sandbox")], {}, active=active)
    service.store = _store(pool)
    with patch("asyncio.create_subprocess_exec", new=fake):
        assert await service._reap_containers_once() == expected
    assert len([call for call in calls if call[1] == "rm"]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [
    None, ("parent", True), ["parent", True, 0], ("", True, 0),
    (None, True, 0), ("parent", True, None), ("parent", True, "0"),
    ("parent", True, False), ("parent", True, -1), ("parent", "confirmed", 0),
    ("parent", 1, 0), ("parent", None, 0),
])
async def test_gc_malformed_or_missing_owner_never_becomes_confirmation(owner):
    entry = _reaper_entry("sandbox", finished_at=datetime.now(timezone.utc) - timedelta(days=30))
    service, calls, fake = _reaper_case([entry], {"sandbox": owner})
    with patch("asyncio.create_subprocess_exec", new=fake):
        assert await service._reap_containers_once() == 0
    assert not [call for call in calls if call[1] == "rm"]


@pytest.mark.asyncio
async def test_gc_retention_diagnostic_is_one_bounded_count_per_batch():
    entries = [_reaper_entry(f"missing-{index}") for index in range(8)]
    service, calls, fake = _reaper_case(entries, {}, batch_size=2)
    with patch("asyncio.create_subprocess_exec", new=fake), patch("kotoyose.execution_service.log") as logger:
        assert await service._reap_containers_once() == 0
    logger.info.assert_called_once_with("execution.container_cleanup_retained", reasons={
        "missing_owner": 2, "malformed_owner": 0, "unconfirmed_stop": 0, "generation_mismatch": 0,
    })
    assert len([call for call in calls if call[1] == "inspect"]) == 2
    assert not [call for call in calls if call[1] == "rm"]


@pytest.mark.asyncio
async def test_gc_postgres_exact_persisted_confirmation(postgres_store, tmp_path):
    """Opt-in: run unchanged SQL against asyncpg and the existing isolated test DB.

    Reuses the owned-store fixture, which refuses non-test database names and
    truncates/drops test tables. Never point this at a service database.
    """
    _, _, request, snapshot = await _admit_ready(postgres_store, tmp_path)
    attempt, _ = await postgres_store.admit("caller-a", request, snapshot, 1)
    attempt_id = attempt["id"]
    generation = attempt["generation"]
    assert await postgres_store.mark_running(attempt_id)
    operation_id = await postgres_store.prepare_operation(attempt_id, "sandbox_command")
    ids = [attempt_id, operation_id, "missing-operation"]
    denied = (attempt_id, False, generation)
    allowed = (attempt_id, True, generation)

    await postgres_store.resolve_operation(operation_id, "confirmed")
    assert await postgres_store.container_attempt_owners(ids) == {attempt_id: denied, operation_id: denied}
    await postgres_store.resolve_operation(operation_id, "unconfirmed")
    await postgres_store.finish(attempt_id, "failed", "worker_lost", "confirmed", None, {})
    assert await postgres_store.container_attempt_owners(ids) == {attempt_id: allowed, operation_id: denied}
    await postgres_store.resolve_operation(operation_id, "confirmed")
    assert await postgres_store.container_attempt_owners(ids) == {attempt_id: allowed, operation_id: allowed}

    await postgres_store.pool.execute(
        "UPDATE kotoyose.execution_operations SET kind='provider_process' WHERE id=$1", operation_id,
    )
    assert (await postgres_store.container_attempt_owners(ids))[operation_id] == denied
    await postgres_store.pool.execute("DELETE FROM kotoyose.execution_operations WHERE id=$1", operation_id)
    assert await postgres_store.container_attempt_owners(ids) == {attempt_id: allowed}
    await postgres_store.pool.execute(
        "UPDATE kotoyose.execution_operations SET state='unknown' WHERE id=$1", "execution:" + attempt_id,
    )
    assert await postgres_store.container_attempt_owners(ids) == {attempt_id: denied}
