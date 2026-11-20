"""Agent execution の admission、sandbox tool、journal、recovery を検証する。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import sys
from urllib.parse import unquote, urlparse
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from kotoyose.execution_contract import AgentExecution, ExecutionInput
from kotoyose.execution_resources import (
    AgentDefinition,
    CallerPolicy,
    ExecutionBudget,
    ExecutionError,
    DriverInstall,
    ExecutionGrant,
    InputText,
    NativeExecutionConfig,
    RepositoryContextPack,
    schema_digest_for,
    TimeoutPolicy,
    WorkspaceRegistration,
)
from kotoyose.execution_service import ExecutionService
from kotoyose.execution_store import ExecutionStore
from kotoyose.local_worker import DockerLocalWorker
from kotoyose.native_claude import NativeAgentResult
from kotoyose.runtime.base import RuntimeEvent
from tests.fixtures.clients import clients


DATABASE_URL = os.environ.get("KOTOYOSE_EXECUTION_AGENT_TEST_DATABASE_URL")
IMAGE_ID = os.environ.get("KOTOYOSE_DOCKER_TEST_IMAGE")
pytestmark = pytest.mark.skipif(
    not IMAGE_ID,
    reason="Agent conformance requires a Docker test image ID",
)


def _database_name(url: str) -> str:
    return unquote(urlparse(url).path.lstrip("/")).split("?", 1)[0]


@pytest_asyncio.fixture
async def store() -> ExecutionStore:
    """test 専用 DB だけを kotoyose schema ごと初期化し、終了時に破棄する。"""
    assert DATABASE_URL is not None, "Dedicated test database connection is required"
    if not _database_name(DATABASE_URL).startswith("ahs_execution_test_agent"):
        pytest.fail("refusing to connect: database name must start with ahs_execution_test_agent")
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=8, command_timeout=15)
    try:
        await pool.execute("DROP SCHEMA IF EXISTS kotoyose CASCADE; CREATE SCHEMA kotoyose")
        result = ExecutionStore(pool)
        await result.initialize()
        yield result
    finally:
        rows = await pool.fetch("SELECT id, generation FROM kotoyose.execution_attempts")
        for row in rows:
            process = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", f"kotoyose-{row['id']}-{row['generation']}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await process.wait()
        await pool.execute("DROP SCHEMA IF EXISTS kotoyose CASCADE")
        await pool.close()


def _ensure_profile_agent() -> None:
    """DB 付き agent 試験が route と agent を解決できるようにする。既存の chat 設定は残す。"""
    from kotoyose import config as config_module
    from kotoyose.config import AgentRoute, AliasAgent, Connection, ModelPreset

    current = config_module.get_config()
    preset = current.models.get("profile-agent")
    if preset is not None and preset.route and preset.agent is not None:
        return
    connections = dict(current.connections)
    routes = dict(current.routes)
    models = dict(current.models)
    connections["profile-conn"] = Connection(provider="claude", kind="api_key", data_policy="no_training", secret="env:KOTOYOSE_AGENT_PROVIDER_TOKEN")
    routes["profile-agent"] = AgentRoute(
        driver="claude.agent-sdk", model="claude-sonnet-4-5", connection="profile-conn",
    )
    models["profile-agent"] = ModelPreset(
        route="profile-agent",
        agent=AliasAgent(place="isolated", surface="none"),
    )
    config_module._config = current.model_copy(update={"connections": connections, "routes": routes, "models": models})


def _config(root: Path, *, trusted: bool = True, caller_profiles: list[str] | None = None) -> NativeExecutionConfig:
    from kotoyose import config as config_module
    from kotoyose.config import Client

    provenance = ["project", "agent_generated", "external_untrusted", "mixed"]
    if trusted:
        provenance.append("trusted")
    aliases = ["profile-agent"] if caller_profiles is None else caller_profiles
    _ensure_profile_agent()
    current = config_module.get_config()
    clients = dict(current.clients)
    clients["caller-a"] = Client(
        workspace_roots=[root],
        models=aliases, places=["isolated"],
        allowed_env_vars=[], allowed_provenance=provenance,
        max_run_seconds=30, max_memory_bytes=128 * 1024 * 1024,
        max_processes=32, max_cpus=1, max_concurrent_runs=2,
    )
    config_module._config = current.model_copy(update={"clients": clients})
    return NativeExecutionConfig(
        schema_version="1", image_id=IMAGE_ID or "sha256:" + "a" * 64,
        drivers={"claude.agent-sdk": DriverInstall(cli_binary=Path("/usr/bin/sh"))},
    )


class _NoopWorker:
    """admission / provider recovery test 用の process 起動なし worker。"""

    def __init__(self):
        self.probe = AsyncMock(return_value=True)
        self.execute = AsyncMock()
        self.stop = AsyncMock(return_value="confirmed")


class _FakeAgentRuntime:
    launcher_path = Path(__file__).resolve().parents[1] / "scripts" / "native-claude"

    def __init__(self, marker: Path | None = None, *, stop_confirmation: str = "confirmed"):
        self.marker = marker
        self.stop_confirmation = stop_confirmation
        self.started_pid: int | None = None
        self.job = None

    async def execute(self, job, cancel, execute_tool, *, on_process_started):
        self.job = job
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(30)",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        self.started_pid = process.pid
        try:
            await on_process_started(process.pid)
            if job.progress_event is not None:
                job.progress_event(RuntimeEvent(kind="turn", summary="Assistant turn 1", payload={"turn": 1}))
            if self.marker is not None:
                result = await execute_tool(("sh", "-c", f"printf tool > {self.marker.name}"), ".")
                assert result.status == "succeeded"
            return NativeAgentResult(
                status="succeeded", reason="completed", content=b"agent output",
                stop_confirmation=self.stop_confirmation,
                usage={"source": "reported", "cost": None, "input_tokens": 7, "output_tokens": 5},
            )
        finally:
            if process.returncode is None:
                process.terminate()
            await process.wait()


async def _resources(
    service: ExecutionService,
    policy: CallerPolicy,
    workspace: Path,
    *,
    input_provenance: str = "trusted",
    input_kind: str = "instructions",
    grant_profiles: list[str] | None = None,
    tool_refs: list[str] | None = None,
) -> tuple[AgentExecution, str, dict[str, str]]:
    workspace.mkdir(parents=True, exist_ok=True)
    workspace_ref = await service.register_workspace(
        "caller-a", policy,
        WorkspaceRegistration(schema_version="1", path=str(workspace), caller_workspace_ref=str(uuid4())),
    )
    now = datetime.now(timezone.utc)
    timeout = await service.store.put_resource(
        "caller-a", TimeoutPolicy(schema_version="1", kind="timeout_policy", soft_seconds=1, hard_seconds=10, grace_seconds=1)
    )
    budget = await service.store.put_resource(
        "caller-a", ExecutionBudget(
            schema_version="1", kind="budget", reservation_ref="reservation-agent", mode="observed",
            deadline=now + timedelta(minutes=2), memory_bytes=64 * 1024 * 1024, pids_limit=16,
            cpus=1, output_limit_bytes=8192,
        )
    )
    definition = await service.register_resource(
        "caller-a", policy,
        AgentDefinition(schema_version="1", kind="agent_definition", instructions="Return the result.",
                        output_contract="text", max_turns=2, timeout_policy_ref=timeout),
    )
    content = await service.register_resource(
        "caller-a", policy,
        InputText(schema_version="1", kind="text", text="Task input", provenance=input_provenance),
    )
    tools = tool_refs if tool_refs is not None else ["builtin:sandbox-exec@1"]
    grant = await service.store.put_resource(
        "caller-a", ExecutionGrant(
            schema_version="1", kind="grant", grant_id=str(uuid4()), version=1,
            caller_run_ref="run-agent", workspace_ref=workspace_ref, isolation="sandbox", network="none",
            command_spec_refs=[], agent_models=grant_profiles if grant_profiles is not None else ["profile-agent"],
            resource_refs=[definition, content, budget, timeout, *tools], policy_ref="policy-agent",
            not_before=now - timedelta(seconds=1), expires_at=now + timedelta(minutes=2),
            max_seconds=20, delegation="none",
        ),
    )
    request = AgentExecution(
        schema_version="1", kind="agent", idempotency_key=str(uuid4()), caller_run_ref="run-agent",
        workspace_ref=workspace_ref, execution_grant_ref=grant, budgets_ref=budget,
        agent_definition_ref=definition, model="profile-agent",
        inputs=[ExecutionInput(kind=input_kind, provenance=input_provenance, content_ref=content)],
        tool_refs=tools,
    )
    return request, workspace_ref, {"definition": definition, "content": content, "budget": budget, "timeout": timeout, "grant": grant}


async def _service_for_admission(store: ExecutionStore, root: Path, *, trusted: bool = True, caller_profiles=None):
    config = _config(root, trusted=trusted, caller_profiles=caller_profiles)
    worker = _NoopWorker()
    service = ExecutionService(config, store, worker)
    service._accepting = True
    return service, worker


@pytest.mark.asyncio
async def test_caller_cannot_issue_trusted_definition_or_input_without_provenance_authority(store, tmp_path):
    """trusted definition / input は Caller policy の provenance 権限なしに発行できない。"""
    service, _ = await _service_for_admission(store, tmp_path, trusted=False)
    policy = clients()["caller-a"]
    with pytest.raises(ExecutionError, match="agent instructions"):
        await service.register_resource(
            "caller-a", policy,
            AgentDefinition(schema_version="1", kind="agent_definition", instructions="x",
                            output_contract="text", max_turns=1, timeout_policy_ref="timeout"),
        )
    with pytest.raises(ExecutionError, match="provenance"):
        await service.register_resource(
            "caller-a", policy, InputText(schema_version="1", kind="text", text="x", provenance="trusted")
        )


@pytest.mark.asyncio
async def test_repository_context_pack_registration_requires_project_provenance(store, tmp_path):
    """Pack は repo 由来の project provenance だけを登録対象にする。"""
    service, _ = await _service_for_admission(store, tmp_path)
    policy = clients()["caller-a"]
    content = "repository facts"
    values = {
        "schema_version": "1",
        "kind": "repository_context_pack",
        "content": content,
        "content_digest": hashlib.sha256(content.encode()).hexdigest(),
        "schema_digest": schema_digest_for("repository_context_pack"),
        "generator": "test-generator",
        "version": "1",
        "config_digest": "a" * 64,
        "source_revision": "b" * 40,
        "repository_ref": "repo:test",
        "dependency_refs": [],
    }
    await service.register_resource(
        "caller-a", policy, RepositoryContextPack(**values, provenance="project"),
    )
    with pytest.raises(ExecutionError, match="project provenance"):
        await service.register_resource(
            "caller-a", policy, RepositoryContextPack(**values, provenance="agent_generated"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unknown_profile", "caller_profile", "grant_profile"])
async def test_agent_profile_must_be_known_and_scoped_before_attempt_or_worker(store, tmp_path, case):
    """未知または Caller / Grant 外の profile は Attempt 作成前に拒否する。"""
    caller_profiles = [] if case == "caller_profile" else None
    service, worker = await _service_for_admission(store, tmp_path, caller_profiles=caller_profiles)
    policy = clients()["caller-a"]
    request, _, refs = await _resources(
        service, policy, tmp_path / "workspace", grant_profiles=[] if case == "grant_profile" else None,
    )
    if case == "unknown_profile":
        request = request.model_copy(update={"model": "unknown-profile", "runtime_profile": None})
    with pytest.raises(ExecutionError):
        await service.dispatch("caller-a", policy, request)
    assert await store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 0
    worker.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "provenance"),
    [("instructions", "project"), ("task_request", "trusted")],
)
async def test_input_provenance_and_instruction_trusted_requirement_are_enforced(store, tmp_path, kind, provenance):
    """request provenance と保存済み InputText の一致、および instructions の trusted 条件を検証する。"""
    service, worker = await _service_for_admission(store, tmp_path)
    policy = clients()["caller-a"]
    request, _, refs = await _resources(service, policy, tmp_path / "workspace", input_provenance="project", input_kind=kind)
    request = request.model_copy(update={"inputs": [ExecutionInput(kind=kind, provenance=provenance, content_ref=refs["content"]) ]})
    with pytest.raises(ExecutionError):
        await service.dispatch("caller-a", policy, request)
    assert await store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 0
    worker.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["skill_refs", "mcp_refs", "tool_refs"])
async def test_agent_skill_mcp_and_unknown_tool_refs_are_rejected_before_dispatch(store, tmp_path, field):
    """未実装 skill / MCP / tool ref は worker に到達させない。"""
    service, worker = await _service_for_admission(store, tmp_path)
    policy = clients()["caller-a"]
    request, _, _ = await _resources(service, policy, tmp_path / "workspace")
    request = request.model_copy(update={field: ["unknown-ref"]})
    with pytest.raises(ExecutionError):
        await service.dispatch("caller-a", policy, request)
    assert await store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 0
    worker.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_fake_runtime_executes_real_docker_tool_and_persists_identity_artifact_usage(store, tmp_path, monkeypatch):
    """fake provider runtime でも PID identity、Docker tool、artifact、usage、世代を一貫保存する。"""
    monkeypatch.setenv("KOTOYOSE_AGENT_CALLER_TOKEN", "caller-agent-token")
    monkeypatch.setenv("KOTOYOSE_AGENT_PROVIDER_TOKEN", "provider-agent-token")
    root = tmp_path / "root"
    root.mkdir()
    workspace = root / "workspace"
    workspace.mkdir()
    config = _config(root)
    worker = DockerLocalWorker(IMAGE_ID or "sha256:" + "a" * 64)
    service = ExecutionService(config, store, worker)
    runtime = _FakeAgentRuntime(workspace / "marker")
    service.agent_executor.runtime = runtime
    await service.start()
    try:
        request, workspace_ref, refs = await _resources(service, clients()["caller-a"], workspace)
        attempt_id = await service.dispatch("caller-a", clients()["caller-a"], request)
        async with asyncio.timeout(35):
            while True:
                attempt = await store.attempt("caller-a", attempt_id)
                if attempt["finished_at"] is not None:
                    break
                await asyncio.sleep(0.05)
        assert attempt["status"] == "succeeded"
        assert attempt["stop_confirmation"] == "confirmed"
        assert attempt["side_effect_resolution"] == "resolved"
        assert attempt["usage"] == {"source": "reported", "cost": None, "input_tokens": 7, "output_tokens": 5}
        assert workspace_ref.endswith(":0")
        assert (workspace / "marker").read_text() == "tool"
        output = next(artifact for artifact in attempt["artifacts"] if artifact["name"] == "output")
        assert (await store.artifact("caller-a", output["ref"]))["content"] == b"agent output"
        assert output["digest"] == hashlib.sha256(b"agent output").hexdigest()
        operations = await store.operations(attempt_id)
        assert {operation["kind"] for operation in operations} >= {"local_execution", "provider_process", "sandbox_command"}
        provider = next(operation for operation in operations if operation["kind"] == "provider_process")
        assert set(provider["result"]) == {"pid", "start_ticks", "boot_id", "uid"}
        assert all(operation["state"] == "confirmed" for operation in operations)
        events = await store.events("caller-a", attempt_id, 0, 100)
        progress = [event for event in events if event["type"] == "provider_progress"]
        assert len(progress) == 1
        assert progress[0]["payload"] == {
            "kind": "turn", "tool_name": None, "summary": "Assistant turn 1", "payload": {"turn": 1},
        }
        assert [event["sequence"] for event in events] == sorted(event["sequence"] for event in events)
        assert events[-1]["type"] == "succeeded"
        assert (await store.workspace("caller-a", workspace_ref.rsplit(":", 1)[0] + ":1"))["generation"] == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_unconfirmed_agent_stop_keeps_writer_until_confirm_stop_without_rewriting_terminal(store, tmp_path, monkeypatch):
    """停止未確認時は writer を保持し、後日の confirm_stop で世代だけ解放する。"""
    monkeypatch.setenv("KOTOYOSE_AGENT_CALLER_TOKEN", "caller-agent-token")
    monkeypatch.setenv("KOTOYOSE_AGENT_PROVIDER_TOKEN", "provider-agent-token")
    root = tmp_path / "root"
    root.mkdir()
    workspace = root / "workspace"
    workspace.mkdir()
    config = _config(root)
    def no_clock(*args, **kwargs):
        raise AssertionError("legacy attempt must not create a clock")
    monkeypatch.setattr("kotoyose.execution_agent.WorkClockTracker", no_clock)
    service = ExecutionService(config, store, _NoopWorker())
    service.agent_executor.runtime = _FakeAgentRuntime(stop_confirmation="unconfirmed")
    await service.start()
    try:
        request, workspace_ref, _ = await _resources(service, clients()["caller-a"], workspace, tool_refs=[])
        attempt_id = await service.dispatch("caller-a", clients()["caller-a"], request)
        async with asyncio.timeout(10):
            while (await store.attempt("caller-a", attempt_id))["finished_at"] is None:
                await asyncio.sleep(0.05)
        before = await store.attempt("caller-a", attempt_id)
        assert "limits_mode" not in before["snapshot"]
        assert not service.agent_executor.work_clocks
        assert (await service.describe_limits("caller-a", attempt_id))["execution_time"]["state"] == "unmeasured"
        assert not any("clock" in event["type"] or "limits" in event["type"]
                       for event in await store.events("caller-a", attempt_id, 0, 100))
        assert before["status"] == "succeeded"
        assert before["stop_confirmation"] == "unconfirmed"
        assert (await store.pool.fetchval("SELECT writer_attempt FROM kotoyose.execution_workspaces WHERE id=$1", before["workspace_id"])) == attempt_id
        await store.confirm_stop(attempt_id)
        await store.append_event(attempt_id, "late_event", {"status": "failed"})
        await store.finish(attempt_id, "failed", "late", "confirmed", 9, {})
        after = await store.attempt("caller-a", attempt_id)
        assert after["status"] == "succeeded"
        assert after["reason"] == "completed"
        assert after["stop_confirmation"] == "confirmed"
        assert (await store.workspace("caller-a", workspace_ref.rsplit(":", 1)[0] + ":1"))["generation"] == 1
        assert not any(event["type"] == "late_event" for event in await store.events("caller-a", attempt_id, 0, 100))
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_progress_writer_finish_stops_writer_before_terminal_event():
    """取消が遅れて効く DB でも、finish が返る前に writer が止まり、後から書かれない。"""
    from kotoyose.execution_agent import _ProgressWriter
    from kotoyose.runtime.base import RuntimeEvent

    order: list[str] = []

    class SlowCancelStore:
        async def append_event(self, attempt_id, kind, payload):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # driver が実行中の query を止めるまでに時間がかかる状況
                await asyncio.sleep(1.0)
                order.append("write")

    writer = _ProgressWriter(SlowCancelStore(), "attempt-1")
    writer.emit(RuntimeEvent(kind="text", summary="first"))
    await writer.finish()
    order.append("finish")
    writer.emit(RuntimeEvent(kind="text", summary="after finish"))
    await asyncio.sleep(1.5)

    assert writer.task.done()
    assert order[-1] == "finish"
