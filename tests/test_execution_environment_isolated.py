"""Explicit environment authority, outer mount identity and official SDK delegation; model-free."""

import asyncio
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kotoyose import config as config_module
from kotoyose.config import AliasAgent
from kotoyose.codex_coding_policy import (
    EXTERNAL_SANDBOX_POLICY, EXTERNAL_TOOL_POLICY_DIGEST, protected_workspace_entries,
)
from kotoyose.execution_resources import ExecutionError
from kotoyose.execution_service import ExecutionService
from kotoyose.native_codex import NativeCodexWorker
from kotoyose.provider_jail import build_jail_command
from tests.test_codex_host_admission import admitted as admitted, pinned
from tests.test_codex_host_coding import job, reviewed_tree
from tests.test_codex_sandbox_settings import SettingsRPC


def prepare(workspace):
    for name in (".codex", ".agents"):
        (workspace / name).mkdir(exist_ok=True)
    return protected_workspace_entries(workspace)


def isolated_job(tmp_path, **overrides):
    current = job(tmp_path)
    from dataclasses import replace
    return replace(current, execution_environment="isolated", codex_sandbox_policy=EXTERNAL_SANDBOX_POLICY,
                   codex_tool_policy_digest=EXTERNAL_TOOL_POLICY_DIGEST,
                   codex_protected_workspace=prepare(current.workspace), **overrides)


class ExternalRPC(SettingsRPC):
    def __init__(self, settings=None, acknowledge=True):
        super().__init__()
        self.settings = settings
        self.acknowledge = acknowledge

    async def request(self, method, params):
        result = await super().request(method, params)
        if method == "thread/settings/update":
            if not self.acknowledge:
                raise ValueError("provider_error")
            settings = self.settings if self.settings is not None else {
                "cwd": "/workspace", "approvalPolicy": "never",
                "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "restricted"},
            }
            self.pending.appendleft({"method": "thread/settings/updated",
                                     "params": {"threadId": "thread", "threadSettings": settings}})
        return result


@pytest.mark.parametrize("saved", [None, "read-only", "workspace-write", "danger-full-access"])
async def test_isolated_uses_explicit_external_policy_not_inactive_host_preference(tmp_path, saved):
    current = isolated_job(tmp_path, codex_sandbox=saved)
    rpc = ExternalRPC()
    assert await NativeCodexWorker()._run(rpc, current, asyncio.Event(), AsyncMock(), {}, {}, {}) == b"repaired"
    methods = [name for name, _ in rpc.requests]
    assert methods == ["initialize", "thread/start", "thread/settings/update", "turn/start"]
    assert dict(rpc.requests)["thread/start"]["sandbox"] == "read-only"
    assert dict(rpc.requests)["thread/settings/update"] == {
        "threadId": "thread", "cwd": "/workspace", "approvalPolicy": "never",
        "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "restricted"},
    }


