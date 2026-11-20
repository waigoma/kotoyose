"""管理者が封印した部分停止証拠を保持し、受付の予約計上だけを隔離する。"""

from datetime import datetime, timezone
import json
from typing import Literal

from pydantic import AwareDatetime, Field

from .codex_stop_evidence import stop_evidence_context, validate_stop_evidence
from .execution_contract import ContractModel
from .execution_resources import ExecutionError, canonical_json, content_digest

_KIND = "administrative_admission_quarantine"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS kotoyose.execution_admission_quarantines (
    id text PRIMARY KEY, principal text NOT NULL UNIQUE, attempt_id text NOT NULL UNIQUE,
    payload jsonb NOT NULL, revoked_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kotoyose.execution_admission_quarantine_extensions (
    id text PRIMARY KEY, quarantine_ref text NOT NULL REFERENCES kotoyose.execution_admission_quarantines(id),
    principal text NOT NULL, predecessor_ref text NOT NULL, payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(quarantine_ref, predecessor_ref)
);
"""


async def initialize_admission_quarantine(pool) -> None:
    """元の実行台帳に依存せず、管理監査を保持する独立台帳を準備する。"""
    await pool.execute(_SCHEMA)


class AdministrativeAdmissionQuarantine(ContractModel):
    """HTTP resource union に加えない、管理者専用の不変監査記録。"""

    schema_version: Literal["1"] = "1"
    kind: Literal["administrative_admission_quarantine"] = _KIND
    attempt_id: str = Field(pattern=r"^[0-9a-f-]{36}$")
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    proof_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_id: str
    generation: int = Field(ge=0, strict=True)
    authorization_ref: str = Field(min_length=1, max_length=256)
    reason: Literal["owned_smoke_scope_proof_missing"] = "owned_smoke_scope_proof_missing"
    created_at: AwareDatetime
    expires_at: AwareDatetime


async def locked_quarantine_target(conn, principal: str, attempt_id: str, *, request_digest: str,
                                   snapshot_digest: str, proof_digest: str):
    """Caller lock の内側で封印対象の原証拠と writer を再検査する。"""
    row = await conn.fetchrow("""
        SELECT a.id,a.generation,a.request_digest,a.finished_at,a.stop_confirmation,a.side_effect_resolution,
          a.workspace_id,w.writer_attempt,w.generation AS live_generation,
          encode(sha256(convert_to(a.snapshot::text,'UTF8')),'hex') AS snapshot_digest,
          jsonb_build_object('workspace_device',a.snapshot->'workspace_device',
            'workspace_inode',a.snapshot->'workspace_inode') AS context_snapshot
        FROM kotoyose.execution_attempts a JOIN kotoyose.execution_workspaces w ON w.id=a.workspace_id
        WHERE a.id=$1 AND a.principal=$2 AND a.snapshot->>'kind'='agent'
          AND a.snapshot->>'provider'='codex' AND a.snapshot->>'backend'='native'
          AND a.snapshot->>'codex_execution_mode'='host_native_coding_v1' FOR UPDATE OF a,w
        """, attempt_id, principal)
    if (row is None or row['finished_at'] is None or row['stop_confirmation'] != 'unconfirmed'
            or row['side_effect_resolution'] != 'unresolved' or row['writer_attempt'] != attempt_id
            or row['generation'] != row['live_generation'] or row['request_digest'] != request_digest
            or row['snapshot_digest'] != snapshot_digest):
        raise ExecutionError("request_conflict", "Quarantine target or sealed identity changed", 409)
    result = await conn.fetchval("SELECT result FROM kotoyose.execution_operations WHERE attempt_id=$1 "
                                 "AND kind='provider_process'", attempt_id)
    ownership = json.loads(result) if isinstance(result, str) else dict(result or {})
    raw = ownership.pop('codex_stop_evidence', None)
    context = {'id': row['id'], 'generation': row['generation'],
               'snapshot': json.loads(row['context_snapshot'])}
    try:
        proof = validate_stop_evidence(raw, stop_evidence_context(context), ownership)
    except (TypeError, KeyError, ValueError) as exc:
        raise ExecutionError("request_conflict", "Quarantine requires valid saved partial proof", 409) from exc
    if (proof.digest != proof_digest or proof.pending_tasks != 0 or proof.model_dump()['components'] != {
            'provider': 'confirmed', 'scope': 'unconfirmed', 'tasks': 'confirmed',
            'pipes': 'confirmed', 'egress': 'confirmed'}):
        raise ExecutionError("request_conflict", "Quarantine requires scope-only uncertainty", 409)
    if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM kotoyose.execution_operations WHERE attempt_id=$1 "
                           "AND kind NOT IN ('provider_process','local_execution') AND state<>'confirmed')", attempt_id):
        raise ExecutionError("request_conflict", "Quarantine cannot exclude unresolved external operations", 409)
    return row


async def seal_admission_quarantine(pool, principal: str, attempt_id: str, *, request_digest: str,
                                    snapshot_digest: str, proof_digest: str,
                                    authorization_ref: str, expires_at: datetime) -> str:
    """管理操作だけで1件を封印する。停止状態・writer・予算・証拠は変更しない。

    Caller ごとの受付 lock と同じ lock を使う。scope 以外の全停止 component が
    肯定された終端のみを対象にし、稼働中処理や他の部分証拠は隔離しない。
    """
    if expires_at.tzinfo is None or not datetime.now(timezone.utc) < expires_at:
        raise ExecutionError("request_conflict", "Quarantine must have a future expiry", 409)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "kotoyose-caller:" + principal)
        if await conn.fetchval("SELECT clock_timestamp()") >= expires_at:
            raise ExecutionError("request_conflict", "Quarantine expired while waiting for caller lock", 409)
        row = await locked_quarantine_target(conn, principal, attempt_id, request_digest=request_digest,
                                             snapshot_digest=snapshot_digest, proof_digest=proof_digest)
        existing = await conn.fetchrow("SELECT id AS ref,payload,revoked_at FROM kotoyose.execution_admission_quarantines WHERE principal=$1 "
                                      "AND payload->>'kind'=$2", principal, _KIND)
        if existing:
            if existing['revoked_at'] is not None:
                raise ExecutionError('request_conflict','Administrative quarantine was revoked',409)
            saved = AdministrativeAdmissionQuarantine.model_validate_json(existing['payload'])
            if (saved.attempt_id == attempt_id and saved.request_digest == request_digest
                    and saved.snapshot_digest == snapshot_digest and saved.proof_digest == proof_digest
                    and saved.authorization_ref == authorization_ref and saved.expires_at == expires_at):
                return existing['ref']
            raise ExecutionError("request_conflict", "An administrative quarantine is already sealed", 409)
        if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM kotoyose.execution_attempts "
                               "WHERE principal=$1 AND finished_at IS NULL)", principal):
            raise ExecutionError("request_conflict", "Quarantine requires an idle caller", 409)
        if await conn.fetchval("SELECT clock_timestamp()") >= expires_at:
            raise ExecutionError('request_conflict','Quarantine expired before sealing',409)
        record = AdministrativeAdmissionQuarantine(
            attempt_id=attempt_id, request_digest=request_digest, snapshot_digest=snapshot_digest,
            proof_digest=proof_digest, workspace_id=row['workspace_id'], generation=row['generation'],
            authorization_ref=authorization_ref, created_at=datetime.now(timezone.utc), expires_at=expires_at,
        ).model_dump(mode='json')
        ref = 'quarantine:sha256:' + content_digest(record)
        await conn.execute("INSERT INTO kotoyose.execution_admission_quarantines(principal,id,payload,attempt_id) VALUES($1,$2,$3::jsonb,$4)",
                           principal, ref, canonical_json(record), attempt_id)
        return ref


async def count_admission_reservations(conn, principal: str) -> int:
    """所有が一致する管理隔離、または人が明示解放した終端を計上から除く。"""
    return await conn.fetchval("""
        SELECT count(*) FROM kotoyose.execution_attempts a
        WHERE a.principal=$1 AND (a.finished_at IS NULL OR a.stop_confirmation='unconfirmed')
          AND NOT EXISTS (
            SELECT 1 FROM kotoyose.execution_admission_releases r
            WHERE r.principal=a.principal AND r.attempt_id=a.id AND r.revoked_at IS NULL
              AND r.payload->>'kind'='administrative_admission_release'
              AND r.payload->>'schema_version'='1' AND r.payload->>'principal'=a.principal
              AND r.payload->>'reason'='human_accepts_unknown_stop'
              AND r.payload->>'attempt_id'=a.id AND r.payload->>'request_digest'=a.request_digest
              AND r.payload->>'snapshot_digest'=encode(sha256(convert_to(a.snapshot::text,'UTF8')),'hex')
              AND r.payload->>'workspace_id'=a.workspace_id
              AND (r.payload->>'generation')::bigint=a.generation
              AND a.finished_at IS NOT NULL AND a.stop_confirmation='unconfirmed'
          )
          AND NOT EXISTS (
            SELECT 1 FROM kotoyose.execution_admission_quarantines q
            JOIN kotoyose.execution_workspaces w ON w.id=a.workspace_id
            JOIN kotoyose.execution_operations o ON o.attempt_id=a.id AND o.kind='provider_process'
            WHERE q.principal=a.principal AND q.revoked_at IS NULL
              AND q.payload->>'kind'='administrative_admission_quarantine'
              AND q.payload->>'schema_version'='1'
              AND q.payload->>'attempt_id'=a.id AND q.payload->>'request_digest'=a.request_digest
              AND q.payload->>'snapshot_digest'=encode(sha256(convert_to(a.snapshot::text,'UTF8')),'hex')
              AND q.payload->>'proof_digest'=o.result->'codex_stop_evidence'->>'digest'
              AND q.payload->>'workspace_id'=a.workspace_id
              AND (q.payload->>'generation')::bigint=a.generation
              AND COALESCE((SELECT max((e.payload->>'expires_at')::timestamptz)
                FROM kotoyose.execution_admission_quarantine_extensions e
                WHERE e.quarantine_ref=q.id AND e.principal=q.principal
                  AND e.payload->>'kind'='administrative_admission_quarantine_extension'
                  AND e.payload->>'schema_version'='1' AND e.payload->>'quarantine_ref'=q.id
                  AND e.payload->>'attempt_id'=q.attempt_id
                  AND e.payload->>'request_digest'=q.payload->>'request_digest'
                  AND e.payload->>'snapshot_digest'=q.payload->>'snapshot_digest'
                  AND e.payload->>'proof_digest'=q.payload->>'proof_digest'
                  AND (e.payload->>'original_expires_at')::timestamptz=(q.payload->>'expires_at')::timestamptz),
                (q.payload->>'expires_at')::timestamptz) > clock_timestamp()
              AND a.finished_at IS NOT NULL AND a.stop_confirmation='unconfirmed'
              AND a.side_effect_resolution='unresolved'
              AND w.writer_attempt=a.id AND w.generation=a.generation
              AND NOT EXISTS (SELECT 1 FROM kotoyose.execution_operations e WHERE e.attempt_id=a.id
                AND e.kind NOT IN ('provider_process','local_execution') AND e.state<>'confirmed')
          )
        """, principal)
