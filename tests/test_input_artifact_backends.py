"""input Artifact の backend retention と削除 recovery を実環境で検証する。"""

from datetime import datetime, timedelta, timezone
import asyncio
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

pytest.importorskip("boto3", reason="S3 adapter tests require the s3 extra")
from botocore.exceptions import ClientError, ReadTimeoutError

from kotoyose.artifact_blob_store import ArtifactBlob, ArtifactBlobRegistry, ArtifactMissingError, ArtifactStorageError
from kotoyose.artifact_storage_config import S3ArtifactStorageConfig
from kotoyose.execution_store import ExecutionStore
from kotoyose.execution_resources import ExecutionError
from kotoyose.input_artifacts import InputArtifactLimits, InputArtifactUpload
from kotoyose.local_artifact_store import LocalArtifactStore
from kotoyose.s3_artifact_store import S3ArtifactStore


DATABASE_URL = os.environ.get("KOTOYOSE_INPUT_ARTIFACT_BACKENDS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="Set a dedicated input-artifact backend test database URL",
)
PNG = b"\x89PNG\r\n\x1a\nbackend-proof"


def _upload(content: bytes = PNG, *, pin: bool = False) -> InputArtifactUpload:
    import base64

    return InputArtifactUpload(
        schema_version="1", media_type="image/png",
        data_base64=base64.b64encode(content).decode("ascii"),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1), retention_pin=pin,
    )


@pytest_asyncio.fixture(params=["postgres", "local", "s3"])
async def backend_runtime(request, tmp_path: Path):
    assert DATABASE_URL is not None
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4, command_timeout=10)
    assert (await pool.fetchval("SELECT current_database()")).startswith("ahs_execution_test")
    await pool.execute("DROP SCHEMA IF EXISTS kotoyose CASCADE; CREATE SCHEMA kotoyose")
    raw = None
    bucket = None
    if request.param == "postgres":
        registry = ArtifactBlobRegistry("postgres", {})
    elif request.param == "local":
        raw = LocalArtifactStore(tmp_path / "artifact-root")
        registry = ArtifactBlobRegistry("local", {"local": raw})
    else:
        endpoint = os.environ.get("KOTOYOSE_S3_TEST_ENDPOINT", "http://127.0.0.1:32773")
        if not os.environ.get("KOTOYOSE_S3_TEST_ACCESS_KEY") or not os.environ.get("KOTOYOSE_S3_TEST_SECRET_KEY"):
            await pool.close()
            pytest.skip("Explicit MinIO test credentials are required")
        config = S3ArtifactStorageConfig(
            kind="s3", endpoint=endpoint, bucket=f"kotoyose-input-test-{uuid4().hex}",
            access_key_env="KOTOYOSE_S3_TEST_ACCESS_KEY", secret_key_env="KOTOYOSE_S3_TEST_SECRET_KEY",
            allow_insecure_http=endpoint.startswith("http://"), request_timeout_seconds=2,
        )
        raw = S3ArtifactStore(config)
        bucket = config.bucket
        raw.client.create_bucket(Bucket=bucket)
        registry = ArtifactBlobRegistry("s3", {"s3": raw})
    store = ExecutionStore(pool, registry)
    await store.initialize()
    limits = InputArtifactLimits(allow_retention_pin=True)
    try:
        yield request.param, store, raw, limits
    finally:
        if bucket is not None:
            for item in raw.client.list_objects_v2(Bucket=bucket).get("Contents", []):
                raw.client.delete_object(Bucket=bucket, Key=item["Key"])
            raw.client.delete_bucket(Bucket=bucket)
        await pool.execute("DROP SCHEMA IF EXISTS kotoyose CASCADE")
        await pool.close()


@pytest.mark.asyncio
async def test_backend_expiry_gc_tombstones_and_releases_bytes(backend_runtime):
    kind, store, raw, limits = backend_runtime
    upload = _upload()
    result = await store.put_input_artifact("caller-a", upload, limits)
    ref = result["ref"]
    await store.pool.execute(
        "UPDATE kotoyose.execution_input_artifacts SET expires_at=now()-interval '1 second' WHERE ref=$1", ref,
    )
    assert await store.gc_expired_input_artifacts() == 1
    row = await store.pool.fetchrow(
        "SELECT state,content FROM kotoyose.execution_input_artifacts WHERE ref=$1", ref,
    )
    assert row["state"] == "tombstone" and row["content"] is None
    if raw is not None:
        blob = ArtifactBlob.from_content(ref, PNG, "image/png")
        with pytest.raises(ArtifactMissingError):
            await raw.read(blob)
    with pytest.raises(ExecutionError) as exc:
        await store.artifact("caller-a", ref)
    assert exc.value.status == 410 and exc.value.code == "artifact_expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_runtime", ["local"], indirect=True)
