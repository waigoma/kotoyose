"""Saved Codex settings reach the existing admitted execution path; no real CLI or auth."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kotoyose import config as config_module
from kotoyose.config import AliasAgent
from kotoyose.codex_coding_policy import HOST_MODE, DEFAULT_CODEX_MODE, resolve_codex_sandbox
from kotoyose.execution_agent import AgentExecutor
from kotoyose.execution_contract import ResumeRequest
from kotoyose.execution_resources import DriverCell, ExecutionError, content_digest, resolve_agent_model
from kotoyose.execution_service import ExecutionService
from kotoyose.execution_snapshot import ResolvedAttemptSnapshot, resolved_attempt_snapshot
from kotoyose.native_claude import NativeAgentResult
from kotoyose.native_codex import NativeCodexWorker
from kotoyose.runtime.resume import ResumeSupport
from tests.test_codex_host_admission import admitted as admitted, pinned
from tests.test_codex_host_coding import ScriptedRPC, answer, event, job


MODES = ("read-only", "workspace-write", "danger-full-access")


def applied(mode):
    policy = {
        "read-only": {"type": "readOnly", "networkAccess": False},
        "workspace-write": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False,
                            "excludeTmpdirEnvVar": False, "excludeSlashTmp": False},
        "danger-full-access": {"type": "dangerFullAccess"},
    }[mode]
    return {"cwd": "/workspace", "approvalPolicy": "never", "sandbox": policy,
            "runtimeWorkspaceRoots": ["/workspace"]}


class SettingsRPC(ScriptedRPC):
    def __init__(self, returned=None):
        super().__init__([answer(), event("turn/completed", turn={"id": "turn", "status": "completed"})])
        self.returned = returned

    async def request(self, method, params):
        result = await super().request(method, params)
        if method == "thread/start":
            result.update(applied(params["sandbox"]) if self.returned is None else self.returned)
        return result


@pytest.mark.parametrize("saved,expected", [
    (None, "workspace-write"), ("full-access", "danger-full-access"), *[(mode, mode) for mode in MODES],
])
async def test_saved_choice_is_explicitly_pinned_at_new_host_admission(admitted, saved, expected):
    preset = config_module._config.models["saved-coding"]
    preset.codex = {} if saved is None else {"sandbox": saved}
    plan = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    assert plan["codex_sandbox"] == expected
    assert plan["codex_preferences"] == {"requested_effort": "max", "effort": "max", "timeout_seconds": None}
    assert "codex_sandbox" in ResolvedAttemptSnapshot.model_fields


async def test_public_resolved_pin_is_in_digest_and_old_absence_stays_absent(admitted):
    snapshot = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    request = admitted.request.model_dump(mode="json")
    attempt = {"id": "attempt", "sequence": 0, "request": request, "request_digest": content_digest(request),
               "snapshot": snapshot}
    public = resolved_attempt_snapshot(attempt)
    assert public.codex_sandbox == "workspace-write"
    assert public.snapshot_digest == content_digest(snapshot)
    changed = {**snapshot, "codex_sandbox": "danger-full-access"}
    assert content_digest(changed) != public.snapshot_digest
    snapshot.pop("codex_sandbox")
    assert "codex_sandbox" not in resolved_attempt_snapshot(attempt).model_dump(mode="json")


@pytest.mark.parametrize("value", [True, False, 0, "", "externalSandbox", "external-sandbox", {}, []])
async def test_invalid_or_external_choice_never_silently_falls_back(admitted, value):
    config_module._config.models["saved-coding"].codex = {"sandbox": value}
    with pytest.raises(ExecutionError, match="codex.sandbox requires"):
        await admitted.admission.resolve("fixture", admitted.policy, admitted.request)


@pytest.mark.parametrize("value", [True, "false", 0, "yes"])
async def test_native_bypass_flag_cannot_override_saved_setting(admitted, value):
    config_module._config.models["saved-coding"].codex = {
        "sandbox": "read-only", "dangerously_bypass_approvals_and_sandbox": value}
    with pytest.raises(ExecutionError, match="does not support dangerously_bypass"):
        await admitted.admission.resolve("fixture", admitted.policy, admitted.request)


@pytest.mark.parametrize("saved", [None, "read-only", "workspace-write", "danger-full-access"])
def test_new_legacy_admission_preserves_omission_but_refuses_incompatible_selection(admitted, saved):
    from kotoyose.config import Connection
    cfg = config_module._config
    cfg.models["saved-coding"].agent = AliasAgent(place="isolated", surface="exec")
    cfg.models["saved-coding"].codex = {} if saved is None else {"sandbox": saved}
    cfg.connections["codex-env"] = Connection(provider="codex", kind="api_key", data_policy="no_training", secret="env:FIXTURE")
    cfg.routes["coding"] = cfg.routes["coding"].model_copy(update={"connection": "codex-env"})
    install = admitted.settings.drivers["codex.app-server"]
    admitted.settings.drivers["codex.app-server"] = install.model_copy(update={"cells": {
        **install.cells,
        "standard": DriverCell(rootfs=Path("/fixture/standard-rootfs"), rootfs_digest="0" * 64),
    }})
    if saved not in {None, "read-only"}:
        with pytest.raises(ValueError, match="requires read-only"):
            resolve_agent_model("saved-coding", admitted.settings)
    else:
        resolved = resolve_agent_model("saved-coding", admitted.settings)
        assert resolved.codex_execution_mode == DEFAULT_CODEX_MODE
        assert "codex_sandbox" not in resolved.model_dump()
        assert resolved.execution_environment is None
        assert resolve_codex_sandbox(DEFAULT_CODEX_MODE, resolved.codex_sandbox) == "read-only"


@pytest.mark.parametrize("mode", MODES)
async def test_exact_selected_mode_is_applied_before_model_input(tmp_path, mode):
    rpc = SettingsRPC()
    result = await NativeCodexWorker()._run(
        rpc, job(tmp_path, codex_sandbox=mode), asyncio.Event(), AsyncMock(), {}, {}, {})
    assert result == b"repaired"
    assert dict(rpc.requests)["thread/start"]["sandbox"] == mode
    assert dict(rpc.requests)["thread/start"]["approvalPolicy"] == "never"
    assert len([method for method, _ in rpc.requests if method == "thread/start"]) == 1


@pytest.mark.parametrize("changed", [
    {"cwd": "/tmp"}, {"approvalPolicy": "on-request"}, {"runtimeWorkspaceRoots": ["/workspace", "/other"]},
    {"sandbox": {"type": "dangerFullAccess"}}, {"sandbox": {"type": "externalSandbox", "networkAccess": "restricted"}},
    {"sandbox": {**applied("workspace-write")["sandbox"], "networkAccess": True}},
    {"sandbox": {**applied("workspace-write")["sandbox"], "writableRoots": ["/other"]}},
])
async def test_applied_policy_mismatch_stops_before_model_input_without_retry(tmp_path, changed):
    rpc = SettingsRPC({**applied("workspace-write"), **changed})
    with pytest.raises(ValueError, match="sandbox_policy_mismatch"):
        await NativeCodexWorker()._run(
            rpc, job(tmp_path, codex_sandbox="workspace-write"), asyncio.Event(), AsyncMock(), {}, {}, {})
    assert [method for method, _ in rpc.requests] == ["initialize", "thread/start"]


@pytest.mark.parametrize("saved,old_pin,expected", [
    ("read-only", "danger-full-access", "danger-full-access"),
    ("danger-full-access", "read-only", "read-only"),
    ("danger-full-access", None, "workspace-write"),
])
async def test_executor_uses_admitted_pin_not_today_alias_or_new_default(admitted, saved, old_pin, expected):
    snapshot = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    if old_pin is None:
        snapshot.pop("codex_sandbox")  # historical host Attempt, before this field existed
    else:
        snapshot["codex_sandbox"] = old_pin
    original = deepcopy(snapshot)
    config_module._config.models["saved-coding"].codex = {"sandbox": saved}
    store = SimpleNamespace(prepare_operation=AsyncMock(return_value="operation"),
                            resolve_operation=AsyncMock(), append_event=AsyncMock())
    executor = AgentExecutor(admitted.settings, store, None)
    executor._render_inputs = AsyncMock(return_value=("Original instructions.", "Original task.", []))
    captured = []

    async def execute(current, cancel, execute_tool, *, on_process_started):
        captured.append(current)
        return NativeAgentResult("succeeded", "completed", b"ok", "confirmed", {})

    executor.codex_runtime = SimpleNamespace(execute=execute)
    await executor.execute({"id": "attempt", "principal": "fixture", "generation": 0,
                            "snapshot": snapshot, "request": {"execution_grant_ref": "grant"}}, asyncio.Event())
    assert captured[0].codex_sandbox == old_pin
    assert resolve_codex_sandbox(captured[0].codex_execution_mode, captured[0].codex_sandbox) == expected
    assert snapshot == original


def test_historical_absence_has_fixed_mode_specific_meaning():
    assert resolve_codex_sandbox(HOST_MODE, None) == "workspace-write"
    assert resolve_codex_sandbox(DEFAULT_CODEX_MODE, None) == "read-only"
    assert ExecutionService.__new__(ExecutionService)._worker_for(pinned()) is None
    with pytest.raises(ExecutionError, match="sandbox is unsupported"):
        ExecutionService.__new__(ExecutionService)._worker_for({**pinned(), "codex_sandbox": "externalSandbox"})


@pytest.mark.parametrize("old_pin,new_choice,allowed", [
    (None, None, True), (None, "workspace-write", True), (None, "read-only", False),
    (None, "danger-full-access", False), ("workspace-write", "workspace-write", True),
    ("read-only", "read-only", True), ("danger-full-access", "danger-full-access", True),
    ("explicit-null", None, False),
    ("read-only", "workspace-write", False), ("danger-full-access", "read-only", False),
])
async def test_actual_resolve_resume_preserves_only_historical_effective_policy(admitted, old_pin, new_choice, allowed):
    config_module._config.models["saved-coding"].codex = {"sandbox": None if old_pin == "explicit-null" else old_pin}
    old = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    if old_pin is None:
        old.pop("codex_sandbox")
    elif old_pin == "explicit-null":
        old["codex_sandbox"] = None
    immutable_old = deepcopy(old)
    previous = {"id": "previous", "snapshot": old, "request": admitted.request.model_dump(mode="json"),
                "workspace_id": old["workspace_id"], "generation": old["generation"], "sequence": 0,
                "status": "succeeded", "finished_at": datetime.now(timezone.utc),
                "stop_confirmation": "confirmed", "side_effect_resolution": "resolved"}
    request = admitted.request.model_copy(update={"workspace_ref": "workspace:fixture:1", "idempotency_key": "resume-1"})
    admitted.resources["grant"] = admitted.resources["grant"].model_copy(update={"workspace_ref": request.workspace_ref})
    workspace = {**admitted.admission.store.workspace.return_value, "generation": 1}
    admitted.admission.store.workspace.return_value = workspace
    admitted.admission.store.operations = AsyncMock(return_value=[])
    config_module._config.models["saved-coding"].codex = {"sandbox": new_choice}
    resume = ResumeRequest(schema_version="1", execution=request, predecessor_snapshot_digest=content_digest(old))
    support = ResumeSupport(modes=("durable_state",), reason="workspace_checkpoint_only")
    if allowed:
        result = await admitted.admission.resolve_resume("fixture", admitted.policy, previous, resume, support)
        assert result["codex_sandbox"] == (new_choice or "workspace-write")
        assert result["execution_environment"] == "isolated"
        assert result["resume_mode"] == "durable_state"
    else:
        with pytest.raises(ExecutionError) as error:
            await admitted.admission.resolve_resume("fixture", admitted.policy, previous, resume, support)
        assert error.value.reason_code == "runtime_mismatch"
    assert old == immutable_old
