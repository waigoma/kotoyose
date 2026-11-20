"""専用 DB で復旧観測の保存、枠解放、元の停止証拠の維持を検証する。"""

from unittest.mock import AsyncMock

import pytest

from kotoyose import codex_stop_recovery
from kotoyose.admission_quarantine import count_admission_reservations
from kotoyose.codex_stop_evidence import (
    STAGES, StopComponents, make_namespace_stop_evidence, make_stop_evidence, stop_evidence_context,
)
from kotoyose.execution_resources import ExecutionError
from kotoyose.execution_service import ExecutionService
from tests.test_codex_stop_store import journal as journal


async def terminal(journal):
    store, attempt, operation, _ = journal
    owner = next(x for x in await store.operations(attempt["id"]) if x["id"] == operation)["result"]
    source = make_stop_evidence(stop_evidence_context(attempt), owner,
        StopComponents(**{**dict.fromkeys(STAGES, "confirmed"), "scope": "unconfirmed"}), 0)
    await store.record_codex_stop(operation, source.model_dump())
    await store.resolve_operation(operation, "unconfirmed")
    await store.finish(attempt["id"], "timeout", "soft_timeout", "unconfirmed", None, {})
    evidence = make_namespace_stop_evidence(stop_evidence_context(attempt), owner, source.model_dump())
    return store, attempt, operation, source, evidence


@pytest.mark.parametrize("external_pending", [False, True])
async def test_regular_reconcile_releases_capacity_without_changing_the_old_verdict(journal, tmp_path, monkeypatch, external_pending):
    external = await journal[0].prepare_operation(journal[1]["id"], "client_tool") if external_pending else None
    store, attempt, operation, source, _ = await terminal(journal)
    monkeypatch.setattr(codex_stop_recovery, "stop_codex_process", AsyncMock(return_value="confirmed"))
    monkeypatch.setattr(codex_stop_recovery, "stop_recovered_provider_scope", AsyncMock(return_value="unconfirmed"))
    service = ExecutionService.__new__(ExecutionService)
    service.store, service._tasks = store, {}
    service._worker_for = lambda _: pytest.fail("Historical provider stop must not resolve the current executor")
    async def describe(principal, attempt_id):
        return await store.attempt(principal, attempt_id)
    service.describe = describe
    assert await count_admission_reservations(store.pool, "caller-a") == 1
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    workspace = await store.register_workspace("caller-a", "fresh", fresh)
    request = {**attempt["request"], "idempotency_key": "new-key",
               "workspace_ref": f"workspace:{workspace['id']}:{workspace['generation']}"}
    snapshot = {**attempt["snapshot"], "workspace_id": workspace["id"], "generation": workspace["generation"],
                "workspace_path": str(fresh), "workspace_device": workspace["device"], "workspace_inode": workspace["inode"]}
    with pytest.raises(ExecutionError, match="concurrency reservation is full") as full:
        await store.admit("caller-a", request, snapshot, 1)
    assert (full.value.code, full.value.status, full.value.reason_code) == (
        "capacity_exhausted", 429, "caller_concurrency")
    result = await service.reconcile("caller-a", attempt["id"])
    assert (result["status"], result["reason"], result["stop_confirmation"]) == ("timeout", "soft_timeout", "confirmed")
    saved = next(x for x in await store.operations(attempt["id"]) if x["id"] == operation)
    assert saved["result"]["codex_stop_evidence"] == source.model_dump()
    assert saved["result"]["codex_namespace_stop_evidence"]["source_stop_digest"] == source.digest
    assert result["side_effect_resolution"] == ("unresolved" if external_pending else "resolved")
    if external_pending:
        workspace = await store.pool.fetchrow("SELECT writer_attempt,generation FROM kotoyose.execution_workspaces WHERE id=$1", attempt["workspace_id"])
        assert workspace["writer_attempt"] == attempt["id"] and workspace["generation"] == 0
        await store.resolve_operation(external, "confirmed")
        await service.reconcile("caller-a", attempt["id"])
    assert await count_admission_reservations(store.pool, "caller-a") == 0
    reopened, created = await store.admit("caller-a", request, snapshot, 1)
    assert created and reopened["id"] != attempt["id"]
    await service.reconcile("caller-a", attempt["id"])
    assert await store.pool.fetchval("SELECT generation FROM kotoyose.execution_workspaces WHERE id=$1", attempt["workspace_id"]) == 1


async def test_recovery_record_is_immutable_idempotent_and_keeps_original_receipt(journal):
    store, attempt, operation, source, evidence = await terminal(journal)
    for _ in range(2):
        await store.record_codex_namespace_stop(operation, evidence.model_dump())
    await store.record_codex_stop(operation, source.model_dump())
    saved = next(x for x in await store.operations(attempt["id"]) if x["id"] == operation)["result"]
    assert saved["codex_stop_evidence"] == source.model_dump()
    assert saved["codex_namespace_stop_evidence"] == evidence.model_dump()
    events = await store.events("caller-a", attempt["id"], 0, 100)
    complete = [x for x in events if x["type"] == "codex_stop_diagnostic" and x["payload"]["stage"] == "complete"]
    assert len(complete) == 1


@pytest.mark.parametrize("drift", ["writer", "generation", "digest", "running", "source"])
async def test_recovery_cannot_adopt_changed_owner_source_or_nonterminal_attempt(journal, drift):
    store, attempt, operation, _, evidence = await terminal(journal)
    raw = evidence.model_dump()
    if drift == "writer":
        await store.pool.execute("UPDATE kotoyose.execution_workspaces SET writer_attempt=NULL WHERE id=$1", attempt["workspace_id"])
    elif drift == "generation":
        await store.pool.execute("UPDATE kotoyose.execution_workspaces SET generation=generation+1 WHERE id=$1", attempt["workspace_id"])
    elif drift == "digest":
        raw["digest"] = "0" * 64
    elif drift == "running":
        await store.pool.execute("UPDATE kotoyose.execution_attempts SET finished_at=NULL WHERE id=$1", attempt["id"])
    else:
        await store.pool.execute("UPDATE kotoyose.execution_operations SET result=result-'codex_stop_evidence' WHERE id=$1", operation)
    with pytest.raises(ExecutionError):
        await store.record_codex_namespace_stop(operation, raw)
    saved = next(x for x in await store.operations(attempt["id"]) if x["id"] == operation)["result"]
    assert "codex_namespace_stop_evidence" not in saved


async def test_failed_recovery_persistence_keeps_the_reservation_and_old_proof(journal, monkeypatch):
    store, attempt, operation, source, evidence = await terminal(journal)
    monkeypatch.setattr(store, "_event", AsyncMock(side_effect=RuntimeError("synthetic persistence failure")))
    with pytest.raises(RuntimeError):
        await store.record_codex_namespace_stop(operation, evidence.model_dump())
    saved = next(x for x in await store.operations(attempt["id"]) if x["id"] == operation)["result"]
    assert "codex_namespace_stop_evidence" not in saved
    assert saved["codex_stop_evidence"] == source.model_dump()
    assert await count_admission_reservations(store.pool, "caller-a") == 1
