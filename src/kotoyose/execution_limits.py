"""Attempt の追加観測契約と、旧 event cursor と独立した保存。"""

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from .execution_contract import ContractModel
from .execution_resources import ExecutionError


class ExecutionTime(ContractModel):
    """既知の稼働時間の下限。旧 Attempt のみ active_ms が null になる。"""
    state: Literal['not_started', 'running', 'ended', 'unmeasured', 'unknown']
    active_ms: int | None = Field(ge=0, strict=True)
    started_at: AwareDatetime | None
    observed_at: AwareDatetime
    finished_at: AwareDatetime | None

    @model_validator(mode='after')
    def validate_measurement(self):
        if (self.active_ms is None) != (self.state == 'unmeasured'):
            raise ValueError('active_ms is null only for unmeasured')
        return self


class ProviderUsageLimit(ContractModel):
    """Provider の利用枠と、その観測時に利用可能な再開経路。"""
    provider: Literal['codex', 'claude', 'print_cli']
    limit_window: Literal['five_hour', 'seven_day', 'seven_day_opus', 'seven_day_sonnet', 'overage', 'rate', 'unknown']
    reset_at: AwareDatetime | None
    detected_at: AwareDatetime
    resume: Literal['native', 'unavailable']
    unavailable_reason: Literal['no_session', 'not_retained', 'adapter_unsupported'] | None

class AgentResourceUsage(ContractModel):
    """観測できない出力量と turn 数は null。"""
    output_bytes: int | None = Field(ge=0, strict=True)
    turns: int | None = Field(ge=0, strict=True)


class AttemptLimits(ContractModel):
    """独立した sequence を持つ Attempt 単位の観測。"""
    schema_version: Literal['1']
    attempt_id: str
    sequence: int = Field(ge=0, strict=True)
    execution_time: ExecutionTime
    resource_usage: AgentResourceUsage
    provider_limit: ProviderUsageLimit | None


_SCHEMA = """
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS limits_state text;
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS limits_active_ms bigint;
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS limits_started_at timestamptz;
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS limits_observed_at timestamptz;
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS limits_finished_at timestamptz;
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS limits_output_bytes bigint;
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS limits_turns bigint;
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS limits_provider text;
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS limits_window text;
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS limits_reset_at timestamptz;
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS limits_detected_at timestamptz;
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS limits_resume text;
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS limits_unavailable_reason text;
ALTER TABLE kotoyose.execution_attempts ADD COLUMN IF NOT EXISTS limits_sequence bigint;
ALTER TABLE kotoyose.execution_attempts ALTER COLUMN limits_sequence DROP NOT NULL;
ALTER TABLE kotoyose.execution_attempts ALTER COLUMN limits_sequence DROP DEFAULT;
-- 許す値 (limits_provider など) を足したとき既存の表にも届くよう、毎回作り直す。
-- IF NOT EXISTS だと本番の古い CHECK が残り、新しい provider の記録が CheckViolation で落ちる。
ALTER TABLE kotoyose.execution_attempts DROP CONSTRAINT IF EXISTS execution_attempt_limits_valid;
DO $$ BEGIN
IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='kotoyose.execution_attempts'::regclass AND conname='execution_attempt_limits_valid') THEN
    ALTER TABLE kotoyose.execution_attempts ADD CONSTRAINT execution_attempt_limits_valid CHECK (
        (limits_sequence IS NULL OR limits_sequence >= 0) AND
        (limits_output_bytes IS NULL OR limits_output_bytes >= 0) AND
        (limits_turns IS NULL OR limits_turns >= 0) AND
        ((limits_state IS NULL AND limits_active_ms IS NULL AND limits_observed_at IS NULL
          AND limits_started_at IS NULL AND limits_finished_at IS NULL)
         OR (limits_state IS NOT NULL AND limits_state IN ('not_started','running','ended','unmeasured','unknown')
             AND limits_observed_at IS NOT NULL AND
             ((limits_state = 'unmeasured' AND limits_active_ms IS NULL)
              OR (limits_state <> 'unmeasured' AND limits_active_ms IS NOT NULL AND limits_active_ms >= 0)))) AND
        ((limits_provider IS NULL AND limits_window IS NULL AND limits_reset_at IS NULL AND limits_detected_at IS NULL
          AND limits_resume IS NULL AND limits_unavailable_reason IS NULL)
         OR (limits_provider IS NOT NULL AND limits_provider IN ('codex','claude','print_cli')
             AND limits_window IS NOT NULL AND limits_window IN ('five_hour','seven_day','seven_day_opus','seven_day_sonnet','overage','rate','unknown')
             AND limits_detected_at IS NOT NULL AND limits_resume IS NOT NULL
             AND limits_resume IN ('native','unavailable')
             AND (limits_unavailable_reason IS NULL OR
                  limits_unavailable_reason IN ('no_session','not_retained','adapter_unsupported'))))
    );
END IF;
END $$;
"""


