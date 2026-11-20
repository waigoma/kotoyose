"""Remote Worker transport の strict wire contract。"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RemoteContract(BaseModel):
    """未知 field を黙って落とさない transport model。"""

    model_config = ConfigDict(extra="forbid")


def read_workspace_challenge(workspace: Path, relative: str, *, max_bytes: int = 4096) -> bytes:
    """共有 binding nonce を symlink を追わず、固定上限と inode 再照合付きで読む。"""
    if max_bytes <= 0:
        raise ValueError("challenge size limit must be positive")
    candidate = workspace / relative
    resolved = candidate.resolve(strict=True)
    if resolved != candidate or not resolved.is_relative_to(workspace.resolve(strict=True)):
        raise ValueError("workspace challenge must not use symlinks or escape workspace")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(candidate, flags)
    try:
        # open 前後の canonical path を比較し、ancestor 差替えを成功扱いにしない。
        if candidate.resolve(strict=True) != resolved:
            raise ValueError("workspace challenge path changed")
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise ValueError("workspace challenge must be a bounded regular file")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(fd, min(4096, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        data = b"".join(chunks)
        if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, len(data)):
            raise ValueError("workspace challenge changed while reading")
        if not data:
            raise ValueError("workspace challenge must not be empty")
        if len(data) > max_bytes:
            raise ValueError("workspace challenge is too large")
        return data
    finally:
        os.close(fd)


class RemoteWorkerRef(RemoteContract):
    """enrollment 済み worker と gateway principal の identity。"""

    worker_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    generation: int = Field(ge=1)
    principal: str = Field(min_length=1, max_length=256)
    certificate_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    server_certificate_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RemoteCommandRequest(RemoteContract):
    """path を含めず、登録済み workspace ref だけで実行を指定する。"""

    schema_version: Literal["1"] = "1"
    operation_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    worker: RemoteWorkerRef
    workspace_ref: str = Field(min_length=1, max_length=512)
    workspace_generation: int = Field(ge=0)
    argv: tuple[str, ...] = Field(min_length=1, max_length=1024)
    cwd: str = Field(min_length=1, max_length=4096)
    env: dict[str, str] = Field(default_factory=dict, max_length=128)
    timeout_seconds: float = Field(gt=0, le=86400)
    grace_seconds: float = Field(ge=0, le=60)
    output_limit_bytes: int = Field(gt=0, le=64 * 1024 * 1024)
    memory_bytes: int = Field(gt=0, le=64 * 1024 * 1024 * 1024)
    pids_limit: int = Field(gt=0, le=100000)
    cpus: float = Field(gt=0, le=256)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any("\x00" in item for item in value):
            raise ValueError("argv must not contain NUL")
        return value

    @field_validator("cwd")
    @classmethod
    def validate_cwd(cls, value: str) -> str:
        from pathlib import Path

        path = Path(value)
        if path.is_absolute() or ".." in path.parts or "\x00" in value:
            raise ValueError("cwd must be workspace-relative")
        return value

    @field_validator("env")
    @classmethod
    def validate_env(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not key or not key.replace("_", "a").isalnum() or not (key[0].isalpha() or key[0] == "_"):
                raise ValueError("invalid environment key")
            if key in {"HOME", "PATH", "DOCKER_HOST", "DOCKER_CONTEXT"} or "\x00" in item:
                raise ValueError("reserved or invalid environment key")
        return value

    def digest_payload(self) -> dict[str, Any]:
        """request digest の対象から自身の digest を除く。"""
        payload = self.model_dump(mode="json")
        payload.pop("request_digest", None)
        return payload


class RemoteCommandResult(RemoteContract):
    """Docker worker の結果と停止確認。"""

    schema_version: Literal["1"] = "1"
    operation_id: str = Field(min_length=1)
    status: Literal["succeeded", "failed", "cancelled", "timeout", "worker_lost"]
    exit_code: int | None
    stdout: str
    stderr: str
    stop_confirmation: Literal["confirmed", "unconfirmed"]
    reason: str = Field(max_length=4096)


class RemoteOperationState(RemoteContract):
    """再接続時に同一 operation を照合する durable response。"""

    schema_version: Literal["1"] = "1"
    operation_id: str = Field(min_length=1)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["prepared", "running", "unknown", "confirmed"]
    result: RemoteCommandResult | None = None


class RemoteStopRequest(RemoteContract):
    schema_version: Literal["1"] = "1"
    operation_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    generation: int = Field(ge=0)
    worker: RemoteWorkerRef
    grace_seconds: float = Field(ge=0, le=60)


class RemoteStopResult(RemoteContract):
    schema_version: Literal["1"] = "1"
    operation_id: str = Field(min_length=1)
    stop_confirmation: Literal["confirmed", "unconfirmed"]


def request_digest(request: RemoteCommandRequest) -> str:
    """canonical JSON の SHA-256 を返す。"""
    payload = json.dumps(request.digest_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
