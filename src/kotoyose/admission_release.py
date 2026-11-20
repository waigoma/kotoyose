"""停止不明を受け入れる人の裁定と、受付枠の計上を分離する管理専用処理。"""

from collections.abc import Sequence
from datetime import datetime, timezone
import json
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .execution_resources import ExecutionError, canonical_json, content_digest


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kotoyose.execution_admission_releases (
    id text PRIMARY KEY, principal text NOT NULL, attempt_id text NOT NULL,
    payload jsonb NOT NULL, revoked_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(principal, attempt_id)
);
"""


async def initialize_admission_releases(pool) -> None:
    """元の停止台帳を変更しない、独立した不変の管理監査を準備する。"""
    await pool.execute(_SCHEMA)


class AdmissionReleaseTarget(BaseModel):
    """人が解放を許可した Attempt の identity。現在の値と一致しなければ拒否する。"""

    model_config = ConfigDict(extra='forbid', strict=True, frozen=True)
    attempt_id: str = Field(pattern=r'^[0-9a-f-]{36}$')
    request_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    snapshot_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    workspace_id: str = Field(min_length=1)
    generation: int = Field(ge=0)


class AdministrativeAdmissionRelease(AdmissionReleaseTarget):
    """公開 resource ではない、停止不明を保持した受付予約の解放記録。"""

    schema_version: Literal['1'] = '1'
    kind: Literal['administrative_admission_release'] = 'administrative_admission_release'
    principal: str = Field(min_length=1)
    authorization_ref: str = Field(min_length=1, max_length=256)
    reason: Literal['human_accepts_unknown_stop'] = 'human_accepts_unknown_stop'
    original_status: str
    original_reason: str | None
    created_at: AwareDatetime


async def release_terminal_admissions(pool, principal: str, targets: Sequence[AdmissionReleaseTarget], *,
                                      authorization_ref: str) -> list[str]:
    """明示された終端だけを受付枠から除外する。停止・writer・予算は変更しない。

    人の裁定を持つ DB 管理者専用で、HTTP/MCP や自動復旧には公開しない。
    同じ Caller lock で admission と直列化し、対象の変更や一件の不備でも全件拒否する。
    """
    if (not targets or len({target.attempt_id for target in targets}) != len(targets)
            or not authorization_ref.strip() or len(authorization_ref) > 256):
        raise ExecutionError('invalid_request', 'Explicit targets and human authorization are required', 422)
    receipts, pending = {}, []
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))', 'kotoyose-caller:' + principal)
        for target in sorted(targets, key=lambda item: item.attempt_id):
            row = await conn.fetchrow("SELECT id AS attempt_id,request_digest,workspace_id,generation,finished_at,"
                "stop_confirmation,status,reason,encode(sha256(convert_to(snapshot::text,'UTF8')),'hex') AS snapshot_digest "
                "FROM kotoyose.execution_attempts WHERE principal=$1 AND id=$2 FOR UPDATE", principal, target.attempt_id)
            if row is None or any(row[key] != value for key, value in target.model_dump().items()):
                raise ExecutionError('request_conflict', 'Admission release target changed', 409)
            existing = await conn.fetchrow('SELECT id,payload,revoked_at FROM kotoyose.execution_admission_releases '
                                          'WHERE principal=$1 AND attempt_id=$2', principal, target.attempt_id)
            if existing:
                raw = json.loads(existing['payload']) if isinstance(existing['payload'], str) else existing['payload']
                record = AdministrativeAdmissionRelease.model_validate_json(canonical_json(raw))
                if (existing['revoked_at'] is not None or existing['id'] != 'admission-release:sha256:' + content_digest(raw)
                        or record.authorization_ref != authorization_ref or record.principal != principal
                        or any(getattr(record, key) != value for key, value in target.model_dump().items())):
                    raise ExecutionError('request_conflict', 'Admission release audit is immutable or revoked', 409)
                receipts[target.attempt_id] = existing['id']
                continue
            if row['finished_at'] is None or row['stop_confirmation'] != 'unconfirmed':
                raise ExecutionError('request_conflict', 'Admission release requires a terminal unknown stop', 409)
            pending.append((target, row))
        if pending and await conn.fetchval('SELECT EXISTS(SELECT 1 FROM kotoyose.execution_attempts '
                                          'WHERE principal=$1 AND finished_at IS NULL)', principal):
            raise ExecutionError('request_conflict', 'Admission release requires an idle caller', 409)
        for target, row in pending:
            record = AdministrativeAdmissionRelease(**target.model_dump(), principal=principal,
                authorization_ref=authorization_ref, original_status=row['status'], original_reason=row['reason'],
                created_at=datetime.now(timezone.utc)).model_dump(mode='json')
            ref = 'admission-release:sha256:' + content_digest(record)
            await conn.execute('INSERT INTO kotoyose.execution_admission_releases(id,principal,attempt_id,payload) '
                               'VALUES($1,$2,$3,$4::jsonb)', ref, principal, target.attempt_id, canonical_json(record))
            receipts[target.attempt_id] = ref
    return [receipts[target.attempt_id] for target in targets]
