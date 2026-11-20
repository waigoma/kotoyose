"""Workspace 本体の保持判断と、削除前に Caller が選ぶ回復方針。"""

from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import Field, field_validator

from .execution_contract import ContractModel, Ref


class WorkspaceLimits(ContractModel):
    """未設定の寿命を推定しない。filesystem GC は管理者の明示設定で起動する。"""

    max_lifetime_seconds: int = Field(default=3650 * 86400, ge=1, le=36500 * 86400)
    max_pins: int = Field(default=128, ge=1, le=1024)
    gc_interval_seconds: int | None = Field(default=None, ge=60, le=86400)
    gc_batch_size: int = Field(default=20, ge=1, le=100)


class DiscardWorkspace(ContractModel):
    """Caller が workspace を回復に使わないと明示した根拠。"""

    mode: Literal['discard']
    decision_ref: Ref


class RecoverFromCheckpoint(ContractModel):
    """同じ現在世代の checkpoint で保持する選択範囲を Caller が確認する。"""

    mode: Literal['checkpoint']
    checkpoint_ref: Ref
    acknowledged_coverage: Literal['selected_files']


class WorkspaceRetentionChange(ContractModel):
    """policy と世代を CAS で固定する。古い世代の保持判断で新しい作業内容を消さない。"""

    schema_version: Literal['1']
    request_id: Ref
    expected_version: int = Field(strict=True, ge=0)
    workspace_ref: Ref
    expires_at: datetime
    pins: list[Ref] = Field(max_length=1024)
    proof_ref: Ref
    recovery: Annotated[DiscardWorkspace | RecoverFromCheckpoint, Field(discriminator='mode')]
    acknowledged_required_actions: list[Ref] = Field(default_factory=list, max_length=1024)

    @field_validator('expires_at')
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        """過去時刻は明示 GC として許可するが、timezone の推定はしない。"""
        if value.tzinfo is None:
            raise ValueError('Workspace expiry requires a timezone')
        return value.astimezone(timezone.utc)

    @field_validator('pins', 'acknowledged_required_actions')
    @classmethod
    def distinct_refs(cls, values: list[str]) -> list[str]:
        """同じ参照の重複を拒み、再送比較の順序を固定する。"""
        if len(values) != len(set(values)):
            raise ValueError('Workspace retention references must be distinct')
        return sorted(values)
