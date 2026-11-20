"""Workspace の保持判断、世代 fence、削除 receipt を永続化する。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import TYPE_CHECKING

from .checkpoint_contract import CHECKPOINT_RETENTION_LOCK
from .execution_resources import ExecutionError, canonical_json, content_digest
from .workspace_identity import parse_workspace_ref
from .workspace_retention import WorkspaceLimits, WorkspaceRetentionChange

if TYPE_CHECKING:
    from .execution_store import ExecutionStore

_SCHEMA = """
ALTER TABLE kotoyose.execution_workspaces ADD COLUMN IF NOT EXISTS lifecycle_state text
    NOT NULL DEFAULT 'active' CHECK(lifecycle_state IN ('active','deleting','tombstone'));
CREATE TABLE IF NOT EXISTS kotoyose.execution_workspace_retention (
    workspace_id text PRIMARY KEY REFERENCES kotoyose.execution_workspaces(id),
    version bigint NOT NULL DEFAULT 0 CHECK(version>=0), policy_generation bigint,
    expires_at timestamptz, pins jsonb NOT NULL DEFAULT '["caller-policy-pending"]',
    proof_ref text, recovery jsonb, acknowledged_required_actions jsonb NOT NULL DEFAULT '[]',
    deleted_at timestamptz, last_checked_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kotoyose.execution_workspace_retention_receipts (
    principal text NOT NULL, workspace_id text NOT NULL REFERENCES kotoyose.execution_workspaces(id),
    request_id text NOT NULL, request_digest text NOT NULL, result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(principal,workspace_id,request_id)
);
CREATE INDEX IF NOT EXISTS execution_workspace_retention_gc_idx
    ON kotoyose.execution_workspace_retention(last_checked_at) WHERE pins='[]'::jsonb;
INSERT INTO kotoyose.execution_workspace_retention(workspace_id)
    SELECT id FROM kotoyose.execution_workspaces ON CONFLICT DO NOTHING;
"""


class WorkspaceRetentionRepository:
    """Caller の意味上の保持判断を受け、runtime が停止と未解決操作を別途検査する。"""

    def __init__(self, store: ExecutionStore):
        self.store, self.pool = store, store.pool
        self.limits = WorkspaceLimits()

    async def initialize(self) -> None:
        """既存 workspace は期限を推定せず、Caller policy の確認まで保持する。"""
        await self.pool.execute(_SCHEMA)

    async def register(self, conn, identity: str) -> None:
        """Workspace 登録と同じ transaction で初期 pin を置く。"""
        await conn.execute('INSERT INTO kotoyose.execution_workspace_retention(workspace_id) VALUES($1)', identity)

    async def row(self, conn, principal: str, ref: str) -> dict:
        """過去世代の参照からも現在状態を取得するが、未来世代は受け入れない。"""
        identity, generation = parse_workspace_ref(ref)
        row = await conn.fetchrow(
            'SELECT w.*,r.* FROM kotoyose.execution_workspaces w JOIN kotoyose.execution_workspace_retention r '
            'ON r.workspace_id=w.id WHERE w.id=$1 AND w.principal=$2 FOR UPDATE OF w,r', identity, principal)
        if row is None:
            raise ExecutionError('invalid_request', 'Unknown workspace', 404)
        if generation > row['generation']:
            raise ExecutionError('request_conflict', 'Workspace generation is not known', 409)
        return dict(row)

    async def view(self, conn, row: dict) -> dict:
        """Caller が保持判断に使える根拠を返し、host path や credential は返さない。"""
        blockers, actions = await self.blockers(conn, row)
        return {'schema_version': '1', 'workspace_identity': row['id'],
                'workspace_ref': f"workspace:{row['id']}:{row['generation']}", 'workspace_generation': row['generation'],
                'version': row['version'], 'policy_generation': row['policy_generation'], 'state': row['lifecycle_state'],
                'expires_at': row['expires_at'].isoformat() if row['expires_at'] else None,
                'pins': json.loads(row['pins']), 'proof_ref': row['proof_ref'],
                'recovery': json.loads(row['recovery']) if row['recovery'] else None,
                'acknowledged_required_actions': json.loads(row['acknowledged_required_actions']),
                'required_actions': actions, 'blocking_conditions': blockers,
                'deleted_at': row['deleted_at'].isoformat() if row['deleted_at'] else None}

    async def blockers(self, conn, row: dict) -> tuple[list[str], list[str]]:
        """期限や Caller pin とは独立に、実行の未確定状態を調べる。"""
        attempts = await conn.fetch(
            'SELECT a.id,a.finished_at,a.stop_confirmation,a.side_effect_resolution,a.required_action,'
            "EXISTS(SELECT 1 FROM kotoyose.execution_operations o WHERE o.attempt_id=a.id AND o.state<>'confirmed') AS pending "
            'FROM kotoyose.execution_attempts a WHERE a.workspace_id=$1', row['id'])
        blockers, actions = set(), set()
        if row['writer_attempt']:
            blockers.add('workspace_writer')
        if row['policy_generation'] != row['generation']:
            blockers.add('policy_generation_changed')
        for attempt in attempts:
            if attempt['finished_at'] is None:
                blockers.add('attempt_active')
            if attempt['stop_confirmation'] != 'confirmed':
                blockers.add('stop_unconfirmed')
            if attempt['side_effect_resolution'] != 'resolved' or attempt['pending']:
                blockers.add('operation_unresolved')
            action = json.loads(attempt['required_action']) if attempt['required_action'] else None
            if action:
                if action.get('call_id'):
                    actions.add(action['call_id'])
                else:
                    blockers.add('required_action_unidentified')
        if actions - set(json.loads(row['acknowledged_required_actions'])):
            blockers.add('required_action_unacknowledged')
        recovery = json.loads(row['recovery']) if row['recovery'] else None
        if recovery and recovery['mode'] == 'checkpoint':
            try:
                await self.recovery_checkpoint(conn, row, recovery['checkpoint_ref'])
            except ExecutionError:
                blockers.add('recovery_checkpoint_unavailable')
        return sorted(blockers), sorted(actions)

    async def get(self, principal: str, ref: str) -> dict:
        """削除済み metadata も所有する Caller に返す。"""
        async with self.pool.acquire() as conn, conn.transaction():
            return {**await self.view(conn, await self.row(conn, principal, ref)), 'requested_workspace_ref': ref}

    async def change(self, principal: str, ref: str, change: WorkspaceRetentionChange) -> dict:
        """保持条件の CAS と immutable receipt を、workspace writer と直列化する。"""
        digest = content_digest(change.model_dump(mode='json'))
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', CHECKPOINT_RETENTION_LOCK)
            row = await self.row(conn, principal, ref)
            prior = await conn.fetchrow('SELECT * FROM kotoyose.execution_workspace_retention_receipts WHERE principal=$1 AND workspace_id=$2 AND request_id=$3', principal, row['id'], change.request_id)
            if prior:
                if prior['request_digest'] != digest:
                    raise ExecutionError('request_conflict', 'Workspace retention request ID changed', 409)
                return json.loads(prior['result'])
            if row['lifecycle_state'] != 'active':
                raise ExecutionError('request_conflict', 'Workspace deletion has already started', 409)
            if change.expected_version != row['version'] or change.workspace_ref != f"workspace:{row['id']}:{row['generation']}":
                raise ExecutionError('request_conflict', 'Workspace retention version or generation changed', 409)
            if len(change.pins) > self.limits.max_pins or change.expires_at > datetime.now(timezone.utc) + timedelta(seconds=self.limits.max_lifetime_seconds):
                raise ExecutionError('permission_denied', 'Workspace retention exceeds administrator limits', 403)
            _, actions = await self.blockers(conn, row)
            if set(change.acknowledged_required_actions) - set(actions):
                raise ExecutionError('request_conflict', 'Required action acknowledgement does not match', 409)
            if change.recovery.mode == 'checkpoint':
                await self.recovery_checkpoint(conn, row, change.recovery.checkpoint_ref)
            await conn.execute(
                'UPDATE kotoyose.execution_workspace_retention SET version=version+1,policy_generation=$2,expires_at=$3,'
                'pins=$4::jsonb,proof_ref=$5,recovery=$6::jsonb,acknowledged_required_actions=$7::jsonb,updated_at=now() WHERE workspace_id=$1',
                row['id'], row['generation'], change.expires_at, canonical_json(change.pins), change.proof_ref,
                canonical_json(change.recovery.model_dump(mode='json')), canonical_json(change.acknowledged_required_actions))
            result = await self.view(conn, await self.row(conn, principal, ref))
            await conn.execute('INSERT INTO kotoyose.execution_workspace_retention_receipts(principal,workspace_id,request_id,request_digest,result) VALUES($1,$2,$3,$4,$5::jsonb)',
                               principal, row['id'], change.request_id, digest, canonical_json(result))
            return result

    async def recovery_checkpoint(self, conn, row: dict, ref: str) -> dict:
        """回復対象と同じ workspace / 世代の、解除されていない保存済み ref に限定する。"""
        checkpoint = await conn.fetchrow(
            "SELECT c.* FROM kotoyose.execution_checkpoints c JOIN kotoyose.execution_artifact_retention r ON r.artifact_ref=c.artifact_ref "
            "WHERE 'checkpoint:'||c.id=$1 AND c.workspace_id=$2 AND c.generation=$3 AND c.released_at IS NULL AND r.state='active'",
            ref, row['id'], row['generation'])
        if checkpoint is None:
            raise ExecutionError('request_conflict', 'Recovery checkpoint is unavailable for the current workspace generation', 409)
        return dict(checkpoint)
