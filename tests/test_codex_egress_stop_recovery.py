"""egress だけが未確認の停止を、egress を持っていた process の不在を確かめたときだけ確定する。"""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kotoyose import codex_egress, codex_egress_owner, codex_stop_recovery, native_codex
from kotoyose.codex_egress import CodexEgress, egress_settled_in_process, forget_unsettled_egress, retain_unsettled_egress
from kotoyose.codex_egress_owner import egress_owner_absent
from kotoyose.codex_stop_evidence import (
    STAGES, StopComponents, make_egress_stop_evidence, make_stop_evidence, stop_evidence_context,
)
from kotoyose.coordinator_lease import PREDECESSOR_EXIT_BOUND_SECONDS
from kotoyose.native_codex import NativeCodexWorker
from kotoyose.process_identity import ProcessIdentity
from tests.test_codex_host_coding import FakeProcess, job
from tests.test_codex_stop_evidence import saved as saved  # noqa: F401

RECOVERER = {"pid": 4242, "start_ticks": 99, "boot_id": "synthetic", "uid": 1000}


def egress_only(saved, **states):
    attempt, ownership, _ = saved
    attempt.update(finished_at=datetime.now(timezone.utc), status="cancelled", reason="cancelled",
                   stop_confirmation="unconfirmed", side_effect_resolution="unresolved")
    components = {**dict.fromkeys(STAGES, "confirmed"), "egress": "unconfirmed", **states}
    proof = make_stop_evidence(stop_evidence_context(attempt), ownership, StopComponents(**components), 0)
    return attempt, {**ownership, "codex_stop_evidence": proof.model_dump()}


def absent(basis="previous_coordinator"):
    recovered_by = {k: v for k, v in RECOVERER.items() if k != "uid"}
    return lambda attempt, ownership: (basis, recovered_by)


def forbid_live_stop(monkeypatch):
    forbidden = AsyncMock(side_effect=AssertionError("egress-only recovery must not reopen a process"))
    monkeypatch.setattr(codex_stop_recovery, "stop_codex_process", forbidden)
    monkeypatch.setattr(codex_stop_recovery, "stop_recovered_provider_scope", forbidden)
    return forbidden


async def test_egress_only_stop_is_confirmed_after_owner_absence_is_durably_recorded(saved, monkeypatch):
    attempt, result = egress_only(saved)
    original = deepcopy(result)
    forbidden = forbid_live_stop(monkeypatch)
    record = AsyncMock()
    stop = await codex_stop_recovery.recover_codex_stop(
        attempt, result, record_egress_stop=record, egress_owner_absent=absent())
    assert stop == "confirmed"
    record.assert_awaited_once()
    evidence = record.await_args.args[0]
    assert evidence["kind"] == "egress_owner_absent" and evidence["basis"] == "previous_coordinator"
    assert evidence["source_stop_digest"] == result["codex_stop_evidence"]["digest"]
    assert evidence["recovered_by"]["pid"] == RECOVERER["pid"]
    assert result == original
    forbidden.assert_not_awaited()


@pytest.mark.parametrize("failure", ["owner_present", "no_checker", "no_writer", "write_failed"])
async def test_unproven_or_unpersisted_absence_stays_unconfirmed(saved, monkeypatch, failure):
    attempt, result = egress_only(saved)
    forbidden = forbid_live_stop(monkeypatch)
    checker = (lambda attempt, ownership: None) if failure == "owner_present" else None if failure == "no_checker" else absent()
    writer = None if failure == "no_writer" else AsyncMock(side_effect=RuntimeError("private detail") if failure == "write_failed" else None)
    stop = await codex_stop_recovery.recover_codex_stop(attempt, result, record_egress_stop=writer, egress_owner_absent=checker)
    assert stop == "unconfirmed"
    if writer is not None and failure != "write_failed":
        writer.assert_not_awaited()
    forbidden.assert_not_awaited()


@pytest.mark.parametrize("field", ["provider", "scope", "tasks", "pipes", "pending_tasks", "egress_confirmed_scope", "running"])
async def test_other_uncertainty_never_uses_the_egress_branch(saved, monkeypatch, field):
    attempt, ownership, _ = saved
    attempt.update(finished_at=None if field == "running" else datetime.now(timezone.utc))
    states = {**dict.fromkeys(STAGES, "confirmed"), "egress": "unconfirmed"}
    if field in STAGES:
        states[field] = "unconfirmed"
    if field == "egress_confirmed_scope":
        states.update(egress="confirmed", scope="unconfirmed")
    proof = make_stop_evidence(stop_evidence_context(attempt), ownership, StopComponents(**states), int(field == "pending_tasks"))
    result = {**ownership, "codex_stop_evidence": proof.model_dump()}
    monkeypatch.setattr(codex_stop_recovery, "stop_codex_process", AsyncMock(return_value="unconfirmed"))
    monkeypatch.setattr(codex_stop_recovery, "stop_recovered_provider_scope", AsyncMock(return_value="unconfirmed"))
    checker = AsyncMock()
    record = AsyncMock()
    stop = await codex_stop_recovery.recover_codex_stop(attempt, result, record_egress_stop=record, egress_owner_absent=checker)
    assert stop == "unconfirmed"
    record.assert_not_awaited()
    checker.assert_not_called()


