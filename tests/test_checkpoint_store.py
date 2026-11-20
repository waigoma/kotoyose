"""Checkpoint の世代 / 所有 / 保存 / 復元 reservation を実 Postgres と bytes で検証する。"""

import asyncio
from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace

import httpx

import pytest

from kotoyose.artifact_blob_store import ArtifactBlobRegistry, ArtifactStorageError
from kotoyose.app import app
from kotoyose.checkpoint_contract import CheckpointRestore, CheckpointRelease
from kotoyose.checkpoint_restore import CheckpointRestorer
from kotoyose.execution_resources import ExecutionError
from kotoyose.execution_store import ExecutionStore
from kotoyose.local_artifact_store import LocalArtifactStore
from tests.test_checkpoint_archive import capture_request, entry
from tests.test_execution_agent import _config
from tests.test_execution_store import _admit_ready, store  # noqa: F401
from tests.test_artifact_retention import policy as retention_policy
from tests.fixtures.clients import clients


async def source(store, tmp_path, stop='confirmed'):
    workspace, _, request, snapshot = await _admit_ready(store, tmp_path)
    (Path(workspace['path']) / 'input.txt').write_bytes(b'checkpoint input')
    attempt, _ = await store.admit('caller-a', request, snapshot, 1)
    await store.finish(attempt['id'], 'succeeded', 'fixture', stop, 0, {})
    capture = capture_request([entry('input.txt', b'checkpoint input')]).model_copy(update={
        'source_attempt_id': attempt['id'], 'workspace_ref': f"workspace:{workspace['id']}:1",
    })
    return workspace, attempt, capture


def restoration(tmp_path, checkpoint):
    return CheckpointRestore(schema_version='1', request_id=uuid4(), caller_workspace_ref='restored-caller',
                             path=str(tmp_path / 'restored'), expected_digest=checkpoint['artifact']['digest'])


@pytest.mark.asyncio
async def test_checkpoint_saved_once_restores_new_workspace_and_preserves_original(tmp_path, store):
    workspace, attempt, capture = await source(store, tmp_path)
    first, second = await asyncio.gather(store.checkpoints.capture('caller-a', capture), store.checkpoints.capture('caller-a', capture))
    assert first == second
    assert await store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_checkpoints') == 1
    assert await store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_artifacts') == 1
    assert (await store.checkpoints.get('caller-a', first['checkpoint_ref']))['retention']['pins'] == ['caller-policy-pending']
    policy = clients(_config(tmp_path))['caller-a']
    request = restoration(tmp_path, first)
    restorer = CheckpointRestorer(store)
    result = await restorer.restore('caller-a', policy, first['checkpoint_ref'], request)
    assert result['workspace_generation'] == 0
    assert result['recovery_point']['later_changes'] == 'not_restored'
    assert result['source_workspace_ref'] == capture.workspace_ref
    assert (Path(request.path) / 'input.txt').read_bytes() == b'checkpoint input'
    assert (Path(workspace['path']) / 'input.txt').read_bytes() == b'checkpoint input'
    reopened = ExecutionStore(store.pool)
    assert await CheckpointRestorer(reopened).restore('caller-a', policy, first['checkpoint_ref'], request) == result
    restored_workspace = await store.workspace('caller-a', result['workspace_ref'])
    assert restored_workspace['writer_attempt'] is None and restored_workspace['id'] != workspace['id']
    for operation in [store.checkpoints.get('caller-b', first['checkpoint_ref']), store.checkpoints.capture('caller-b', capture),
                      restorer.restore('caller-b', policy, first['checkpoint_ref'], request)]:
        with pytest.raises(ExecutionError) as denied:
            await operation
        assert denied.value.status == 404
    with pytest.raises(ExecutionError):
        await store.checkpoints.capture('caller-a', capture.model_copy(update={'source_revision': 'changed'}))
    with pytest.raises(ExecutionError):
        await restorer.restore('caller-a', policy, first['checkpoint_ref'], request.model_copy(update={'path': str(tmp_path / 'other')}))


