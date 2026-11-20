"""Workspace の停止済み世代と回復 bytes を、Caller ごとの台帳へ固定する。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5

from .checkpoint_archive import capture_checkpoint
from .checkpoint_contract import CheckpointCapture, CheckpointRelease, CHECKPOINT_RETENTION_LOCK
from .execution_artifacts import ArtifactCaptureError, CapturedArtifact
from .execution_resources import ExecutionError, canonical_json, content_digest

if TYPE_CHECKING:
    from .execution_store import ExecutionStore

MAX_WORKSPACE_CHECKPOINTS = 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kotoyose.execution_checkpoints (
    id text PRIMARY KEY, principal text NOT NULL, request_id text NOT NULL, request_digest text NOT NULL,
    workspace_id text NOT NULL REFERENCES kotoyose.execution_workspaces(id),
    source_attempt_id text NOT NULL REFERENCES kotoyose.execution_attempts(id),
    generation bigint NOT NULL, artifact_ref text NOT NULL REFERENCES kotoyose.execution_artifacts(ref),
    result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    release_request_id text, released_at timestamptz,
    UNIQUE(principal,request_id)
);
ALTER TABLE kotoyose.execution_checkpoints ADD COLUMN IF NOT EXISTS release_request_id text;
ALTER TABLE kotoyose.execution_checkpoints ADD COLUMN IF NOT EXISTS released_at timestamptz;
CREATE INDEX IF NOT EXISTS execution_checkpoints_artifact_idx ON kotoyose.execution_checkpoints(artifact_ref);
CREATE INDEX IF NOT EXISTS execution_checkpoints_workspace_idx ON kotoyose.execution_checkpoints(workspace_id);
CREATE TABLE IF NOT EXISTS kotoyose.execution_checkpoint_restores (
    id text PRIMARY KEY, principal text NOT NULL, request_id text NOT NULL, request_digest text NOT NULL,
    checkpoint_id text NOT NULL REFERENCES kotoyose.execution_checkpoints(id),
    caller_ref text NOT NULL, path text NOT NULL UNIQUE,
    state text NOT NULL DEFAULT 'prepared', device bigint, inode bigint,
    result jsonb, created_at timestamptz NOT NULL DEFAULT now(), finished_at timestamptz,
    UNIQUE(principal,request_id), UNIQUE(principal,caller_ref)
);
ALTER TABLE kotoyose.execution_checkpoint_restores ADD COLUMN IF NOT EXISTS abandon_request_id text;
ALTER TABLE kotoyose.execution_checkpoint_restores ADD COLUMN IF NOT EXISTS abandon_digest text;
ALTER TABLE kotoyose.execution_checkpoint_restores ADD COLUMN IF NOT EXISTS abandon_request jsonb;
ALTER TABLE kotoyose.execution_checkpoint_restores ADD COLUMN IF NOT EXISTS abandon_result jsonb;
"""


