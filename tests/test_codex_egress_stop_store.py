"""専用 DB で egress 復旧観測の保存、枠と writer の解放、元の停止証拠の維持を検証する。"""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from kotoyose.admission_quarantine import count_admission_reservations
from kotoyose.codex_stop_evidence import (
    STAGES, StopComponents, make_egress_stop_evidence, make_stop_evidence, stop_evidence_context,
)
from kotoyose.coordinator_lease import PREDECESSOR_EXIT_BOUND_SECONDS
from kotoyose.execution_resources import ExecutionError
from kotoyose.execution_service import ExecutionService
from kotoyose.process_identity import ProcessIdentity
from tests.test_codex_stop_store import journal as journal  # noqa: F401

RECOVERED_BY = {"pid": 4242, "start_ticks": 99, "boot_id": "synthetic"}


async def egress_terminal(journal):
    store, attempt, operation, _ = journal
    owner = next(x for x in await store.operations(attempt["id"]) if x["id"] == operation)["result"]
    source = make_stop_evidence(stop_evidence_context(attempt), owner,
        StopComponents(**{**dict.fromkeys(STAGES, "confirmed"), "egress": "unconfirmed"}), 0)
    await store.record_codex_stop(operation, source.model_dump())
    await store.resolve_operation(operation, "unconfirmed")
    await store.finish(attempt["id"], "cancelled", "cancelled", "unconfirmed", None, {})
    evidence = make_egress_stop_evidence(stop_evidence_context(attempt), owner, source.model_dump(),
                                         basis="previous_coordinator", recovered_by=RECOVERED_BY)
    return store, attempt, operation, source, evidence


@pytest.fixture(autouse=True)
def same_boot(monkeypatch):
    """journal の provider は boot "synthetic" で起動した。判断する process も同じ boot に置く。"""
    monkeypatch.setattr("kotoyose.process_identity.capture_process_identity",
                        lambda pid: ProcessIdentity(pid, 99, "synthetic", 1000))


def successor_service(store, acquired_at, monotonic_age):
    """lock を acquired_at に取った coordinator の service。単調時計は取得からの経過を返す。"""
    import time
    service = ExecutionService.__new__(ExecutionService)
    service.store, service._tasks = store, {}
    service._worker_for = lambda _: pytest.fail("Historical provider stop must not resolve the current executor")
    service._coordinator = SimpleNamespace(state="held", connection=object(), acquired_at=acquired_at,
                                           acquired_monotonic=time.monotonic() - monotonic_age)
    async def describe(principal, attempt_id):
        return await store.attempt(principal, attempt_id)
    service.describe = describe
    return service


async def test_receipt_and_event_are_immutable_and_idempotent(journal):
    store, attempt, operation, source, evidence = await egress_terminal(journal)
    await store.record_codex_egress_stop(operation, evidence.model_dump())
    await store.record_codex_egress_stop(operation, evidence.model_dump())
    saved = next(x for x in await store.operations(attempt["id"]) if x["id"] == operation)
    assert saved["result"]["codex_stop_evidence"] == source.model_dump()
    assert saved["result"]["codex_egress_stop_evidence"] == evidence.model_dump()
    diagnostics = [row["payload"] for row in await store.events("caller-a", attempt["id"], 0, 100)
                   if row["type"] == "codex_stop_diagnostic"]
    assert diagnostics == [source.diagnostic(), {"schema_version": "1", "stage": "complete", "reason": "confirmed", "pending_tasks": 0}]
    owner = {k: v for k, v in saved["result"].items() if k not in ("codex_stop_evidence", "codex_egress_stop_evidence")}
    other = make_egress_stop_evidence(stop_evidence_context(attempt), owner, source.model_dump(),
                                      basis="previous_boot", recovered_by=RECOVERED_BY)
    with pytest.raises(ExecutionError, match="immutable"):
        await store.record_codex_egress_stop(operation, other.model_dump())
    # 一度 egress で回復した停止を namespace 側から重ねて回復しない。
    with pytest.raises(ExecutionError, match="already recovered"):
        await store.record_codex_namespace_stop(operation, {"schema_version": "1"})


@pytest.mark.parametrize("drift", ["running", "digest", "not_egress_only"])
async def test_invalid_or_premature_receipt_never_commits(journal, drift):
    store, attempt, operation, source, evidence = await egress_terminal(journal)
    raw = evidence.model_dump()
    if drift == "running":
        await store.pool.execute("UPDATE kotoyose.execution_attempts SET finished_at=NULL WHERE id=$1", attempt["id"])
    elif drift == "digest":
        raw["digest"] = "0" * 64
    else:
        raw["source_stop_digest"] = "0" * 64
    with pytest.raises(ExecutionError):
        await store.record_codex_egress_stop(operation, raw)
    saved = next(x for x in await store.operations(attempt["id"]) if x["id"] == operation)
    assert "codex_egress_stop_evidence" not in saved["result"]


async def test_successor_coordinator_releases_capacity_and_writer_after_the_exit_bound(journal):
    store, attempt, operation, source, _ = await egress_terminal(journal)
    created = await store.pool.fetchval("SELECT created_at FROM kotoyose.execution_attempts WHERE id=$1", attempt["id"])
    acquired = created + timedelta(minutes=30)
    assert await count_admission_reservations(store.pool, "caller-a") == 1

    early = successor_service(store, acquired, PREDECESSOR_EXIT_BOUND_SECONDS - 5)
    result = await early.reconcile("caller-a", attempt["id"])
    assert result["stop_confirmation"] == "unconfirmed"
    saved = next(x for x in await store.operations(attempt["id"]) if x["id"] == operation)
    assert "codex_egress_stop_evidence" not in saved["result"]

    later = successor_service(store, acquired, PREDECESSOR_EXIT_BOUND_SECONDS + 1)
    result = await later.reconcile("caller-a", attempt["id"])
    assert (result["status"], result["reason"]) == ("cancelled", "cancelled")
    assert (result["stop_confirmation"], result["side_effect_resolution"]) == ("confirmed", "resolved")
    saved = next(x for x in await store.operations(attempt["id"]) if x["id"] == operation)
    assert saved["result"]["codex_stop_evidence"] == source.model_dump()
    receipt = saved["result"]["codex_egress_stop_evidence"]
    assert receipt["basis"] == "previous_coordinator" and receipt["source_stop_digest"] == source.digest
    workspace = await store.pool.fetchrow("SELECT writer_attempt,generation FROM kotoyose.execution_workspaces WHERE id=$1", attempt["workspace_id"])
    assert workspace["writer_attempt"] is None and workspace["generation"] == 1
    assert await count_admission_reservations(store.pool, "caller-a") == 0


async def test_same_coordinator_without_its_own_settled_egress_stays_unconfirmed(journal):
    store, attempt, operation, _, _ = await egress_terminal(journal)
    created = await store.pool.fetchval("SELECT created_at FROM kotoyose.execution_attempts WHERE id=$1", attempt["id"])
    service = successor_service(store, created - timedelta(seconds=1), 10 ** 6)
    result = await service.reconcile("caller-a", attempt["id"])
    assert result["stop_confirmation"] == "unconfirmed"
    assert await count_admission_reservations(store.pool, "caller-a") == 1