@pytest.mark.asyncio
async def test_unconfirmed_stop_stale_generation_and_unknown_operations_prevent_capture(tmp_path, store):
    workspace, attempt, capture = await source(store, tmp_path, stop='unconfirmed')
    with pytest.raises(ExecutionError):
        await store.checkpoints.capture('caller-a', capture)
    await store.confirm_stop(attempt['id'])
    with pytest.raises(ExecutionError):
        await store.checkpoints.capture('caller-a', capture.model_copy(update={'workspace_ref': f"workspace:{workspace['id']}:0"}))
    await store.pool.execute("INSERT INTO kotoyose.execution_operations(id,attempt_id,kind,state) VALUES($1,$2,'fixture','unknown')", str(uuid4()), attempt['id'])
    with pytest.raises(ExecutionError):
        await store.checkpoints.capture('caller-a', capture)
    assert await store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_checkpoints') == 0


@pytest.mark.asyncio
async def test_storage_failure_cannot_publish_checkpoint_and_local_backend_roundtrips(tmp_path, store, monkeypatch):
    storage = tmp_path / 'artifacts'
    storage.mkdir(mode=0o700)
    backend = LocalArtifactStore(storage)
    store.artifact_storage = ArtifactBlobRegistry('local', {'local': backend})
    _, _, capture = await source(store, tmp_path)
    original = backend.put
    async def unavailable(*args):
        raise ArtifactStorageError('unavailable')
    monkeypatch.setattr(backend, 'put', unavailable)
    with pytest.raises(ArtifactStorageError):
        await store.checkpoints.capture('caller-a', capture)
    assert await store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_checkpoints') == 0
    monkeypatch.setattr(backend, 'put', original)
    checkpoint = await store.checkpoints.capture('caller-a', capture)
    result = await CheckpointRestorer(store).restore('caller-a', clients(_config(tmp_path))['caller-a'], checkpoint['checkpoint_ref'], restoration(tmp_path, checkpoint))
    assert result['workspace_generation'] == 0


@pytest.mark.asyncio
async def test_existing_path_is_never_overwritten_and_incomplete_restore_is_reserved(tmp_path, store, monkeypatch):
    _, _, capture = await source(store, tmp_path)
    checkpoint = await store.checkpoints.capture('caller-a', capture)
    request = restoration(tmp_path, checkpoint)
    policy = clients(_config(tmp_path))['caller-a']
    restorer = CheckpointRestorer(store)
    Path(request.path).mkdir()
    with pytest.raises(ExecutionError):
        await restorer.restore('caller-a', policy, checkpoint['checkpoint_ref'], request)
    assert await store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_checkpoint_restores') == 0
    Path(request.path).rmdir()
    import kotoyose.checkpoint_restore as implementation
    original = implementation.write_checkpoint
    def interrupted(*args):
        original(*args)
        raise OSError('response lost after files were saved')
    monkeypatch.setattr(implementation, 'write_checkpoint', interrupted)
    with pytest.raises(ExecutionError):
        await restorer.restore('caller-a', policy, checkpoint['checkpoint_ref'], request)
    with pytest.raises(ExecutionError):
        await store.register_workspace('caller-a', 'bypass', Path(request.path))
    await store.artifact_retention.change('caller-a', checkpoint['artifact']['ref'], retention_policy())
    release = CheckpointRelease(schema_version='1', request_id=uuid4())
    released = await store.checkpoints.release('caller-a', checkpoint['checkpoint_ref'], release)
    assert await store.checkpoints.release('caller-a', checkpoint['checkpoint_ref'], release) == released
    assert await store.artifact_retention.gc() == 0
    monkeypatch.setattr(implementation, 'write_checkpoint', original)
    resumed = await CheckpointRestorer(ExecutionStore(store.pool)).restore('caller-a', policy, checkpoint['checkpoint_ref'], request)
    assert resumed['workspace_generation'] == 0
    assert await store.artifact_retention.gc() == 1
    tombstone = await store.checkpoints.get('caller-a', checkpoint['checkpoint_ref'])
    assert tombstone['retention']['state'] == 'tombstone'
    assert await restorer.restore('caller-a', policy, checkpoint['checkpoint_ref'], request) == resumed
    with pytest.raises(ExecutionError) as gone:
        await restorer.restore('caller-a', policy, checkpoint['checkpoint_ref'], request.model_copy(update={'request_id': uuid4(), 'path': str(tmp_path / 'another'), 'caller_workspace_ref': 'another'}))
    assert gone.value.status == 410


