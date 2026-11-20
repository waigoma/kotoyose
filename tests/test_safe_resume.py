"""再開の同値分割、admission 競合、旧世代の遅延応答を専用 DB で検証する。"""

import asyncio
from copy import deepcopy
from dataclasses import replace
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock

import pytest
import httpx

from kotoyose.checkpoint_contract import CheckpointCapture
from kotoyose.execution_contract import AgentExecution, ResumeError, ResumeRequest
from kotoyose.config import AgentRoute, AliasAgent, Connection, HarnessConfig, ModelPreset, get_config
from kotoyose.execution_resources import (
    AgentDefinition, DriverCell, DriverInstall, ExecutionError, InputText, content_digest,
)
from kotoyose.native_claude import NativeClaudeWorker
from kotoyose.runtime.resume import ResumeSupport
from tests.test_execution_service import DATABASE_URL, IMAGE_ID, native, _execution, _terminal
from tests.test_execution_agent import _FakeAgentRuntime, store
from tests.test_client_tools import client_attempt, connect, create, delivery, result
from tests.fixtures.clients import clients, set_client


pytestmark = pytest.mark.skipif(not IMAGE_ID, reason="Native test Docker image required")


class _ResumeRuntime(_FakeAgentRuntime):
    def __init__(self, modes=("durable_state",), retain_session=True):
        super().__init__()
        self.modes = modes
        self.jobs = []
        self.retain_session = retain_session
        self.started = asyncio.Event()
        self.release = None

    def resume_support(self):
        return ResumeSupport(self.modes, "provider_session_state_not_retained")

    def resume_session_ref(self, session_id):
        return session_id if "native" in self.modes and self.retain_session else None

    async def execute(self, job, *args, **kwargs):
        self.jobs.append(job)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        response = await super().execute(job, *args, **kwargs)
        return replace(response, provider_session_id="provider-session:" + job.attempt_id)


async def _resume_body(service, principal, old_id):
    old = await service.store.attempt(principal, old_id)
    description = await service.describe(principal, old_id)
    grant = await service.store.resource(principal, old["request"]["execution_grant_ref"])
    new_grant = grant.model_copy(update={"version": grant.version + 1, "workspace_ref": description["workspace_ref"]})
    grant_ref = await service.store.put_resource(principal, new_grant)
    return {
        "schema_version": "1", "predecessor_snapshot_digest": content_digest(old["snapshot"]),
        "execution": {**old["request"], "idempotency_key": str(uuid4()),
                      "workspace_ref": description["workspace_ref"], "execution_grant_ref": grant_ref},
    }


async def _source(native, monkeypatch, modes=("durable_state",), retain_session=True):
    client, workspace, service, isolation = native
    body = (await _execution(client, workspace, ["true"], isolation))["execution"]
    principal = "test-caller"
    policy = clients()[principal].model_copy(update={"models": ["profile-agent"]})
    set_client(principal, policy)
    monkeypatch.setenv("KOTOYOSE_SAFE_RESUME_PROVIDER_TOKEN", "fake-provider-only")
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(
        connections={"resume-conn": Connection(provider="claude", kind="api_key", data_policy="no_training", secret="env:KOTOYOSE_SAFE_RESUME_PROVIDER_TOKEN")},
        routes={"profile-agent": AgentRoute(
            driver="claude.agent-sdk", model="claude-sonnet-4-5",
            connection="resume-conn",
        )},
        models={"profile-agent": ModelPreset(
            route="profile-agent",
            agent=AliasAgent(place="isolated", surface="none"),
        )},
    ))
    service.config.drivers = {"claude.agent-sdk": DriverInstall(cli_binary=Path("/usr/bin/sh"))}
    runtime = _ResumeRuntime(modes, retain_session)
    monkeypatch.setattr(service.agent_executor, "runtime", runtime)
    definition = await service.store.put_resource(principal, AgentDefinition(
        schema_version="1", kind="agent_definition", instructions="Continue the task from durable workspace state.",
        output_contract="text", max_turns=2, timeout_policy_ref=body["timeout_policy_ref"],
    ))
    content = await service.store.put_resource(principal, InputText(
        schema_version="1", kind="text", text="Task input", provenance="project",
    ))
    grant = await service.store.resource(principal, body["execution_grant_ref"])
    grant_ref = await service.store.put_resource(principal, grant.model_copy(update={
        "version": 2, "agent_models": ["profile-agent"], "resource_refs": [*grant.resource_refs, definition, content],
    }))
    common = {key: value for key, value in body.items() if key in AgentExecution.model_fields}
    common.pop("runtime_profile", None)
    common.pop("model", None)
    request = AgentExecution.model_validate({
        **common, "kind": "agent", "execution_grant_ref": grant_ref, "agent_definition_ref": definition,
        "model": "profile-agent", "inputs": [{"kind": "task_request", "provenance": "project", "content_ref": content}],
    })
    old_id = await service.dispatch(principal, policy, request)
    terminal = await _terminal(client, old_id)
    assert terminal["status"] == "succeeded", terminal
    return SimpleNamespace(client=client, workspace=workspace, service=service, store=service.store,
                           principal=principal, policy=policy, old_id=old_id, runtime=runtime,
                           body=await _resume_body(service, principal, old_id))