class CheckpointRepository:
    """Checkpoint は停止・世代・保存確認を満たすまで公開しない。"""

    def __init__(self, store: ExecutionStore):
        self.store = store
        self.pool = store.pool

    async def initialize(self) -> None:
        """既存 Attempt と Artifact の参照を保ったまま回復台帳を追加する。"""
        await self.pool.execute(_SCHEMA)

    async def replay(self, principal: str, request: CheckpointCapture) -> dict | None:
        """元 workspace が失われても、既に確定した同一 intent の receipt を再読する。"""
        row = await self.pool.fetchrow('SELECT * FROM kotoyose.execution_checkpoints WHERE principal=$1 AND request_id=$2', principal, str(request.request_id))
        return _capture_receipt(row, content_digest(request.model_dump(mode='json')))

    async def capture(self, principal: str, request: CheckpointCapture) -> dict:
        """同一 intent を再読し、新規回収は停止済みの現在 workspace 世代に限定する。"""
        request_digest = content_digest(request.model_dump(mode='json'))
        checkpoint_id = str(uuid5(NAMESPACE_URL, f'kotoyose-checkpoint:{principal}:{request.request_id}'))
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', f'checkpoint:{checkpoint_id}')
            prior = await conn.fetchrow('SELECT * FROM kotoyose.execution_checkpoints WHERE principal=$1 AND request_id=$2', principal, str(request.request_id))
            if prior:
                return _capture_receipt(prior, request_digest)
            attempt = await conn.fetchrow('SELECT * FROM kotoyose.execution_attempts WHERE principal=$1 AND id=$2 FOR UPDATE', principal, request.source_attempt_id)
            if attempt is None:
                raise ExecutionError('invalid_request', 'Unknown source attempt', 404)
            workspace = await conn.fetchrow('SELECT * FROM kotoyose.execution_workspaces WHERE id=$1 FOR UPDATE', attempt['workspace_id'])
            current_ref = f"workspace:{workspace['id']}:{workspace['generation']}"
            unresolved = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM kotoyose.execution_operations WHERE attempt_id=$1 AND state<>'confirmed')", attempt['id'])
            if (attempt['finished_at'] is None or attempt['stop_confirmation'] != 'confirmed' or attempt['side_effect_resolution'] != 'resolved'
                    or workspace['lifecycle_state'] != 'active' or unresolved or workspace['writer_attempt'] is not None or request.workspace_ref != current_ref
                    or workspace['generation'] != attempt['generation'] + 1):
                raise ExecutionError('request_conflict', 'Checkpoint requires the current, stopped and reconciled workspace generation', 409)
            count = await conn.fetchval('SELECT count(*) FROM kotoyose.execution_checkpoints WHERE workspace_id=$1', workspace['id'])
            if count >= MAX_WORKSPACE_CHECKPOINTS:
                raise ExecutionError('permission_denied', 'Workspace checkpoint limit exceeded', 403)
            try:
                content = capture_checkpoint(Path(workspace['path']), workspace['device'], workspace['inode'], request)
            except (OSError, ArtifactCaptureError) as exc:
                raise ExecutionError('request_conflict', 'Checkpoint files no longer match the selected manifest', 409) from exc
            artifact = await self.store.save_artifact(conn, attempt['id'], f'checkpoint-{checkpoint_id}.zip', CapturedArtifact(content, 'application/zip'))
            result = {'schema_version': '1', 'checkpoint_ref': f'checkpoint:{checkpoint_id}',
                      'request_id': str(request.request_id), 'created_at': (await conn.fetchval('SELECT now()')).isoformat(),
                      'source_attempt_id': attempt['id'], 'workspace_ref': current_ref,
                      'source_revision': request.source_revision, 'files': [entry.model_dump(mode='json') for entry in request.files],
                      'coverage': 'selected_files', 'session_state': 'excluded', 'artifact': artifact}
            await conn.execute('INSERT INTO kotoyose.execution_checkpoints(id,principal,request_id,request_digest,workspace_id,source_attempt_id,generation,artifact_ref,result) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)',
                               checkpoint_id, principal, str(request.request_id), request_digest, workspace['id'], attempt['id'], workspace['generation'], artifact['ref'], canonical_json(result))
            await self.store._event(conn, attempt['id'], 'checkpoint_created', {'checkpoint_ref': result['checkpoint_ref'], 'artifact': artifact, 'workspace_ref': current_ref})
            return result

    async def get(self, principal: str, ref: str) -> dict:
        """他 Caller の checkpoint を隠し、保存後に変わった bytes の保持状態も返す。"""
        checkpoint_id = ref.removeprefix('checkpoint:')
        row = await self.pool.fetchrow('SELECT * FROM kotoyose.execution_checkpoints WHERE principal=$1 AND id=$2', principal, checkpoint_id)
        if row is None or ref != f'checkpoint:{checkpoint_id}':
            raise ExecutionError('invalid_request', 'Unknown checkpoint', 404)
        retention = await self.store.artifact_retention.get(principal, row['artifact_ref'])
        return {**json.loads(row['result']), 'retention': retention,
                'hold': {'state': 'released' if row['released_at'] else 'retained',
                         'released_at': row['released_at'].isoformat() if row['released_at'] else None}}

    async def release(self, principal: str, ref: str, request: CheckpointRelease) -> dict:
        """回復点を不要と判断した Caller の一度限りの解除を保存する。"""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', CHECKPOINT_RETENTION_LOCK)
            row = await conn.fetchrow('SELECT * FROM kotoyose.execution_checkpoints WHERE principal=$1 AND id=$2 FOR UPDATE', principal, ref.removeprefix('checkpoint:'))
            if row is None or ref != f"checkpoint:{row['id']}":
                raise ExecutionError('invalid_request', 'Unknown checkpoint', 404)
            if row['release_request_id'] and row['release_request_id'] != str(request.request_id):
                raise ExecutionError('request_conflict', 'Checkpoint was released by another request', 409)
            if row['released_at'] is None:
                deleting = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM kotoyose.execution_workspace_retention r JOIN kotoyose.execution_workspaces w ON w.id=r.workspace_id WHERE w.lifecycle_state='deleting' AND r.recovery->>'checkpoint_ref'=$1)", ref)
                if deleting:
                    raise ExecutionError('request_conflict', 'Checkpoint is needed by an in-progress workspace deletion', 409)
                row = await conn.fetchrow('UPDATE kotoyose.execution_checkpoints SET release_request_id=$2,released_at=now() WHERE id=$1 RETURNING *', row['id'], str(request.request_id))
                await self.store._event(conn, row['source_attempt_id'], 'checkpoint_released', {'checkpoint_ref': ref, 'request_id': str(request.request_id)})
            return {'schema_version': '1', 'checkpoint_ref': ref, 'request_id': str(request.request_id),
                    'state': 'released', 'released_at': row['released_at'].isoformat()}


async def reject_reserved_restore_path(conn, path: str, *, restore_id: str | None = None) -> None:
    """未登録の復元途中ディレクトリを通常の workspace admission に使わせない。"""
    overlap = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM kotoyose.execution_checkpoint_restores WHERE state IN ('prepared','abandoning','abandoned') AND id IS DISTINCT FROM $2 "
        "AND (path=$1 OR starts_with(path,$1||'/') OR starts_with($1,path||'/')))", path, restore_id,
    )
    if overlap:
        raise ExecutionError('request_conflict', 'Workspace overlaps a pending checkpoint restore', 409)


def _capture_receipt(row, digest):
    if row is None:
        return None
    if row['request_digest'] != digest:
        raise ExecutionError('request_conflict', 'Checkpoint request ID changed', 409)
    return json.loads(row['result'])
