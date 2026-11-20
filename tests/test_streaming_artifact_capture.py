"""ディスク保存の境界、破損、容量不足、再送をモデルなしで確認する。"""
import hashlib
import os
import tracemalloc
import asyncio
import threading

import pytest

from kotoyose.artifact_blob_store import ArtifactBlob, ArtifactStorageError
from kotoyose.artifact_file import ArtifactFile, ArtifactFileError, CHUNK_BYTES
from kotoyose.execution_artifacts import ArtifactCaptureSpec, collect_execution_artifacts, read_artifact_file
from kotoyose.local_artifact_store import LocalArtifactStore


def capture(workspace, limit, name='result.bin'):
    info = workspace.stat()
    spec = ArtifactCaptureSpec(schema_version='1', kind='artifact_spec', max_total_bytes=limit,
        entries=[{'name': 'result', 'path': name, 'media_type': 'application/octet-stream',
                  'required': True, 'max_bytes': limit}])
    return collect_execution_artifacts(workspace, info.st_dev, info.st_ino, spec, reader=read_artifact_file)


def fixture_file(tmp_path, size):
    workspace = tmp_path/'workspace'
    workspace.mkdir()
    with (workspace/'result.bin').open('wb') as stream:
        chunk = b'x'*CHUNK_BYTES
        while size:
            n = min(size, len(chunk))
            stream.write(chunk[:n])
            size -= n
    return workspace


async def test_large_capture_save_and_read_are_bounded_in_memory(tmp_path):
    size = 40*CHUNK_BYTES
    workspace = fixture_file(tmp_path, size)
    store = LocalArtifactStore(tmp_path/'private-artifacts')
    tracemalloc.start()
    result = capture(workspace, size)
    source = result.artifacts['result'].content
    assert isinstance(source, ArtifactFile) and result.contract_valid
    blob = ArtifactBlob('a'*64, source.digest, size, 'application/octet-stream')
    try:
        await store.put_file(blob, source, 0)
        count, digest = 0, hashlib.sha256()
        async for chunk in store.stream(blob):
            assert len(chunk) <= CHUNK_BYTES
            count += len(chunk)
            digest.update(chunk)
        assert count == size and digest.hexdigest() == blob.digest
        assert tracemalloc.get_traced_memory()[1] < 8*CHUNK_BYTES
    finally:
        source.close()
        tracemalloc.stop()
    assert (workspace/'result.bin').stat().st_size == size


async def test_size_boundary_and_no_partial_blob_on_source_change(tmp_path):
    workspace = fixture_file(tmp_path, 1024)
    assert not capture(workspace, 1023).contract_valid
    source = capture(workspace, 1024).artifacts['result'].content
    store = LocalArtifactStore(tmp_path/'artifacts')
    blob = ArtifactBlob('b'*64, source.digest, source.size, 'application/octet-stream')
    (workspace/'result.bin').write_bytes(b'y'*1024)
    try:
        with pytest.raises((ArtifactFileError, ArtifactStorageError)):
            await store.put_file(blob, source, 0)
    finally:
        source.close()
    assert list(store.root.iterdir()) == []
    assert (workspace/'result.bin').read_bytes() == b'y'*1024


async def test_capacity_failure_keeps_original_and_cleans_upload(tmp_path, monkeypatch):
    workspace = fixture_file(tmp_path, 1024)
    source = capture(workspace, 1024).artifacts['result'].content
    store = LocalArtifactStore(tmp_path/'artifacts')
    blob = ArtifactBlob('c'*64, source.digest, source.size, 'application/octet-stream')
    monkeypatch.setattr(store, 'check_capacity', lambda *args: (_ for _ in ()).throw(ArtifactStorageError('full')))
    try:
        with pytest.raises(ArtifactStorageError, match='full'):
            await store.put_file(blob, source, 0)
    finally:
        source.close()
    assert not list(store.root.iterdir()) and (workspace/'result.bin').is_file()


async def test_stream_retry_is_immutable_and_corruption_is_not_successful_eof(tmp_path):
    workspace = fixture_file(tmp_path, 4096)
    source = capture(workspace, 4096).artifacts['result'].content
    store = LocalArtifactStore(tmp_path/'artifacts')
    blob = ArtifactBlob('d'*64, source.digest, source.size, 'application/octet-stream')
    try:
        await store.put_file(blob, source, 0)
        await store.put_file(blob, source, 0)
    finally:
        source.close()
    assert len(list(store.root.iterdir())) == 1
    (store.root/blob.key).write_bytes(b'z'*4096)
    delivered = 0
    with pytest.raises(ArtifactStorageError, match='digest'):
        async for chunk in store.stream(blob):
            delivered += len(chunk)
    assert delivered < blob.size


def test_symlink_and_hardlink_never_become_capture_inputs(tmp_path):
    workspace = fixture_file(tmp_path, 100)
    os.symlink(workspace/'result.bin', workspace/'alias')
    assert not capture(workspace, 100, 'alias').contract_valid
    os.link(workspace/'result.bin', workspace/'linked')
    assert not capture(workspace, 100, 'linked').contract_valid


async def test_cancelled_upload_joins_its_thread_and_keeps_original(tmp_path):
    workspace = fixture_file(tmp_path, 2*CHUNK_BYTES)
    source = capture(workspace, 2*CHUNK_BYTES).artifacts['result'].content
    backend = LocalArtifactStore(tmp_path/'artifacts')
    blob = ArtifactBlob('e'*64, source.digest, source.size, 'application/octet-stream')
    entered, release = threading.Event(), threading.Event()
    original = source.chunks
    def delayed():
        for chunk in original():
            entered.set()
            assert release.wait(2)
            yield chunk
    source.chunks = delayed
    task = asyncio.create_task(backend.put_file(blob, source, 0))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not list(backend.root.iterdir())
        assert (workspace/'result.bin').stat().st_size == 2*CHUNK_BYTES
    finally:
        release.set()
        source.close()
