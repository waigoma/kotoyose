"""Output artifact の retention CAS、receipt と GC の永続境界。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

import asyncpg

from .artifact_blob_store import ArtifactBlob, ArtifactBlobRegistry, ArtifactMissingError, ArtifactStorageError
from .artifact_retention import ArtifactRetentionChange, OutputArtifactLimits
from .execution_resources import ExecutionError, canonical_json
from .checkpoint_contract import CHECKPOINT_RETENTION_LOCK


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kotoyose.execution_artifact_retention (
    artifact_ref text PRIMARY KEY REFERENCES kotoyose.execution_artifacts(ref),
    version bigint NOT NULL DEFAULT 0 CHECK(version >= 0),
    state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','deleting','tombstone')),
    expires_at timestamptz NOT NULL,
    pins jsonb NOT NULL DEFAULT '["caller-policy-pending"]',
    proof_ref text,
    acknowledged_required_action_ref text,
    deleted_at timestamptz,
    tombstone_reason text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kotoyose.execution_artifact_retention_receipts (
    principal text NOT NULL, artifact_ref text NOT NULL REFERENCES kotoyose.execution_artifacts(ref),
    request_id text NOT NULL, request_digest text NOT NULL, result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(principal, artifact_ref, request_id)
);
CREATE INDEX IF NOT EXISTS execution_artifact_retention_gc_idx
    ON kotoyose.execution_artifact_retention(expires_at) WHERE state='active' AND pins='[]'::jsonb;
CREATE INDEX IF NOT EXISTS execution_artifact_retention_deleting_idx
    ON kotoyose.execution_artifact_retention(updated_at) WHERE state='deleting';
"""


