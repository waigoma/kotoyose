"""互換 client の既定 Grant を管理者の明示設定から組み立てる。"""

from datetime import datetime, timedelta
import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field, field_validator, model_serializer, model_validator

from .execution_contract import ContractModel
from .execution_resources import ExecutionError, TimeoutPolicy

if TYPE_CHECKING:
    from .config import ModelPreset


class CompatibilityProfile(ContractModel):
    """公開 model alias と固定 Native profile / workspace / 制限を結び付ける。"""

    runtime_profile: str | None = Field(default=None, min_length=1)
    workspace_path: Path
    caller_workspace_ref: str = Field(min_length=1)
    policy_ref: str = Field(min_length=1)
    policy_version: int = Field(default=1, ge=1)
    expires_at: datetime | None = None
    # Operator-owned template; each first admission still issues a finite Grant.
    expires_after_seconds: int | None = Field(default=None, strict=True, ge=1, le=86400)
    instructions: str = Field(min_length=1, max_length=65536)
    accept_system_instructions: bool = False
    tool_refs: list[Literal["builtin:sandbox-exec@1"]] = Field(default_factory=list)
    # chat の 1 往復が収まる長さにし、Caller policy の上限は admission が別に検査する。
    max_turns: int = Field(default=10, ge=1, le=1000)
    timeout: TimeoutPolicy = Field(default_factory=lambda: TimeoutPolicy(
        schema_version="1", kind="timeout_policy", soft_seconds=120, hard_seconds=300, grace_seconds=10,
    ))
    memory_bytes: int = Field(default=512 * 1024 * 1024, ge=16 * 1024 * 1024)
    pids_limit: int = Field(default=128, ge=1)
    cpus: float = Field(default=1, gt=0, allow_inf_nan=False)
    output_limit_bytes: int = Field(default=1024 * 1024, ge=1024, le=8 * 1024 * 1024)

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime | None) -> datetime | None:
        """期限を host の timezone に依存させない。"""
        if value is not None and value.utcoffset() is None:
            raise ValueError("expires_at must include a timezone")
        return value

    @model_validator(mode="after")
    def one_expiry_mode(self):
        if (self.expires_at is None) == (self.expires_after_seconds is None):
            raise ValueError("exactly one of expires_at or expires_after_seconds is required")
        return self

    @model_serializer(mode="wrap")
    def serialize_expiry_mode(self, handler):
        # Old absolute profiles must retain their canonical request digest on upgrade.
        # Do not use exclude_none globally: explicit null fields are part of that digest.
        value = handler(self)
        if self.expires_after_seconds is None:
            value.pop("expires_after_seconds", None)
        if self.expires_at is None:
            value.pop("expires_at", None)
        return value

    def execution_deadline(self, now: datetime) -> datetime:
        """Only first preparation calls this; replay uses the persisted execution."""
        expiry = self.expires_at
        if expiry is None:
            expiry = now + timedelta(seconds=self.expires_after_seconds)
        return min(expiry, now + timedelta(seconds=self.timeout.hard_seconds))

    @model_validator(mode="after")
    def absolute_workspace(self):
        """管理者が指定した既存 workspace だけを Native admission へ渡す。"""
        if not self.workspace_path.is_absolute():
            raise ValueError("workspace_path must be absolute")
        return self


class NativeAliasSettings(CompatibilityProfile):
    """共通の Native 制限に、alias を利用できる principal の一覧を加える。"""

    callers: list[Annotated[str, Field(min_length=1, pattern=r"\S")]] = Field(min_length=1)

    def profile(self) -> CompatibilityProfile:
        """Caller ごとの adapter 設定へ、同じ検証契約のまま投影する。"""
        return CompatibilityProfile.model_validate(self.model_dump(exclude={"callers"}))


class CompatibilityConfig(ContractModel):
    """principal ごとの公開 alias。client は default Grant の内容を変更できない。"""

    schema_version: Literal["1"]
    callers: dict[str, dict[str, CompatibilityProfile]]

    def has_model(self, model: str) -> bool:
        """他 Caller 専用の alias も Native と判定し、direct chat への迂回を防ぐ。"""
        return any(model in profiles for profiles in self.callers.values())

    def profile(self, principal: str, model: str) -> CompatibilityProfile:
        """認証済み主体の alias だけを解決し、名前による routing を行わない。"""
        profile = self.callers.get(principal, {}).get(model)
        if profile is None:
            raise ExecutionError("permission_denied", "Model is not configured for this Caller", 403)
        return profile


def load_compatibility_config(models: "dict[str, ModelPreset]") -> CompatibilityConfig:
    """config.yml の Native alias に移行用 JSON を合成し、名前衝突を拒否する。"""
    config = CompatibilityConfig(schema_version="1", callers={})
    filename = os.environ.get("KOTOYOSE_COMPATIBILITY_CONFIG")
    if filename:
        try:
            config = CompatibilityConfig.model_validate_json(Path(filename).read_bytes())
        except (OSError, ValueError) as error:
            raise ExecutionError("invalid_configuration", "Native chat defaults are invalid", 503) from error
        duplicates = sorted(alias for alias in models if config.has_model(alias))
        if duplicates:
            raise ValueError(
                "Duplicate model aliases in config.yml and KOTOYOSE_COMPATIBILITY_CONFIG: "
                + ", ".join(duplicates)
            )
    for alias, preset in models.items():
        if preset.execution == "native" and preset.native is not None:
            for principal in preset.native.callers:
                config.callers.setdefault(principal, {})[alias] = preset.native.profile()
    return config
