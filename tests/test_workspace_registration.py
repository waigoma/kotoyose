"""inode 再利用、実体の別名、旧 path/ref、削除中 fence を実 DB で検証する。"""

from pathlib import Path

import pytest

from kotoyose.execution_resources import ExecutionError
from tests.test_execution_store import store  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['active', 'tombstone'])
async def test_recycled_inode_at_different_path_does_not_resurrect_old_registration(store, tmp_path, state):
    original = tmp_path / 'original'
    original.mkdir()
    old = await store.register_workspace('caller-a', 'old', original)
    original.rmdir()
    fresh = tmp_path / 'fresh'
    fresh.mkdir()
    info = fresh.stat()
    # 再利用される inode の値だけを決定的に再現し、OS の allocator 順序には依存しない。
    await store.pool.execute(
        'UPDATE kotoyose.execution_workspaces SET device=$2,inode=$3,lifecycle_state=$4 WHERE id=$1',
        old['id'], info.st_dev, info.st_ino, state,
    )
    new = await store.register_workspace('caller-a', 'new', fresh)
    assert new['id'] != old['id'] and new['generation'] == 0
    prior = await store.pool.fetchrow('SELECT * FROM kotoyose.execution_workspaces WHERE id=$1', old['id'])
    assert prior['path'] == str(original) and prior['generation'] == 0
    with pytest.raises(ExecutionError):
        await store.register_workspace('caller-a', 'old', fresh)
    original.mkdir()
    with pytest.raises(ExecutionError):
        await store.register_workspace('caller-a', 'different-ref', original)


@pytest.mark.asyncio
async def test_live_alias_and_parent_child_paths_remain_exclusive(store, tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    await store.register_workspace('caller-a', 'original', root)
    alias = tmp_path / 'alias'
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ExecutionError, match='aliases'):
        await store.register_workspace('caller-b', 'alias', alias)
    child = root / 'child'
    child.mkdir()
    with pytest.raises(ExecutionError, match='overlaps'):
        await store.register_workspace('caller-b', 'child', child)
    with pytest.raises(ExecutionError, match='overlaps'):
        await store.register_workspace('caller-b', 'parent', tmp_path)


@pytest.mark.asyncio
async def test_deleting_or_active_writer_keeps_physical_identity_fenced(store, tmp_path):
    prior = tmp_path / 'prior'
    prior.mkdir()
    row = await store.register_workspace('caller-a', 'prior', prior)
    prior.rmdir()
    fresh = tmp_path / 'fresh'
    fresh.mkdir()
    info = fresh.stat()
    await store.pool.execute(
        "UPDATE kotoyose.execution_workspaces SET device=$2,inode=$3,lifecycle_state='deleting' WHERE id=$1",
        row['id'], info.st_dev, info.st_ino,
    )
    with pytest.raises(ExecutionError, match='reserved'):
        await store.register_workspace('caller-a', 'new', fresh)
    await store.pool.execute(
        "UPDATE kotoyose.execution_workspaces SET lifecycle_state='active',writer_attempt='writer:unknown' WHERE id=$1", row['id'],
    )
    with pytest.raises(ExecutionError, match='reserved'):
        await store.register_workspace('caller-a', 'new', fresh)


@pytest.mark.asyncio
async def test_unreadable_old_path_cannot_be_assumed_gone(store, tmp_path, monkeypatch):
    prior = tmp_path / 'prior'
    prior.mkdir()
    row = await store.register_workspace('caller-a', 'prior', prior)
    fresh = tmp_path / 'fresh'
    fresh.mkdir()
    info = fresh.stat()
    await store.pool.execute('UPDATE kotoyose.execution_workspaces SET device=$2,inode=$3 WHERE id=$1', row['id'], info.st_dev, info.st_ino)
    original = Path.stat
    def denied(path, *args, **kwargs):
        if path == prior:
            raise PermissionError('fixture unavailable')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', denied)
    with pytest.raises(ExecutionError, match='inspected'):
        await store.register_workspace('caller-a', 'new', fresh)