async def _resume(service, principal, attempt_id, body):
    try:
        policy = clients()[principal]
        new_id = await service.resume(principal, policy, attempt_id, ResumeRequest.model_validate(body))
        description = await service.describe(principal, new_id)
        return httpx.Response(202, json={"schema_version": "1", "attempt_id": new_id,
            "resume_manifest": description["resume_manifest"],
            "resume_manifest_digest": description["resume_manifest_digest"], "resume_mode": description["resume_mode"]})
    except ExecutionError as exc:
        return httpx.Response(exc.status, json={"error": {"type": exc.code, "code": exc.code,
            "message": str(exc), "request_id": "unknown", "retryable": exc.status >= 500 and exc.status != 501,
            "safe_to_retry": exc.safe_to_retry, **({"reason_code": exc.reason_code} if exc.reason_code is not None else {})}})


async def _post(h, body=None):
    return await _resume(h.service, h.principal, h.old_id, h.body if body is None else body)


async def test_failed_source_without_session_retains_workspace_and_one_durable_resume(native, monkeypatch):
    h = await _source(native, monkeypatch)
    await h.store.pool.execute("UPDATE kotoyose.execution_attempts SET status='timeout',reason='hard_timeout' WHERE id=$1", h.old_id)
    (h.workspace/'untracked-partial.txt').write_text('unfinished work')
    source = await h.store.attempt(h.principal, h.old_id)
    replies = await asyncio.gather(_post(h), _post(h))
    assert all(response.status_code == 202 for response in replies), [r.text for r in replies]
    assert replies[0].json()['attempt_id'] == replies[1].json()['attempt_id']
    assert replies[0].json()['resume_mode'] == 'durable_state'
    assert replies[0].json()['resume_manifest']['provider_session_ref'] is None
    await _terminal(h.client, replies[0].json()['attempt_id'])
    previous = await h.store.attempt(h.principal, h.old_id)
    assert (previous['status'], previous['reason'], previous['snapshot']) == (source['status'], source['reason'], source['snapshot'])
    assert (h.workspace/'untracked-partial.txt').read_text() == 'unfinished work'
    assert len(h.runtime.jobs) == 2


async def test_missing_retained_state_falls_back_before_acceptance_without_clearing_old_ref(native, monkeypatch):
    h = await _source(native, monkeypatch, modes=('native', 'durable_state'))
    old = await h.store.attempt(h.principal, h.old_id)
    assert old['provider_session_ref']
    h.runtime.can_resume_session = AsyncMock(return_value=False)
    required = {**h.body, 'required_resume_mode': 'native'}
    rejection = await _post(h, required)
    assert rejection.status_code == 409
    assert rejection.json()['error']['reason_code'] == 'adapter_unsupported'
    assert await h.store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_attempts') == 1
    response = await _post(h)
    assert response.status_code == 202, response.text
    assert response.json()['resume_mode'] == 'durable_state'
    await _terminal(h.client, response.json()['attempt_id'])
    assert (await h.store.attempt(h.principal, h.old_id))['provider_session_ref'] == old['provider_session_ref']
    assert h.runtime.jobs[-1].provider_session_ref is None


