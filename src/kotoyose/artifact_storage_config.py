"""Worker や request から変更できない ArtifactStore の管理者設定。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .execution_contract import ContractModel


class LocalArtifactStorageConfig(ContractModel):
    """workspace と分離した、controller 所有の非公開 root。"""

    kind: Literal["local_fs"]
    root: Path


class S3ArtifactStorageConfig(ContractModel):
    """S3 / MinIO の接続先と credential の環境変数名だけを固定する。"""

    kind: Literal["s3"]
    endpoint: str
    bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    prefix: str = Field(default="kotoyose-artifacts", pattern=r"^[A-Za-z0-9_-]+$")
    region: str = Field(default="us-east-1", min_length=1)
    access_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    secret_key_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    allow_insecure_http: bool = False
    request_timeout_seconds: int = Field(default=30, ge=1, le=120)

    @model_validator(mode="after")
    def trusted_endpoint(self) -> S3ArtifactStorageConfig:
        """credential を含む URL、query、暗黙の非 TLS 接続を拒否する。"""
        url = urlsplit(self.endpoint)
        if (url.scheme not in {"https", "http"} or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment or url.path not in {"", "/"}
                or (url.scheme == "http" and not self.allow_insecure_http)):
            raise ValueError("S3 requires a trusted origin; HTTP requires explicit opt-in")
        self.endpoint = self.endpoint.rstrip("/")
        return self


ArtifactBackendConfig = Annotated[
    LocalArtifactStorageConfig | S3ArtifactStorageConfig, Field(discriminator="kind"),
]


class ArtifactStorageConfig(ContractModel):
    """既存 Postgres bytes の可読性を保つ、保存先の明示 registry。"""

    default_backend: str = "postgres"
    backends: dict[str, ArtifactBackendConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def configured_backend(self) -> ArtifactStorageConfig:
        """設定漏れを起動前に拒否し、Postgres への暗黙 fallback を作らない。"""
        if "postgres" in self.backends:
            raise ValueError("postgres is reserved")
        if self.default_backend != "postgres" and self.default_backend not in self.backends:
            raise ValueError("Default artifact backend must be configured")
        return self


class ArtifactDiskLimits(ContractModel):
    """明示有効化したlocal保存だけの容量枠。会話出力や通信上限へ転用しない。"""

    max_attempt_bytes: int = Field(default=50_000_000_000, ge=1, le=50_000_000_000)
    minimum_free_bytes: int = Field(default=5_000_000_000, ge=0)