class ArtifactRetentionRepository:
    """削除対象を先に durable に閉じ、backend 応答不明時は tombstone を捏造しない。"""

    def __init__(self, pool: asyncpg.Pool, storage: ArtifactBlobRegistry,
                 limits: OutputArtifactLimits | None = None):
        self.pool, self.storage = pool, storage
        self.limits = limits or OutputArtifactLimits()

    async def initialize(self) -> None:
        """既存 bytes は明示的な policy-pending pin で移行し、初回 GC で消さない。"""
        await self.pool.execute(_SCHEMA)
        await self.pool.execute('UPDATE kotoyose.execution_artifacts SET byte_size=octet_length(content) WHERE byte_size IS NULL AND content IS NOT NULL')
        await self.pool.execute(
            "INSERT INTO kotoyose.execution_artifact_retention(artifact_ref,expires_at) "
            "SELECT ref,created_at + $1::double precision * interval '1 second' "
            "FROM kotoyose.execution_artifacts ON CONFLICT DO NOTHING",
            self.limits.default_lifetime_seconds,
        )

    async def register(self, conn: asyncpg.Connection, ref: str) -> None:
        """保存完了と同じ transaction 内で、Caller の保持判断が来るまで pin する。"""
        await conn.execute(
            "INSERT INTO kotoyose.execution_artifact_retention(artifact_ref,expires_at) "
            "VALUES($1,now() + $2::double precision * interval '1 second') ON CONFLICT DO NOTHING",
            ref, self.limits.default_lifetime_seconds,
        )

    @staticmethod
    def _view(row: asyncpg.Record | dict) -> dict:
        pins = row['pins']
        return {'schema_version': '1', 'artifact_ref': row['artifact_ref'], 'version': row['version'],
                'state': row['state'], 'expires_at': row['expires_at'].isoformat(),
                'pins': json.loads(pins) if isinstance(pins, str) else pins,
                'proof_ref': row['proof_ref'], 'acknowledged_required_action_ref': row['acknowledged_required_action_ref'],
                'deleted_at': row['deleted_at'].isoformat() if row['deleted_at'] else None,
                'tombstone_reason': row['tombstone_reason']}

    async def get(self, principal: str, ref: str) -> dict:
        """所有する ref に限り、削除済みの metadata も返す。"""
        row = await self.pool.fetchrow(
            "SELECT r.* FROM kotoyose.execution_artifact_retention r "
            "JOIN kotoyose.execution_artifacts b ON b.ref=r.artifact_ref "
            "JOIN kotoyose.execution_attempts a ON a.id=b.attempt_id "
            "WHERE r.artifact_ref=$1 AND a.principal=$2", ref, principal,
        )
        if row is None:
            raise ExecutionError('invalid_request', 'Unknown artifact', 404)
        return self._view(row)

    async def change(self, principal: str, ref: str, change: ArtifactRetentionChange) -> dict:
        """pin / expiry と request receipt を一度だけ確定する。GC 開始後は policy を更新しない。"""
        digest = hashlib.sha256(canonical_json(change.model_dump(mode='json')).encode()).hexdigest()
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT r.*,a.required_action FROM kotoyose.execution_artifact_retention r "
                "JOIN kotoyose.execution_artifacts b ON b.ref=r.artifact_ref "
                "JOIN kotoyose.execution_attempts a ON a.id=b.attempt_id "
                "WHERE r.artifact_ref=$1 AND a.principal=$2 FOR UPDATE OF r", ref, principal,
            )
            if row is None:
                raise ExecutionError('invalid_request', 'Unknown artifact', 404)
            prior = await conn.fetchrow(
                "SELECT request_digest,result FROM kotoyose.execution_artifact_retention_receipts "
                "WHERE principal=$1 AND artifact_ref=$2 AND request_id=$3", principal, ref, change.request_id,
            )
            if prior:
                if prior['request_digest'] != digest:
                    raise ExecutionError('request_conflict', 'Artifact retention request ID changed', 409)
                return json.loads(prior['result'])
            # 管理者が上限を狭めても、確定済み receipt の再送は新たな延長ではない。
            if len(change.pins) > self.limits.max_pins:
                raise ExecutionError('permission_denied', 'Artifact pin count exceeds administrator limits', 403)
            if change.expires_at > datetime.now(timezone.utc) + timedelta(seconds=self.limits.max_lifetime_seconds):
                raise ExecutionError('permission_denied', 'Artifact retention exceeds administrator limits', 403)
            if row['state'] != 'active':
                raise ExecutionError('artifact_expired', 'Artifact deletion has already started', 410)
            if row['version'] != change.expected_version:
                raise ExecutionError('request_conflict', 'Artifact retention version changed', 409)
            action = row['required_action']
            action = json.loads(action) if isinstance(action, str) else action
            acknowledgement = change.acknowledged_required_action_ref
            if acknowledgement is not None and (not action or acknowledgement != action.get('call_id')):
                raise ExecutionError('request_conflict', 'Required action acknowledgement does not match', 409)
            updated = await conn.fetchrow(
                "UPDATE kotoyose.execution_artifact_retention SET version=version+1,expires_at=$2,pins=$3::jsonb,"
                "proof_ref=$4,acknowledged_required_action_ref=$5,updated_at=now() WHERE artifact_ref=$1 RETURNING *",
                ref, change.expires_at, canonical_json(change.pins), change.proof_ref, acknowledgement,
            )
            result = self._view(updated)
            await conn.execute(
                "INSERT INTO kotoyose.execution_artifact_retention_receipts(principal,artifact_ref,request_id,request_digest,result) "
                "VALUES($1,$2,$3,$4,$5::jsonb)", principal, ref, change.request_id, digest, canonical_json(result),
            )
            return result

    async def gc(self, *, limit: int = 100) -> int:
        """確認済み停止・解決済み効果・空 pin・期限をすべて満たす bytes だけを削除する。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 1000:
            raise ValueError('GC limit must be between 1 and 1000')
        async with self.pool.acquire() as conn, conn.transaction():
            # 同じ artifact への復元参照の追加と直列化し、古い MVCC snapshot で削除を選ばない。
            await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', CHECKPOINT_RETENTION_LOCK)
            rows = await conn.fetch(
                "SELECT r.artifact_ref,b.* FROM kotoyose.execution_artifact_retention r "
                "JOIN kotoyose.execution_artifacts b ON b.ref=r.artifact_ref "
                "JOIN kotoyose.execution_attempts a ON a.id=b.attempt_id "
                "WHERE r.state='active' AND r.expires_at <= now() AND r.pins='[]'::jsonb "
                "AND a.finished_at IS NOT NULL AND a.stop_confirmation='confirmed' AND a.side_effect_resolution='resolved' "
                "AND (a.required_action IS NULL OR r.acknowledged_required_action_ref=a.required_action->>'call_id') "
                "AND NOT EXISTS(SELECT 1 FROM kotoyose.execution_operations o WHERE o.attempt_id=a.id AND o.state<>'confirmed') "
                "AND NOT EXISTS(SELECT 1 FROM kotoyose.execution_checkpoint_restores x JOIN kotoyose.execution_checkpoints c ON c.id=x.checkpoint_id WHERE c.artifact_ref=b.ref AND x.state IN ('prepared','abandoning')) "
                "AND NOT EXISTS(SELECT 1 FROM kotoyose.execution_checkpoints c WHERE c.artifact_ref=b.ref AND c.released_at IS NULL) "
                "ORDER BY r.expires_at LIMIT $1 FOR UPDATE OF r SKIP LOCKED", limit,
            )
            if rows:
                await conn.execute(
                    "UPDATE kotoyose.execution_artifact_retention SET state='deleting',updated_at=now() "
                    "WHERE artifact_ref=ANY($1::text[])", [row['artifact_ref'] for row in rows],
                )
        deleted = 0
        for row in rows:
            deleted += await self._delete(row)
        return deleted

    async def reconcile(self, *, limit: int = 100) -> int:
        """process loss / 応答消失後は同じ locator の削除を再確認する。"""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 1000:
            raise ValueError('Reconcile limit must be between 1 and 1000')
        rows = await self.pool.fetch(
            "SELECT r.artifact_ref,b.* FROM kotoyose.execution_artifact_retention r "
            "JOIN kotoyose.execution_artifacts b ON b.ref=r.artifact_ref WHERE r.state='deleting' "
            "ORDER BY r.updated_at LIMIT $1", limit,
        )
        return sum([await self._delete(row) for row in rows])

    async def _delete(self, row: asyncpg.Record) -> int:
        reason = 'expired'
        try:
            if row['storage_backend'] != 'postgres':
                if not row['storage_identity'] or not row['storage_key']:
                    raise ArtifactStorageError('Artifact storage locator is incomplete')
                backend = self.storage.resolve(row['storage_backend'], row['storage_identity'])
                await backend.delete(ArtifactBlob(row['storage_key'], row['digest'], row['byte_size'], row['media_type']))
        except ArtifactMissingError:
            reason = 'storage_missing'
        except (ArtifactStorageError, OSError):
            return 0
        async with self.pool.acquire() as conn, conn.transaction():
            changed = await conn.fetchval(
                "UPDATE kotoyose.execution_artifact_retention SET state='tombstone',deleted_at=now(),"
                "tombstone_reason=$2,updated_at=now() WHERE artifact_ref=$1 AND state='deleting' RETURNING artifact_ref",
                row['artifact_ref'], reason,
            )
            if changed:
                await conn.execute('UPDATE kotoyose.execution_artifacts SET content=NULL WHERE ref=$1', row['artifact_ref'])
        return int(changed is not None)
