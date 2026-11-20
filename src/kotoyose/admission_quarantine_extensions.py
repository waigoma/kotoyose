"""原隔離を更新せず、明示された管理延長だけを追加する。"""
from datetime import datetime, timezone
import json
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from .admission_quarantine import AdministrativeAdmissionQuarantine, locked_quarantine_target
from .execution_contract import ContractModel
from .execution_resources import ExecutionError, canonical_json, content_digest


class AdministrativeQuarantineExtension(ContractModel):
    """モデル向け resource union には含めない管理監査。"""
    schema_version: Literal['1'] = '1'
    kind: Literal['administrative_admission_quarantine_extension'] = 'administrative_admission_quarantine_extension'
    request_id: str
    quarantine_ref: str
    predecessor_ref: str
    attempt_id: str
    request_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    snapshot_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    proof_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    input_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    authorization_ref: str = Field(min_length=1, max_length=256)
    original_expires_at: AwareDatetime
    previous_expires_at: AwareDatetime
    expires_at: AwareDatetime
    created_at: AwareDatetime


async def extend_admission_quarantine(pool, principal: str, quarantine_ref: str, *, request_id: str,
                                      predecessor_ref: str, expected_expires_at: datetime,
                                      expires_at: datetime, authorization_ref: str) -> str:
    """Caller lock 下で原証拠を再検証し、期限延長の監査を一件だけ追加する。

    原監査・停止状態・writer・予算は変更しない。既に切れた期限も明示承認で延長できるが、
    新期限は未来でなければならず、過去の返答の replay は新しい延長を作らない。
    """
    try:
        request_id = str(UUID(request_id))
    except (ValueError, TypeError) as exc:
        raise ExecutionError('request_conflict', 'Extension requires a request UUID', 409) from exc
    if expected_expires_at.tzinfo is None or expires_at.tzinfo is None or expires_at <= expected_expires_at:
        raise ExecutionError('request_conflict', 'Extension requires ordered aware deadlines', 409)
    inputs = dict(request_id=request_id, quarantine_ref=quarantine_ref, predecessor_ref=predecessor_ref,
                  expected_expires_at=expected_expires_at.astimezone(timezone.utc).isoformat(),
                  expires_at=expires_at.astimezone(timezone.utc).isoformat(), authorization_ref=authorization_ref)
    input_digest = content_digest(inputs)
    ref = 'quarantine-extension:' + request_id
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', 'kotoyose-caller:' + principal)
        existing = await conn.fetchrow('SELECT principal,quarantine_ref,payload FROM kotoyose.execution_admission_quarantine_extensions WHERE id=$1', ref)
        if existing:
            saved = AdministrativeQuarantineExtension.model_validate_json(existing['payload'])
            if existing['principal'] != principal or existing['quarantine_ref'] != quarantine_ref or saved.input_digest != input_digest:
                raise ExecutionError('request_conflict', 'Extension request is bound to different data', 409)
            return ref
        if await conn.fetchval('SELECT clock_timestamp()') >= expires_at:
            raise ExecutionError('request_conflict', 'Extension expired while waiting for caller lock', 409)
        base = await conn.fetchrow('SELECT id,principal,attempt_id,payload,revoked_at FROM kotoyose.execution_admission_quarantines WHERE id=$1', quarantine_ref)
        if base is None or base['principal'] != principal or base['revoked_at'] is not None:
            raise ExecutionError('request_conflict', 'Original quarantine is unavailable or revoked', 409)
        raw_base = json.loads(base['payload']) if isinstance(base['payload'], str) else dict(base['payload'])
        if quarantine_ref != 'quarantine:sha256:' + content_digest(raw_base):
            raise ExecutionError('request_conflict', 'Original quarantine audit changed', 409)
        sealed = AdministrativeAdmissionQuarantine.model_validate(raw_base)
        if base['attempt_id'] != sealed.attempt_id:
            raise ExecutionError('request_conflict', 'Original quarantine identity changed', 409)
        await locked_quarantine_target(conn, principal, sealed.attempt_id, request_digest=sealed.request_digest,
                                       snapshot_digest=sealed.snapshot_digest, proof_digest=sealed.proof_digest)
        previous = await conn.fetchrow("SELECT id,payload FROM kotoyose.execution_admission_quarantine_extensions WHERE quarantine_ref=$1 "
                                       "ORDER BY (payload->>'expires_at')::timestamptz DESC LIMIT 1", quarantine_ref)
        last_ref = previous['id'] if previous else quarantine_ref
        last_expiry = AdministrativeQuarantineExtension.model_validate_json(previous['payload']).expires_at if previous else sealed.expires_at
        if predecessor_ref != last_ref or expected_expires_at != last_expiry:
            raise ExecutionError('request_conflict', 'Extension predecessor or expected expiry is stale', 409)
        if await conn.fetchval('SELECT EXISTS(SELECT 1 FROM kotoyose.execution_attempts WHERE principal=$1 AND finished_at IS NULL)', principal):
            raise ExecutionError('request_conflict', 'Extension requires an idle caller', 409)
        if await conn.fetchval('SELECT clock_timestamp()') >= expires_at:
            raise ExecutionError('request_conflict', 'Extension expired before sealing', 409)
        payload = AdministrativeQuarantineExtension(
            request_id=request_id, quarantine_ref=quarantine_ref, predecessor_ref=predecessor_ref,
            attempt_id=sealed.attempt_id, request_digest=sealed.request_digest, snapshot_digest=sealed.snapshot_digest,
            proof_digest=sealed.proof_digest, input_digest=input_digest, authorization_ref=authorization_ref,
            original_expires_at=sealed.expires_at, previous_expires_at=last_expiry, expires_at=expires_at, created_at=datetime.now(timezone.utc),
        ).model_dump(mode='json')
        await conn.execute('INSERT INTO kotoyose.execution_admission_quarantine_extensions(id,quarantine_ref,principal,predecessor_ref,payload) VALUES($1,$2,$3,$4,$5::jsonb)',
                           ref, quarantine_ref, principal, predecessor_ref, canonical_json(payload))
        return ref