async def test_missing_storage_root_is_not_proof_that_the_artifact_was_deleted(backend_runtime):
    _, store, raw, limits = backend_runtime
    result = await store.put_input_artifact("caller-a", _upload(), limits)
    ref = result["ref"]
    moved = raw.root.with_name("offline-artifact-root")
    raw.root.rename(moved)
    try:
        with pytest.raises(ArtifactStorageError) as error:
            await raw.read(ArtifactBlob.from_content(ref, PNG, "image/png"))
        assert not isinstance(error.value, ArtifactMissingError)
        await store.pool.execute("UPDATE kotoyose.execution_input_artifacts SET expires_at=now()-interval '1 second' WHERE ref=$1", ref)
        assert await store.gc_expired_input_artifacts() == 0
        assert await store.reconcile_input_artifacts() == 0
        assert await store.pool.fetchval("SELECT state FROM kotoyose.execution_input_artifacts WHERE ref=$1", ref) == "deleting"
    finally:
        moved.rename(raw.root)
    assert await store.reconcile_input_artifacts() == 1
    assert await store.pool.fetchval("SELECT state FROM kotoyose.execution_input_artifacts WHERE ref=$1", ref) == "tombstone"


@pytest.mark.asyncio
async def test_retention_and_unresolved_attempt_pins_block_gc(backend_runtime):
    _, store, _, limits = backend_runtime
    pinned = await store.put_input_artifact("caller-a", _upload(pin=True), limits)
    unpinned = await store.put_input_artifact("caller-a", _upload(PNG + b"-attempt"), limits)
    await store.pool.execute(
        "UPDATE kotoyose.execution_input_artifacts SET expires_at=now()-interval '1 second' WHERE ref=ANY($1::text[])",
        [pinned["ref"], unpinned["ref"]],
    )
    workspace_id, attempt_id = str(uuid4()), str(uuid4())
    await store.pool.execute(
        "INSERT INTO kotoyose.execution_workspaces(id,principal,caller_ref,path,device,inode) "
        "VALUES($1,'caller-a','caller-workspace','/tmp',0,0)", workspace_id,
    )
    await store.pool.execute(
        "INSERT INTO kotoyose.execution_attempts(id,principal,idempotency_key,request_digest,request,snapshot,workspace_id,generation,status) "
        "VALUES($1,'caller-a',$2,'digest','{}'::jsonb,'{}'::jsonb,$3,0,'running')",
        attempt_id, str(uuid4()), workspace_id,
    )
    await store.pool.execute(
        "UPDATE kotoyose.execution_input_artifacts SET pin_count=1 WHERE ref=$1", unpinned["ref"],
    )
    await store.pool.execute(
        "INSERT INTO kotoyose.execution_attempt_input_artifacts(attempt_id,artifact_ref,principal) VALUES($1,$2,'caller-a')",
        attempt_id, unpinned["ref"],
    )
    assert await store.gc_expired_input_artifacts() == 0
    assert await store.pool.fetchval("SELECT state FROM kotoyose.execution_input_artifacts WHERE ref=$1", pinned["ref"]) == "active"
    assert await store.pool.fetchval("SELECT state FROM kotoyose.execution_input_artifacts WHERE ref=$1", unpinned["ref"]) == "active"


@pytest.mark.asyncio
async def test_unresolved_attempt_pin_keeps_expired_bytes_readable(backend_runtime):
    """未確認 Attempt の pin 中は upload TTL 後も内部 execute 読取を許可する。"""
    _, store, _, limits = backend_runtime
    result = await store.put_input_artifact("caller-a", _upload(), limits)
    workspace_id, attempt_id = str(uuid4()), str(uuid4())
    await store.pool.execute(
        "INSERT INTO kotoyose.execution_workspaces(id,principal,caller_ref,path,device,inode) "
        "VALUES($1,'caller-a','caller-workspace','/tmp',0,0)", workspace_id,
    )
    await store.pool.execute(
        "INSERT INTO kotoyose.execution_attempts(id,principal,idempotency_key,request_digest,request,snapshot,workspace_id,generation,status) "
        "VALUES($1,'caller-a',$2,'digest','{}'::jsonb,'{}'::jsonb,$3,0,'running')",
        attempt_id, str(uuid4()), workspace_id,
    )
    await store.pool.execute(
        "UPDATE kotoyose.execution_input_artifacts SET expires_at=now()-interval '1 second',pin_count=1 WHERE ref=$1",
        result["ref"],
    )
    await store.pool.execute(
        "INSERT INTO kotoyose.execution_attempt_input_artifacts(attempt_id,artifact_ref,principal) VALUES($1,$2,'caller-a')",
        attempt_id, result["ref"],
    )
    artifact = await store.artifact("caller-a", result["ref"], attempt_id=attempt_id)
    assert artifact["content"] == PNG
    with pytest.raises(ExecutionError) as exc:
        await store.artifact("caller-a", result["ref"])
    assert exc.value.status == 410 and exc.value.code == "artifact_expired"


