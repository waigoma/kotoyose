"""削除対象の実体差替えと外部参照を使い、workspace 外を消さないことを確認する。"""

import pytest

from kotoyose.workspace_deletion import WorkspaceDeletionError, delete_owned_workspace


def test_only_owned_directory_is_removed_and_missing_replay_is_idempotent(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep').write_text('keep')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    (workspace / 'file').write_text('remove')
    (workspace / 'link').symlink_to(outside, target_is_directory=True)
    info = workspace.stat()
    delete_owned_workspace(workspace, info.st_dev, info.st_ino)
    delete_owned_workspace(workspace, info.st_dev, info.st_ino)
    assert not workspace.exists()
    assert (outside / 'keep').read_text() == 'keep'


def test_replaced_root_or_unsupported_platform_is_never_deleted(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    info = workspace.stat()
    workspace.rename(tmp_path / 'original')
    workspace.mkdir()
    (workspace / 'keep').write_text('new directory')
    with pytest.raises(WorkspaceDeletionError):
        delete_owned_workspace(workspace, info.st_dev, info.st_ino)
    assert (workspace / 'keep').exists()
    import shutil
    monkeypatch.setattr(shutil.rmtree, 'avoids_symlink_attacks', False)
    with pytest.raises(WorkspaceDeletionError):
        delete_owned_workspace(workspace, workspace.stat().st_dev, workspace.stat().st_ino)


def test_replaced_root_symlink_cannot_point_deletion_at_another_tree(tmp_path):
    original = tmp_path / 'original'
    original.mkdir()
    info = original.stat()
    link = tmp_path / 'workspace'
    link.symlink_to(original, target_is_directory=True)
    with pytest.raises(WorkspaceDeletionError):
        delete_owned_workspace(link, info.st_dev, info.st_ino)
    assert original.is_dir()