async def test_safe_resume_pins_manifest_and_continues_durable_workspace(native, monkeypatch):
    assert (await native[0].get("/v1/capabilities")).json()["capabilities"]["safe_resume"]["status"] == "unsupported"
    h = await _source(native, monkeypatch)
    (h.workspace / "durable.txt").write_text("prior work")
    response = await _post(h)
    assert response.status_code == 202, response.text
    receipt = response.json()
    new_id = receipt["attempt_id"]
    assert new_id != h.old_id
    manifest = receipt["resume_manifest"]
    assert manifest["predecessor_attempt_id"] == h.old_id
    assert manifest["mode"] == receipt["resume_mode"] == "durable_state"
    assert manifest["provider_session_ref"] is None
    assert manifest["provider_session_reason"]
    assert manifest["stop_confirmation"] == "confirmed" and manifest["side_effect_resolution"] == "resolved"
    assert manifest["workspace_generation"] == 1 and manifest["grant_version"] == 3
    assert manifest["resume_request_key"] == h.body["execution"]["idempotency_key"]
    assert content_digest(manifest) == receipt["resume_manifest_digest"]
    terminal = await _terminal(h.client, new_id)
    assert terminal["status"] == "succeeded", terminal
    assert terminal["workspace_generation"] == 2
    snapshot = (await h.client.get(f"/v1/attempts/{new_id}/snapshot")).json()
    assert snapshot["resume_manifest"] == manifest
    assert snapshot["resume_manifest_digest"] == receipt["resume_manifest_digest"]
    assert snapshot["resume_mode"] == "durable_state"
    assert (h.workspace / "durable.txt").read_text() == "prior work"
    assert h.runtime.jobs[-1].resume_mode == "durable_state"
    assert h.runtime.jobs[-1].provider_session_ref is None
    assert receipt["resume_manifest_digest"] in h.runtime.jobs[-1].prompt
    old = await h.store.attempt(h.principal, h.old_id)
    assert old["status"] == "succeeded" and old["resumed_by"] == new_id


@pytest.mark.parametrize("case,reason", [
    ("terminal", "attempt_not_terminal"), ("stop", "stop_unconfirmed"),
    ("effects", "effects_unresolved"), ("unknown", "operation_unknown"),
    ("workspace", "workspace_mismatch"), ("grant", "grant_mismatch"),
    ("revoked", "grant_mismatch"), ("provider", "provider_mismatch"),
    ("runtime", "runtime_mismatch"), ("snapshot", "snapshot_mismatch"),
    ("budget", "admission_rejected"), ("run", "grant_mismatch"),
])
async def test_unsafe_conditions_are_typed_before_new_attempt(native, monkeypatch, case, reason):
    h = await _source(native, monkeypatch, modes=('native', 'durable_state') if case == 'effects' else ('durable_state',))
    if case == 'effects':
        h.runtime.can_resume_session = AsyncMock(return_value=True)
        h.body['required_resume_mode'] = 'native'
    changes = {"terminal": "status='running',finished_at=NULL", "stop": "stop_confirmation='unconfirmed'",
               "effects": "side_effect_resolution='unresolved'"}
    if case in changes:
        await h.store.pool.execute(f"UPDATE kotoyose.execution_attempts SET {changes[case]} WHERE id=$1", h.old_id)
    elif case == "unknown":
        await h.store.resolve_operation("execution:" + h.old_id, "unconfirmed")
    elif case == "workspace":
        h.body["execution"]["workspace_ref"] = h.body["execution"]["workspace_ref"].rsplit(":", 1)[0] + ":0"
    elif case == "grant":
        h.body["execution"]["execution_grant_ref"] = "resource:other-principal"
    elif case == "revoked":
        await h.store.revoke_grant(h.principal, h.body["execution"]["execution_grant_ref"])
    elif case == "provider":
        cfg = get_config()
        cfg.connections["resume-codex-conn"] = Connection(
            provider="codex", kind="api_key", data_policy="no_training", secret="env:KOTOYOSE_SAFE_RESUME_PROVIDER_TOKEN",
        )
        cfg.routes["profile-agent"] = AgentRoute(
            driver="codex.app-server", model="gpt-6-luna", connection="resume-codex-conn",
        )
        cfg.models["profile-agent"] = cfg.models["profile-agent"].model_copy(update={
            "agent": AliasAgent(place="isolated", surface="exec"),
        })
        h.service.config.drivers["codex.app-server"] = DriverInstall(
            cli_binary=Path("/usr/bin/sh"),
            cells={"standard": DriverCell(rootfs=Path("/fixture/standard-rootfs"), rootfs_digest="0" * 64)},
        )
    elif case == "runtime":
        cfg = get_config()
        cfg.routes["profile-agent"] = cfg.routes["profile-agent"].model_copy(update={"model": "claude-changed"})
    elif case == "snapshot":
        h.body["predecessor_snapshot_digest"] = "0" * 64
    elif case == "budget":
        monkeypatch.setattr(h.policy, "max_memory_bytes", 1)
    elif case == "run":
        h.body["execution"]["caller_run_ref"] = "another-run"
    response = await _post(h)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "resume_unsafe"
    assert response.json()["error"]["reason_code"] == reason
    ResumeError.model_validate(response.json()["error"])
    assert len(h.runtime.jobs) == 1
    assert await h.store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 1