def project_limits(row: dict) -> AttemptLimits:
    """認可済み row から内部情報を含まない観測だけを組み立てる。"""
    provider = None
    if row['limits_provider'] is not None:
        provider = ProviderUsageLimit(provider=row['limits_provider'], limit_window=row['limits_window'],
            reset_at=row['limits_reset_at'], detected_at=row['limits_detected_at'],
            resume=row['limits_resume'], unavailable_reason=row['limits_unavailable_reason'])
    return AttemptLimits(schema_version='1', attempt_id=row['id'], sequence=row['limits_sequence'] if row['limits_sequence'] is not None else 0,
        execution_time=ExecutionTime(state=row['limits_state'] or 'unmeasured',
            active_ms=row['limits_active_ms'], started_at=row['limits_started_at'],
            observed_at=row['limits_observed_at'] or row['created_at'], finished_at=row['limits_finished_at']),
        resource_usage=AgentResourceUsage(output_bytes=row['limits_output_bytes'], turns=row['limits_turns']),
        provider_limit=provider)


async def live_attempt_limits(store, executor, principal: str, attempt_id: str) -> AttemptLimits:
    """所有確認の後、同一プロセス内の時計を更新して観測を返す。"""
    row = await store.attempt(principal, attempt_id)
    tracker = executor.work_clocks.get(attempt_id) if executor is not None else None
    if tracker is not None and tracker.principal == principal:
        await tracker.checkpoint()
        row = await store.attempt(principal, attempt_id)
    return project_limits(row)


class LimitsRepository:
    """完全な観測を原子的に保存し、専用 sequence を増やす。"""
    def __init__(self, pool):
        self.pool = pool

    async def initialize(self):
        """既存行を計測済みと見なさずに nullable 列を追加する。"""
        await self.pool.execute(_SCHEMA)

    async def save_time(self, principal: str, attempt_id: str, observation: ExecutionTime) -> None:
        """時間だけ更新し、別の観測フィールドを上書きしない。"""
        await self.pool.execute("""UPDATE kotoyose.execution_attempts SET
            limits_state=$3, limits_active_ms=$4, limits_started_at=$5,
            limits_observed_at=$6, limits_finished_at=$7,
            limits_sequence=COALESCE(limits_sequence, 0)+1
            WHERE principal=$1 AND id=$2 AND limits_state IS NOT NULL
            AND NOT (limits_state='unknown' AND $3='not_started')
            AND reason IS DISTINCT FROM 'coordinator_restarted'""",
            principal, attempt_id, observation.state, observation.active_ms,
            observation.started_at, observation.observed_at, observation.finished_at)

    async def mark_unknown(self, attempt_id: str) -> None:
        await self.pool.execute("""UPDATE kotoyose.execution_attempts SET
            limits_state='unknown', limits_finished_at=NULL, limits_observed_at=now(),
            limits_sequence=COALESCE(limits_sequence, 0)+1
            WHERE id=$1 AND (limits_state='running' OR
                (limits_state='not_started' AND snapshot->>'kind'='agent' AND EXISTS (
                    SELECT 1 FROM kotoyose.execution_events e WHERE e.attempt_id=$1 AND e.type='running'
                )))""", attempt_id)

    async def save_result(self, principal: str, attempt_id: str, usage: AgentResourceUsage,
                          provider: ProviderUsageLimit | None, *, connection=None) -> None:
        """Persist terminal details without overwriting the independent work clock."""
        await (connection or self.pool).execute("""UPDATE kotoyose.execution_attempts SET
            limits_output_bytes=$3, limits_turns=$4, limits_provider=$5, limits_window=$6,
            limits_reset_at=$7, limits_detected_at=$8, limits_resume=$9,
            limits_unavailable_reason=$10, limits_sequence=COALESCE(limits_sequence, 0)+1
            WHERE principal=$1 AND id=$2 AND limits_state IS NOT NULL""",
            principal, attempt_id, usage.output_bytes, usage.turns, provider.provider if provider else None,
            provider.limit_window if provider else None, provider.reset_at if provider else None,
            provider.detected_at if provider else None, provider.resume if provider else None,
            provider.unavailable_reason if provider else None)

    async def save(self, principal: str, observation: AttemptLimits) -> int:
        """所有者の観測を保存し、新しい sequence を返す。対象外は 404。"""
        # Pydantic の model_construct や後からの代入を含め、書込境界で再検証する。
        observation = AttemptLimits.model_validate(observation.model_dump())
        time = observation.execution_time
        usage = observation.resource_usage
        provider = observation.provider_limit
        sequence = await self.pool.fetchval('''UPDATE kotoyose.execution_attempts SET
            limits_state=$3, limits_active_ms=$4, limits_started_at=$5, limits_observed_at=$6,
            limits_finished_at=$7, limits_output_bytes=$8, limits_turns=$9,
            limits_provider=$10, limits_window=$11, limits_reset_at=$12, limits_detected_at=$13,
            limits_resume=$14, limits_unavailable_reason=$15, limits_sequence=COALESCE(limits_sequence, 0)+1
            WHERE principal=$1 AND id=$2 RETURNING limits_sequence''',
            principal, observation.attempt_id, time.state, time.active_ms, time.started_at, time.observed_at,
            time.finished_at, usage.output_bytes, usage.turns, provider.provider if provider else None,
            provider.limit_window if provider else None, provider.reset_at if provider else None,
            provider.detected_at if provider else None, provider.resume if provider else None,
            provider.unavailable_reason if provider else None)
        if sequence is None:
            raise ExecutionError('invalid_request', 'Unknown attempt', 404)
        return sequence
