"""Admission, replay and resume pins for broker limits; no sockets, models or processes."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kotoyose.execution_agent import AgentExecutor
from kotoyose.execution_contract import ResumeRequest
from kotoyose.execution_resources import (
    CodexEgressLimits, DriverCell, ExecutionError, content_digest, pinned_codex_egress_limits,
)
from kotoyose.execution_snapshot import ResolvedAttemptSnapshot, resolved_attempt_snapshot
from kotoyose.native_claude import NativeAgentResult
from kotoyose import native_codex
from kotoyose.native_codex import NativeCodexWorker
from kotoyose.runtime.resume import ResumeSupport
from tests.test_codex_host_admission import admitted as admitted
from tests.test_codex_host_coding import job


LIMITS = {"version": 1, "total_bytes": "disabled", "total_connections": "disabled",
          "request_bytes": 16777216, "response_bytes": 33554432}
FINITE = {**LIMITS, "total_bytes": 1048576, "total_connections": 5}


def select(admitted, limits):
    install = admitted.settings.drivers["codex.app-server"]
    parsed = None if limits is None else CodexEgressLimits.model_validate(limits)
    cell = install.cells["coding"]
    payload = {"rootfs": cell.rootfs, "rootfs_digest": cell.rootfs_digest}
    if parsed is not None:
        payload["codex_egress_limits"] = parsed
    admitted.settings.drivers["codex.app-server"] = install.model_copy(update={
        "cells": {**install.cells, "coding": DriverCell(**payload)},
    })


@pytest.mark.parametrize("limits", [None, LIMITS, FINITE])
async def test_admission_pins_explicit_profile_limits_and_preserves_legacy_omission(admitted, limits):
    select(admitted, limits)
    snapshot = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    assert ("codex_egress_limits" in snapshot) == (limits is not None)
    assert snapshot.get("codex_egress_limits") == limits
    original = deepcopy(snapshot)
    select(admitted, FINITE if limits != FINITE else LIMITS)
    assert snapshot == original


@pytest.mark.parametrize("limits", [None, LIMITS, FINITE])
async def test_public_projection_keeps_pin_and_legacy_digest_without_mutation(admitted, limits):
    select(admitted, limits)
    snapshot = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    request = admitted.request.model_dump(mode="json")
    original = deepcopy(snapshot)
    attempt = {"id": "attempt", "sequence": 0, "request": request,
               "request_digest": content_digest(request), "snapshot": snapshot}
    public = resolved_attempt_snapshot(attempt).model_dump(mode="json")
    assert ("codex_egress_limits" in public) == (limits is not None)
    assert public.get("codex_egress_limits") == limits
    assert public["snapshot_digest"] == content_digest(original)
    assert snapshot == original
    with pytest.raises(ValueError):
        ResolvedAttemptSnapshot.model_validate({**public, "codex_egress_limits": None})


@pytest.mark.parametrize("malformed", [None, {}, {**LIMITS, "version": 2}, {**LIMITS, "total_bytes": None}])
async def test_invalid_saved_pin_is_rejected_before_any_job_or_profile_lookup(admitted, malformed):
    snapshot = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    snapshot["codex_egress_limits"] = malformed
    store = SimpleNamespace(prepare_operation=AsyncMock(), append_event=AsyncMock())
    executor = AgentExecutor(admitted.settings, store, None)
    executor.codex_runtime = SimpleNamespace(execute=AsyncMock())
    with pytest.raises(ExecutionError, match="Invalid pinned Codex egress limits"):
        await executor.execute({"snapshot": snapshot}, asyncio.Event())
    store.prepare_operation.assert_not_awaited()
    executor.codex_runtime.execute.assert_not_awaited()
    with pytest.raises(ValueError):
        pinned_codex_egress_limits(snapshot)


@pytest.mark.parametrize("old,new", [(None, LIMITS), (LIMITS, None), (FINITE, LIMITS), (LIMITS, FINITE)])
async def test_executor_uses_only_saved_pin_and_persists_numeric_metrics(admitted, old, new):
    select(admitted, old)
    snapshot = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    original = deepcopy(snapshot)
    select(admitted, new)
    store = SimpleNamespace(prepare_operation=AsyncMock(return_value="operation"),
                            resolve_operation=AsyncMock(), append_event=AsyncMock())
    executor = AgentExecutor(admitted.settings, store, None)
    executor._render_inputs = AsyncMock(return_value=("Original instructions.", "Original task.", []))
    metrics = {"accounted_request_bytes": 100, "request_bytes_read": 80, "response_bytes_read": 200,
               "connections_started": 2, "peak_concurrent": 1, "cap_reason": 0}
    captured = []

    async def execute(job, cancel, execute_tool, *, on_process_started):
        captured.append(job)
        return NativeAgentResult("succeeded", "completed", b"ok", "confirmed", {}, codex_egress_metrics=metrics)

    executor.codex_runtime = SimpleNamespace(execute=execute)
    await executor.execute({"id": "attempt", "principal": "fixture", "generation": 0,
                            "snapshot": snapshot, "request": {"execution_grant_ref": "grant"}}, asyncio.Event())
    pin = captured[0].codex_egress_limits
    assert (pin.model_dump() if pin is not None else None) == old
    assert captured[0].output_limit_bytes == snapshot["budget"]["output_limit_bytes"]
    store.append_event.assert_awaited_once_with("attempt", "codex_egress_metrics", metrics)
    assert snapshot == original


@pytest.mark.parametrize("old,new", [(None, LIMITS), (LIMITS, None), (FINITE, LIMITS), (LIMITS, FINITE)])
async def test_actual_resume_inherits_old_pin_including_absence(admitted, old, new):
    select(admitted, old)
    snapshot = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    original = deepcopy(snapshot)
    previous = {"id": "previous", "snapshot": snapshot, "request": admitted.request.model_dump(mode="json"),
                "workspace_id": snapshot["workspace_id"], "generation": snapshot["generation"], "sequence": 0,
                "status": "succeeded", "finished_at": datetime.now(timezone.utc),
                "stop_confirmation": "confirmed", "side_effect_resolution": "resolved"}
    request = admitted.request.model_copy(update={"workspace_ref": "workspace:fixture:1", "idempotency_key": "resume-1"})
    admitted.resources["grant"] = admitted.resources["grant"].model_copy(update={"workspace_ref": request.workspace_ref})
    admitted.admission.store.workspace.return_value = {**admitted.admission.store.workspace.return_value, "generation": 1}
    admitted.admission.store.operations = AsyncMock(return_value=[])
    select(admitted, new)
    resume = ResumeRequest(schema_version="1", execution=request, predecessor_snapshot_digest=content_digest(snapshot))
    result = await admitted.admission.resolve_resume("fixture", admitted.policy, previous, resume,
        ResumeSupport(modes=("durable_state",), reason="workspace_checkpoint_only"))
    assert ("codex_egress_limits" in result) == (old is not None)
    assert result.get("codex_egress_limits") == old
    assert snapshot == original


@pytest.mark.parametrize("value", [None, {}, {**LIMITS, "version": 2}])
async def test_resume_rejects_invalid_pin_even_with_valid_current_profile(admitted, value):
    select(admitted, LIMITS)
    snapshot = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    snapshot["codex_egress_limits"] = value
    previous = {"id": "previous", "snapshot": snapshot, "request": admitted.request.model_dump(mode="json"),
                "workspace_id": snapshot["workspace_id"], "generation": snapshot["generation"], "sequence": 0,
                "status": "succeeded", "finished_at": datetime.now(timezone.utc),
                "stop_confirmation": "confirmed", "side_effect_resolution": "resolved"}
    request = admitted.request.model_copy(update={"workspace_ref": "workspace:fixture:1", "idempotency_key": "resume-1"})
    admitted.resources["grant"] = admitted.resources["grant"].model_copy(update={"workspace_ref": request.workspace_ref})
    admitted.admission.store.workspace.return_value = {**admitted.admission.store.workspace.return_value, "generation": 1}
    admitted.admission.store.operations = AsyncMock(return_value=[])
    resume = ResumeRequest(schema_version="1", execution=request, predecessor_snapshot_digest=content_digest(snapshot))
    with pytest.raises(ExecutionError) as error:
        await admitted.admission.resolve_resume("fixture", admitted.policy, previous, resume,
            ResumeSupport(modes=("durable_state",), reason="workspace_checkpoint_only"))
    assert error.value.reason_code == "snapshot_mismatch"


def test_pin_value_is_frozen():
    value = CodexEgressLimits.model_validate(LIMITS)
    with pytest.raises(ValueError):
        value.total_bytes = "disabled"

