"""削除台帳で排他済みの workspace だけを、filesystem identity を照合して削除する。"""

import os
from pathlib import Path
import shutil
import stat


class WorkspaceDeletionError(ValueError):
    """別ディレクトリへの差替えや未対応 platform を成功扱いしない。"""


def delete_owned_workspace(path: Path, device: int, inode: int) -> None:
    """呼出側の deleting fence を前提とし、symlink を辿らず全削除または確定した不在を返す。"""
    if not shutil.rmtree.avoids_symlink_attacks:
        raise WorkspaceDeletionError('Workspace deletion requires fd-safe rmtree support')
    if not path.is_absolute() or path.name in {'', '.', '..'}:
        raise WorkspaceDeletionError('Workspace deletion requires a specific absolute directory')
    try:
        parent = path.parent.resolve(strict=True)
    except FileNotFoundError:
        return
    if parent != path.parent:
        raise WorkspaceDeletionError('Workspace parent identity changed')
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        try:
            info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (device, inode):
            raise WorkspaceDeletionError('Workspace directory identity changed')
        shutil.rmtree(path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
