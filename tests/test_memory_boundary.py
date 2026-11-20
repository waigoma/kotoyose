"""Memory Platform v2 と Kotoyose の injection / Tool enforcement 境界を検証する。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from kotoyose.capabilities import capability_manifest
from kotoyose.execution_contract import ExecutionInput
from kotoyose.execution_resources import (
    ExecutionError,
    MemorySnapshot,
    NativeExecutionConfig,
    ResourceEnvelope,
    schema_digest_for,
)
from kotoyose.execution_service import ExecutionService
from kotoyose.execution_snapshot import resolved_attempt_snapshot
from kotoyose.mcp_contract import (
    ExternalMcpServerConfig,
    McpPinRequest,
    McpToolCeiling,
    mcp_content_digest,
)
from kotoyose.mcp_resources import McpResourceResolver
from kotoyose.native_claude import NativeAgentResult
from tests.test_execution_agent import _NoopWorker, _config, _resources
from tests.test_execution_resource_pins import _admission
from tests.test_execution_store import store  # noqa: F401
from tests.test_mcp_broker import admitted
from tests.test_mcp_transport import remote  # noqa: F401


pytestmark = pytest.mark.usefixtures("client_api_key")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _memory_snapshot(**overrides) -> MemorySnapshot:
    content = "Memory bootstrap data"
    values = {
        "schema_version": "1",
        "kind": "memory_snapshot",
        "content": content,
        "content_digest": _digest(content),
        "schema_digest": schema_digest_for("memory_snapshot"),
        "generator": "memory-platform-bootstrap",
        "version": "rev3",
        "config_digest": _digest("memory-config"),
        "source_revision": "a" * 40,
        "repository_ref": "memory-platform:installation-a",
        "provenance": "agent_generated",
        "dependency_refs": [],
        "namespace_refs": ["project:caller-a:repo-a", "agent-role:caller-a:coder"],
        "captured_at": datetime(2026, 9, 15, 1, 2, 3, tzinfo=timezone.utc),
        "revocation_epoch": "epoch:42",
    }
    values.update(overrides)
    return MemorySnapshot(**values)


def _pin_request(constraint: dict) -> McpPinRequest:
    return McpPinRequest(
        schema_version="1",
        server_id="fixture",
        tools={"append": "fixture_append"},
        narrowing_constraints={"append": constraint},
    )


@pytest.mark.asyncio
async def test_pin_accepts_only_monotonic_narrowing_and_digests_it(remote):
    """Caller schema は管理者 schema の const / required / enum を緩和できない。"""
    config, policy, _ = remote
    admin = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "string", "enum": ["one", "two"]}},
    }
    config = config.model_copy(update={
        "tools": {"append": McpToolCeiling(effect="write", argument_constraints=admin)},
    })
    narrower = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "string", "const": "one"}},
        "additionalProperties": False,
    }
    binding = await McpResourceResolver({"fixture": config}).pin(policy, _pin_request(narrower))
    assert binding.tools[0].narrowing_constraints == narrower
    assert binding.tools[0].narrowing_digest == _digest(
        '{"additionalProperties":false,"properties":{"value":{"const":"one","type":"string"}},"required":["value"],"type":"object"}'
    )
    legacy = await McpResourceResolver({"fixture": config}).pin(
        policy,
        McpPinRequest(
            schema_version="1", server_id="fixture", tools={"append": "fixture_append"},
        ),
    )
    assert "narrowing_constraints" not in legacy.tools[0].model_dump(mode="json")
    assert "narrowing_digest" not in legacy.tools[0].model_dump(mode="json")
    assert mcp_content_digest(binding.model_dump(mode="json")) != mcp_content_digest(
        legacy.model_dump(mode="json"),
    )

    cases = [
        (
            {"type": "object", "properties": {"value": {"const": "one"}}},
            {"type": "object", "properties": {"value": {"type": "string"}}},
        ),
        (
            {"type": "object", "required": ["value"], "properties": {"value": {"type": "string"}}},
            {"type": "object", "required": [], "properties": {"value": {"type": "string"}}},
        ),
        (
            {"type": "object", "properties": {"value": {"enum": ["one"]}}},
            {"type": "object", "properties": {"value": {"enum": ["one", "two"]}}},
        ),
    ]
    for ceiling, relaxation in cases:
        changed = config.model_copy(update={
            "tools": {"append": McpToolCeiling(effect="write", argument_constraints=ceiling)},
        })
        with pytest.raises(ExecutionError) as rejected:
            await McpResourceResolver({"fixture": changed}).pin(policy, _pin_request(relaxation))
        assert rejected.value.code == "invalid_request"


@pytest.mark.asyncio
async def test_narrowing_violation_never_marks_an_operation_sent(store, remote, tmp_path):
    """narrowing に反する引数は remote POST と sent claim より前に拒否する。"""
    constraint = {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"const": "allowed"}},
    }
    service, attempt, tool_ref, state = await admitted(
        store, remote, tmp_path, pin_request=_pin_request(constraint),
    )
    with pytest.raises(ExecutionError) as rejected:
        await service.agent_executor.mcp_broker.call(
            "caller-a", attempt["id"], tool_ref, {"value": "denied"}, asyncio.Event(),
        )
    assert rejected.value.code == "permission_denied"
    assert state.calls == []
    assert not await store.pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM kotoyose.execution_operations "
        "WHERE attempt_id=$1 AND kind='mcp_tool' AND state='sent')",
        attempt["id"],
    )


def test_memory_snapshot_restricts_provenance_namespaces_and_epoch():
    """Memory snapshot は低信頼 provenance と失効照合 metadata を必須にする。"""
    memory = _memory_snapshot()
    assert memory.revocation_epoch == "epoch:42"
    assert ResourceEnvelope.model_validate({
        "resource": memory.model_dump(mode="json"),
    }).resource == memory
    for overrides in (
        {"provenance": "verified_fact"},
        {"provenance": "project"},
        {"namespace_refs": []},
        {"namespace_refs": ["project:a", "project:a"]},
        {"captured_at": datetime(2026, 9, 15)},
        {"revocation_epoch": ""},
    ):
        with pytest.raises(ValidationError):
            _memory_snapshot(**overrides)


@pytest.mark.asyncio
async def test_admission_rejects_memory_snapshot_epoch_mismatch_with_typed_error(tmp_path):
    """Caller の期待世代と固定 snapshot の世代が異なれば開始しない。"""
    (tmp_path / "workspace").mkdir()
    memory = _memory_snapshot()
    resources = {("caller-a", "memory"): memory}
    admission, policy, request = _admission(
        resources,
        tmp_path,
        grant_refs=["timeout", "budget", "definition", "memory"],
    )
    request = request.model_copy(update={
        "inputs": [ExecutionInput(
            kind="memory_snapshot",
            provenance="agent_generated",
            content_ref="memory",
            expected_revocation_epoch="epoch:41",
        )],
        "skill_refs": [],
    })
    with pytest.raises(ExecutionError) as rejected:
        await admission.resolve("caller-a", policy, request)
    assert rejected.value.code == "memory_snapshot_epoch_mismatch"
    assert rejected.value.status == 409


@pytest.mark.asyncio
async def test_memory_injection_is_data_and_is_recorded_in_snapshot_and_event(
    store, tmp_path, monkeypatch,
):
    """Memory 本文を system instruction へ昇格せず、pin identity を監査面へ残す。"""
    from kotoyose.config import get_client
    root = tmp_path / "root"
    root.mkdir()
    config = _config(root)
    service = ExecutionService(config, store, _NoopWorker())
    policy = get_client("caller-a")
    request, _, refs = await _resources(service, policy, root / "workspace", tool_refs=[])
    memory = _memory_snapshot()
    memory_ref = await service.register_resource("caller-a", policy, memory)
    grant = await store.resource("caller-a", refs["grant"])
    grant_ref = await store.put_resource("caller-a", grant.model_copy(update={
        "version": 2,
        "resource_refs": [*grant.resource_refs, memory_ref],
    }))
    request = request.model_copy(update={
        "execution_grant_ref": grant_ref,
        "inputs": [ExecutionInput(
            kind="memory_snapshot",
            provenance="agent_generated",
            content_ref=memory_ref,
            expected_revocation_epoch=memory.revocation_epoch,
        )],
        "requirements": request.requirements.model_copy(update={"capabilities": ["memory_bootstrap"]}),
    })
    snapshot = await service.admission.resolve("caller-a", policy, request)
    attempt, _ = await store.admit("caller-a", request.model_dump(mode="json"), snapshot, 2)
    await store.mark_running(attempt["id"])

    captured = SimpleNamespace(job=None)

    async def execute(job, cancel, execute_tool, *, on_process_started):
        captured.job = job
        return NativeAgentResult(
            status="succeeded",
            reason="completed",
            content=b"done",
            stop_confirmation="confirmed",
            usage={"source": "unknown", "cost": None, "input_tokens": None, "output_tokens": None},
        )

    service.agent_executor.runtime = SimpleNamespace(execute=execute)
    monkeypatch.setenv("KOTOYOSE_AGENT_PROVIDER_TOKEN", "provider-test")
    await service.agent_executor.execute(attempt, asyncio.Event())

    item = snapshot["memory_snapshots"][0]
    expected = {
        "resource_ref": memory_ref,
        "content_digest": memory.content_digest,
        "revocation_epoch": memory.revocation_epoch,
        "captured_at": memory.captured_at.isoformat(),
    }
    assert {key: item[key] for key in expected} == expected
    public = resolved_attempt_snapshot(attempt).memory_snapshots[0]
    assert public.model_dump(mode="json") == expected
    dispatched = (await store.events("caller-a", attempt["id"], 0, 10))[0]
    assert dispatched["payload"]["memory_snapshots"] == [expected]
    assert memory.content not in captured.job.instructions
    assert memory.content in captured.job.prompt


def test_memory_bootstrap_capability_reports_tool_enforcement_by_configuration(tmp_path, monkeypatch):
    """snapshot 対応と external MCP narrowing の有無を capability reason で区別する。"""
    from kotoyose import config as config_module
    assert capability_manifest().capabilities["memory_bootstrap"].status == "unsupported"
    config = _config(tmp_path)
    without_mcp = ExecutionService(config, SimpleNamespace(
        artifact_retention=SimpleNamespace(limits=None),
        workspace_retention=SimpleNamespace(limits=None),
    ), _NoopWorker())
    capability = without_mcp.manifest.capabilities["memory_bootstrap"]
    assert capability.status == "supported"
    assert "requires configured external_mcp" in capability.reason

    server = ExternalMcpServerConfig(
        endpoint="https://memory.example.test/mcp",
        server_name="memory-platform",
        server_version="1",
        trust_profile_ref="operator:memory-platform",
        tools={"search_memory": {"effect": "read"}},
    )
    hcfg = config_module.get_config()
    monkeypatch.setattr(
        config_module,
        "_config",
        hcfg.model_copy(update={
            "clients": {
                **hcfg.clients,
                "caller-a": hcfg.clients["caller-a"].model_copy(update={"external_mcps": ["memory"]}),
            }
        }),
    )
    configured = NativeExecutionConfig.model_validate({
        **config.model_dump(mode="python"),
        "mcp_servers": {"memory": server},
    })
    with_mcp = ExecutionService(configured, SimpleNamespace(
        artifact_retention=SimpleNamespace(limits=None),
        workspace_retention=SimpleNamespace(limits=None),
    ), _NoopWorker())
    capability = with_mcp.manifest.capabilities["memory_bootstrap"]
    assert capability.status == "supported"
    assert "Caller narrowing" in capability.reason