@pytest.mark.asyncio
async def test_checkpoint_reference_outlives_artifact_expiry_until_explicit_release(tmp_path, store):
    _, _, capture = await source(store, tmp_path)
    checkpoint = await store.checkpoints.capture('caller-a', capture)
    await store.artifact_retention.change('caller-a', checkpoint['artifact']['ref'], retention_policy())
    assert await store.artifact_retention.gc() == 0
    assert (await store.checkpoints.get('caller-a', checkpoint['checkpoint_ref']))['hold']['state'] == 'retained'
    request = CheckpointRelease(schema_version='1', request_id=uuid4())
    with pytest.raises(ExecutionError):
        await store.checkpoints.release('caller-b', checkpoint['checkpoint_ref'], request)
    await store.checkpoints.release('caller-a', checkpoint['checkpoint_ref'], request)
    assert await store.artifact_retention.gc() == 1
    with pytest.raises(ExecutionError):
        await store.checkpoints.release('caller-a', checkpoint['checkpoint_ref'], request.model_copy(update={'request_id': uuid4()}))


@pytest.mark.asyncio
async def test_restore_and_gc_race_either_restores_verified_bytes_or_refuses_before_registration(tmp_path, store):
    _, _, capture = await source(store, tmp_path)
    checkpoint = await store.checkpoints.capture('caller-a', capture)
    await store.artifact_retention.change('caller-a', checkpoint['artifact']['ref'], retention_policy())
    await store.checkpoints.release('caller-a', checkpoint['checkpoint_ref'], CheckpointRelease(schema_version='1', request_id=uuid4()))
    request = restoration(tmp_path, checkpoint)
    restored, deleted = await asyncio.gather(
        CheckpointRestorer(store).restore('caller-a', clients(_config(tmp_path))['caller-a'], checkpoint['checkpoint_ref'], request),
        store.artifact_retention.gc(), return_exceptions=True,
    )
    assert deleted in (0, 1)
    if isinstance(restored, ExecutionError):
        assert restored.status in (409, 410, 503)
        assert await store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_workspaces WHERE caller_ref=$1', request.caller_workspace_ref) == 0
    else:
        assert isinstance(restored, dict)
        assert (Path(request.path) / 'input.txt').read_bytes() == b'checkpoint input'


@pytest.mark.asyncio
async def test_http_auth_capture_replay_after_workspace_loss_and_fresh_restore(tmp_path, store, monkeypatch):
    workspace, _, capture = await source(store, tmp_path)
    config = _config(tmp_path)
    from tests.fixtures.api_keys import fixture_key_store

    monkeypatch.setattr(app.state, "api_key_store", await fixture_key_store(**{"caller-a": "checkpoint-test-token"}))
    previous = getattr(app.state, 'execution_service', None)
    app.state.execution_service = SimpleNamespace(config=config, store=store)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            body = capture.model_dump(mode='json')
            assert (await client.post('/v1/checkpoints', json=body)).status_code == 401
            headers = {'Authorization': 'Bearer kys_checkpoint-test-token'}
            response = await client.post('/v1/checkpoints', json=body, headers=headers)
            assert response.status_code == 201, response.text
            checkpoint = response.json()
            Path(workspace['path']).rename(tmp_path / 'lost-workspace')
            replay = await client.post('/v1/checkpoints', json=body, headers=headers)
            assert replay.status_code == 201 and replay.json() == checkpoint
            request = restoration(tmp_path, checkpoint)
            restored = await client.post(f"/v1/checkpoints/{checkpoint['checkpoint_ref']}/restores", json=request.model_dump(mode='json'), headers=headers)
            assert restored.status_code == 201, restored.text
            assert restored.json()['checkpoint_digest'] == checkpoint['artifact']['digest']
            assert (Path(request.path) / 'input.txt').read_bytes() == b'checkpoint input'
    finally:
        app.state.execution_service = previous
