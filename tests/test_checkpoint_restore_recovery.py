"""未完了復元の照会・破棄を、実体差替え・取消競合・GC 保留とともに検証する。"""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from kotoyose.checkpoint_contract import CheckpointAbandon, CheckpointRelease
from kotoyose.checkpoint_restore import CheckpointRestorer
from kotoyose.checkpoint_restore_recovery import CheckpointRestoreRecovery
from kotoyose.execution_resources import ExecutionError
from tests.test_artifact_retention import policy as retention_policy
from tests.test_checkpoint_store import source, restoration
from tests.test_execution_agent import _config
from tests.test_execution_store import store  # noqa: F401
from tests.fixtures.clients import clients, set_client


async def incomplete(store, tmp_path, monkeypatch):
    _, _, capture = await source(store, tmp_path)
    checkpoint = await store.checkpoints.capture('caller-a', capture)
    request = restoration(tmp_path, checkpoint)
    policy = clients(_config(tmp_path))['caller-a']
    import kotoyose.checkpoint_restore as implementation
    def interrupted(path, *_args):
        (path / 'partial.txt').write_text('incomplete')
        raise OSError('interrupted while writing')
    with monkeypatch.context() as patch:
        patch.setattr(implementation, 'write_checkpoint', interrupted)
        with pytest.raises(ExecutionError):
            await CheckpointRestorer(store).restore('caller-a', policy, checkpoint['checkpoint_ref'], request)
    return checkpoint, request, policy


def abandonment():
    return CheckpointAbandon(schema_version='1', request_id=uuid4(), proof_ref='human:discard-partial-restore')


@pytest.mark.asyncio
async def test_partial_restore_is_inspected_and_abandoned_once_without_resurrecting_path(store, tmp_path, monkeypatch):
    checkpoint, request, policy = await incomplete(store, tmp_path, monkeypatch)
    recovery = CheckpointRestoreRecovery(store)
    view = await recovery.get('caller-a', policy, checkpoint['checkpoint_ref'], request.request_id)
    assert view['state'] == 'prepared' and view['directory_state'] == 'owned'
    assert view['result'] is None and view['restore_request_id'] == str(request.request_id)
    assert view['path'] == request.path
    action = abandonment()
    first, second = await asyncio.gather(*[
        recovery.abandon('caller-a', policy, checkpoint['checkpoint_ref'], request.request_id, action)
        for _ in range(2)
    ])
    assert first == second and first['state'] == 'abandoned'
    assert first['proof_ref'] == action.proof_ref and not Path(request.path).exists()
    assert await recovery.abandon('caller-a', policy, checkpoint['checkpoint_ref'], request.request_id, action) == first
    with pytest.raises(ExecutionError):
        await recovery.abandon('caller-a', policy, checkpoint['checkpoint_ref'], request.request_id, abandonment())
    with pytest.raises(ExecutionError):
        await CheckpointRestorer(store).restore('caller-a', policy, checkpoint['checkpoint_ref'], request)
    Path(request.path).mkdir()
    with pytest.raises(ExecutionError):
        await store.register_workspace('caller-a', 'cannot-resurrect', Path(request.path))


@pytest.mark.asyncio
async def test_delete_response_loss_keeps_checkpoint_pin_until_same_intent_reconciles(store, tmp_path, monkeypatch):
    checkpoint, request, policy = await incomplete(store, tmp_path, monkeypatch)
    recovery = CheckpointRestoreRecovery(store)
    action = abandonment()
    await store.artifact_retention.change('caller-a', checkpoint['artifact']['ref'], retention_policy())
    await store.checkpoints.release('caller-a', checkpoint['checkpoint_ref'], CheckpointRelease(schema_version='1', request_id=uuid4()))
    import kotoyose.checkpoint_restore_recovery as implementation
    original = implementation.delete_owned_workspace
    def lost(*args):
        original(*args)
        raise OSError('lost deletion receipt')
    with monkeypatch.context() as patch:
        patch.setattr(implementation, 'delete_owned_workspace', lost)
        with pytest.raises(ExecutionError):
            await recovery.abandon('caller-a', policy, checkpoint['checkpoint_ref'], request.request_id, action)
    view = await recovery.get('caller-a', policy, checkpoint['checkpoint_ref'], request.request_id)
    assert view['state'] == 'abandoning' and view['directory_state'] == 'absent'
    assert await store.artifact_retention.gc() == 0
    await recovery.abandon('caller-a', policy, checkpoint['checkpoint_ref'], request.request_id, action)
    assert await store.artifact_retention.gc() == 1