@pytest.mark.parametrize('native', ['sandbox'], indirect=True)
async def test_native_required_lost_response_reuses_one_successor(native, monkeypatch):
    h = await _source(native, monkeypatch, modes=('native', 'durable_state'))
    h.runtime.can_resume_session = AsyncMock(return_value=True)
    execute = h.runtime.execute
    async def checked_execute(job, *args, **kwargs):
        await job.validate_start()
        return await execute(job, *args, **kwargs)
    h.runtime.execute = checked_execute
    h.body['required_resume_mode'] = 'native'
    first = await _post(h)
    assert first.status_code == 202, first.text
    receipt = first.json()
    assert receipt['resume_mode'] == 'native'
    await _terminal(h.client, receipt['attempt_id'])
    replay = await _post(h)
    assert replay.status_code == 202 and replay.json()['attempt_id'] == receipt['attempt_id']
    assert await h.store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_attempts') == 2
    assert h.runtime.jobs[-1].resume_mode == 'native'
    assert h.runtime.can_resume_session.await_count == 2


async def test_resume_replay_is_durable_scoped_and_conflicting_manifest_is_rejected(native, monkeypatch):
    h = await _source(native, monkeypatch)
    old = await h.store.attempt(h.principal, h.old_id)
    # 同じ文字列 key を dispatch と resume に使っても API operation の scope は独立する。
    h.body["execution"]["idempotency_key"] = old["request"]["idempotency_key"]
    responses = await asyncio.gather(*(_post(h) for _ in range(4)))
    assert all(response.status_code == 202 for response in responses), [r.text for r in responses]
    assert len({r.json()["attempt_id"] for r in responses}) == 1
    receipt = responses[0].json()
    await _terminal(h.client, receipt["attempt_id"])
    changed_key = deepcopy(h.body)
    changed_key["execution"]["idempotency_key"] = str(uuid4())
    assert (await _post(h, changed_key)).json()["error"]["reason_code"] == "already_resumed"
    normalized = {**h.body, "checkpoint_ref": None, "checkpoint_digest": None}
    assert (await _post(h, normalized)).json() == receipt
    await h.store.initialize()
    await h.store.revoke_grant(h.principal, h.body["execution"]["execution_grant_ref"])
    monkeypatch.setattr(h.service, "_accepting", False)
    assert (await _post(h)).json() == receipt
    changed = deepcopy(h.body)
    changed["predecessor_snapshot_digest"] = "f" * 64
    conflict = await _post(h, changed)
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "request_conflict"
    other_source = await _resume(h.service, h.principal, receipt["attempt_id"], h.body)
    assert other_source.status_code == 409
    replay = await h.service.dispatch(h.principal, h.policy, AgentExecution.model_validate(old["request"]))
    assert replay == h.old_id
    assert len(h.runtime.jobs) == 2


async def test_late_client_worker_and_compression_updates_are_fenced(client_attempt, monkeypatch):
    h = client_attempt
    await connect(h)
    call = await create(h)
    await delivery(h)
    assert (await result(h, call)).status_code == 200
    await h.store.finish(h.id, "cancelled", "user_cancelled", "confirmed", None, {})
    body = await _resume_body(h.service, "caller-a", h.id)
    runtime = _ResumeRuntime()
    runtime.release = asyncio.Event()
    monkeypatch.setattr(h.service.agent_executor, "runtime", runtime)
    monkeypatch.setenv("KOTOYOSE_AGENT_PROVIDER_TOKEN", "fake-provider-only")
    response = await _resume(h.service, "caller-a", h.id, body)
    assert response.status_code == 202, response.text
    new_id = response.json()["attempt_id"]
    try:
        async with asyncio.timeout(10):
            await runtime.started.wait()
        before = await h.service.describe("caller-a", new_id)
        assert before["status"] == "running"
        assert (await result(h, call, value={"expression": "late"})).status_code in {401, 409}
        rejected = await h.client.post(f"/v1/attempts/{h.id}/client-connections", json={
            "schema_version": "1", "client_key": "client:fixture", "generation": 1,
        })
        assert rejected.status_code == 409
        assert not await h.store.mark_running(h.id)
        with pytest.raises(ExecutionError):
            await h.store.prepare_operation(h.id, "sandbox_command")
        await h.store.append_event(h.id, "compact_boundary", {"late": True})
        await h.store.resolve_operation("execution:" + h.id, "unconfirmed")
        await h.store.finish(h.id, "failed", "late_worker", "confirmed", 9, {})
        assert await h.service.describe("caller-a", new_id) == before
        assert not any(event["type"] == "compact_boundary" for event in await h.store.events("caller-a", h.id, 0, 100))
        assert all(op["state"] == "confirmed" for op in await h.store.operations(h.id))
        assert (await h.store.attempt("caller-a", h.id))["status"] == "cancelled"
    finally:
        runtime.release.set()
        await _terminal(h.client, new_id)
        await h.service.close()