async def test_saved_egress_recovery_survives_restart_without_touching_processes(saved, monkeypatch):
    attempt, result = egress_only(saved)
    ownership = {k: v for k, v in result.items() if k != "codex_stop_evidence"}
    result["codex_egress_stop_evidence"] = make_egress_stop_evidence(
        stop_evidence_context(attempt), ownership, result["codex_stop_evidence"],
        basis="previous_coordinator", recovered_by={"pid": 1, "start_ticks": 2, "boot_id": "synthetic"}).model_dump()
    forbidden = forbid_live_stop(monkeypatch)
    assert await codex_stop_recovery.recover_codex_stop(attempt, result) == "confirmed"
    forbidden.assert_not_awaited()


@pytest.mark.parametrize("drift", ["receipt", "attempt", "generation", "workspace", "source", "owner", "basis"])
async def test_saved_egress_receipt_cannot_be_corrupted_or_replayed(saved, monkeypatch, drift):
    attempt, result = egress_only(saved)
    ownership = {k: v for k, v in result.items() if k != "codex_stop_evidence"}
    result["codex_egress_stop_evidence"] = make_egress_stop_evidence(
        stop_evidence_context(attempt), ownership, result["codex_stop_evidence"],
        basis="previous_coordinator", recovered_by={"pid": 1, "start_ticks": 2, "boot_id": "synthetic"}).model_dump()
    if drift == "receipt":
        result["codex_egress_stop_evidence"]["digest"] = "0" * 64
    elif drift == "attempt":
        attempt["id"] = "different-attempt"
    elif drift == "generation":
        attempt["generation"] += 1
    elif drift == "workspace":
        attempt["snapshot"]["workspace_inode"] += 1
    elif drift == "source":
        result["codex_stop_evidence"]["pending_tasks"] = 1
    elif drift == "owner":
        result["pid"] += 1
    else:
        result["codex_egress_stop_evidence"]["basis"] = "settled_in_process"
    forbidden = forbid_live_stop(monkeypatch)
    assert await codex_stop_recovery.recover_codex_stop(attempt, result) == "unconfirmed"
    forbidden.assert_not_awaited()


def coordinator(acquired_at, *, state="held", monotonic=0.0):
    return SimpleNamespace(state=state, connection=object(), acquired_at=acquired_at, acquired_monotonic=monotonic)


PROCESS = ProcessIdentity(4242, 99, "synthetic", 1000)


def decide(attempt_created, lease, *, boot="synthetic", now=PREDECESSOR_EXIT_BOUND_SECONDS, settled=False):
    attempt = {"id": "attempt-x", "created_at": attempt_created}
    return egress_owner_absent(attempt, {"boot_id": boot}, coordinator=lease, process=PROCESS,
                               settled_in_process=lambda _: settled, monotonic=lambda: now)


def test_previous_coordinator_is_absent_only_after_its_bounded_exit():
    acquired = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
    earlier = acquired - timedelta(minutes=40)
    assert decide(earlier, coordinator(acquired), now=PREDECESSOR_EXIT_BOUND_SECONDS - .01) is None
    basis, process = decide(earlier, coordinator(acquired), now=PREDECESSOR_EXIT_BOUND_SECONDS)
    assert basis == "previous_coordinator" and process == {"pid": 4242, "start_ticks": 99, "boot_id": "synthetic"}


def test_previous_boot_is_absent_without_a_coordinator():
    assert decide(None, None, boot="older-boot")[0] == "previous_boot"


@pytest.mark.parametrize("case", ["no_lease", "lost", "stopping", "no_connection", "unknown_time", "unknown_created"])
def test_unproven_coordinator_succession_fails_closed(case):
    acquired = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
    lease = {"no_lease": None, "lost": coordinator(acquired, state="lost"),
             "stopping": coordinator(acquired, state="stopping"),
             "no_connection": SimpleNamespace(state="held", connection=None, acquired_at=acquired, acquired_monotonic=0.0),
             "unknown_time": coordinator(True), "unknown_created": coordinator(acquired)}[case]
    created = "2026-11-10T09:00:00Z" if case == "unknown_created" else acquired - timedelta(hours=1)
    assert decide(created, lease, now=10 ** 6) is None


@pytest.mark.parametrize("settled", [False, True])
def test_current_process_attempt_needs_its_own_settled_egress(settled):
    acquired = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
    result = decide(acquired + timedelta(seconds=1), coordinator(acquired), settled=settled, now=10 ** 6)
    assert (result[0] if result else None) == ("settled_in_process" if settled else None)


async def test_registry_reports_settled_only_for_a_retained_and_finished_egress(tmp_path):
    egress = CodexEgress(1, 1024, "model", "credential", None)
    assert not egress_settled_in_process("attempt-r")
    blocker = asyncio.Event()
    task = asyncio.create_task(blocker.wait())
    egress.tasks.add(task)
    egress.closed = True
    retain_unsettled_egress("attempt-r", egress)
    try:
        assert not egress_settled_in_process("attempt-r")
        blocker.set()
        await task
        assert egress_settled_in_process("attempt-r")
    finally:
        forget_unsettled_egress("attempt-r")
    assert not egress_settled_in_process("attempt-r")


def test_decision_module_uses_the_coordinator_exit_bound():
    assert codex_egress_owner.PREDECESSOR_EXIT_BOUND_SECONDS == PREDECESSOR_EXIT_BOUND_SECONDS >= 30
