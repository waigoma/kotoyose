"""ExecutionStore の PostgreSQL 原子性、所有境界、replay を検証する。"""

import asyncio
import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import asyncpg
import pytest

from kotoyose.execution_resources import (
    ArgumentList,
    CommandSpec,
    Environment,
    ExecutionBudget,
    ExecutionGrant,
    RepositoryContextPack,
    SkillResource,
    TimeoutPolicy,
    WorkingDirectory,
    content_digest,
    ExecutionError,
    schema_digest_for,
)
from kotoyose.execution_artifacts import CapturedArtifact
from kotoyose.execution_store import ExecutionStore


_TABLES = (
    "execution_workspace_retention_receipts", "execution_workspace_retention",
    "execution_checkpoint_restores", "execution_checkpoints",
    "execution_mcp_calls",
    "execution_artifact_retention_receipts", "execution_artifact_retention",
    "execution_cancellations",
    "execution_events", "execution_artifacts", "execution_operations",
    "execution_attempts", "execution_resources", "execution_workspaces",
)


def _database_name(url: str) -> str:
    return unquote(urlparse(url).path.lstrip("/")).split("?", 1)[0]


@pytest.fixture
async def store():
    """明示された test DB だけを使い、各 test の台帳を完全に消去する。"""
    url = os.environ.get("KOTOYOSE_EXECUTION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("KOTOYOSE_EXECUTION_TEST_DATABASE_URL is not set")
    database = _database_name(url)
    if not database.startswith("ahs_execution_test"):
        pytest.fail("refusing to connect: database name must start with ahs_execution_test")

    pool = await asyncpg.create_pool(url, min_size=1, max_size=8)
    try:
        await pool.execute("CREATE SCHEMA IF NOT EXISTS kotoyose")
        result = ExecutionStore(pool)
        await result.initialize()
        await pool.execute("TRUNCATE " + ", ".join(f"kotoyose.{name}" for name in _TABLES) + " CASCADE")
        yield result
    finally:
        await pool.execute("DROP TABLE IF EXISTS " + ", ".join(f"kotoyose.{name}" for name in _TABLES) + " CASCADE")
        await pool.close()


def _resources(now: datetime | None = None) -> dict[str, object]:
    now = now or datetime.now(timezone.utc)
    return {
        "command": CommandSpec(schema_version="1", kind="command_spec", executable="/bin/echo"),
        "argv": ArgumentList(schema_version="1", kind="argv", values=["hello"]),
        "cwd": WorkingDirectory(schema_version="1", kind="cwd", path="."),
        "env": Environment(schema_version="1", kind="env", values={"LANG": "C.UTF-8"}),
        "timeout": TimeoutPolicy(schema_version="1", kind="timeout_policy", soft_seconds=1,
                                  hard_seconds=10, grace_seconds=1),
        "budget": ExecutionBudget(schema_version="1", kind="budget", reservation_ref="reservation-1",
                                   mode="observed", deadline=now + timedelta(minutes=5),
                                   memory_bytes=64 * 1024 * 1024, pids_limit=4, cpus=1,
                                   output_limit_bytes=1024),
    }


async def _fixture_resources(store: ExecutionStore, principal: str, now: datetime | None = None):
    resources = _resources(now)
    refs = {name: await store.put_resource(principal, resource) for name, resource in resources.items()}
    grant = ExecutionGrant(
        schema_version="1", kind="grant", grant_id="grant-1", version=1,
        caller_run_ref="run-1", workspace_ref="workspace-ref", isolation="sandbox", network="none",
        command_spec_refs=[refs["command"]], resource_refs=[refs[name] for name in ("argv", "cwd", "env", "budget", "timeout")],
        policy_ref="policy-1", not_before=(now or datetime.now(timezone.utc)) - timedelta(minutes=1),
        expires_at=(now or datetime.now(timezone.utc)) + timedelta(minutes=5), max_seconds=10,
        delegation="none",
    )
    refs["grant"] = await store.put_resource(principal, grant)
    return refs


async def _admit_ready(store: ExecutionStore, tmp_path: Path, *, principal="caller-a", key="key-1"):
    workspace_path = tmp_path / principal
    workspace_path.mkdir()
    registered = await store.register_workspace(principal, f"caller-workspace-{principal}", workspace_path)
    workspace_ref = f"workspace:{registered['id']}:{registered['generation']}"
    refs = await _fixture_resources(store, principal)
    request = {
        "idempotency_key": key, "caller_run_ref": "run-1", "workspace_ref": workspace_ref,
        "execution_grant_ref": refs["grant"],
    }
    snapshot = {"workspace_id": registered["id"], "generation": registered["generation"], "workspace_path": str(workspace_path)}
    return registered, refs, request, snapshot


@pytest.mark.parametrize("drift", ["generation", "writer", "revoked", "expired", "unknown", "principal"])
async def test_provider_request_guard_rechecks_workspace_and_current_grant(store, tmp_path, drift):
    registered, refs, request, snapshot = await _admit_ready(store, tmp_path)
    attempt, _ = await store.admit("caller-a", request, snapshot, 1)
    attempt_id = attempt["id"]
    await store.pool.execute("UPDATE kotoyose.execution_attempts SET status='running' WHERE id=$1", attempt_id)
    await store.authorize_provider_request(attempt_id, "caller-a")
    if drift == "generation":
        await store.pool.execute("UPDATE kotoyose.execution_workspaces SET generation=generation+1 WHERE id=$1", registered["id"])
    elif drift == "writer":
        await store.pool.execute("UPDATE kotoyose.execution_workspaces SET writer_attempt=NULL WHERE id=$1", registered["id"])
    elif drift == "revoked":
        await store.revoke_grant("caller-a", refs["grant"])
    elif drift == "expired":
        await store.pool.execute("UPDATE kotoyose.execution_resources SET payload=jsonb_set(payload,'{expires_at}',to_jsonb((now()-interval '1 second')::text)) WHERE ref=$1", refs["grant"])
    elif drift == "unknown":
        operation = await store.prepare_operation(attempt_id, "provider_process")
        await store.resolve_operation(operation, "unconfirmed")
    with pytest.raises(ExecutionError):
        await store.authorize_provider_request(attempt_id, "different-caller" if drift == "principal" else "caller-a")


@pytest.mark.asyncio
async def test_dispatch_cancellation_closes_admission_across_restart(store, tmp_path):
    """取消が先着した key は、再接続や遅延した POST でも Attempt を作らない。"""
    _, _, request, snapshot = await _admit_ready(store, tmp_path)
    assert await store.cancel_dispatch("caller-a", request["idempotency_key"]) is None
    reopened = ExecutionStore(store.pool)
    await reopened.initialize()
    assert await reopened.cancel_dispatch("caller-a", request["idempotency_key"]) is None
    with pytest.raises(ExecutionError, match="admission was cancelled"):
        await reopened.admit("caller-a", request, snapshot, 1)
    assert await store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 0


@pytest.mark.asyncio
async def test_dispatch_cancellation_is_caller_scoped_and_preserves_admitted_identity(store, tmp_path):
    """別 Caller の取消は影響せず、受理済み key の取消は既存 Attempt へ収束する。"""
    _, _, request, snapshot = await _admit_ready(store, tmp_path)
    assert await store.cancel_dispatch("caller-b", request["idempotency_key"]) is None
    attempt, created = await store.admit("caller-a", request, snapshot, 1)
    assert created
    assert await store.cancel_dispatch("caller-a", request["idempotency_key"]) == attempt["id"]
    assert not await store.mark_running(attempt["id"])
    replay, created = await store.admit("caller-a", request, snapshot, 1)
    assert replay["id"] == attempt["id"] and not created


@pytest.mark.asyncio
async def test_concurrent_cancellation_and_admission_cannot_leave_a_runnable_dispatch(store, tmp_path):
    """競合のどちらが先でも、取消完了後に新しい worker を開始できない。"""
    _, _, request, snapshot = await _admit_ready(store, tmp_path)
    admitted, cancelled = await asyncio.gather(
        store.admit("caller-a", request, snapshot, 1),
        store.cancel_dispatch("caller-a", request["idempotency_key"]),
        return_exceptions=True,
    )
    if isinstance(admitted, ExecutionError):
        assert cancelled is None
        assert await store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 0
    else:
        attempt, _ = admitted
        assert cancelled == attempt["id"]
        assert not await store.mark_running(attempt["id"])


@pytest.mark.asyncio
async def test_resource_refs_are_content_addressed_immutable_and_caller_scoped(store, tmp_path):
    """同じ payload は同じ ref に収束し、別 principal は読み取れない。"""
    resource = CommandSpec(schema_version="1", kind="command_spec", executable="/bin/echo")
    ref1 = await store.put_resource("caller-a", resource)
    ref2 = await store.put_resource("caller-a", resource)
    assert ref1 == ref2
    assert (await store.resource("caller-a", ref1)).executable == "/bin/echo"
    with pytest.raises(ExecutionError) as error:
        await store.resource("caller-b", ref1)
    assert error.value.code == "invalid_request"


@pytest.mark.asyncio
async def test_pinned_context_and_skill_resources_are_content_addressed_and_caller_scoped(store):
    """Pack / Skill は通常 resource と同じ immutable ref で保存し principal を越境しない。"""
    content = "fixed repository context"
    digest = hashlib.sha256(content.encode()).hexdigest()
    pack = RepositoryContextPack(
        schema_version="1", kind="repository_context_pack", content=content,
        content_digest=digest, schema_digest=schema_digest_for("repository_context_pack"),
        generator="taskhub", version="1", config_digest=digest, source_revision="a" * 40,
        repository_ref="repo:fixed", provenance="project",
    )
    skill_content = "fixed skill instructions"
    skill_digest = hashlib.sha256(skill_content.encode()).hexdigest()
    skill = SkillResource(
        schema_version="1", kind="skill", content=skill_content,
        content_digest=skill_digest, schema_digest=schema_digest_for("skill"),
        generator="taskhub", version="1", config_digest=digest, source_revision="b" * 40,
        repository_ref="repo:fixed", provenance="project",
    )
    pack_ref = await store.put_resource("caller-a", pack)
    skill_ref = await store.put_resource("caller-a", skill)
    assert pack_ref == await store.put_resource("caller-a", pack)
    assert (await store.resource("caller-a", pack_ref)).content == content
    assert (await store.resource("caller-a", skill_ref)).content == skill_content
    with pytest.raises(ExecutionError) as error:
        await store.resource("caller-b", pack_ref)
    assert error.value.code == "invalid_request"


@pytest.mark.asyncio
async def test_grant_identity_cannot_change_or_reactivate_revoked_version(store):
    """同じ Grant ID/version の変更と失効済み内容の再登録による権限復活を防ぐ。"""
    refs = await _fixture_resources(store, "caller-a")
    grant = await store.resource("caller-a", refs["grant"])
    assert await store.put_resource("caller-a", grant) == refs["grant"]
    with pytest.raises(ExecutionError) as conflict:
        await store.put_resource("caller-a", grant.model_copy(update={"max_seconds": 30}))
    assert conflict.value.status == 409
    await store.revoke_grant("caller-a", refs["grant"])
    assert await store.put_resource("caller-a", grant) == refs["grant"]
    with pytest.raises(ExecutionError, match="revoked"):
        await store.resource("caller-a", refs["grant"])
    replacement = await store.put_resource("caller-a", grant.model_copy(update={"version": 2}))
    assert (await store.resource("caller-a", replacement)).version == 2


@pytest.mark.asyncio
async def test_coordinator_lease_and_recovery_keep_unknown_writer_fenced(store, tmp_path, monkeypatch):
    """別 coordinator の二重起動と、停止不明の workspace 解放を防ぎ、後日の証拠だけを反映する。"""
    from kotoyose.execution_resources import NativeExecutionConfig
    from kotoyose.execution_service import ExecutionService

    class Worker:
        confirmation = "unconfirmed"

        async def probe(self):
            return True

        async def stop(self, attempt_id, generation, grace_seconds):
            return self.confirmation

        async def execute(self, job, cancel):
            pytest.fail("Recovery must not redispatch an execution")

    registered, _, request, snapshot = await _admit_ready(store, tmp_path)
    from kotoyose.execution_resources import default_worker_capabilities
    snapshot.update({"kind": "command", "worker_id": "local-docker",
                     "worker_capabilities": default_worker_capabilities()["local-docker"].public_snapshot()})
    attempt, _ = await store.admit("caller-a", request, snapshot, 1)
    await store.mark_running(attempt["id"])
    from kotoyose import config as config_module
    from kotoyose.config import Client, HarnessConfig
    monkeypatch.setenv("KOTOYOSE_COORDINATOR_TEST_TOKEN", "test-only")
    monkeypatch.setattr(config_module, "_config", HarnessConfig(clients={"caller-a": Client(
        workspace_roots=[tmp_path],
        max_run_seconds=60, max_memory_bytes=64 * 1024 * 1024,
        max_processes=16, max_cpus=1, max_concurrent_runs=1,
    )}))
    config = NativeExecutionConfig(schema_version="1", image_id="sha256:" + "a" * 64)
    worker = Worker()
    first = ExecutionService(config, store, worker)
    second = ExecutionService(config, store, worker)
    await first.start()
    try:
        with pytest.raises(RuntimeError, match="Another Kotoyose coordinator"):
            await second.start()
        unknown = await store.attempt("caller-a", attempt["id"])
        assert (unknown["status"], unknown["stop_confirmation"]) == ("worker_lost", "unconfirmed")
        workspace = await store.pool.fetchrow("SELECT * FROM kotoyose.execution_workspaces WHERE id=$1", registered["id"])
        assert workspace["writer_attempt"] == attempt["id"]
        assert workspace["generation"] == 0
    finally:
        await first.close()
    worker.confirmation = "confirmed"
    await second.start()
    try:
        confirmed = await store.attempt("caller-a", attempt["id"])
        assert (confirmed["status"], confirmed["stop_confirmation"]) == ("worker_lost", "confirmed")
        events = await store.events("caller-a", attempt["id"], 0, 100)
        assert events[-1]["type"] == "stop_confirmed"
        workspace = await store.pool.fetchrow("SELECT * FROM kotoyose.execution_workspaces WHERE id=$1", registered["id"])
        assert workspace["writer_attempt"] is None
        assert workspace["generation"] == 1
    finally:
        await second.close()


@pytest.mark.parametrize("loss", ["released_lock", "terminated_session"])
async def test_coordinator_detects_session_or_lock_loss(store, tmp_path, monkeypatch, loss):
    """F1/F3: 自分の test session だけを対象に、再入取得せず喪失を検出する。"""
    from kotoyose import config as config_module
    from kotoyose.config import Client, HarnessConfig
    from kotoyose.coordinator_lease import CoordinatorLease
    from kotoyose.execution_resources import NativeExecutionConfig
    from kotoyose.execution_service import ExecutionService
    from unittest.mock import AsyncMock

    monkeypatch.setenv("KOTOYOSE_COORDINATOR_TEST_TOKEN", "fixture-only")
    monkeypatch.setattr(config_module, "_config", HarnessConfig(
        models={},
        clients={"caller": Client(
            workspace_roots=[tmp_path],
            max_run_seconds=60, max_memory_bytes=64 * 1024 * 1024,
            max_processes=16, max_cpus=1, max_concurrent_runs=1,
        )},
    ))
    config = NativeExecutionConfig(schema_version="1", image_id="sha256:" + "a" * 64)
    service = ExecutionService(config, store, type("Worker", (), {"probe": AsyncMock(return_value=True)})())
    exited = asyncio.Event()
    heartbeat_ready = asyncio.Event()
    lease_queries = asyncio.Lock()
    verify = CoordinatorLease._verify
    successful_checks = 0

    async def verify_lease(coordinator):
        nonlocal successful_checks
        async with lease_queries:
            await verify(coordinator)
            successful_checks += 1
            if successful_checks == 3:
                heartbeat_ready.set()

    monkeypatch.setattr(CoordinatorLease, "_verify", verify_lease)
    monkeypatch.setattr("kotoyose.coordinator_lease.HEARTBEAT_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("kotoyose.coordinator_lease.exit_for_restart", exited.set)
    await service.start()
    try:
        # 起動時の確認と正常 heartbeat 2 回を待つ。故障注入は監視と同じ接続を使う。
        await asyncio.wait_for(heartbeat_ready.wait(), timeout=2)
        async with lease_queries:
            if loss == "released_lock":
                assert await service._lease.fetchval("SELECT pg_advisory_unlock(hashtextextended('kotoyose-local-worker',0))")
                assert await service._lease.fetchval("SELECT 1") == 1
            else:
                # fixture の専用 test DB に作った、この coordinator 自身の PID だけを終了する。
                pid = await service._lease.fetchval("SELECT pg_backend_pid()")
                assert await store.pool.fetchval("SELECT pg_terminate_backend($1)", pid)
        async with asyncio.timeout(0.5):
            while service._accepting:
                await asyncio.sleep(0.001)
        await asyncio.wait_for(exited.wait(), timeout=0.5)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_same_key_race_creates_one_attempt_one_event_and_one_writer(store, tmp_path):
    """同一 caller / key の同時 admission は一つの dispatch intent に直列化される。"""
    registered, refs, request, snapshot = await _admit_ready(store, tmp_path)
    results = await asyncio.gather(*(
        store.admit("caller-a", request, snapshot, 2) for _ in range(8)
    ))
    assert sum(created for _, created in results) == 1
    attempt_ids = {attempt["id"] for attempt, _ in results}
    assert len(attempt_ids) == 1
    attempt_id = next(iter(attempt_ids))
    counts = await store.pool.fetchrow(
        "SELECT (SELECT count(*) FROM kotoyose.execution_attempts WHERE id=$1) AS attempts, "
        "(SELECT count(*) FROM kotoyose.execution_events WHERE attempt_id=$1) AS events, "
        "(SELECT writer_attempt FROM kotoyose.execution_workspaces WHERE id=$2) AS writer",
        attempt_id, registered["id"],
    )
    assert counts["attempts"] == 1
    assert counts["events"] == 1
    assert counts["writer"] == attempt_id


@pytest.mark.asyncio
async def test_same_key_with_different_payload_is_conflict(store, tmp_path):
    """idempotency key の payload 変更を新規実行へ変換しない。"""
    _, _, request, snapshot = await _admit_ready(store, tmp_path)
    await store.admit("caller-a", request, snapshot, 2)
    changed = {**request, "caller_run_ref": "different-run"}
    with pytest.raises(ExecutionError) as error:
        await store.admit("caller-a", changed, snapshot, 2)
    assert error.value.code == "request_conflict"


@pytest.mark.asyncio
async def test_different_keys_competing_for_one_workspace_have_one_winner(store, tmp_path):
    """workspace writer lease は key が異なっても同時実行を一つに制限する。"""
    _, _, request, snapshot = await _admit_ready(store, tmp_path)
    second = {**request, "idempotency_key": "key-2"}
    results = await asyncio.gather(
        store.admit("caller-a", request, snapshot, 2),
        store.admit("caller-a", second, snapshot, 2),
        return_exceptions=True,
    )
    assert sum(isinstance(result, tuple) and result[1] for result in results) == 1
    errors = [result for result in results if isinstance(result, ExecutionError)]
    assert len(errors) == 1
    assert errors[0].code == "request_conflict"


@pytest.mark.asyncio
async def test_attempt_events_and_artifacts_are_caller_isolated(store, tmp_path):
    """Attempt / event / artifact は principal を横取りして取得できない。"""
    _, _, request, snapshot = await _admit_ready(store, tmp_path)
    attempt, _ = await store.admit("caller-a", request, snapshot, 2)
    with pytest.raises(ExecutionError) as error:
        await store.attempt("caller-b", attempt["id"])
    assert error.value.status == 404
    with pytest.raises(ExecutionError) as error:
        await store.events("caller-b", attempt["id"], 0, 100)
    assert error.value.status == 404


@pytest.mark.asyncio
async def test_finish_atomically_persists_artifacts_terminal_event_operation_and_release(store, tmp_path):
    """終端記録と証跡・operation・writer 解放を一つの transaction にする。"""
    registered, _, request, snapshot = await _admit_ready(store, tmp_path)
    attempt, _ = await store.admit("caller-a", request, snapshot, 2)
    await store.finish(attempt["id"], "completed", "ok", "confirmed", 0, {"stdout": b"done"})
    row = await store.attempt("caller-a", attempt["id"])
    operation = await store.pool.fetchrow("SELECT * FROM kotoyose.execution_operations WHERE attempt_id=$1", attempt["id"])
    workspace = await store.pool.fetchrow("SELECT * FROM kotoyose.execution_workspaces WHERE id=$1", registered["id"])
    events = await store.events("caller-a", attempt["id"], 0, 100)
    assert row["status"] == "completed"
    assert len(row["artifacts"]) == 1
    assert operation["state"] == "confirmed"
    assert workspace["writer_attempt"] is None
    assert workspace["generation"] == 1
    assert events[-1]["type"] == "completed"


@pytest.mark.asyncio
async def test_finish_persists_artifact_media_digest_and_manifest(store, tmp_path):
    """collector の binary metadata と manifest を終端 transaction に固定する。"""
    _, _, request, snapshot = await _admit_ready(store, tmp_path)
    attempt, _ = await store.admit("caller-a", request, snapshot, 2)
    content = b"\x00\x01png"
    manifest = [{"name": "evidence", "state": "captured", "digest": hashlib.sha256(content).hexdigest()}]
    await store.finish(
        attempt["id"], "completed", "ok", "confirmed", 0,
        {"evidence": CapturedArtifact(content, "application/octet-stream")},
        artifact_manifest=manifest,
    )
    row = await store.attempt("caller-a", attempt["id"])
    artifact = await store.artifact("caller-a", row["artifacts"][0]["ref"])
    assert artifact["content"] == content
    assert artifact["media_type"] == "application/octet-stream"
    assert artifact["digest"] == manifest[0]["digest"]
    assert row["artifact_manifest"] == manifest


@pytest.mark.asyncio
async def test_repeated_terminal_finish_is_immutable(store, tmp_path):
    """終端 Attempt の再通知は status、artifact、event を上書きしない。"""
    _, _, request, snapshot = await _admit_ready(store, tmp_path)
    attempt, _ = await store.admit("caller-a", request, snapshot, 2)
    await store.finish(attempt["id"], "completed", "first", "confirmed", 0, {"stdout": b"one"})
    before = await store.attempt("caller-a", attempt["id"])
    await store.finish(attempt["id"], "failed", "second", "unknown", 9, {"stdout": b"two"})
    after = await store.attempt("caller-a", attempt["id"])
    assert (after["status"], after["reason"], after["exit_code"]) == (
        before["status"], before["reason"], before["exit_code"]
    )
    assert after["artifacts"] == before["artifacts"]
    assert after["sequence"] == before["sequence"]


@pytest.mark.asyncio
async def test_terminal_replay_survives_workspace_generation_change(store, tmp_path):
    """終端後に workspace generation が進んでも同一 dispatch を replay できる。"""
    registered, _, request, snapshot = await _admit_ready(store, tmp_path)
    attempt, _ = await store.admit("caller-a", request, snapshot, 2)
    digest = content_digest(request)
    await store.finish(attempt["id"], "completed", "ok", "confirmed", 0, {})
    replay = await store.replay("caller-a", request["idempotency_key"], digest)
    assert replay is not None and replay["id"] == attempt["id"]
    assert (await store.workspace("caller-a", f"workspace:{registered['id']}:1"))["generation"] == 1


@pytest.mark.asyncio
async def test_revoke_and_cancel_are_idempotent_and_record_one_transition(store, tmp_path):
    """Grant revoke と cancel の反復は同じ状態を保ち、重複 event を作らない。"""
    _, refs, request, snapshot = await _admit_ready(store, tmp_path)
    attempt, _ = await store.admit("caller-a", request, snapshot, 2)
    first = await store.revoke_grant("caller-a", refs["grant"])
    events_after_revoke = await store.pool.fetchval(
        "SELECT count(*) FROM kotoyose.execution_events WHERE attempt_id=$1", attempt["id"]
    )
    second = await store.revoke_grant("caller-a", refs["grant"])
    await store.cancel("caller-a", attempt["id"])
    row = await store.attempt("caller-a", attempt["id"])
    event_count = await store.pool.fetchval(
        "SELECT count(*) FROM kotoyose.execution_events WHERE attempt_id=$1", attempt["id"]
    )
    assert first == second == [attempt["id"]]
    assert row["status"] == "cancelling"
    assert event_count == events_after_revoke
    finished = await store.finish(attempt["id"], "succeeded", "completed", "confirmed", None, {})
    assert finished is not None
    assert finished[0] == "cancelled"


@pytest.mark.asyncio
async def test_stale_workspace_generation_rejects_old_admission_snapshot(store, tmp_path):
    """writer 解放後の古い generation を使った admission は再利用できない。"""
    registered, _, request, snapshot = await _admit_ready(store, tmp_path)
    attempt, _ = await store.admit("caller-a", request, snapshot, 2)
    await store.finish(attempt["id"], "completed", "ok", "confirmed", 0, {})
    with pytest.raises(ExecutionError) as error:
        await store.workspace("caller-a", request["workspace_ref"])
    assert error.value.code == "request_conflict"
    fresh = {**request, "idempotency_key": "key-after-stale"}
    with pytest.raises(ExecutionError) as error:
        await store.admit("caller-a", fresh, snapshot, 2)
    assert error.value.code == "request_conflict"


@pytest.mark.asyncio
async def test_event_replay_is_ordered_and_cursor_based(store, tmp_path):
    """永続 event は sequence 順で cursor より後だけを再送する。"""
    _, _, request, snapshot = await _admit_ready(store, tmp_path)
    attempt, _ = await store.admit("caller-a", request, snapshot, 2)
    assert await store.mark_running(attempt["id"])
    await store.append_event(attempt["id"], "progress", {"step": 1})
    await store.finish(attempt["id"], "completed", "ok", "confirmed", 0, {})
    events = await store.events("caller-a", attempt["id"], 0, 100)
    assert [event["sequence"] for event in events] == [1, 2, 3, 4]
    assert [event["type"] for event in events] == ["dispatched", "running", "progress", "completed"]
    assert [event["sequence"] for event in await store.events("caller-a", attempt["id"], 2, 100)] == [3, 4]
