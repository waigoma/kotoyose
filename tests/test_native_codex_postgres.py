"""専用 PostgreSQL と偽 provider で Codex の admission と実 tool を確認する。"""

import asyncio
import os
from pathlib import Path
import shutil
from uuid import uuid4
import time
from unittest.mock import AsyncMock

import asyncpg
import pytest
import pytest_asyncio

from kotoyose.config import AgentRoute, AliasAgent, Connection, HarnessConfig, ModelPreset, get_config
from kotoyose.codex_rootfs import rootfs_digest
from kotoyose.execution_contract import ResumeRequest, RuntimeAuthenticationResolution
from kotoyose.execution_resources import (
    AgentDefinition, CallerPolicy, DriverCell, DriverInstall, ExecutionError,
    ExecutionGrant, NativeExecutionConfig, content_digest,
)
from kotoyose.execution_service import ExecutionService
from kotoyose.execution_store import ExecutionStore
from kotoyose.local_worker import DockerLocalWorker
from tests.test_execution_agent import _resources
from tests.codex_runtime_fixture import codex_rootfs  # noqa: F401
from tests.codex_fake_api import fake_api
from tests.codex_subscription_fixture import subscription_auth
from kotoyose.codex_subscription import read_subscription


DATABASE_URL = os.environ.get("KOTOYOSE_NATIVE_CODEX_TEST_DATABASE_URL")
IMAGE_ID = os.environ.get("KOTOYOSE_DOCKER_TEST_IMAGE")
pytestmark = pytest.mark.skipif(not DATABASE_URL or not IMAGE_ID, reason="Dedicated Codex test DB and image required")


def _install(service):
    return service.config.drivers["codex.app-server"]


def _cell(service):
    return _install(service).cells["standard"]


def _use_subscription(service, auth_file, *, cli_binary=None):
    """jail の standard cell はそのまま、認証だけ subscription の connection に切り替える。"""
    install = _install(service)
    if cli_binary is not None:
        install.cli_binary = cli_binary
    cfg = get_config()
    conn = Connection(provider="codex", kind="subscription", data_policy="no_training", auth_file=auth_file)
    cfg.connections["codex-chatgpt"] = conn
    cfg.routes["automatic"] = cfg.routes["automatic"].model_copy(update={"connection": "codex-chatgpt"})
    return conn


def _use_api(service):
    cfg = get_config()
    cfg.connections["codex-api"] = Connection(provider="codex", kind="api_key", data_policy="no_training", secret="env:FIXTURE_CODEX_TOKEN")
    cfg.routes["automatic"] = cfg.routes["automatic"].model_copy(update={"connection": "codex-api"})


