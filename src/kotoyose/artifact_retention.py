"""Caller が所有する保持判断と、runtime が強制する削除条件の契約。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .execution_contract import ContractModel, Ref


class OutputArtifactLimits(ContractModel):
    """GC は管理者が周期を明示した場合にだけ起動する。保持未決定の artifact は pin する。"""

    default_lifetime_seconds: int = Field(default=30 * 86400, ge=1, le=3650 * 86400)
    max_lifetime_seconds: int = Field(default=3650 * 86400, ge=1, le=36500 * 86400)
    max_pins: int = Field(default=128, ge=1, le=1024)
    gc_interval_seconds: int | None = Field(default=None, ge=60, le=86400)
    gc_batch_size: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode='after')
    def default_fits_limit(self) -> OutputArtifactLimits:
        """既定期限も管理者の上限を越えない。"""
        if self.default_lifetime_seconds > self.max_lifetime_seconds:
            raise ValueError('default artifact lifetime exceeds maximum')
        return self


class ArtifactRetentionChange(ContractModel):
    """同じ request ID の再送は同じ receipt を返す。pins の意味は認証済み Caller が所有する。"""

    schema_version: Literal['1']
    request_id: Ref
    expected_version: int = Field(ge=0)
    expires_at: datetime
    pins: list[Ref] = Field(max_length=1024)
    proof_ref: Ref
    acknowledged_required_action_ref: Ref | None = None

    @field_validator('expires_at')
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        """期限を timezone によって読み替えない。過去時刻は明示的な GC 要求として許可する。"""
        if value.tzinfo is None:
            raise ValueError('expires_at must include a timezone')
        return value.astimezone(timezone.utc)

    @field_validator('pins')
    @classmethod
    def distinct_pins(cls, value: list[str]) -> list[str]:
        """同じ保持理由の二重登録を避け、再送比較を順序に依存させない。"""
        if len(set(value)) != len(value):
            raise ValueError('pins must be distinct')
        return sorted(value)
