"""保存 backend の切替が Artifact の認可・不変性・終端原子性を変えないことを検証する。"""

import asyncio
import os
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from kotoyose.artifact_blob_store import ArtifactBlob, ArtifactBlobRegistry, ArtifactStorageError
from kotoyose.artifact_storage_config import ArtifactStorageConfig, S3ArtifactStorageConfig
from kotoyose.artifact_storage_factory import create_artifact_storage
from kotoyose.execution_resources import ExecutionError
from kotoyose.execution_store import ExecutionStore
from kotoyose.local_artifact_store import LocalArtifactStore
from kotoyose.artifact_file import ArtifactFile
from kotoyose.execution_artifacts import CapturedArtifact
from tests.test_execution_store import _admit_ready, store  # noqa: F401


CONTENT = b"immutable evidence\x00\xff"
BLOB = ArtifactBlob.from_content("artifact:attempt:a:digest", CONTENT, "application/octet-stream")


async def test_streamed_failure_output_preserves_verdict_and_owner_bound_read(store, tmp_path):
    backend = LocalArtifactStore(tmp_path / 'streamed-artifacts')
    external = ExecutionStore(store.pool, ArtifactBlobRegistry('disk', {'disk': backend}))
    _, _, request, snapshot = await _admit_ready(external, tmp_path)
    attempt, _ = await external.admit('caller-a', request, snapshot, 1)
    path = tmp_path/'partial.bin'
    path.write_bytes(b'partial implementation remains')
    with path.open('rb') as stream:
        source = ArtifactFile.capture(stream.fileno(), 1024)
    try:
        await external.finish(attempt['id'], 'timeout', 'hard_timeout', 'confirmed', None,
                              {'partial': CapturedArtifact(source, 'application/octet-stream')})
    finally:
        source.close()
    old = await external.attempt('caller-a', attempt['id'])
    assert old['status'] == 'timeout' and old['reason'] == 'hard_timeout'
    assert old['stop_confirmation'] == 'confirmed'
    ref = old['artifacts'][0]['ref']
    metadata, chunks = await external.artifact_stream('caller-a', ref)
    assert metadata['size'] == path.stat().st_size
    assert b''.join([chunk async for chunk in chunks]) == path.read_bytes()
    assert await store.pool.fetchval('SELECT content IS NULL FROM kotoyose.execution_artifacts WHERE ref=$1', ref)
    with pytest.raises(ExecutionError) as denied:
        await external.artifact_stream('another-caller', ref)
    assert denied.value.status == 404
    assert path.is_file()


@pytest.mark.asyncio
async def test_local_parallel_creation_replay_and_reopen_preserve_bytes(tmp_path):
    backend = LocalArtifactStore(tmp_path / "artifacts")
    await asyncio.gather(*(backend.put(BLOB, CONTENT) for _ in range(12)))
    assert await LocalArtifactStore(tmp_path / "artifacts").read(BLOB) == CONTENT
    assert list((tmp_path / "artifacts").iterdir()) == [tmp_path / "artifacts" / BLOB.key]
    with pytest.raises(ArtifactStorageError):
        await backend.put(BLOB, b"replacement")
    assert await backend.read(BLOB) == CONTENT


@pytest.mark.asyncio
@pytest.mark.parametrize("attack", ["symlink", "fifo", "corruption", "oversize", "traversal"])
async def test_local_never_reads_a_redirected_or_corrupt_object(tmp_path, attack):
    backend = LocalArtifactStore(tmp_path / "artifacts")
    await backend.put(BLOB, CONTENT)
    file = tmp_path / "artifacts" / BLOB.key
    file.unlink()
    blob = BLOB
    if attack == "symlink":
        external = tmp_path / "external"
        external.write_bytes(CONTENT)
        file.symlink_to(external)
    elif attack == "fifo":
        os.mkfifo(file)
    elif attack == "traversal":
        blob = replace(BLOB, key="../external")
    else:
        file.write_bytes(b"x" * (len(CONTENT) + (attack == "oversize")))
    with pytest.raises(ArtifactStorageError):
        await asyncio.wait_for(backend.read(blob), timeout=2)


def test_local_root_cannot_overlap_a_worker_workspace_or_be_public(tmp_path):
    config = ArtifactStorageConfig.model_validate({"default_backend": "disk", "backends": {
        "disk": {"kind": "local_fs", "root": str(tmp_path / "workspace" / "artifacts")},
    }})
    with pytest.raises(ValueError, match="overlap"):
        create_artifact_storage(config, [tmp_path / "workspace"])
    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private"):
        LocalArtifactStore(public)


@pytest.mark.parametrize("endpoint", ["http://s3.example", "https://user:pass@s3.example", "https://s3.example/path", "https://s3.example?token=x"])
def test_s3_config_rejects_ambiguous_or_insecure_origins(endpoint):
    with pytest.raises(ValidationError):
        S3ArtifactStorageConfig(kind="s3", endpoint=endpoint, bucket="artifacts",
                                access_key_env="TEST_ACCESS", secret_key_env="TEST_SECRET")


