"""Jailed delegation across admission, execution lifetime, and writer ownership."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kotoyose import config
from kotoyose.agent_spec import HOST_CODING, READ_MODE
from kotoyose.capabilities import CapabilityManifest, CapabilitySupport
from kotoyose.execution_admission import ExecutionAdmission
from kotoyose.execution_contract import AgentExecution
from kotoyose.execution_delegation import ASK_AGENT, LIST_AGENTS, TOOL_SPECS, WORKSPACE_IDENTITY, DelegationCalls
from kotoyose.execution_resources import (
    AgentDefinition, ExecutionBudget, ExecutionError, ExecutionGrant, InputText, NativeExecutionConfig, TimeoutPolicy,
)
from kotoyose.execution_service import ExecutionService
from kotoyose.execution_store import ExecutionStore


@pytest.fixture
def delegated(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    info = workspace.stat()
    cli = tmp_path / "fake-claude"
    cli.write_text("binary fixture")
    cli.chmod(0o700)
    cfg = config.HarnessConfig(
        connections={"claude": {"provider": "claude", "kind": "api_key", "secret": "env:FIXTURE_ANTHROPIC_KEY", "data_policy": "no_training"}},
        routes={
            "main": {"driver": "claude.agent-sdk", "connection": "claude", "model": "claude-sonnet-4-6"},
            "research": {"driver": "claude.agent-sdk", "connection": "claude", "model": "claude-haiku-4-5"},
        },
        clients={"client": {"models": ["coding", "researcher"], "workspace_roots": [tmp_path], "max_concurrent_runs": 8}},
        models={
            "coding": {"route": "main", "agent": {"place": "isolated", "surface": "write"},
                       "mcp_refs": ["kotoyose"], "delegates": ["researcher", "hidden"]},
            "researcher": {"route": "research", "agent": {"place": "isolated", "surface": "read"},
                           "mcp_refs": ["kotoyose"], "description": "Investigate", "timeout_seconds": 7},
            "hidden": {"route": "research", "agent": {"place": "isolated", "surface": "read"}},
            "host": {},
        },
    )
    monkeypatch.setattr(config, "_config", cfg)
    settings = NativeExecutionConfig(
        schema_version="1", image_id="sha256:" + "a" * 64,
        agent_container={"agent_root": str(tmp_path), "home_root": str(tmp_path)},
        drivers={"claude.agent-sdk": {"cli_binary": cli}},
    )
    request = AgentExecution(
        schema_version="1", kind="agent", idempotency_key="parent", caller_run_ref="run",
        workspace_ref="workspace:ws:0", execution_grant_ref="grant", budgets_ref="budget",
        agent_definition_ref="definition", tool_refs=[HOST_CODING], model="coding",
        inputs=[{"kind": "task_request", "provenance": "project", "content_ref": "input"}],
    )
    now = datetime.now(timezone.utc)
    resources = {
        "grant": ExecutionGrant(
            schema_version="1", kind="grant", grant_id="grant", version=1, caller_run_ref="run",
            workspace_ref=request.workspace_ref, isolation="sandbox", network="none", agent_models=["coding"],
            resource_refs=["definition", "budget", "timeout", "input", HOST_CODING], policy_ref="policy",
            not_before=now - timedelta(seconds=10), expires_at=now + timedelta(minutes=2),
            max_seconds=60, delegation="none",
        ),
        "definition": AgentDefinition(schema_version="1", kind="agent_definition", instructions="Parent instructions",
                                      output_contract="json", max_turns=8, timeout_policy_ref="timeout"),
        "timeout": TimeoutPolicy(schema_version="1", kind="timeout_policy", hard_seconds=60,
                                 soft_seconds=10, grace_seconds=1),
        "budget": ExecutionBudget(schema_version="1", kind="budget", reservation_ref="reserved", mode="observed",
                                  deadline=now + timedelta(seconds=30), memory_bytes=128 * 1024 * 1024,
                                  cpus=1, pids_limit=16, output_limit_bytes=65536),
        "input": InputText(schema_version="1", kind="text", text="Parent task", provenance="project"),
    }
    row = {"id": "ws", "path": str(workspace), "device": info.st_dev, "inode": info.st_ino, "generation": 0,
           "lifecycle_state": "active", "writer_attempt": "parent"}
    store = SimpleNamespace(resource=AsyncMock(side_effect=lambda principal, ref: resources[ref]),
                            workspace=AsyncMock(return_value=row))
    support = CapabilitySupport(status="supported", reason="fixture")
    manifest = CapabilityManifest(capabilities={"native_execution": support, "sandbox": support})
    admission = ExecutionAdmission(store, manifest, settings.image_id, settings=settings)
    return SimpleNamespace(cfg=cfg, request=request, policy=cfg.clients["client"], admission=admission, row=row,
                           settings=settings, now=now)


async def parent_attempt(delegated):
    plan = await delegated.admission.resolve("client", delegated.policy, delegated.request)
    return {"id": "parent", "principal": "client", "status": "running", "snapshot": plan,
            "request": delegated.request.model_dump(mode="json")}


async def test_admission_pins_delegation_only_from_alias_opt_in(delegated):
    parent = await parent_attempt(delegated)
    assert parent["snapshot"]["tool_refs"] == [HOST_CODING, LIST_AGENTS, ASK_AGENT]
    assert parent["snapshot"]["delegation_tools"] == list(TOOL_SPECS.values())
    delegated.cfg.models["coding"].mcp_refs = []
    assert "delegation_tools" not in (await parent_attempt(delegated))["snapshot"]
    explicit_tool = delegated.request.model_copy(update={"tool_refs": [HOST_CODING, ASK_AGENT]})
    with pytest.raises(ExecutionError):
        await delegated.admission.resolve("client", delegated.policy, explicit_tool)


@pytest.mark.parametrize("candidate", ["host", "coding", "hidden"])
def test_config_rejects_invalid_targets(delegated, candidate):
    payload = delegated.cfg.model_dump()
    payload["models"]["coding"]["delegates"] = [candidate]
    if candidate == "hidden":
        payload["models"]["hidden"]["agent"]["surface"] = "write"
    with pytest.raises(ValueError, match="delegates"):
        config.HarnessConfig.model_validate(payload)


async def test_targets_obey_both_alias_and_client_scope(delegated):
    parent = await parent_attempt(delegated)
    service = object.__new__(ExecutionService)
    service.config = delegated.settings
    calls = DelegationCalls(asyncio.Event())
    # Simulate updated config when an already-running parent next invokes a tool.
    delegated.cfg.models["coding"].delegates += ["host", "coding", "opt-out"]
    delegated.cfg.models["opt-out"] = delegated.cfg.models["researcher"].model_copy(update={"delegable": False})
    delegated.policy.models += ["host", "opt-out"]
    outcome = await service._delegation_tool(parent, calls, LIST_AGENTS, {}, lambda: 20)
    assert outcome.result == [{"alias": "researcher", "description": "Investigate", "model": "claude-haiku-4-5",
                               "place": "jail"}]
    outcome = await service._delegation_tool(parent, calls, ASK_AGENT,
        {"alias": "researcher", "task": "Look up X", "conversation_id": None}, lambda: 20)
    assert outcome.state == "failed" and "conversation_id" in outcome.result["error"]


@pytest.mark.parametrize("remaining", [2, 20])
async def test_child_plan_pins_read_identity_deadline_and_no_grandchildren(delegated, remaining):
    parent = await parent_attempt(delegated)
    before = datetime.now(timezone.utc)
    deadline = before + timedelta(seconds=remaining)
    _, child = await delegated.admission.resolve_shared_child(parent, delegated.policy, "researcher", "Investigate", deadline)
    assert all(child[key] == parent["snapshot"][key] for key in WORKSPACE_IDENTITY)
    assert (child["isolation"], child["surface"], child["codex_execution_mode"]) == ("sandbox", "read", READ_MODE)
    assert child["parent_attempt_id"] == "parent" and child["delegated_by"] == "coding" and child["depth"] == 1
    assert "delegation_tools" not in child and child["tool_refs"] == []
    assert child["definition"]["output_contract"] == "text"
    assert child["inputs"] == [{"kind": "task_request", "text": "Investigate", "provenance": "agent_generated"}]
    actual = datetime.fromisoformat(child["deadline"])
    assert before + timedelta(seconds=min(remaining, 7)) <= actual <= min(
        deadline, datetime.now(timezone.utc) + timedelta(seconds=7),
    )


async def test_child_plan_rejects_write_alias_and_replaced_workspace(delegated):
    parent = await parent_attempt(delegated)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=10)
    candidate = delegated.cfg.models["researcher"]
    candidate.agent.surface = "write"
    with pytest.raises(ExecutionError, match="outside the delegation scope"):
        await delegated.admission.resolve_shared_child(parent, delegated.policy, "researcher", "Investigate", deadline)
    candidate.agent.surface = "read"
    parent["snapshot"]["workspace_inode"] += 1
    with pytest.raises(ExecutionError, match="binding changed"):
        await delegated.admission.resolve_shared_child(parent, delegated.policy, "researcher", "Investigate", deadline)


@asynccontextmanager
async def context(value=None):
    yield value


async def test_shared_child_admit_keeps_parent_writer(delegated):
    parent = await parent_attempt(delegated)
    request, snapshot = await delegated.admission.resolve_shared_child(
        parent, delegated.policy, "researcher", "Investigate", datetime.now(timezone.utc) + timedelta(seconds=10),
    )
    queries = []

    async def fetchrow(sql, *_):
        if "FOR UPDATE" in sql and "execution_resources" in sql:
            return {"revoked_at": None, "valid": True}
        if "workspace_id=$1 AND snapshot->>" in sql:
            return {"active": 0, "total": 0}
        if "execution_workspaces" in sql:
            return delegated.row
        if "id=$1 AND principal=$2 FOR UPDATE" in sql:
            return {**parent, "snapshot": json.dumps(parent["snapshot"])}
        if "id=$1" in sql:
            return {"id": "child"}
        return None

    async def execute(sql, *args):
        queries.append((sql, args))

    conn = SimpleNamespace(transaction=context, fetchrow=fetchrow, fetchval=AsyncMock(return_value=0), execute=execute)
    store = object.__new__(ExecutionStore)
    store.pool = SimpleNamespace(acquire=lambda: context(conn))
    store._event = AsyncMock()
    wire = request.model_dump(mode="json")
    assert await store.admit("client", wire, snapshot, 8) == ({"id": "child"}, True)
    assert not any("UPDATE kotoyose.execution_workspaces" in sql for sql, _ in queries)
    assert any("INSERT INTO kotoyose.execution_attempts" in sql for sql, _ in queries)
    with pytest.raises(ExecutionError, match="Workspace writer"):
        await store.admit("client", wire, parent["snapshot"], 8)
    with pytest.raises(ExecutionError, match="binding changed"):
        await store.admit("client", wire, {**snapshot, "surface": "write"}, 8)


@pytest.mark.parametrize("ending", ["cancel", "end"])
async def test_lifetime_cancels_child_and_waits_for_stop(delegated, ending):
    parent = await parent_attempt(delegated)
    service = object.__new__(ExecutionService)
    service.config = delegated.settings
    service.admission = delegated.admission
    service._require_accepting = lambda: None
    service._cancels, service._tasks, service._task_jobs = {}, {}, {}
    service.store = SimpleNamespace(admit=AsyncMock(return_value=({"id": "child", "generation": 0}, True)),
                                    cancel=AsyncMock())
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def execute(attempt, cancel):
        started.set()
        await cancel.wait()
        await asyncio.sleep(0.02)
        stopped.set()
        return ("cancelled", "cancelled", "confirmed", b"", {})

    service._execute = execute
    cancel = asyncio.Event()
    calls = DelegationCalls(cancel)
    reply = asyncio.create_task(service._delegation_tool(parent, calls, ASK_AGENT,
        {"alias": "researcher", "task": "Inspect shared files"}, lambda: 20))
    async with asyncio.timeout(2):
        await started.wait()
        if ending == "cancel":
            cancel.set()
            assert (await reply).state == "failed"
        await calls.close()
        assert stopped.is_set()
        assert (await reply).state == "failed"
    service.store.cancel.assert_awaited_once_with("client", "child", "parent_execution_ended")


async def test_per_attempt_concurrency_and_total_limits():
    calls = DelegationCalls(asyncio.Event())
    release = asyncio.Event()
    requests = [asyncio.create_task(calls.run(lambda _: release.wait())) for _ in range(4)]
    await asyncio.sleep(0)
    with pytest.raises(ExecutionError, match="limit"):
        await calls.run(lambda _: release.wait())
    release.set()
    await asyncio.gather(*requests)
    for _ in range(28):
        await calls.run(lambda _: release.wait())
    with pytest.raises(ExecutionError, match="limit"):
        await calls.run(lambda _: release.wait())
    await calls.close()


async def test_scheduled_parent_runs_child_through_normal_execution_and_finishes_after_it(delegated, monkeypatch):
    from kotoyose.native_claude import NativeAgentResult

    parent = await parent_attempt(delegated)
    parent.update(status="dispatched", generation=0, workspace_id="ws")
    service = object.__new__(ExecutionService)
    service.config = delegated.settings
    service.admission = delegated.admission
    service._accepting = True
    service._cancels, service._tasks, service._task_jobs = {}, {}, {}
    service._observe_pool = AsyncMock()
    monkeypatch.setattr("kotoyose.execution_service.AgentInteraction", lambda _: SimpleNamespace(finish=lambda *args: None))
    usage = {"source": "reported", "cost": None, "input_tokens": 4, "output_tokens": 2}

    async def admit(principal, request, snapshot, max_concurrent):
        assert snapshot["parent_attempt_id"] == "parent"
        assert delegated.row["writer_attempt"] == "parent"
        return {"id": "child", "status": "dispatched", "principal": principal, "workspace_id": "ws",
                "generation": 0, "snapshot": snapshot, "request": request}, True

    service.store = SimpleNamespace(
        resource=delegated.admission.store.resource, mark_running=AsyncMock(return_value=True),
        admit=admit, operations=AsyncMock(return_value=[]), finish=AsyncMock(return_value=None),
    )

    async def execute(attempt, cancel, *, worker, interaction, runtime_entry, delegation_call=None):
        if attempt["id"] == "child":
            assert delegation_call is None
            assert attempt["snapshot"]["surface"] == "read"
            return NativeAgentResult("succeeded", "completed", b"findings", "confirmed", usage)
        outcome = await delegation_call(ASK_AGENT, {"alias": "researcher", "task": "Inspect shared files"}, lambda: 20)
        assert outcome.result == {"text": "findings", "usage": usage}
        return NativeAgentResult("succeeded", "completed", b"parent summary", "confirmed", usage)

    service.agent_executor = SimpleNamespace(execute=execute, runtime_for=lambda _: None)
    service._schedule(parent)
    async with asyncio.timeout(2):
        result = await service._tasks["parent"]
    assert result[0] == "succeeded"
    assert [call.args[0] for call in service.store.finish.await_args_list] == ["child", "parent"]


async def test_shared_child_authorizes_provider_operations_only_while_parent_holds_writer(delegated):
    parent = await parent_attempt(delegated)
    request, snapshot = await delegated.admission.resolve_shared_child(
        parent, delegated.policy, "researcher", "Investigate", datetime.now(timezone.utc) + timedelta(seconds=10),
    )
    child = {"id": "child", "principal": "client", "status": "running", "api_operation": "dispatch",
             "request": request.model_dump_json(), "snapshot": json.dumps(snapshot)}

    async def fetchrow(sql, *args):
        if "SELECT * FROM" in sql:
            assert args == ("child",)
            return child
        if "execution_resources" in sql:
            assert args == ("client", "grant")
            return {"revoked_at": None, "valid": True}
        if "SELECT status,snapshot" in sql:
            assert args == ("parent", "client")
            return {"status": parent["status"], "snapshot": json.dumps(parent["snapshot"])}
        assert "w.writer_attempt=$2" in sql and args == ("child", "parent")
        if delegated.row["writer_attempt"] == "parent":
            return child
        return None

    conn = SimpleNamespace(transaction=context, fetchrow=fetchrow, fetchval=AsyncMock(return_value=False), execute=AsyncMock())
    store = object.__new__(ExecutionStore)
    store.pool = SimpleNamespace(acquire=lambda: context(conn))
    store._event = AsyncMock()
    await store.prepare_operation("child", "provider_process")
    await store.authorize_provider_request("child", "client")
    with pytest.raises(ExecutionError, match="cannot admit"):
        await store.prepare_operation("child", "sandbox_command")
    parent["status"] = "cancelling"
    with pytest.raises(ExecutionError, match="binding changed"):
        await store.authorize_provider_request("child", "client")
    parent["status"] = "running"
    delegated.row["writer_attempt"] = None
    with pytest.raises(ExecutionError, match="no longer admits"):
        await store.authorize_provider_request("child", "client")


async def test_child_without_alias_timeout_is_bounded_by_the_parent(delegated, monkeypatch):
    # An alias without timeout_seconds (the production researcher) must not break the deadline math.
    monkeypatch.setattr(config.get_config().models["researcher"], "timeout_seconds", None)
    parent = await parent_attempt(delegated)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    _, child = await delegated.admission.resolve_shared_child(parent, delegated.policy, "researcher", "Investigate", deadline)
    assert datetime.fromisoformat(child["deadline"]) <= deadline


def test_raise_site_reports_module_and_line_without_the_message():
    from kotoyose.execution_service import _raise_site

    try:
        raise TypeError("secret task text")
    except TypeError as exc:
        site = _raise_site(exc)
    assert site.startswith(__name__ + ":") and "secret" not in site
