"""専用 PostgreSQL DB で証拠保存の原子性と通常 reconcile の writer 境界を検証する。"""

import os
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest

from kotoyose.codex_stop_evidence import STAGES, StopComponents, make_stop_evidence, stop_evidence_context
from kotoyose.execution_resources import ExecutionError
from kotoyose.execution_service import ExecutionService
from kotoyose.execution_store import ExecutionStore
from tests.test_execution_store import _admit_ready


@pytest.fixture
async def journal(tmp_path):
    url = os.environ.get("KOTOYOSE_RUNTIME_IDENTITY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Isolated CI test database is not configured")
    assert urlparse(url).path.lstrip("/").startswith("ahs_execution_test"), "refusing a non-test database"
    database = "ahs_execution_test_stop_" + uuid4().hex
    admin = await asyncpg.connect(url)
    pool = None
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        pool = await asyncpg.create_pool(urlparse(url)._replace(path="/" + database).geturl(), min_size=1, max_size=2)
        await pool.execute("CREATE SCHEMA kotoyose")
        store = ExecutionStore(pool)
        await store.initialize()
        workspace, _, request, snapshot = await _admit_ready(store, tmp_path)
        snapshot.update(kind="agent", backend="native", provider="codex", codex_execution_mode="host_native_coding_v1",
                        workspace_device=workspace["device"], workspace_inode=workspace["inode"])
        attempt, _ = await store.admit("caller-a", request, snapshot, 4)
        await pool.execute("UPDATE kotoyose.execution_attempts SET status='running' WHERE id=$1", attempt["id"])
        operation = await store.prepare_operation(attempt["id"], "provider_process")
        ownership = {"pid": 11, "start_ticks": 12, "boot_id": "synthetic", "uid": 1000,
                     "codex_provider_scope": {"unit": f"kotoyose-native-{attempt['id']}-provider-0.scope",
                        "control_group": "/synthetic-owned-scope", "device": 31, "inode": 32,
                        "namespace_init": {"pid": 21, "start_ticks": 22, "boot_id": "synthetic", "uid": 1000}}}
        await store.record_process(operation, ownership)
        proof = make_stop_evidence(stop_evidence_context(attempt), ownership,
                                  StopComponents(**dict.fromkeys(STAGES, "confirmed")), 0)
        yield store, attempt, operation, proof
    finally:
        if pool is not None: await pool.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.close()


async def test_receipt_and_sanitized_event_are_atomic_immutable_and_idempotent(journal):
    store, attempt, operation, proof = journal
    await store.record_codex_stop(operation, proof.model_dump())
    await store.record_codex_stop(operation, proof.model_dump())
    saved = next(row for row in await store.operations(attempt["id"]) if row["id"] == operation)
    assert saved["result"]["codex_stop_evidence"] == proof.model_dump()
    events = await store.events("caller-a", attempt["id"], 0, 100)
    diagnostics = [row["payload"] for row in events if row["type"] == "codex_stop_diagnostic"]
    assert diagnostics == [proof.diagnostic()]
    changed = make_stop_evidence(stop_evidence_context(attempt),
        {key: value for key, value in saved["result"].items() if key != "codex_stop_evidence"},
        StopComponents(**{**dict.fromkeys(STAGES, "confirmed"), "scope": "unconfirmed"}), 0)
    with pytest.raises(ExecutionError, match="immutable"):
        await store.record_codex_stop(operation, changed.model_dump())
    assert len([row for row in await store.events("caller-a", attempt["id"], 0, 100)
                if row["type"] == "codex_stop_diagnostic"]) == 1


@pytest.mark.parametrize("drift", ["writer", "generation", "digest"])
async def test_wrong_ownership_or_corrupt_receipt_never_commits(journal, drift):
    store, attempt, operation, proof = journal
    raw = proof.model_dump()
    if drift == "writer":
        await store.pool.execute("UPDATE kotoyose.execution_workspaces SET writer_attempt=NULL WHERE id=$1", attempt["workspace_id"])
    elif drift == "generation":
        await store.pool.execute("UPDATE kotoyose.execution_workspaces SET generation=generation+1 WHERE id=$1", attempt["workspace_id"])
    else:
        raw["digest"] = "0" * 64
    with pytest.raises(ExecutionError): await store.record_codex_stop(operation, raw)
    saved = next(row for row in await store.operations(attempt["id"]) if row["id"] == operation)
    assert "codex_stop_evidence" not in saved["result"]
    assert not any(row["type"] == "codex_stop_diagnostic" for row in await store.events("caller-a", attempt["id"], 0, 100))


@pytest.mark.parametrize("external_pending", [False, True])
async def test_restart_regular_reconcile_preserves_verdict_and_only_releases_settled_writer(journal, external_pending):
    store, attempt, operation, proof = journal
    external = await store.prepare_operation(attempt["id"], "client_tool") if external_pending else None
    await store.record_codex_stop(operation, proof.model_dump())
    # 保存後、worker が finish へ進む前の crash を模し、復旧で元 verdict を確定する。
    await store.resolve_operation(operation, "unconfirmed")
    await store.finish(attempt["id"], "timeout", "soft_timeout", "unconfirmed", None, {})
    service = ExecutionService.__new__(ExecutionService)
    service.store, service._tasks = ExecutionStore(store.pool), {}
    service._worker_for = lambda snapshot: None
    async def describe(principal, attempt_id): return await service.store.attempt(principal, attempt_id)
    service.describe = describe
    result = await service.reconcile("caller-a", attempt["id"])
    assert (result["status"], result["reason"], result["stop_confirmation"]) == ("timeout", "soft_timeout", "confirmed")
    workspace = await store.pool.fetchrow("SELECT * FROM kotoyose.execution_workspaces WHERE id=$1", attempt["workspace_id"])
    if external_pending:
        assert result["side_effect_resolution"] == "unresolved"
        assert workspace["writer_attempt"] == attempt["id"] and workspace["generation"] == 0
        await store.resolve_operation(external, "confirmed")
        result = await service.reconcile("caller-a", attempt["id"])
    assert result["side_effect_resolution"] == "resolved"
    workspace = await store.pool.fetchrow("SELECT * FROM kotoyose.execution_workspaces WHERE id=$1", attempt["workspace_id"])
    assert workspace["writer_attempt"] is None and workspace["generation"] == 1
    await service.reconcile("caller-a", attempt["id"])
    assert await store.pool.fetchval("SELECT generation FROM kotoyose.execution_workspaces WHERE id=$1", attempt["workspace_id"]) == 1


async def test_diagnostic_failure_rolls_back_receipt_in_same_transaction(journal, monkeypatch):
    from unittest.mock import AsyncMock
    store, attempt, operation, proof = journal
    monkeypatch.setattr(store, "_event", AsyncMock(side_effect=RuntimeError("synthetic journal failure")))
    with pytest.raises(RuntimeError): await store.record_codex_stop(operation, proof.model_dump())
    saved = next(row for row in await store.operations(attempt["id"]) if row["id"] == operation)
    assert "codex_stop_evidence" not in saved["result"]
    assert not any(row["type"] == "codex_stop_diagnostic" for row in await store.events("caller-a", attempt["id"], 0, 100))