@pytest.mark.parametrize("bad", [
    {"cwd": "/tmp"}, {"approvalPolicy": "on-request"},
    {"sandboxPolicy": {"type": "dangerFullAccess"}},
    {"sandboxPolicy": {"type": "externalSandbox", "networkAccess": "enabled"}},
])
async def test_external_applied_settings_must_match_before_model_input(tmp_path, bad):
    settings = {"cwd": "/workspace", "approvalPolicy": "never",
                "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "restricted"}, **bad}
    rpc = ExternalRPC(settings)
    with pytest.raises(ValueError, match="sandbox_policy_mismatch"):
        await NativeCodexWorker()._run(rpc, isolated_job(tmp_path), asyncio.Event(), AsyncMock(), {}, {}, {})
    assert "turn/start" not in dict(rpc.requests)


async def test_external_update_error_never_retries_without_sandbox(tmp_path):
    rpc = ExternalRPC(acknowledge=False)
    with pytest.raises(ValueError, match="provider_error"):
        await NativeCodexWorker()._run(rpc, isolated_job(tmp_path), asyncio.Event(), AsyncMock(), {}, {}, {})
    assert [name for name, _ in rpc.requests] == ["initialize", "thread/start", "thread/settings/update"]


async def test_queue_ack_is_not_applied_settings_and_never_starts_turn(tmp_path):
    rpc = SettingsRPC()
    rpc.pending.clear()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(.02):
            await NativeCodexWorker()._run(rpc, isolated_job(tmp_path), asyncio.Event(), AsyncMock(), {}, {}, {})
    assert "thread/settings/update" in dict(rpc.requests) and "turn/start" not in dict(rpc.requests)


async def test_admission_pins_external_authority_and_prepared_mount_identities(admitted):
    workspace = Path(admitted.admission.store.workspace.return_value["path"])
    protection = prepare(workspace)
    plan = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    assert plan["execution_environment"] == "isolated"
    assert plan["codex_sandbox_policy"] == EXTERNAL_SANDBOX_POLICY
    assert plan["codex_tool_policy_digest"] == EXTERNAL_TOOL_POLICY_DIGEST
    assert plan["codex_protected_workspace"] == protection
    assert plan["network"] == "none" and plan["network_policy_enforced"] is True
    assert plan["resource_limits"] == "provider_systemd_cgroup_v1"
    assert plan["credential_isolation"] is True
    assert ExecutionService.__new__(ExecutionService)._worker_for(plan) is None


async def test_missing_metadata_never_creates_workspace_placeholders(admitted):
    workspace = Path(admitted.admission.store.workspace.return_value["path"])
    for child in list(workspace.iterdir()):
        child.rmdir()
    with pytest.raises(ExecutionError, match="requires_prepared"):
        await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    assert list(workspace.iterdir()) == []


@pytest.mark.parametrize("mode", ["linked", "symlink", "commondir", "alternates"])
def test_external_git_metadata_is_rejected_without_new_mounts(tmp_path, mode):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    prepare(workspace)
    git = workspace / ".git"
    if mode == "linked":
        git.write_text("gitdir: /outside/repo/.git/worktrees/fixture\n")
    elif mode == "symlink":
        git.symlink_to(tmp_path)
    else:
        git.mkdir()
        path = git / ("commondir" if mode == "commondir" else "objects/info/alternates")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("/outside/repo/.git\n")
    with pytest.raises(ValueError, match="(self_contained|shared_git_metadata)"):
        protected_workspace_entries(workspace)


def test_protected_paths_are_read_only_but_git_is_in_workspace_rw_mount(tmp_path, monkeypatch):
    current = isolated_job(tmp_path)
    (current.workspace / ".git").mkdir()
    rootfs = reviewed_tree(tmp_path, True)
    sock = tmp_path / "broker.sock"
    monkeypatch.setattr(Path, "is_socket", lambda self: self == sock)
    catalog = tmp_path / "models.json"
    catalog.write_text("{}")
    command = build_jail_command(Path("/usr/bin/false"), [], provider="codex", rootfs=rootfs,
        egress_socket=sock, model_catalog=catalog, workspace=current.workspace,
        protected_workspace=current.codex_protected_workspace)
    ro = [command[i + 1:i + 3] for i, item in enumerate(command) if item == "--ro-bind"]
    rw = [command[i + 1:i + 3] for i, item in enumerate(command) if item == "--bind"]
    assert rw == [[str(current.workspace), "/workspace"]]
    assert [str(current.workspace / ".codex"), "/workspace/.codex"] in ro
    assert [str(current.workspace / ".agents"), "/workspace/.agents"] in ro
    assert not any(destination.endswith("/.git") for _, destination in ro)
    assert "--share-net" not in command


@pytest.mark.parametrize("change", ["replace", "symlink", "forged"])
def test_protected_identity_cannot_be_substituted_before_launch(tmp_path, change):
    current = isolated_job(tmp_path)
    saved = deepcopy(current.codex_protected_workspace)
    if change == "forged":
        saved[0]["inode"] += 1
    else:
        path = current.workspace / ".codex"
        path.rename(current.workspace / "old")
        if change == "replace": path.mkdir()
        else: path.symlink_to(current.workspace / "old", target_is_directory=True)
    with pytest.raises(ValueError):
        protected_workspace_entries(current.workspace, saved)


@pytest.mark.parametrize("change", [
    {"execution_environment": "isolated"}, {"codex_sandbox_policy": EXTERNAL_SANDBOX_POLICY},
    {"codex_tool_policy_digest": EXTERNAL_TOOL_POLICY_DIGEST},
])
def test_historical_policy_cannot_be_partially_upgraded(change):
    with pytest.raises(ExecutionError, match="executor changed"):
        ExecutionService.__new__(ExecutionService)._worker_for({**pinned(), **change})


@pytest.mark.parametrize("bad", [None, {}, [], [{"name": ".codex", "device": 1, "inode": 2}],
                                 [{"name": ".codex", "device": True, "inode": 2},
                                  {"name": ".agents", "device": 1, "inode": 2}]])
def test_protection_pin_requires_exact_typed_two_entry_shape(tmp_path, bad):
    current = isolated_job(tmp_path)
    with pytest.raises(ValueError, match="protected_workspace_changed"):
        protected_workspace_entries(current.workspace, bad)


@pytest.mark.parametrize("event", [
    {"method": "thread/settings/updated", "params": None},
    {"method": "thread/settings/updated", "params": {"threadSettings": None}},
    {"method": "thread/settings/updated", "params": {"threadId": "other", "threadSettings": {}}},
    {"method": "item/started", "params": {}}, {"id": 1, "method": "approval/request"},
    {"method": None},
])
async def test_invalid_or_early_effect_messages_never_start_model_input(tmp_path, event):
    rpc = SettingsRPC()
    rpc.pending.clear()
    rpc.pending.append(event)
    with pytest.raises(ValueError, match="(sandbox_policy_mismatch|invalid_protocol)"):
        await NativeCodexWorker()._run(rpc, isolated_job(tmp_path), asyncio.Event(), AsyncMock(), {}, {}, {})
    assert "turn/start" not in dict(rpc.requests)


async def test_saved_attempt_cannot_switch_execution_environment_before_launch(admitted):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from kotoyose.execution_agent import AgentExecutor
    preset = config_module._config.models["saved-coding"]
    workspace = Path(admitted.admission.store.workspace.return_value["path"])
    prepare(workspace)
    preset.execution_environment = None
    plan = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    assert plan["execution_environment"] == "isolated"
    original = deepcopy(plan)
    preset.execution_environment = "host"
    again = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    assert again["execution_environment"] == "isolated"
    store = SimpleNamespace(prepare_operation=AsyncMock(side_effect=AssertionError("launch forbidden")))
    executor = AgentExecutor(admitted.settings, store, Mock())
    with pytest.raises(AssertionError, match="launch forbidden"):
        await executor.execute({"id": "attempt", "principal": "fixture", "generation": 0,
                                "snapshot": plan, "request": admitted.request.model_dump()}, asyncio.Event())
    stripped = deepcopy(plan)
    stripped.pop("execution_environment")
    store.prepare_operation.reset_mock()
    with pytest.raises(ExecutionError, match="mode or workspace changed"):
        await executor.execute({"id": "attempt", "principal": "fixture", "generation": 0,
                                "snapshot": stripped, "request": admitted.request.model_dump()}, asyncio.Event())
    assert plan == original
    store.prepare_operation.assert_not_awaited()


def host_request(admitted, *, preserve_billing=False):
    preset = config_module._config.models["saved-coding"]
    preset.agent = AliasAgent(place="host", surface="write")
    preset.execution_environment = "host"
    admitted.admission.worker_capabilities = admitted.settings.worker_capabilities
    changes = {"tool_refs": []}
    if not preserve_billing:
        changes.update(required_provider=None, required_billing_mode=None, required_authentication_mode=None,
                       expected_authentication_binding_digest=None)
    return admitted.request.model_copy(update=changes)


async def test_host_switch_rejects_unsupported_codex_agent_backend(admitted):
    request = host_request(admitted)
    admitted.policy = admitted.policy.model_copy(update={"places": ["isolated", "host"]})
    admitted.resources["grant"] = admitted.resources["grant"].model_copy(update={"isolation": "native"})
    with pytest.raises(ExecutionError, match="Host agent execution is not supported") as exc_info:
        await admitted.admission.resolve("fixture", admitted.policy, request)
    assert exc_info.value.status == 422


async def test_actual_resume_keeps_environment_and_external_policy_immutable(admitted):
    from datetime import datetime, timezone
    from kotoyose.execution_contract import ResumeRequest
    from kotoyose.execution_resources import content_digest
    from kotoyose.runtime.resume import ResumeSupport
    preset = config_module._config.models["saved-coding"]
    prepare(Path(admitted.admission.store.workspace.return_value["path"]))
    preset.execution_environment = None
    snapshot = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    original = deepcopy(snapshot)
    assert snapshot["execution_environment"] == "isolated"
    assert snapshot["codex_sandbox_policy"] == EXTERNAL_SANDBOX_POLICY
    previous = {"id": "previous", "snapshot": snapshot, "request": admitted.request.model_dump(mode="json"),
                "workspace_id": snapshot["workspace_id"], "generation": snapshot["generation"], "sequence": 0,
                "status": "succeeded", "finished_at": datetime.now(timezone.utc),
                "stop_confirmation": "confirmed", "side_effect_resolution": "resolved"}
    request = admitted.request.model_copy(update={"workspace_ref": "workspace:fixture:1", "idempotency_key": "resume"})
    admitted.resources["grant"] = admitted.resources["grant"].model_copy(update={"workspace_ref": request.workspace_ref})
    admitted.admission.store.workspace.return_value = {**admitted.admission.store.workspace.return_value, "generation": 1}
    admitted.admission.store.operations = AsyncMock(return_value=[])
    preset.execution_environment = "isolated"
    resume = ResumeRequest(schema_version="1", execution=request, predecessor_snapshot_digest=content_digest(snapshot))
    support = ResumeSupport(modes=("durable_state",), reason="workspace_checkpoint_only")
    result = await admitted.admission.resolve_resume("fixture", admitted.policy, previous, resume, support)
    assert result["execution_environment"] == "isolated" and result["codex_sandbox_policy"] == EXTERNAL_SANDBOX_POLICY
    assert result["codex_protected_workspace"] == snapshot["codex_protected_workspace"]
    historical = deepcopy(previous)
    historical["snapshot"] = {key: value for key, value in snapshot.items() if key != "execution_environment"}
    historical_request = request.model_copy(update={"idempotency_key": "historical"})
    historical_resume = ResumeRequest(
        schema_version="1", execution=historical_request,
        predecessor_snapshot_digest=content_digest(historical["snapshot"]),
    )
    with pytest.raises(ExecutionError) as error:
        await admitted.admission.resolve_resume(
            "fixture", admitted.policy, historical, historical_resume, support,
        )
    assert error.value.reason_code == "runtime_mismatch"
    assert snapshot == original


async def test_read_room_also_delegates_to_external_sandbox(tmp_path):
    """閲覧部屋も容器が境界。SDK の read-only では容器内で command が動かないので externalSandbox へ委譲する。"""
    from dataclasses import replace

    from kotoyose.codex_coding_policy import READ_SANDBOX_POLICY, READ_TOOL_POLICY_DIGEST

    current = replace(job(tmp_path), codex_execution_mode="room_read_v1",
                      tool_refs=["builtin:workspace-read@1"], execution_environment="isolated",
                      codex_sandbox="read-only", codex_sandbox_policy=READ_SANDBOX_POLICY,
                      codex_tool_policy_digest=READ_TOOL_POLICY_DIGEST)
    rpc = ExternalRPC()
    assert await NativeCodexWorker()._run(rpc, current, asyncio.Event(), AsyncMock(), {}, {}, {}) == b"repaired"
    methods = [name for name, _ in rpc.requests]
    assert methods == ["initialize", "thread/start", "thread/settings/update", "turn/start"]
    assert dict(rpc.requests)["thread/start"]["cwd"] == "/workspace"
    assert dict(rpc.requests)["thread/settings/update"]["sandboxPolicy"] == {
        "type": "externalSandbox", "networkAccess": "restricted",
    }
