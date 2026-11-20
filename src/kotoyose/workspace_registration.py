"""登録済みの論理 identity と、現存する filesystem の別名を区別する。"""

import asyncio
from pathlib import Path

from .execution_resources import ExecutionError


async def reject_workspace_overlap(conn, path: Path, info) -> None:
    """予約済み path と現存する同一実体を拒否し、削除後の inode 再利用は別登録にする。"""
    overlaps = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM kotoyose.execution_workspaces WHERE path=$1 "
        "OR starts_with(path,$1||'/') OR starts_with($1,path||'/'))", str(path),
    )
    if overlaps:
        raise ExecutionError("request_conflict", "Workspace overlaps an existing registration", 409)
    aliases = await conn.fetch(
        "SELECT id,path,lifecycle_state,writer_attempt FROM kotoyose.execution_workspaces "
        "WHERE device=$1 AND inode=$2 AND lifecycle_state<>'tombstone'",
        info.st_dev, info.st_ino,
    )
    for prior in aliases:
        if prior["lifecycle_state"] == "deleting" or prior["writer_attempt"]:
            raise ExecutionError("request_conflict", "Workspace physical identity is still reserved", 409)
        try:
            current = await asyncio.to_thread(Path(prior["path"]).stat)
        except FileNotFoundError:
            # inode は削除後に再利用される。旧 path/ref の予約と世代はそのまま残す。
            continue
        except OSError as error:
            raise ExecutionError("request_conflict", "Workspace alias could not be inspected", 409) from error
        if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
            raise ExecutionError("request_conflict", "Workspace aliases an existing registration", 409)