@pytest.mark.parametrize("modes,retain_session", [(("native", "durable_state"), True),
                                                 (("native", "durable_state"), False),
                                                 (("durable_state",), False), ((), False)])
async def test_adapter_modes_and_capability_are_explicit(native, monkeypatch, modes, retain_session):
    h = await _source(native, monkeypatch, modes, retain_session)
    capabilities = (await h.client.get("/v1/capabilities")).json()
    capability = capabilities["capabilities"]["safe_resume"]
    assert capability["status"] == ("supported" if modes else "unsupported")
    assert capabilities["safe_resume_modes"] == list(modes)
    response = await _post(h)
    if not modes:
        assert response.json()["error"]["reason_code"] == "adapter_unsupported"
    else:
        assert response.status_code == 202, response.text
        expected = "native" if "native" in modes and retain_session else "durable_state"
        assert response.json()["resume_mode"] == expected
        await _terminal(h.client, response.json()["attempt_id"])
        assert h.runtime.jobs[-1].resume_mode == expected
        assert h.runtime.jobs[-1].provider_session_ref == (
            "provider-session:" + h.old_id if expected == "native" else None
        )
    monkeypatch.setattr(h.service.agent_executor, "runtime", _FakeAgentRuntime())
    assert (await h.client.get("/v1/capabilities")).json()["capabilities"]["safe_resume"]["status"] == "unsupported"
    adapter = NativeClaudeWorker()
    assert adapter.resume_support().modes == ("durable_state",)
    assert adapter.resume_session_ref("observed-sdk-id") is None


@pytest.mark.parametrize("change,reason", [("workspace", "workspace_mismatch"), ("operation", "operation_unknown"), ("grant", "grant_mismatch")])
async def test_safety_is_rechecked_inside_admission_transaction(native, monkeypatch, change, reason):
    h = await _source(native, monkeypatch)
    request = ResumeRequest.model_validate(h.body)
    old = await h.store.attempt(h.principal, h.old_id)
    snapshot = await h.service.admission.resolve_resume(h.principal, h.policy, old, request, h.service.resume_support())
    if change == "workspace":
        await h.store.pool.execute("UPDATE kotoyose.execution_workspaces SET generation=generation+1 WHERE id=$1", old["workspace_id"])
    elif change == "operation":
        await h.store.resolve_operation("execution:" + h.old_id, "unconfirmed")
    else:
        await h.store.revoke_grant(h.principal, request.execution.execution_grant_ref)
    with pytest.raises(ExecutionError) as caught:
        await h.store.admit(h.principal, request.execution.model_dump(mode="json"), snapshot, 2)
    assert caught.value.code == "resume_unsafe" and caught.value.reason_code == reason
    assert (await h.store.attempt(h.principal, h.old_id))["resumed_by"] is None
    assert await h.store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 1


@pytest.mark.parametrize("changed", [None, "files", "digest", "released"])
async def test_selected_checkpoint_digest_is_verified_before_resume(native, monkeypatch, changed):
    h = await _source(native, monkeypatch)
    data = b"checkpoint evidence"
    (h.workspace / "state.txt").write_bytes(data)
    receipt = await h.store.checkpoints.capture(h.principal, CheckpointCapture(
        schema_version="1", request_id=uuid4(), workspace_ref=h.body["execution"]["workspace_ref"],
        source_attempt_id=h.old_id, files=[{"path": "state.txt", "digest": hashlib.sha256(data).hexdigest(),
                                        "size": len(data), "mode": "0644", "origin": "generated"}],
    ))
    h.body.update(checkpoint_ref=receipt["checkpoint_ref"], checkpoint_digest=receipt["artifact"]["digest"])
    if changed == "files":
        (h.workspace / "state.txt").write_bytes(b"changed")
    elif changed == "digest":
        h.body["checkpoint_digest"] = "0" * 64
    elif changed == "released":
        await h.store.pool.execute("UPDATE kotoyose.execution_checkpoints SET released_at=now() WHERE source_attempt_id=$1", h.old_id)
    response = await _post(h)
    if changed:
        assert response.status_code == 409 and response.json()["error"]["reason_code"] == "checkpoint_mismatch"
    else:
        assert response.status_code == 202, response.text
        assert response.json()["resume_manifest"]["checkpoint_digest"] == receipt["artifact"]["digest"]
        await _terminal(h.client, response.json()["attempt_id"])
