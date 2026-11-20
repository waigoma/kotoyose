"""Existing wire/Grant admission and restart proof for opt-in host coding; no database or OS launch."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from kotoyose.capabilities import CapabilityManifest, CapabilitySupport
from kotoyose.config import AgentRoute, AliasAgent, Client, Connection, HarnessConfig, ModelPreset
from kotoyose.codex_coding_policy import (
    EXTERNAL_SANDBOX_POLICY, EXTERNAL_TOOL_POLICY_DIGEST, HOST_MODE, HOST_TOOL_REF, TOOL_POLICY_DIGEST,
)
from kotoyose.codex_provider_scope import CodexProviderExecutor, PROVIDER_WORKER_ID
from kotoyose.execution_admission import ExecutionAdmission
from kotoyose.execution_contract import AgentExecution
from kotoyose.execution_resources import (
    AgentDefinition, CallerPolicy, DriverCell, DriverInstall, ExecutionBudget, ExecutionError, ExecutionGrant,
    InputText, NativeExecutionConfig, NativeRuntimeProfile, TimeoutPolicy, codex_authentication_binding,
    codex_profile_authentication, default_worker_capabilities,
)
from kotoyose.execution_service import ExecutionService
from kotoyose.codex_rootfs import rootfs_digest
from tests.test_codex_host_coding import reviewed_tree
from tests.codex_subscription_fixture import subscription_auth


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    from kotoyose import config as config_module
    root = reviewed_tree(tmp_path, True)
    auth, _ = subscription_auth(tmp_path)
    monkeypatch.setattr(config_module, "_config", HarnessConfig(
        connections={"codex-chatgpt": Connection(provider="codex", kind="subscription", data_policy="no_training", auth_file=auth)},
        routes={"coding": AgentRoute(driver="codex.app-server", model="gpt-6-luna", connection="codex-chatgpt")},
        models={"saved-coding": ModelPreset(
            effort="max", route="coding",
            agent=AliasAgent(place="isolated", surface="write"),
        )},
        clients={"fixture": Client(
            workspace_roots=[tmp_path], models=["saved-coding"],
            places=["isolated"], max_run_seconds=60, max_memory_bytes=128 * 1024 * 1024,
            max_processes=32, max_cpus=1, max_concurrent_runs=1,
        )},
    ))
    runtime = NativeRuntimeProfile(provider="codex", model="gpt-6-luna", cli_binary="/usr/bin/false",
        codex_execution_mode=HOST_MODE, codex_rootfs=root, codex_rootfs_digest=rootfs_digest(root, coding=True),
        credential_mode="chatgpt_subscription", credential_name="CODEX_CHATGPT_AUTH", codex_auth_file=auth)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in (".codex", ".agents"):
        (workspace / name).mkdir()
    info = workspace.stat()
    now = datetime.now(timezone.utc)
    policy = CallerPolicy(workspace_roots=[tmp_path], agent_models=["saved-coding"],
        places=["isolated"], max_seconds=60, max_memory_bytes=128 * 1024 * 1024,
        max_pids=32, max_cpus=1, max_concurrent=1)
    settings = NativeExecutionConfig(schema_version="1", image_id="sha256:" + "a" * 64,
        drivers={"codex.app-server": DriverInstall(
            cli_binary=runtime.cli_binary,
            cells={"coding": DriverCell(rootfs=runtime.codex_rootfs, rootfs_digest=runtime.codex_rootfs_digest)},
        )})
    grant = ExecutionGrant(schema_version="1", kind="grant", grant_id="grant-1", version=1,
        caller_run_ref="run-1", workspace_ref="workspace:fixture:0", isolation="sandbox", network="none",
        agent_models=["saved-coding"], resource_refs=["definition", "budget", "timeout", "input", HOST_TOOL_REF],
        policy_ref="policy", not_before=now - timedelta(seconds=1), expires_at=now + timedelta(seconds=60),
        max_seconds=60, delegation="none")
    resources = {
        "grant": grant,
        "definition": AgentDefinition(schema_version="1", kind="agent_definition", instructions="Original instructions.",
            output_contract="text", max_turns=8, timeout_policy_ref="timeout"),
        "timeout": TimeoutPolicy(schema_version="1", kind="timeout_policy", soft_seconds=20, hard_seconds=30, grace_seconds=1),
        "budget": ExecutionBudget(schema_version="1", kind="budget", reservation_ref="reservation", mode="observed",
            deadline=now + timedelta(seconds=45), memory_bytes=128 * 1024 * 1024, pids_limit=32, cpus=.5, output_limit_bytes=65536),
        "input": InputText(schema_version="1", kind="text", text="Original task.", provenance="project"),
    }
    async def resource(principal, ref):
        return resources[ref]
    store = SimpleNamespace(resource=resource, workspace=AsyncMock(return_value={"id": "fixture", "path": str(workspace),
        "device": info.st_dev, "inode": info.st_ino, "generation": 0, "caller_ref": "repo-1"}))
    manifest = CapabilityManifest(capabilities={name: CapabilitySupport(status="supported", reason="fixture")
                                               for name in ("native_execution", "sandbox")})
    admission = ExecutionAdmission(store, manifest, settings.image_id, settings=settings)
    binding = codex_authentication_binding(codex_profile_authentication(runtime))
    request = AgentExecution(schema_version="1", kind="agent", idempotency_key="dispatch-1", caller_run_ref="run-1",
        workspace_ref="workspace:fixture:0", execution_grant_ref="grant", budgets_ref="budget", agent_definition_ref="definition",
        model="saved-coding", tool_refs=[HOST_TOOL_REF], inputs=[{"kind": "task_request", "provenance": "project", "content_ref": "input"}],
        required_provider="codex", required_billing_mode="subscription", required_authentication_mode="chatgpt_subscription",
        expected_authentication_binding_digest=binding["binding_digest"])
    return SimpleNamespace(admission=admission, request=request, policy=policy, settings=settings, resources=resources, runtime=runtime)


async def test_saved_alias_host_wire_pins_provider_only_executor(admitted, monkeypatch):
    legacy_select = Mock(side_effect=AssertionError("must not select Docker/CommandWorker"))
    monkeypatch.setattr(admitted.admission, "_select_worker", legacy_select)
    plan = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    assert plan["alias"] == "saved-coding" and plan["model"] == "gpt-6-luna"
    assert plan["codex_preferences"]["effort"] == "max"
    assert plan["codex_execution_mode"] == HOST_MODE
    assert plan["execution_environment"] == "isolated"
    assert plan["codex_sandbox_policy"] == EXTERNAL_SANDBOX_POLICY
    assert plan["codex_tool_policy_digest"] == EXTERNAL_TOOL_POLICY_DIGEST
    assert plan["worker_id"] == PROVIDER_WORKER_ID and plan["worker_capabilities"] == CodexProviderExecutor.public_snapshot()
    assert plan["image_id"] is None and plan["isolation"] == "sandbox" and plan["network"] == "none"
    assert plan["client_tools"] == [] and plan["client_key"] is None
    assert plan["definition"]["instructions"] == "Original instructions."
    assert plan["inputs"][0]["text"] == "Original task."
    assert plan["tool_refs"] == [HOST_TOOL_REF]
    assert plan["runtime_billing"]["metered_fallback"] == "forbidden"
    assert "access_token" not in str(plan) and "false-refresh-secret" not in str(plan)
    legacy_select.assert_not_called()


@pytest.mark.parametrize("refs", [[], ["builtin:sandbox-exec@1"], [HOST_TOOL_REF, "builtin:sandbox-exec@1"]])
async def test_host_admission_requires_distinct_tool_authority(admitted, refs):
    with pytest.raises(ExecutionError, match="explicitly admitted workspace tool"):
        await admitted.admission.resolve("fixture", admitted.policy, admitted.request.model_copy(update={"tool_refs": refs}))


async def test_request_host_ref_without_grant_ref_is_denied(admitted):
    admitted.resources["grant"] = admitted.resources["grant"].model_copy(update={
        "resource_refs": ["definition", "budget", "timeout", "input", "builtin:sandbox-exec@1"]})
    with pytest.raises(ExecutionError, match="outside grant scope"):
        await admitted.admission.resolve("fixture", admitted.policy, admitted.request)


def pinned():
    return {"kind": "agent", "backend": "native", "provider": "codex", "codex_execution_mode": HOST_MODE,
            "codex_tool_policy_digest": TOOL_POLICY_DIGEST, "worker_id": PROVIDER_WORKER_ID,
            "worker_capabilities": CodexProviderExecutor.public_snapshot(), "tool_refs": [HOST_TOOL_REF], "image_id": None}


@pytest.mark.parametrize("change", [{"kind": "command"}, {"image_id": "sha256:" + "a" * 64},
    {"worker_id": "local-docker"}, {"codex_tool_policy_digest": "0" * 64}, {"tool_refs": ["builtin:sandbox-exec@1"]},
    {"worker_capabilities": {}}])
def test_replay_mismatch_never_selects_command_worker(change):
    service = ExecutionService.__new__(ExecutionService)
    with pytest.raises(ExecutionError, match="executor changed"):
        service._worker_for({**pinned(), **change})


def test_provider_descriptor_does_not_change_command_registry():
    service = ExecutionService.__new__(ExecutionService)
    service.config = SimpleNamespace(worker_capabilities=default_worker_capabilities())
    docker = object()
    service.workers = {"local-docker": docker}
    assert service._worker_for(pinned()) is None
    old = {"worker_id": "local-docker", "worker_capabilities": service.config.worker_capabilities["local-docker"].public_snapshot()}
    assert service._worker_for(old) is docker
    assert PROVIDER_WORKER_ID not in service.config.worker_capabilities


@pytest.mark.parametrize("pid_proof,scope_proof,expected", [("confirmed", "confirmed", "confirmed"),
    ("confirmed", "unconfirmed", "unconfirmed"), ("unconfirmed", "confirmed", "unconfirmed")])
async def test_restart_requires_pid_and_scope_evidence(monkeypatch, pid_proof, scope_proof, expected):
    import kotoyose.codex_stop_recovery as recovery_module
    pid_stop = AsyncMock(return_value=pid_proof)
    scope_stop = AsyncMock(return_value=scope_proof)
    monkeypatch.setattr(recovery_module, "stop_codex_process", pid_stop)
    monkeypatch.setattr(recovery_module, "stop_recovered_provider_scope", scope_stop)
    identity = {"pid": 42, "start_ticks": 7, "uid": 1000, "boot_id": "fixture-boot"}
    scope = {"unit": "kotoyose-native-attempt-1-provider-7.scope", "control_group": "/fixture", "device": 1, "inode": 2}
    service = ExecutionService.__new__(ExecutionService)
    service.store = SimpleNamespace(operations=AsyncMock(return_value=[{"id": "operation", "kind": "provider_process",
        "state": "unknown", "result": {**identity, "codex_provider_scope": scope}}]), resolve_operation=AsyncMock())
    result = await service._stop_attempt({"id": "attempt-1", "generation": 7, "snapshot": pinned()})
    assert result == expected
    scope_stop.assert_awaited_once_with("attempt-1", 7, scope)
    assert pid_stop.await_args.args[1] == 0
    service.store.resolve_operation.assert_awaited_once_with("operation", expected)


@pytest.mark.parametrize("pin", [None, "future-mode"])
async def test_saved_attempt_mode_is_never_inferred_from_current_alias(admitted, pin):
    from kotoyose.execution_agent import AgentExecutor
    plan = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    if pin is None:
        plan.pop("codex_execution_mode")
        plan.pop("codex_tool_policy_digest")
    else:
        plan["codex_execution_mode"] = pin
    original = dict(plan)
    store = SimpleNamespace(prepare_operation=AsyncMock(side_effect=AssertionError("must fail before journal/launch")))
    executor = AgentExecutor(admitted.settings, store, Mock())
    with pytest.raises(ExecutionError, match="mode or workspace changed"):
        await executor.execute({"id": "attempt-1", "principal": "fixture", "generation": 0,
                                "snapshot": plan, "request": admitted.request.model_dump()}, __import__("asyncio").Event())
    assert plan == original
    store.prepare_operation.assert_not_awaited()


def _room_capacity(running: int, free: int, *, max_containers: int = 2, min_free_disk_bytes: int = 50):
    from kotoyose.agent_container import AgentRoomCapacity
    from kotoyose.execution_resources import AgentContainerConfig
    limits = AgentContainerConfig(
        agent_root="/opt/agent", home_root="/var/lib/kotoyose/home",
        max_containers=max_containers, min_free_disk_bytes=min_free_disk_bytes,
    )
    async def count():
        return running
    return AgentRoomCapacity(limits, running=count, free_bytes=lambda path: free)


async def test_host_plan_resolves_while_containers_are_full(admitted):
    """部屋が満杯でも計画は作る。枠の拒否は Hub の作成ではなく admit の鎖の下で行う。"""
    admitted.admission.store.admit = AsyncMock(side_effect=AssertionError("attempt must not be created"))
    admitted.admission.store.agent_room_capacity = _room_capacity(2, 50, max_containers=2)
    snapshot = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    assert snapshot["codex_execution_mode"] == HOST_MODE
    admitted.admission.store.admit.assert_not_awaited()


async def test_host_plan_resolves_while_disk_is_low(admitted):
    """空き不足でも計画は作る。disk の拒否も admit まで遅らせる。"""
    admitted.admission.store.admit = AsyncMock(side_effect=AssertionError("attempt must not be created"))
    admitted.admission.store.agent_room_capacity = _room_capacity(0, 49, min_free_disk_bytes=50)
    snapshot = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    assert snapshot["codex_execution_mode"] == HOST_MODE
    admitted.admission.store.admit.assert_not_awaited()


async def test_host_admission_proceeds_below_room_limits(admitted):
    admitted.admission.store.agent_room_capacity = _room_capacity(1, 50, max_containers=2, min_free_disk_bytes=50)
    snapshot = await admitted.admission.resolve("fixture", admitted.policy, admitted.request)
    assert snapshot["codex_execution_mode"] == HOST_MODE