@pytest.mark.asyncio
async def test_unknown_mkdir_identity_never_deletes_unowned_directory(store, tmp_path, monkeypatch):
    checkpoint, request, policy = await incomplete(store, tmp_path, monkeypatch)
    await store.pool.execute('UPDATE kotoyose.execution_checkpoint_restores SET device=NULL,inode=NULL')
    recovery = CheckpointRestoreRecovery(store)
    action = abandonment()
    with pytest.raises(ExecutionError):
        await recovery.abandon('caller-a', policy, checkpoint['checkpoint_ref'], request.request_id, action)
    assert (Path(request.path) / 'partial.txt').read_text() == 'incomplete'
    # 外部で実体を確認して除去した後だけ、同じ取消 intent を不在として確定できる。
    (Path(request.path) / 'partial.txt').unlink()
    Path(request.path).rmdir()
    result = await recovery.abandon('caller-a', policy, checkpoint['checkpoint_ref'], request.request_id, action)
    assert result['state'] == 'abandoned'


@pytest.mark.asyncio
async def test_replaced_path_and_revoked_root_are_not_deleted(store, tmp_path, monkeypatch):
    checkpoint, request, policy = await incomplete(store, tmp_path, monkeypatch)
    recovery = CheckpointRestoreRecovery(store)
    path = Path(request.path)
    path.rename(tmp_path / 'original-partial')
    path.mkdir()
    (path / 'keep.txt').write_text('foreign')
    action = abandonment()
    with pytest.raises(ExecutionError):
        await recovery.abandon('caller-a', policy, checkpoint['checkpoint_ref'], request.request_id, action)
    assert (path / 'keep.txt').read_text() == 'foreign'
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    restricted = policy.model_copy(update={'workspace_roots': [elsewhere]})
    with pytest.raises(ExecutionError):
        await recovery.abandon('caller-a', restricted, checkpoint['checkpoint_ref'], request.request_id, action)
    with pytest.raises(ExecutionError):
        await recovery.get('caller-a', restricted, checkpoint['checkpoint_ref'], request.request_id)


@pytest.mark.asyncio
async def test_other_caller_or_checkpoint_is_hidden_and_confirmed_restore_is_not_abandoned(store, tmp_path):
    _, _, capture = await source(store, tmp_path)
    checkpoint = await store.checkpoints.capture('caller-a', capture)
    request = restoration(tmp_path, checkpoint)
    policy = clients(_config(tmp_path))['caller-a']
    result = await CheckpointRestorer(store).restore('caller-a', policy, checkpoint['checkpoint_ref'], request)
    recovery = CheckpointRestoreRecovery(store)
    for principal, ref in [('caller-b', checkpoint['checkpoint_ref']), ('caller-a', f'checkpoint:{uuid4()}')]:
        with pytest.raises(ExecutionError) as denied:
            await recovery.get(principal, policy, ref, request.request_id)
        assert denied.value.status == 404
        with pytest.raises(ExecutionError) as denied:
            await recovery.abandon(principal, policy, ref, request.request_id, abandonment())
        assert denied.value.status == 404
    view = await recovery.get('caller-a', policy, checkpoint['checkpoint_ref'], request.request_id)
    assert view['state'] == 'confirmed' and view['result'] == result
    with pytest.raises(ExecutionError):
        await recovery.abandon('caller-a', policy, checkpoint['checkpoint_ref'], request.request_id, abandonment())
    assert (Path(request.path) / 'input.txt').exists()


@pytest.mark.asyncio
async def test_resume_and_abandon_share_one_fence_and_never_delete_registered_workspace(store, tmp_path, monkeypatch):
    _, _, capture = await source(store, tmp_path)
    checkpoint = await store.checkpoints.capture('caller-a', capture)
    request = restoration(tmp_path, checkpoint)
    policy = clients(_config(tmp_path))['caller-a']
    import kotoyose.checkpoint_restore as implementation
    original = implementation.write_checkpoint
    def lost(*args):
        original(*args)
        raise OSError('lost completion')
    with monkeypatch.context() as patch:
        patch.setattr(implementation, 'write_checkpoint', lost)
        with pytest.raises(ExecutionError):
            await CheckpointRestorer(store).restore('caller-a', policy, checkpoint['checkpoint_ref'], request)
    resumed, abandoned = await asyncio.gather(
        CheckpointRestorer(store).restore('caller-a', policy, checkpoint['checkpoint_ref'], request),
        CheckpointRestoreRecovery(store).abandon('caller-a', policy, checkpoint['checkpoint_ref'], request.request_id, abandonment()),
        return_exceptions=True,
    )
    if isinstance(resumed, dict):
        assert isinstance(abandoned, ExecutionError)
        assert (Path(request.path) / 'input.txt').read_bytes() == b'checkpoint input'
        assert await store.workspace('caller-a', resumed['workspace_ref'])
    else:
        assert isinstance(resumed, ExecutionError) and isinstance(abandoned, dict)
        assert not Path(request.path).exists()
        assert await store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_workspaces WHERE caller_ref=$1', request.caller_workspace_ref) == 0