@pytest_asyncio.fixture
async def codex_service(tmp_path, monkeypatch, provider):
    monkeypatch.setenv("FIXTURE_CALLER_TOKEN", "fake-caller")
    monkeypatch.setenv("FIXTURE_CODEX_TOKEN", "fake-provider-secret")
    policy_cfg = {
        "workspace_roots": [tmp_path],
        "models": ["automatic"], "places": ["isolated"],
        "allowed_env_vars": [], "allowed_provenance": ["trusted", "project", "agent_generated", "external_untrusted", "mixed"],
        "max_run_seconds": 30, "max_memory_bytes": 128 * 1024 * 1024, "max_processes": 32, "max_cpus": 1,
        "max_concurrent_runs": 2,
    }
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(
        connections={"codex-api": Connection(provider="codex", kind="api_key", data_policy="no_training", secret="env:FIXTURE_CODEX_TOKEN")},
        routes={"automatic": AgentRoute(
            driver="codex.app-server", model="gpt-6-luna", connection="codex-api",
        )},
        models={"automatic": ModelPreset(
            route="automatic",
            agent=AliasAgent(place="isolated", surface="exec"),
        )},
        clients={"caller-a": policy_cfg},
    ))
    config = NativeExecutionConfig(
        schema_version="1", image_id=IMAGE_ID,
        drivers={"codex.app-server": DriverInstall(
            cli_binary=provider.launcher_path,
            cells={"standard": DriverCell(
                rootfs=provider.fixture_rootfs[0], rootfs_digest=provider.fixture_rootfs[1],
            )},
        )},
    )
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=8, command_timeout=10)
    assert (await pool.fetchval("SELECT current_database()")).startswith("ahs_execution_test_codex_")
    await pool.execute("DROP SCHEMA IF EXISTS kotoyose CASCADE; CREATE SCHEMA kotoyose")
    service = ExecutionService(config, ExecutionStore(pool), DockerLocalWorker(IMAGE_ID))
    await service.start()
    policy = get_config().clients["caller-a"]
    request, workspace_ref, refs = await _resources(service, policy, tmp_path / "workspace", input_kind="task_request")
    definition = await service.store.put_resource("caller-a", AgentDefinition(
        schema_version="1", kind="agent_definition", instructions="tool", output_contract="text",
        max_turns=2, timeout_policy_ref=refs["timeout"],
    ))
    grant = await service.store.resource("caller-a", refs["grant"])
    grant = grant.model_copy(update={"version": 2, "agent_models": ["automatic"],
                                    "resource_refs": [*grant.resource_refs, definition]})
    grant_ref = await service.store.put_resource("caller-a", grant)
    request = request.model_copy(update={"model": "automatic", "runtime_profile": None,
                                        "agent_definition_ref": definition, "execution_grant_ref": grant_ref})
    try:
        yield service, policy, request, tmp_path / "workspace"
    finally:
        await service.close()
        rows = await pool.fetch("SELECT id,generation FROM kotoyose.execution_attempts")
        operations = await pool.fetch("SELECT id FROM kotoyose.execution_operations WHERE kind='sandbox_command'")
        for row in operations:
            for generation in {r["generation"] for r in rows}:
                process = await asyncio.create_subprocess_exec("docker", "rm", "-f", f"kotoyose-{row['id']}-{generation}",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await process.wait()
        await pool.execute("DROP SCHEMA kotoyose CASCADE")
        await pool.close()


async def terminal(service, attempt_id):
    async with asyncio.timeout(15):
        while True:
            attempt = await service.store.attempt("caller-a", attempt_id)
            if attempt["status"] not in {"accepted", "dispatched", "running", "cancelling"}:
                return attempt
            await asyncio.sleep(0.02)


async def test_concurrent_submission_admits_once_and_executes_real_sandbox_tool(codex_service):
    service, policy, request, workspace = codex_service
    ids = await asyncio.gather(*(service.dispatch("caller-a", policy, request) for _ in range(6)))
    assert len(set(ids)) == 1
    attempt = await terminal(service, ids[0])
    assert (attempt["status"], attempt["stop_confirmation"]) == ("succeeded", "confirmed"), attempt
    assert (workspace / "marker").read_text() == "ok"
    operations = await service.store.operations(ids[0])
    assert sum(item["kind"] == "provider_process" for item in operations) == 1
    assert sum(item["kind"] == "sandbox_command" for item in operations) == 1
    assert all(item["state"] == "confirmed" for item in operations)
    assert attempt["snapshot"]["provider"] == "codex" and attempt["snapshot"]["isolation"] == "sandbox"
    assert "fake-provider-secret" not in str(attempt)


@pytest.mark.skipif(not os.environ.get("KOTOYOSE_CODEX_CONFORMANCE_BINARY"), reason="Explicit pinned CLI fixture required")
@pytest.mark.parametrize("authentication", ["subscription", "api"])
async def test_actual_cli_fake_api_executes_journaled_real_docker_tool(codex_service, monkeypatch, authentication):
    service, policy, request, workspace = codex_service
    _install(service).cli_binary = Path(os.environ["KOTOYOSE_CODEX_CONFORMANCE_BINARY"])
    token = None
    if authentication == "subscription":
        path, token = subscription_auth(workspace.parent)
        _use_subscription(service, path)
        binding = service.resolve_runtime_authentication(policy, RuntimeAuthenticationResolution(schema_version="1", model="automatic"))
        request = request.model_copy(update={"required_authentication_mode": "chatgpt_subscription",
            "expected_authentication_binding_digest": binding["runtime_authentication"]["binding_digest"]})
    async with fake_api(monkeypatch, tool=True) as calls:
        attempt_id = await service.dispatch("caller-a", policy, request)
        attempt = await terminal(service, attempt_id)
    assert (attempt["status"], attempt["stop_confirmation"]) == ("succeeded", "confirmed"), attempt
    assert (workspace / "marker").read_text() == "ok"
    assert len(calls) == 2
    assert all([tool["name"] for tool in body["tools"]] == ["sandbox_exec"] for _, body in calls)
    operations = await service.store.operations(attempt_id)
    assert sum(item["kind"] == "sandbox_command" for item in operations) == 1
    assert all(item["state"] == "confirmed" for item in operations)
    assert "fake-provider-secret" not in str(attempt) and "fake-provider-secret" not in str(operations)
    if token is not None:
        assert token not in str(attempt) + str(operations)
        assert not (workspace / "auth.json").exists()
        assert all(b"host: chatgpt.com" in header for header, _ in calls)
        assert attempt["snapshot"]["credential_mode"] == "chatgpt_subscription"
        public = await service.describe("caller-a", attempt_id)
        assert public["runtime_authentication"]["mode"] == "chatgpt_subscription"
        assert len(public["runtime_authentication"]["binding_digest"]) == 64
        assert token not in str(public) and str(path) not in str(public)


async def test_subscription_required_request_rejects_paid_api_profile_before_dispatch(codex_service):
    service, policy, request, workspace = codex_service
    request = request.model_copy(update={"required_authentication_mode": "chatgpt_subscription",
                                        "expected_authentication_binding_digest": "0" * 64})
    with pytest.raises(ExecutionError, match="authentication mode"):
        await service.dispatch("caller-a", policy, request)
    assert not (workspace / "marker").exists()


async def test_subscription_auth_source_and_account_drift_fence_snapshot(codex_service, monkeypatch):
    service, policy, request, workspace = codex_service
    path, token = subscription_auth(workspace.parent)
    credential = _use_subscription(service, path)
    binding = service.resolve_runtime_authentication(policy, RuntimeAuthenticationResolution(schema_version="1", model="automatic"))
    request = request.model_copy(update={"required_authentication_mode": "chatgpt_subscription",
        "expected_authentication_binding_digest": binding["runtime_authentication"]["binding_digest"]})
    snapshot = await service.admission.resolve("caller-a", policy, request)
    assert snapshot["codex_auth_identity"] == read_subscription(path).identity
    assert token not in str(snapshot) and "false-refresh-secret" not in str(snapshot)
    subscription_auth(workspace.parent, account="changed-account")
    with pytest.raises(ExecutionError, match="binding changed"):
        await service.agent_executor.execute({"snapshot": snapshot}, asyncio.Event())
    subscription_auth(workspace.parent)
    monkeypatch.setattr(credential, "auth_file", path.parent / "different.json")
    with pytest.raises(ExecutionError, match="authentication source changed"):
        await service.agent_executor.execute({"snapshot": snapshot}, asyncio.Event())


@pytest.mark.parametrize("change", ["account", "mode"])
async def test_agent_execution_pinned_authentication_rejects_drift_before_admission(codex_service, monkeypatch, change):
    service, policy, request, workspace = codex_service
    path, _ = subscription_auth(workspace.parent)
    _use_subscription(service, path)
    binding = service.resolve_runtime_authentication(policy, RuntimeAuthenticationResolution(schema_version="1", model="automatic"))
    request = request.model_copy(update={"required_authentication_mode": "chatgpt_subscription",
        "expected_authentication_binding_digest": binding["runtime_authentication"]["binding_digest"]})
    if change == "account":
        subscription_auth(workspace.parent, account="another-account")
    else:
        _use_api(service)
    async with fake_api(monkeypatch, tool=False) as calls:
        with pytest.raises(ExecutionError) as error:
            await service.dispatch("caller-a", policy, request)
    assert error.value.reason_code == ("authentication_mode_mismatch" if change == "mode" else "authentication_binding_mismatch")
    assert calls == [] and not (workspace / "marker").exists()
    assert await service.store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 0


async def test_subscription_pin_accepts_same_account_at_new_path_before_admission(codex_service, monkeypatch):
    service, policy, request, workspace = codex_service
    path, _ = subscription_auth(workspace.parent)
    credential = _use_subscription(service, path)
    resolution = RuntimeAuthenticationResolution(schema_version="1", model="automatic")
    binding = service.resolve_runtime_authentication(policy, resolution)
    expected = binding["runtime_authentication"]["binding_digest"]
    request = request.model_copy(update={"required_authentication_mode": "chatgpt_subscription",
                                         "expected_authentication_binding_digest": expected})
    replacement = workspace.parent / "replacement"
    replacement.mkdir()
    new_path, _ = subscription_auth(replacement)
    monkeypatch.setattr(credential, "auth_file", new_path)

    assert service.resolve_runtime_authentication(policy, resolution) == binding
    snapshot = await service.admission.resolve("caller-a", policy, request)
    assert snapshot["codex_auth_file"] == str(new_path)
    assert snapshot["expected_authentication_binding_digest"] == expected


@pytest.mark.parametrize("missing", ["required_authentication_mode", "expected_authentication_binding_digest"])
async def test_subscription_pin_is_required_before_admission(codex_service, missing):
    service, policy, request, workspace = codex_service
    path, _ = subscription_auth(workspace.parent)
    _use_subscription(service, path)
    binding = service.resolve_runtime_authentication(policy, RuntimeAuthenticationResolution(schema_version="1", model="automatic"))
    request = request.model_copy(update={"required_authentication_mode": "chatgpt_subscription",
        "expected_authentication_binding_digest": binding["runtime_authentication"]["binding_digest"], missing: None})
    with pytest.raises(ExecutionError) as error:
        await service.dispatch("caller-a", policy, request)
    assert error.value.reason_code == "authentication_binding_required"
    assert await service.store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 0


@pytest.mark.skipif(not os.environ.get("KOTOYOSE_CODEX_CONFORMANCE_BINARY"), reason="Explicit pinned CLI fixture required")
@pytest.mark.parametrize("change", ["account", "source", "token"])
async def test_provider_rechecks_binding_after_tls_before_any_model_request(codex_service, monkeypatch, change):
    from kotoyose import codex_egress
    service, policy, request, workspace = codex_service
    path, _ = subscription_auth(workspace.parent)
    credential = _use_subscription(
        service, path, cli_binary=Path(os.environ["KOTOYOSE_CODEX_CONFORMANCE_BINARY"]),
    )
    binding = service.resolve_runtime_authentication(policy, RuntimeAuthenticationResolution(schema_version="1", model="automatic"))
    request = request.model_copy(update={"required_authentication_mode": "chatgpt_subscription",
        "expected_authentication_binding_digest": binding["runtime_authentication"]["binding_digest"]})
    async with fake_api(monkeypatch, tool=False) as calls:
        connect = codex_egress.connect_subscription_endpoint

        async def change_during_connect():
            connection = await connect()
            if change == "source":
                monkeypatch.setattr(credential, "auth_file", workspace.parent / "different-auth.json")
            subscription_auth(workspace.parent, account="another-account" if change == "account" else "fixture-account",
                              token_label="renewed")
            return connection

        monkeypatch.setattr(codex_egress, "connect_subscription_endpoint", change_during_connect)
        attempt_id = await service.dispatch("caller-a", policy, request)
        attempt = await terminal(service, attempt_id)
    if change == "token":
        assert attempt["status"] == "succeeded" and len(calls) == 1
        assert service.resolve_runtime_authentication(policy, RuntimeAuthenticationResolution(schema_version="1", model="automatic")) == binding
    else:
        assert attempt["status"] == "failed" and calls == []
        assert attempt["reason"] == "authentication_binding_mismatch"
    assert not (workspace / "marker").exists()


@pytest.mark.skipif(not os.environ.get("KOTOYOSE_CODEX_CONFORMANCE_BINARY"), reason="Explicit pinned CLI fixture required")
@pytest.mark.parametrize("stage", ["process_start", "tool_followup"])
@pytest.mark.parametrize("change", ["expired", "deleted", "account"])
async def test_production_authorize_preserves_auth_failure_before_start_or_followup(codex_service, monkeypatch, caplog,
                                                                                   stage, change):
    from kotoyose import codex_egress
    service, policy, request, workspace = codex_service
    path, token = subscription_auth(workspace.parent)
    _use_subscription(service, path, cli_binary=Path(os.environ["KOTOYOSE_CODEX_CONFORMANCE_BINARY"]))
    binding = service.resolve_runtime_authentication(policy, RuntimeAuthenticationResolution(schema_version="1", model="automatic"))
    request = request.model_copy(update={"required_authentication_mode": "chatgpt_subscription",
        "expected_authentication_binding_digest": binding["runtime_authentication"]["binding_digest"]})

    def invalidate_login():
        if change == "deleted":
            path.unlink()
        else:
            subscription_auth(workspace.parent, expiry=time.time() - 1 if change == "expired" else None,
                              account="another-account" if change == "account" else "fixture-account")

    if stage == "process_start":
        record_process = service.store.record_process

        async def record_then_invalidate(operation_id, identity):
            await record_process(operation_id, identity)
            invalidate_login()

        monkeypatch.setattr(service.store, "record_process", record_then_invalidate)
    else:
        execute = service.worker.execute

        async def tool_then_invalidate(job, cancel):
            result = await execute(job, cancel)
            invalidate_login()
            return result

        monkeypatch.setattr(service.worker, "execute", tool_then_invalidate)
    paid = AsyncMock(side_effect=AssertionError("paid fallback"))
    async with fake_api(monkeypatch, tool=True) as calls:
        monkeypatch.setattr(codex_egress, "connect_endpoint", paid)
        attempt_id = await service.dispatch("caller-a", policy, request)
        attempt = await terminal(service, attempt_id)
    assert attempt["status"] == "failed"
    assert attempt["reason"] == ("authentication_binding_mismatch" if change == "account" else "authentication_failed")
    assert len(calls) == (1 if stage == "tool_followup" else 0)
    assert (workspace / "marker").exists() == (stage == "tool_followup")
    public = await service.describe("caller-a", attempt_id)
    assert token not in str(public) + caplog.text + str(attempt["artifacts"])
    paid.assert_not_awaited()


async def test_codex_root_gone_does_not_release_writer_on_reconcile_or_restart(codex_service, monkeypatch):
    from kotoyose.native_claude import NativeAgentResult
    service, policy, request, workspace = codex_service

    async def root_exits(job, cancel, tool, *, on_process_started):
        process = await asyncio.create_subprocess_exec("/usr/bin/python3", "-c", "import time;time.sleep(.1)")
        await on_process_started(process.pid)
        await process.wait()
        return NativeAgentResult("failed", "missing_completion", b"", "unconfirmed",
            {"source": "unknown", "cost": None, "input_tokens": None, "output_tokens": None})

    monkeypatch.setattr(service.agent_executor.codex_runtime, "execute", root_exits)
    attempt_id = await service.dispatch("caller-a", policy, request)
    attempt = await terminal(service, attempt_id)
    async with asyncio.timeout(5):
        while attempt_id in service._tasks:
            await asyncio.sleep(.01)
    for restart in (False, True):
        if restart:
            await service.close()
            await service.start()
        result = await service.reconcile("caller-a", attempt_id)
        assert result["stop_confirmation"] == "unconfirmed"
        operations = await service.store.operations(attempt_id)
        assert any(item["state"] == "unknown" for item in operations)
        row = await service.store.pool.fetchrow("SELECT writer_attempt,generation FROM kotoyose.execution_workspaces WHERE id=$1", attempt["workspace_id"])
        assert row["writer_attempt"] == attempt_id and row["generation"] == attempt["generation"]


async def test_profile_drift_and_host_requirement_fail_without_fallback(codex_service, monkeypatch):
    service, policy, request, workspace = codex_service
    bad = request.model_copy(update={"requirements": request.requirements.model_copy(update={"isolation": "native"})})
    with pytest.raises(ExecutionError):
        await service.dispatch("caller-a", policy, bad)
    cfg = get_config()
    cfg.models["automatic"] = cfg.models["automatic"].model_copy(update={"agent": None, "route": None})
    with pytest.raises(ExecutionError):
        await service.dispatch("caller-a", policy, request)
    assert not (workspace / "marker").exists()


async def test_direct_profile_credential_drift_is_rejected_before_provider(codex_service, monkeypatch):
    service, policy, request, workspace = codex_service
    # 直接 profile 名は alias に置き換わった。alias の認証 pin は execute が同じ場所で拒む。
    bare = CallerPolicy.model_validate(policy.model_dump(exclude={"agent_models"}))
    grant = await service.store.resource("caller-a", request.execution_grant_ref)
    direct_grant = ExecutionGrant.model_validate(grant.model_dump(exclude={"agent_models"}))
    ref = await service.store.put_resource("caller-a", direct_grant.model_copy(update={"version": grant.version + 1}))
    direct = request.model_copy(update={"model": None, "runtime_profile": "profile-agent", "execution_grant_ref": ref})
    with pytest.raises(ExecutionError, match="runtime_profile was replaced"):
        await service.admission.resolve("caller-a", bare, direct)
    snapshot = await service.admission.resolve("caller-a", policy, request)
    assert snapshot["credential_env"] == "FIXTURE_CODEX_TOKEN"
    cfg = get_config()
    cfg.routes["automatic"] = cfg.routes["automatic"].model_copy(update={"credential": "env:CHANGED_FAKE_TOKEN"})
    with pytest.raises(ExecutionError, match="alias changed"):
        await service.agent_executor.execute({"snapshot": snapshot}, asyncio.Event())
    assert not (workspace / "marker").exists()


async def test_removed_codex_alias_mapping_fences_an_already_resolved_snapshot(codex_service, monkeypatch):
    service, policy, request, workspace = codex_service
    snapshot = await service.admission.resolve("caller-a", policy, request)
    cfg = get_config()
    cfg.models["automatic"] = cfg.models["automatic"].model_copy(update={"agent": None, "route": None})
    with pytest.raises(ExecutionError, match="alias changed"):
        await service.agent_executor.execute({"snapshot": snapshot}, asyncio.Event())
    assert not (workspace / "marker").exists()


async def test_codex_binary_content_change_fences_a_resolved_snapshot(codex_service):
    service, policy, request, workspace = codex_service
    snapshot = await service.admission.resolve("caller-a", policy, request)
    binary = _install(service).cli_binary
    binary.write_text(binary.read_text() + "\n# changed fixture CLI after admission\n")
    with pytest.raises(ExecutionError, match="binary changed"):
        await service.agent_executor.execute({"snapshot": snapshot}, asyncio.Event())
    assert not (workspace / "marker").exists()


async def test_codex_rootfs_pin_change_fences_a_resolved_snapshot(codex_service, monkeypatch):
    service, policy, request, workspace = codex_service
    snapshot = await service.admission.resolve("caller-a", policy, request)
    monkeypatch.setattr(_cell(service), "rootfs_digest", "0" * 64)
    with pytest.raises(ExecutionError, match="rootfs changed"):
        await service.agent_executor.execute({"snapshot": snapshot}, asyncio.Event())
    assert not (workspace / "marker").exists()


async def test_durable_resume_requires_new_generation_grant_and_rejects_replay(codex_service):
    service, policy, request, workspace = codex_service
    old_id = await service.dispatch("caller-a", policy, request)
    old = await terminal(service, old_id)
    description = await service.describe("caller-a", old_id)
    grant = await service.store.resource("caller-a", request.execution_grant_ref)
    fresh = grant.model_copy(update={"version": grant.version + 1, "workspace_ref": description["workspace_ref"]})
    fresh_ref = await service.store.put_resource("caller-a", fresh)
    continuation = request.model_copy(update={"idempotency_key": str(uuid4()),
        "workspace_ref": description["workspace_ref"], "execution_grant_ref": fresh_ref})
    resume = ResumeRequest(schema_version="1", predecessor_snapshot_digest=content_digest(old["snapshot"]),
                           execution=continuation)
    ids = await asyncio.gather(*(service.resume("caller-a", policy, old_id, resume) for _ in range(4)))
    assert len(set(ids)) == 1
    current = await terminal(service, ids[0])
    assert current["status"] == "succeeded", current
    assert current["generation"] == old["generation"] + 1
    assert current["snapshot"]["resume_mode"] == "durable_state"
    assert current["provider_session_ref"] is None
    stale = resume.model_copy(update={"execution": continuation.model_copy(update={"idempotency_key": str(uuid4())})})
    with pytest.raises(ExecutionError):
        await service.resume("caller-a", policy, old_id, stale)


@pytest.mark.parametrize("change", ["path", "digest", "path_and_digest"])
async def test_durable_resume_rejects_other_valid_reviewed_rootfs(codex_service, tmp_path, monkeypatch, change):
    service, policy, request, workspace = codex_service
    old_id = await service.dispatch("caller-a", policy, request)
    old = await terminal(service, old_id)
    assert old["status"] == "succeeded", old
    description = await service.describe("caller-a", old_id)
    grant = await service.store.resource("caller-a", request.execution_grant_ref)
    fresh_ref = await service.store.put_resource("caller-a", grant.model_copy(update={
        "version": grant.version + 1, "workspace_ref": description["workspace_ref"],
    }))
    continuation = request.model_copy(update={"idempotency_key": str(uuid4()),
        "workspace_ref": description["workspace_ref"], "execution_grant_ref": fresh_ref})
    resume = ResumeRequest(schema_version="1", predecessor_snapshot_digest=content_digest(old["snapshot"]),
                           execution=continuation)
    cell = _cell(service)
    root = cell.rootfs
    if change in {"path", "path_and_digest"}:
        root = tmp_path / "other-reviewed-runtime"
        shutil.copytree(cell.rootfs, root)
    changed_file = root / "usr/lib/reviewed-fixture.txt"
    if change in {"digest", "path_and_digest"}:
        changed_file.write_text("another reviewed fixture artifact")
        changed_file.chmod(0o644)
    monkeypatch.setattr(cell, "rootfs", root)
    operations_before = await service.store.operations(old_id)
    try:
        monkeypatch.setattr(cell, "rootfs_digest", rootfs_digest(root))
        with pytest.raises(ExecutionError) as caught:
            await service.resume("caller-a", policy, old_id, resume)
        assert (caught.value.code, caught.value.reason_code) == ("resume_unsafe", "runtime_mismatch")
        assert await service.store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 1
        assert await service.store.operations(old_id) == operations_before
        assert (await service.store.attempt("caller-a", old_id))["resumed_by"] is None
        assert (workspace / "marker").read_text() == "ok"
    finally:
        # The session-scoped artifact is shared with the next test.
        if change == "digest":
            changed_file.unlink()


async def test_cancelled_codex_process_resumes_only_after_confirmed_stop(codex_service):
    service, policy, request, workspace = codex_service
    source = await service.store.resource("caller-a", request.agent_definition_ref)
    hanging = await service.store.put_resource("caller-a", source.model_copy(update={"instructions": "hang"}))
    grant = await service.store.resource("caller-a", request.execution_grant_ref)
    source_grant = grant.model_copy(update={"version": grant.version + 1, "resource_refs": [*grant.resource_refs, hanging]})
    source_ref = await service.store.put_resource("caller-a", source_grant)
    source_request = request.model_copy(update={"agent_definition_ref": hanging, "execution_grant_ref": source_ref})
    old_id = await service.dispatch("caller-a", policy, source_request)
    async with asyncio.timeout(5):
        while not any(item["kind"] == "provider_process" and item["result"]
                      for item in await service.store.operations(old_id)):
            await asyncio.sleep(0.02)
        # 外側 launcher の記録と private PID namespace の成立には時間差がある。
        # confirmed stop/resume を試すため、namespace init の実在を先に観測する。
        from kotoyose.codex_process_stop import _children, _private_init
        from kotoyose.process_identity import ProcessIdentity
        while True:
            operation = next(item for item in await service.store.operations(old_id)
                             if item["kind"] == "provider_process" and item["result"])
            try:
                if _private_init(_children(ProcessIdentity(**operation["result"]))) is not None:
                    break
            except (OSError, ValueError, RuntimeError):
                pass
            await asyncio.sleep(.02)
    await service.cancel("caller-a", old_id)
    old = await terminal(service, old_id)
    assert (old["status"], old["stop_confirmation"]) == ("cancelled", "confirmed"), {
        "status": old["status"], "reason": old["reason"], "stop": old["stop_confirmation"],
        "operations": await service.store.operations(old_id),
    }
    assert not (workspace / "marker").exists()
    description = await service.describe("caller-a", old_id)
    fresh_ref = await service.store.put_resource("caller-a", source_grant.model_copy(update={
        "version": source_grant.version + 1, "workspace_ref": description["workspace_ref"],
    }))
    continuation = request.model_copy(update={"idempotency_key": str(uuid4()),
        "workspace_ref": description["workspace_ref"], "execution_grant_ref": fresh_ref})
    resume = ResumeRequest(schema_version="1", predecessor_snapshot_digest=content_digest(old["snapshot"]), execution=continuation)
    current_id = await service.resume("caller-a", policy, old_id, resume)
    current = await terminal(service, current_id)
    assert current["status"] == "succeeded", current
    assert current["snapshot"]["resume_mode"] == "durable_state"
    assert current["generation"] == old["generation"] + 1
