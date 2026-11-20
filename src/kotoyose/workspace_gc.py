"""現在の管理設定、停止確認、回復点の bytes を確かめて workspace を削除する。"""

import asyncio
import json
from pathlib import Path

import structlog

from .checkpoint_archive import verify_checkpoint
from .checkpoint_contract import CHECKPOINT_RETENTION_LOCK
from .execution_artifacts import ArtifactCaptureError
from .execution_resources import CallerPolicy, ExecutionError
from .workspace_deletion import WorkspaceDeletionError, delete_owned_workspace
from .workspace_retention_store import WorkspaceRetentionRepository

log = structlog.get_logger()


class WorkspaceCollector:
    """削除 intent を先に確定し、別の writer や新しい登録が同じ実体を使えないようにする。"""

    def __init__(self, repository: WorkspaceRetentionRepository, callers: dict[str, CallerPolicy]):
        self.repository, self.callers = repository, callers
        self.pool = repository.pool

    def _allowed_path(self, row: dict) -> Path:
        policy = self.callers.get(row['principal'])
        if policy is None:
            raise WorkspaceDeletionError('Workspace caller is no longer configured')
        path = Path(row['path'])
        roots = [root.resolve(strict=True) for root in policy.workspace_roots]
        if not path.is_absolute() or not any(path != root and path.is_relative_to(root) for root in roots):
            raise WorkspaceDeletionError('Workspace is outside current caller roots')
        return path

    async def _verify_recovery(self, conn, row: dict) -> None:
        recovery = json.loads(row['recovery'])
        if recovery['mode'] == 'checkpoint':
            checkpoint = await self.repository.recovery_checkpoint(conn, row, recovery['checkpoint_ref'])
            saved = await self.repository.store.artifact(row['principal'], checkpoint['artifact_ref'])
            verify_checkpoint(saved['content'], json.loads(checkpoint['result'])['artifact']['digest'])

    async def gc(self, *, limit: int = 20) -> int:
        """確認不能な対象も検査時刻を進め、後方の削除可能な workspace を飢餓させない。"""
        self._validate_limit(limit)
        candidates = await self.pool.fetch(
            "SELECT w.id,w.principal,w.generation FROM kotoyose.execution_workspaces w "
            "JOIN kotoyose.execution_workspace_retention r ON r.workspace_id=w.id "
            "WHERE w.lifecycle_state='active' AND r.expires_at<=now() AND r.pins='[]'::jsonb "
            'ORDER BY r.last_checked_at,w.id LIMIT $1', limit)
        deleted = 0
        for candidate in candidates:
            claimed = None
            async with self.pool.acquire() as conn, conn.transaction():
                await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', CHECKPOINT_RETENTION_LOCK)
                row = await self.repository.row(conn, candidate['principal'], f"workspace:{candidate['id']}:{candidate['generation']}")
                await conn.execute('UPDATE kotoyose.execution_workspace_retention SET last_checked_at=now() WHERE workspace_id=$1', row['id'])
                ready = await conn.fetchval("SELECT w.lifecycle_state='active' AND r.expires_at<=now() AND r.pins='[]'::jsonb FROM kotoyose.execution_workspaces w JOIN kotoyose.execution_workspace_retention r ON r.workspace_id=w.id WHERE w.id=$1", row['id'])
                blockers, _ = await self.repository.blockers(conn, row)
                if not ready or blockers or not row['recovery']:
                    continue
                try:
                    self._allowed_path(row)
                    await self._verify_recovery(conn, row)
                except (ExecutionError, ArtifactCaptureError, WorkspaceDeletionError, OSError) as exc:
                    log.warning('workspace.gc_precondition_unconfirmed', workspace_id=row['id'], error_type=type(exc).__name__)
                    continue
                await conn.execute("UPDATE kotoyose.execution_workspaces SET lifecycle_state='deleting' WHERE id=$1", row['id'])
                claimed = row
            if claimed:
                deleted += await self._delete(claimed)
        return deleted

    async def reconcile(self, *, limit: int = 20) -> int:
        """旧 process が確定した削除 intent と同じ filesystem identity だけを再確認する。"""
        self._validate_limit(limit)
        rows = await self.pool.fetch(
            "SELECT w.*,r.recovery FROM kotoyose.execution_workspaces w JOIN kotoyose.execution_workspace_retention r ON r.workspace_id=w.id "
            "WHERE w.lifecycle_state='deleting' ORDER BY r.last_checked_at,w.id LIMIT $1", limit)
        return sum([await self._delete(dict(row)) for row in rows])

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 100:
            raise ValueError('Workspace GC limit must be between 1 and 100')

    async def _delete(self, row: dict) -> int:
        async with self.pool.acquire() as conn:
            lock = f"workspace-delete:{row['id']}"
            if not await conn.fetchval('SELECT pg_try_advisory_lock(hashtextextended($1,0))', lock):
                return 0
            try:
                state = await conn.fetchval('SELECT lifecycle_state FROM kotoyose.execution_workspaces WHERE id=$1', row['id'])
                if state != 'deleting':
                    return 0
                await conn.execute('UPDATE kotoyose.execution_workspace_retention SET last_checked_at=now() WHERE workspace_id=$1', row['id'])
                try:
                    path = self._allowed_path(row)
                    # deleting のまま再起動した場合も、現在の backend で回復 bytes を検証する。
                    await self._verify_recovery(conn, row)
                    await asyncio.to_thread(delete_owned_workspace, path, row['device'], row['inode'])
                except (ExecutionError, ArtifactCaptureError, WorkspaceDeletionError, OSError) as exc:
                    log.warning('workspace.deletion_unconfirmed', workspace_id=row['id'], error_type=type(exc).__name__)
                    return 0
                async with conn.transaction():
                    await conn.execute("UPDATE kotoyose.execution_workspaces SET lifecycle_state='tombstone' WHERE id=$1 AND lifecycle_state='deleting'", row['id'])
                    await conn.execute('UPDATE kotoyose.execution_workspace_retention SET deleted_at=now(),updated_at=now() WHERE workspace_id=$1', row['id'])
                return 1
            finally:
                await conn.execute('SELECT pg_advisory_unlock(hashtextextended($1,0))', lock)