@pytest.mark.asyncio
async def test_external_finish_keeps_owner_metadata_and_historical_postgres_readable(store, tmp_path):
    _, _, request, snapshot = await _admit_ready(store, tmp_path)
    attempt, _ = await store.admit("caller-a", request, snapshot, 1)
    await store.finish(attempt["id"], "succeeded", "ok", "confirmed", 0, {"stdout": b"inline"})
    inline_ref = (await store.attempt("caller-a", attempt["id"]))["artifacts"][0]["ref"]
    backend = LocalArtifactStore(tmp_path / "artifacts")
    external = ExecutionStore(store.pool, ArtifactBlobRegistry("disk", {"disk": backend}))
    _, _, request, snapshot = await _admit_ready(external, tmp_path, principal="caller-b", key="external")
    attempt, _ = await external.admit("caller-b", request, snapshot, 1)
    await external.finish(attempt["id"], "succeeded", "ok", "confirmed", 0, {"stdout": CONTENT})
    result = await external.attempt("caller-b", attempt["id"])
    ref = result["artifacts"][0]["ref"]
    assert result["artifacts"][0]["size"] == len(CONTENT)
    assert (await external.artifact("caller-a", inline_ref))["content"] == b"inline"
    assert (await external.artifact("caller-b", ref))["content"] == CONTENT
    assert not any(key.startswith("storage_") for key in await external.artifact("caller-b", ref))
    assert await store.pool.fetchval("SELECT content IS NULL FROM kotoyose.execution_artifacts WHERE ref=$1", ref)
    with pytest.raises(ExecutionError) as denied:
        await external.artifact("caller-a", ref)
    assert denied.value.status == 404
    changed = ExecutionStore(store.pool, ArtifactBlobRegistry("disk", {
        "disk": LocalArtifactStore(tmp_path / "different-artifacts"),
    }))
    with pytest.raises(ExecutionError) as unavailable:
        await changed.artifact("caller-b", ref)
    assert unavailable.value.code == "artifact_storage_error"
    with pytest.raises(ExecutionError):
        await ExecutionStore(store.pool).artifact("caller-b", ref)


@pytest.mark.asyncio
@pytest.mark.parametrize('status,reason,stop', [
    ('succeeded', 'ok', 'confirmed'), ('timeout', 'hard_timeout', 'confirmed'),
    ('timeout', 'hard_timeout', 'unconfirmed'),
])
async def test_failed_external_save_preserves_stop_without_publishing_success(store, tmp_path, status, reason, stop):
    class FailedStore:
        identity = "fixture"

        async def put(self, blob, content):
            raise ArtifactStorageError("storage unavailable")

        async def read(self, blob):
            raise AssertionError("not saved")

    external = ExecutionStore(store.pool, ArtifactBlobRegistry("unavailable", {"unavailable": FailedStore()}))
    workspace, _, request, snapshot = await _admit_ready(external, tmp_path)
    attempt, _ = await external.admit("caller-a", request, snapshot, 1)
    await external.finish(attempt["id"], status, reason, stop, 0, {"stdout": CONTENT})
    result = await external.attempt("caller-a", attempt["id"])
    assert result['status'] == ('failed' if status == 'succeeded' else status)
    assert result['reason'] == ('artifact_storage_error' if status == 'succeeded' else reason)
    assert result['stop_confirmation'] == stop
    assert result['side_effect_resolution'] == ('resolved' if stop == 'confirmed' else 'unresolved')
    assert result["finished_at"] is not None and result["artifacts"] == []
    writer = await store.pool.fetchval("SELECT writer_attempt FROM kotoyose.execution_workspaces WHERE id=$1", workspace["id"])
    assert writer == (None if stop == 'confirmed' else attempt['id'])
    assert not await store.pool.fetchval("SELECT EXISTS(SELECT 1 FROM kotoyose.execution_events WHERE attempt_id=$1 AND type='succeeded')", attempt["id"])
    event = (await external.events('caller-a', attempt['id'], 0, 100))[-1]
    assert event['payload']['execution_outcome'] == {'status': status, 'reason': reason, 'exit_code': 0}


@pytest.mark.asyncio
async def test_uploaded_object_is_reused_after_journal_rollback(store, tmp_path):
    backend = LocalArtifactStore(tmp_path / "artifacts")
    external = ExecutionStore(store.pool, ArtifactBlobRegistry("disk", {"disk": backend}))
    _, _, request, snapshot = await _admit_ready(external, tmp_path)
    attempt, _ = await external.admit("caller-a", request, snapshot, 1)
    # Artifact insert 後に終端書込みが失敗する実 DB rollback を作る。
    await store.pool.execute("ALTER TABLE kotoyose.execution_attempts ADD CONSTRAINT reject_success CHECK(status <> 'succeeded')")
    with pytest.raises(Exception, match="reject_success"):
        await external.finish(attempt["id"], "succeeded", "ok", "confirmed", 0, {"stdout": CONTENT})
    assert (await external.attempt("caller-a", attempt["id"]))["finished_at"] is None
    assert len(list((tmp_path / "artifacts").iterdir())) == 1
    await store.pool.execute("ALTER TABLE kotoyose.execution_attempts DROP CONSTRAINT reject_success")
    await external.finish(attempt["id"], "succeeded", "ok", "confirmed", 0, {"stdout": CONTENT})
    await external.finish(attempt["id"], "failed", "replayed", "confirmed", 1, {"stdout": b"changed"})
    result = await external.attempt("caller-a", attempt["id"])
    assert result["status"] == "succeeded" and len(result["artifacts"]) == 1
    assert (await external.artifact("caller-a", result["artifacts"][0]["ref"]))["content"] == CONTENT
