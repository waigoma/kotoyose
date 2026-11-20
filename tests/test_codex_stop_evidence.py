"""モデルなしで、消失前の停止観測と restart 後の厳密な照合を検証する。"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kotoyose import codex_process_stop, codex_stop_recovery, native_codex
from kotoyose.agent_deadlines import coding_deadlines
from kotoyose.codex_stop_evidence import (
    STAGES, StopComponents, make_stop_evidence, stop_evidence_context, validate_stop_evidence,
)
from kotoyose.execution_resources import ExecutionError
from kotoyose.execution_service import ExecutionService
from kotoyose.native_codex import NativeCodexWorker
from kotoyose.process_identity import ProcessIdentity
from tests.test_codex_host_coding import FakeProcess, job


@pytest.fixture
def saved():
    attempt = {"id": "attempt-1", "generation": 7, "snapshot": {
        "kind": "agent", "backend": "native", "provider": "codex",
        "codex_execution_mode": "host_native_coding_v1", "workspace_device": 9, "workspace_inode": 10}}
    ownership = {"pid": 11, "start_ticks": 12, "boot_id": "fixture", "uid": 1000,
                 "codex_provider_scope": {"unit": "kotoyose-native-attempt-1-provider-7.scope",
                    "control_group": "/owned/scope", "device": 31, "inode": 32,
                    "namespace_init": {"pid": 21, "start_ticks": 22, "boot_id": "fixture", "uid": 1000}}}
    components = StopComponents(**dict.fromkeys(STAGES, "confirmed"))
    proof = make_stop_evidence(stop_evidence_context(attempt), ownership, components, 0)
    return attempt, ownership, proof


def test_identity_bound_complete_receipt_and_safe_diagnostic(saved):
    attempt, ownership, proof = saved
    assert validate_stop_evidence(proof.model_dump(), stop_evidence_context(attempt), ownership) == proof
    assert proof.confirmed()
    assert proof.diagnostic() == {"schema_version": "1", "stage": "complete", "reason": "confirmed", "pending_tasks": 0}


@pytest.mark.parametrize("field,value", [
    ("schema_version", "2"), ("digest", "0" * 64), ("attempt_id", "another"),
    ("generation", 8), ("generation", True), ("workspace_device", 33), ("workspace_inode", 44),
    ("pending_tasks", 1), ("ownership_digest", "0" * 64),
])
def test_corrupt_or_transplanted_receipt_rejected(saved, field, value):
    attempt, ownership, proof = saved
    raw = {**proof.model_dump(), field: value}
    with pytest.raises(ValueError):
        validate_stop_evidence(raw, stop_evidence_context(attempt), ownership)


@pytest.mark.parametrize("change", ["missing", "unknown", "extra", "incomplete", "version"])
def test_partial_receipt_is_not_positive(saved, change):
    attempt, ownership, proof = saved
    raw = proof.model_dump()
    if change == "missing": raw["components"].pop("scope")
    if change == "unknown": raw["components"]["scope"] = "secret-canary"
    if change == "extra": raw["provider_text"] = "secret-canary"
    if change == "incomplete": raw.pop("digest")
    if change == "version": raw.pop("schema_version")
    with pytest.raises(ValueError):
        validate_stop_evidence(raw, stop_evidence_context(attempt), ownership)


@pytest.mark.parametrize("stage", STAGES)
async def test_any_unknown_component_prevents_gone_scope_recovery(saved, monkeypatch, stage):
    attempt, ownership, _ = saved
    states = dict.fromkeys(STAGES, "confirmed")
    states[stage] = "unconfirmed"
    proof = make_stop_evidence(stop_evidence_context(attempt), ownership, StopComponents(**states), 0)
    assert not proof.confirmed()
    assert proof.diagnostic()["stage"] == stage
    monkeypatch.setattr(codex_stop_recovery, "stop_recovered_provider_scope", AsyncMock(return_value="unconfirmed"))
    monkeypatch.setattr(codex_stop_recovery, "stop_codex_process", AsyncMock(return_value="confirmed"))
    assert await codex_stop_recovery.recover_codex_stop(attempt, {**ownership, "codex_stop_evidence": proof.model_dump()}) == "unconfirmed"


async def test_new_service_reconcile_uses_saved_positive_without_live_process_or_scope(saved, monkeypatch):
    attempt, ownership, proof = saved
    forbidden = AsyncMock(side_effect=AssertionError("historical proof must not reopen a vanished scope"))
    monkeypatch.setattr(codex_stop_recovery, "stop_recovered_provider_scope", forbidden)
    monkeypatch.setattr(codex_stop_recovery, "stop_codex_process", forbidden)
    attempt.update(finished_at="finished", status="timeout", reason="soft_timeout",
                   stop_confirmation="unconfirmed", side_effect_resolution="unresolved")
    operation = {"id": "op", "kind": "provider_process", "state": "unknown",
                 "result": {**ownership, "codex_stop_evidence": proof.model_dump()}}
    service = ExecutionService.__new__(ExecutionService)
    service._tasks = {}
    service._worker_for = lambda snapshot: None
    service.store = SimpleNamespace(attempt=AsyncMock(return_value=attempt),
        operations=AsyncMock(return_value=[operation]), resolve_operation=AsyncMock(), confirm_stop=AsyncMock())
    service.describe = AsyncMock(return_value={"status": "timeout", "reason": "soft_timeout"})
    assert await service.reconcile("caller", attempt["id"]) == {"status": "timeout", "reason": "soft_timeout"}
    service.store.resolve_operation.assert_awaited_once_with("op", "confirmed")
    service.store.confirm_stop.assert_awaited_once_with(attempt["id"])
    forbidden.assert_not_awaited()


@pytest.mark.parametrize("receipt", ["missing", "corrupt"])
async def test_old_or_corrupt_receipt_does_not_infer_stop_from_absence(saved, monkeypatch, receipt):
    attempt, ownership, proof = saved
    monkeypatch.setattr(codex_stop_recovery, "stop_recovered_provider_scope", AsyncMock(return_value="unconfirmed"))
    monkeypatch.setattr(codex_stop_recovery, "stop_codex_process", AsyncMock(return_value="confirmed"))
    result = deepcopy(ownership)
    if receipt == "corrupt": result["codex_stop_evidence"] = {**proof.model_dump(), "digest": "0" * 64}
    assert await codex_stop_recovery.recover_codex_stop(attempt, result) == "unconfirmed"


async def test_pinned_init_survives_concurrent_tree_disappearance(saved, monkeypatch):
    _, ownership, _ = saved
    scope = ownership.pop("codex_provider_scope")
    root, init = ProcessIdentity(**ownership), ProcessIdentity(**scope["namespace_init"])
    monkeypatch.setattr(codex_process_stop, "_children", Mock(side_effect=RuntimeError("tree gone")))
    stop = AsyncMock(return_value="confirmed")
    monkeypatch.setattr(codex_process_stop, "stop_identified_process", stop)
    assert await codex_process_stop.stop_codex_process(root, 0, namespace_init=init) == "confirmed"
    assert stop.await_args_list[0].args == (init, 0)
    assert stop.await_args_list[1].args == (root, 0)
    # この component だけでは scope の停止を確定しない。
    assert await codex_process_stop.stop_codex_process(root, 0) == "unconfirmed"


@pytest.mark.parametrize("remaining,hard,soft", [(1810, 1800, 1440), (110, 100, 80), (10000, 3600, 2880)])
def test_actual_start_reserves_grace_and_original_ratio(remaining, hard, soft):
    timeout = {"hard_seconds": 3600, "soft_seconds": 2880, "grace_seconds": 10}
    assert coding_deadlines(100, remaining, timeout, 3600) == (100 + hard, 100 + soft)


@pytest.mark.parametrize("remaining", [0, 9, 10, float("inf"), float("nan")])
def test_expired_actual_start_refused_before_provider_intent(remaining):
    with pytest.raises(ExecutionError, match="no stop grace"):
        coding_deadlines(100, remaining, {"hard_seconds": 3600, "soft_seconds": 2880, "grace_seconds": 10}, 3600)


@pytest.mark.parametrize("change", ["init_missing", "init_uid", "init_boot", "init_older", "unit"])
def test_matching_digest_does_not_replace_required_initial_namespace_observation(saved, change):
    attempt, ownership, _ = saved
    scope = ownership["codex_provider_scope"]
    if change == "init_missing": scope.pop("namespace_init")
    if change == "init_uid": scope["namespace_init"]["uid"] += 1
    if change == "init_boot": scope["namespace_init"]["boot_id"] = "another"
    if change == "init_older": scope["namespace_init"]["start_ticks"] = 1
    if change == "unit": scope["unit"] = "another.scope"
    proof = make_stop_evidence(stop_evidence_context(attempt), ownership, StopComponents(**dict.fromkeys(STAGES, "confirmed")), 0)
    with pytest.raises(ValueError): validate_stop_evidence(proof.model_dump(), stop_evidence_context(attempt), ownership)

