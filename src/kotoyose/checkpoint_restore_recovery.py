"""未完了の復元を照会し、Caller の破棄 intent を実体の削除前に永続化する。"""

import asyncio
import json
import stat
from pathlib import Path
from uuid import UUID, NAMESPACE_URL, uuid5

from .checkpoint_contract import CheckpointAbandon
from .checkpoint_restore import restore_path
from .execution_resources import CallerPolicy, ExecutionError, canonical_json, content_digest
from .execution_store import ExecutionStore
from .workspace_deletion import delete_owned_workspace, WorkspaceDeletionError


def _directory_state(path: Path, row) -> str:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 'absent'
    except OSError:
        return 'unconfirmed'
    if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (row['device'], row['inode']):
        return 'owned'
    return 'unconfirmed'


class CheckpointRestoreRecovery:
    """復元と破棄を同じ lock で直列化し、確定済み workspace は破棄対象にしない。"""

    def __init__(self, store: ExecutionStore):
        self.store = store
        self.pool = store.pool

    async def _row(self, conn, principal: str, ref: str, request_id: UUID):
        row = await conn.fetchrow(
            'SELECT * FROM kotoyose.execution_checkpoint_restores WHERE principal=$1 AND request_id=$2 AND checkpoint_id=$3',
            principal, str(request_id), ref.removeprefix('checkpoint:'),
        )
        if row is None or ref != f"checkpoint:{row['checkpoint_id']}":
            raise ExecutionError('invalid_request', 'Unknown checkpoint restore', 404)
        return row

    async def get(self, principal: str, policy: CallerPolicy, ref: str, request_id: UUID) -> dict:
        """保存済み復元の状態と確認可能な実体だけを返し、存在を成功とは扱わない。"""
        async with self.pool.acquire() as conn:
            row = await self._row(conn, principal, ref, request_id)
        path = restore_path(policy, row['path'])
        return {
            'schema_version': '1', 'checkpoint_ref': ref, 'restore_request_id': str(request_id),
            'caller_workspace_ref': row['caller_ref'], 'path': row['path'], 'state': row['state'],
            'directory_state': await asyncio.to_thread(_directory_state, path, row),
            'result': json.loads(row['result']) if row['result'] else None,
            'abandon_request': json.loads(row['abandon_request']) if row['abandon_request'] else None,
            'abandon_result': json.loads(row['abandon_result']) if row['abandon_result'] else None,
        }

    async def abandon(self, principal: str, policy: CallerPolicy, ref: str, restore_request_id: UUID, request: CheckpointAbandon) -> dict:
        """旧 path 予約を保ち、保存済み identity の部分復元または確認済みの不在だけを破棄する。"""
        identity = str(uuid5(NAMESPACE_URL, f'kotoyose-restore:{principal}:{restore_request_id}'))
        digest = content_digest({'checkpoint_ref': ref, 'restore_request_id': str(restore_request_id), **request.model_dump(mode='json')})
        async with self.pool.acquire() as conn:
            await conn.execute('SELECT pg_advisory_lock(hashtextextended($1,0))', f'checkpoint-restore:{identity}')
            try:
                row = await self._row(conn, principal, ref, restore_request_id)
                if row['abandon_digest'] and row['abandon_digest'] != digest:
                    raise ExecutionError('request_conflict', 'Restore abandonment intent changed', 409)
                if row['state'] == 'abandoned':
                    return json.loads(row['abandon_result'])
                if row['state'] not in {'prepared', 'abandoning'}:
                    raise ExecutionError('request_conflict', 'Confirmed workspace requires its own retention policy', 409)
                path = restore_path(policy, row['path'])
                async with conn.transaction():
                    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('kotoyose-workspaces',0))")
                    registered = await conn.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM kotoyose.execution_workspaces WHERE path=$1 OR starts_with(path,$1||'/') OR starts_with($1,path||'/'))", str(path),
                    )
                    if registered:
                        raise ExecutionError('request_conflict', 'Restore path overlaps a registered workspace', 409)
                    await conn.execute(
                        "UPDATE kotoyose.execution_checkpoint_restores SET state='abandoning',abandon_request_id=$2,abandon_digest=$3,abandon_request=$4::jsonb WHERE id=$1",
                        identity, str(request.request_id), digest, canonical_json(request.model_dump(mode='json')),
                    )
                directory = await asyncio.to_thread(_directory_state, path, row)
                if directory == 'unconfirmed':
                    raise ExecutionError('request_conflict', 'Restore directory identity requires inspection; nothing was deleted', 409)
                if directory == 'owned':
                    await asyncio.to_thread(delete_owned_workspace, path, row['device'], row['inode'])
                # 応答消失後は同じ intent で不在を照合する。台帳の path 予約は解除しない。
                if await asyncio.to_thread(_directory_state, path, row) != 'absent':
                    raise ExecutionError('request_conflict', 'Restore directory absence is unconfirmed', 409)
                async with conn.transaction():
                    result = {
                        'schema_version': '1', 'checkpoint_ref': ref, 'restore_request_id': str(restore_request_id),
                        'request_id': str(request.request_id), 'state': 'abandoned', 'proof_ref': request.proof_ref,
                        'abandoned_at': (await conn.fetchval('SELECT now()')).isoformat(),
                    }
                    await conn.execute(
                        "UPDATE kotoyose.execution_checkpoint_restores SET state='abandoned',abandon_result=$2::jsonb,finished_at=now() WHERE id=$1", identity, canonical_json(result),
                    )
                    attempt_id = await conn.fetchval('SELECT source_attempt_id FROM kotoyose.execution_checkpoints WHERE id=$1', row['checkpoint_id'])
                    await self.store._event(conn, attempt_id, 'checkpoint_restore_abandoned', result)
                return result
            except (OSError, WorkspaceDeletionError) as exc:
                raise ExecutionError('request_conflict', 'Restore deletion is unconfirmed; its identity remains reserved', 409) from exc
            finally:
                await conn.execute('SELECT pg_advisory_unlock(hashtextextended($1,0))', f'checkpoint-restore:{identity}')
