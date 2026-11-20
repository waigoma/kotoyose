"""Native image input の upload 契約と、bounded な登録上限。"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import binascii
import hashlib
from typing import Literal

from pydantic import Field, field_validator

from .execution_contract import ContractModel


ImageMediaType = Literal["image/png", "image/jpeg", "image/gif", "image/webp"]


class InputArtifactLimits(ContractModel):
    """管理者が固定する Caller 単位の input Artifact 保存上限。"""

    max_bytes: int = Field(default=20 * 1024 * 1024, ge=1, le=20 * 1024 * 1024)
    # 画像 1 request の枚数 / 合計 bytes は execution_images 側で制限する。
    # ここは複数 Attempt をまたぐ、Caller 単位の保存 quota である。
    max_count: int = Field(default=256, ge=1, le=10_000)
    max_total_bytes: int = Field(default=512 * 1024 * 1024, ge=1, le=20 * 1024 * 1024 * 1024)
    max_lifetime_seconds: int = Field(default=7 * 24 * 60 * 60, ge=1, le=90 * 24 * 60 * 60)
    allow_retention_pin: bool = False
    gc_interval_seconds: int | None = Field(default=None, ge=60, le=24 * 60 * 60)
    gc_batch_size: int = Field(default=100, ge=1, le=1000)


class InputArtifactUpload(ContractModel):
    """URL fetch を行わず、base64 bytes だけを受け付ける upload envelope。"""

    schema_version: Literal["1"]
    media_type: ImageMediaType
    data_base64: str = Field(min_length=1, max_length=28 * 1024 * 1024)
    expires_at: datetime
    retention_pin: bool = False

    @field_validator("expires_at")
    @classmethod
    def future_expiry(cls, value: datetime) -> datetime:
        """期限なしの不変 bytes を登録させない。"""
        if value.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        if value <= datetime.now(timezone.utc):
            raise ValueError("expires_at must be in the future")
        return value


class InputArtifactError(ValueError):
    """upload bytes が immutable image input 契約を満たさない。"""


def decode_input_upload(upload: InputArtifactUpload, limits: InputArtifactLimits) -> tuple[bytes, str]:
    """base64、size、実 signature を bounded に検証する。"""
    from .execution_images import image_signature_valid

    if len(upload.data_base64) > 4 * ((limits.max_bytes + 2) // 3) + 4:
        raise InputArtifactError("input artifact exceeds the byte limit")
    try:
        content = base64.b64decode(upload.data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InputArtifactError("data_base64 is invalid") from exc
    if not content or len(content) > limits.max_bytes:
        raise InputArtifactError("input artifact exceeds the byte limit")
    if not image_signature_valid(upload.media_type, content):
        raise InputArtifactError("image signature does not match media type")
    return content, hashlib.sha256(content).hexdigest()


def input_artifact_ref(
    principal: str,
    digest: str,
    media_type: str = "image/png",
    expires_at: datetime | None = None,
    retention_pin: bool = False,
) -> str:
    """bytes と upload metadata に束縛した opaque ref を作る。

    digest は ref の末尾に残し、opaque 部分へ principal と全 upload metadata を含める。
    これにより期限や retention policy が異なる upload は古い ref を復活させない。
    """
    expiry = expires_at.isoformat() if expires_at is not None else ""
    identity = f"{principal}:{digest}:{media_type}:{expiry}:{int(retention_pin)}"
    opaque = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    return f"artifact:input:{opaque}:{digest}"