@pytest.mark.asyncio
async def test_service_maintenance_loop_runs_and_cancels_bounded():
    """明示 interval の保守 task が一回実行され、close 相当の cancel で回収できる。"""
    from types import SimpleNamespace

    from kotoyose.execution_service import ExecutionService

    calls: list[tuple[str, int]] = []

    class FakeStore:
        async def reconcile_input_artifacts(self, *, limit: int) -> int:
            calls.append(("reconcile", limit))
            return 0

        async def gc_expired_input_artifacts(self, *, limit: int) -> int:
            calls.append(("gc", limit))
            return 0

    service = ExecutionService.__new__(ExecutionService)
    service.store = FakeStore()
    service.config = SimpleNamespace(
        input_artifact_limits=SimpleNamespace(gc_batch_size=2),
    )
    task = asyncio.create_task(service._input_artifact_maintenance(0))
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert calls[:2] == [("reconcile", 2), ("gc", 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["forbidden", "timeout"])
async def test_external_delete_failure_stays_deleting_and_reconcile_retries(backend_runtime, failure, monkeypatch):
    kind, store, raw, limits = backend_runtime
    if kind == "postgres":
        pytest.skip("external delete failures apply to LocalFS / MinIO")
    result = await store.put_input_artifact("caller-a", _upload(), limits)
    ref = result["ref"]
    await store.pool.execute(
        "UPDATE kotoyose.execution_input_artifacts SET expires_at=now()-interval '1 second' WHERE ref=$1", ref,
    )
    original_delete = raw.delete
    original_client_delete = raw.client.delete_object if isinstance(raw, S3ArtifactStore) else None

    if isinstance(raw, S3ArtifactStore):
        def fail_client_delete(**kwargs):
            if failure == "forbidden":
                raise ClientError({"Error": {"Code": "403", "Message": "forbidden"}}, "DeleteObject")
            raise ReadTimeoutError(endpoint_url=raw.config.endpoint)

        monkeypatch.setattr(raw.client, "delete_object", fail_client_delete)
    else:
        async def fail_delete(blob):
            raise ArtifactStorageError("provider unavailable")

        monkeypatch.setattr(raw, "delete", fail_delete)
    assert await store.gc_expired_input_artifacts() == 0
    assert await store.pool.fetchval("SELECT state FROM kotoyose.execution_input_artifacts WHERE ref=$1", ref) == "deleting"
    if isinstance(raw, S3ArtifactStore):
        monkeypatch.setattr(raw.client, "delete_object", original_client_delete)
    else:
        monkeypatch.setattr(raw, "delete", original_delete)
    assert await store.reconcile_input_artifacts() == 1
    assert await store.pool.fetchval("SELECT state FROM kotoyose.execution_input_artifacts WHERE ref=$1", ref) == "tombstone"


@pytest.mark.asyncio
async def test_delete_response_loss_is_reconciled_without_second_upload(backend_runtime, monkeypatch):
    """delete 後に応答だけ失われても、次回 reconcile は同じ key を再送せず収束する。"""
    kind, store, raw, limits = backend_runtime
    if kind == "postgres":
        pytest.skip("external delete response loss applies to LocalFS / MinIO")
    result = await store.put_input_artifact("caller-a", _upload(), limits)
    ref = result["ref"]
    await store.pool.execute(
        "UPDATE kotoyose.execution_input_artifacts SET expires_at=now()-interval '1 second' WHERE ref=$1", ref,
    )
    if isinstance(raw, S3ArtifactStore):
        original = raw.client.delete_object

        def delete_then_timeout(**kwargs):
            original(**kwargs)
            raise ReadTimeoutError(endpoint_url=raw.config.endpoint)

        monkeypatch.setattr(raw.client, "delete_object", delete_then_timeout)
    else:
        original = raw.delete

        async def delete_then_error(blob):
            await original(blob)
            raise ArtifactStorageError("delete response lost")

        monkeypatch.setattr(raw, "delete", delete_then_error)
    assert await store.gc_expired_input_artifacts() == 0
    assert await store.pool.fetchval("SELECT state FROM kotoyose.execution_input_artifacts WHERE ref=$1", ref) == "deleting"
    monkeypatch.undo()
    assert await store.reconcile_input_artifacts() == 1
    assert await store.pool.fetchval("SELECT state FROM kotoyose.execution_input_artifacts WHERE ref=$1", ref) == "tombstone"


@pytest.mark.asyncio
async def test_concurrent_gc_and_reconcile_have_one_terminal_tombstone(backend_runtime):
    _, store, _, limits = backend_runtime
    result = await store.put_input_artifact("caller-a", _upload(), limits)
    ref = result["ref"]
    await store.pool.execute(
        "UPDATE kotoyose.execution_input_artifacts SET expires_at=now()-interval '1 second' WHERE ref=$1", ref,
    )
    await __import__("asyncio").gather(
        store.gc_expired_input_artifacts(), store.reconcile_input_artifacts(),
    )
    assert await store.pool.fetchval("SELECT state FROM kotoyose.execution_input_artifacts WHERE ref=$1", ref) == "tombstone"
