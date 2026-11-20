"""実行終了後に workspace から安全に artifact を収集する契約と collector。"""

from __future__ import annotations

import hashlib
import os
import stat
from contextlib import contextmanager
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Mapping

from pydantic import ConfigDict, Field, field_validator, model_validator

from .execution_contract import ContractModel
from .artifact_media import validate_artifact_media_type
from .artifact_file import ArtifactFile, ArtifactFileError

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_DISK_BYTES = 50_000_000_000
_CHUNK_SIZE = 1024 * 1024


class ArtifactCaptureError(ValueError):
    """artifact 収集の filesystem identity または安全境界に反する要求。"""


class ArtifactSpecEntry(ContractModel):
    """一つの workspace 相対 artifact の収集条件。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    path: str = Field(min_length=1, max_length=4096)
    media_type: str

    @field_validator("media_type")
    @classmethod
    def validate_media_type(cls, value: str) -> str:
        return validate_artifact_media_type(value)

    required: bool = False
    max_bytes: int = Field(ge=1, le=MAX_DISK_BYTES)

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        """絶対 path、NUL、dot segment、portable boundary を拒否する。"""
        if "\0" in value or "\\" in value or value.startswith("/"):
            raise ValueError("artifact path must be workspace-relative")
        parts = value.split("/")
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("artifact path contains an unsafe segment")
        return value


class ArtifactCaptureSpec(ContractModel):
    """終端後に保存する artifact の固定仕様。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"]
    kind: Literal["artifact_spec"]
    entries: list[ArtifactSpecEntry] = Field(min_length=1, max_length=128)
    max_total_bytes: int = Field(ge=1, le=MAX_DISK_BYTES)

    @model_validator(mode="after")
    def validate_unique_entries(self) -> "ArtifactCaptureSpec":
        """同じ保存先を複数の意味で収集しない。"""
        names = [entry.name for entry in self.entries]
        paths = [entry.path for entry in self.entries]
        if len(names) != len(set(names)):
            raise ValueError("artifact names must be unique")
        if len(paths) != len(set(paths)):
            raise ValueError("artifact paths must be unique")
        return self


@dataclass(frozen=True)
class CapturedArtifact:
    """収集済み artifact の immutable payload。"""

    content: bytes | ArtifactFile
    media_type: str


@dataclass(frozen=True)
class CaptureManifestEntry:
    """artifact ごとの収集結果。"""

    name: str
    state: Literal["captured", "missing", "error"]
    reason: str | None = None
    digest: str | None = None


@dataclass(frozen=True)
class ArtifactCaptureResult:
    """収集 payload と required 契約の判定結果。"""

    artifacts: Mapping[str, CapturedArtifact]
    manifest: tuple[CaptureManifestEntry, ...]
    contract_valid: bool


def collect_execution_artifacts(
    workspace: str | os.PathLike[str],
    expected_dev: int,
    expected_inode: int,
    spec: ArtifactCaptureSpec,
    *,
    reader: Callable[[int, str, int], bytes | ArtifactFile] | None = None,
) -> ArtifactCaptureResult:
    """root fd を基準に artifact を bounded read し、欠落・異常を manifest に残す。

    workspace path の再解決や glob は行わない。required artifact が欠落・異常なら
    `contract_valid=False` とし、呼出元が成功扱いしないようにする。
    """
    if not isinstance(spec, ArtifactCaptureSpec):
        raise ArtifactCaptureError("artifact spec is invalid")
    if not isinstance(expected_dev, int) or not isinstance(expected_inode, int):
        raise ArtifactCaptureError("workspace identity is required")
    root_fd = _open_root(workspace, expected_dev, expected_inode)
    read_entry = reader or _read_entry
    artifacts: dict[str, CapturedArtifact] = {}
    manifest: list[CaptureManifestEntry] = []
    total_bytes = 0
    try:
        for entry in spec.entries:
            remaining = spec.max_total_bytes - total_bytes
            if remaining <= 0:
                manifest.append(
                    CaptureManifestEntry(entry.name, "error", "aggregate byte limit exceeded")
                )
                continue
            try:
                content = read_entry(root_fd, entry.path, min(entry.max_bytes, remaining))
            except FileNotFoundError:
                state: Literal["missing", "error"] = "missing"
                manifest.append(CaptureManifestEntry(entry.name, state, "artifact is absent"))
                continue
            except (ArtifactCaptureError, ArtifactFileError, OSError) as exc:
                manifest.append(
                    CaptureManifestEntry(entry.name, "error", _safe_reason(exc))
                )
                continue
            digest = content.digest if isinstance(content, ArtifactFile) else hashlib.sha256(content).hexdigest()
            artifacts[entry.name] = CapturedArtifact(content, entry.media_type)
            manifest.append(CaptureManifestEntry(entry.name, "captured", digest=digest))
            total_bytes += content.size if isinstance(content, ArtifactFile) else len(content)
        _verify_root_identity(root_fd, expected_dev, expected_inode)
    except BaseException:
        for artifact in artifacts.values():
            if isinstance(artifact.content, ArtifactFile):
                artifact.content.close()
        raise
    finally:
        os.close(root_fd)
    contract_valid = all(
        item.state == "captured"
        or (item.state == "missing" and not next(entry for entry in spec.entries if entry.name == item.name).required)
        for item in manifest
    )
    return ArtifactCaptureResult(artifacts, tuple(manifest), contract_valid)


def _open_root(workspace: str | os.PathLike[str], expected_dev: int, expected_inode: int) -> int:
    try:
        fd = os.open(
            os.fspath(workspace),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as exc:
        raise ArtifactCaptureError("workspace root is not a safe directory") from exc
    try:
        _verify_root_identity(fd, expected_dev, expected_inode)
    except Exception:
        os.close(fd)
        raise
    return fd


def _verify_root_identity(fd: int, expected_dev: int, expected_inode: int) -> None:
    identity = os.fstat(fd)
    if identity.st_dev != expected_dev or identity.st_ino != expected_inode:
        raise ArtifactCaptureError("workspace identity changed")


@contextmanager
def open_artifact_entry(root_fd: int, relative_path: str, max_bytes: int):
    """既存の root fd 境界を保ち、通常ファイルだけを開く。fd は scope 終了で閉じる。"""
    parts = relative_path.split("/")
    parent_fd = root_fd
    opened: list[int] = []
    try:
        for component in parts[:-1]:
            parent_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            opened.append(parent_fd)
        try:
            fd = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            raise
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise ArtifactCaptureError("artifact is not a regular file")
            if before.st_nlink != 1:
                raise ArtifactCaptureError("hardlinked artifacts are not allowed")
            if before.st_size > max_bytes:
                raise ArtifactCaptureError("artifact exceeds byte limit")
            yield fd
        finally:
            os.close(fd)
    finally:
        for fd in reversed(opened):
            os.close(fd)


def read_artifact_file(root_fd: int, relative_path: str, max_bytes: int) -> ArtifactFile:
    """stream保存用の検証済みfdを返す。呼出元がcloseする。"""
    with open_artifact_entry(root_fd, relative_path, max_bytes) as fd:
        return ArtifactFile.capture(fd, max_bytes)


def _read_entry(root_fd: int, relative_path: str, max_bytes: int) -> bytes:
    if max_bytes > MAX_ARTIFACT_BYTES:
        raise ArtifactCaptureError("large artifacts require streaming storage")
    item = read_artifact_file(root_fd, relative_path, max_bytes)
    try:
        return b"".join(item.chunks())
    finally:
        item.close()


def _safe_reason(exc: BaseException) -> str:
    if isinstance(exc, FileNotFoundError):
        return "artifact is absent"
    if isinstance(exc, PermissionError):
        return "artifact permission denied"
    if isinstance(exc, ArtifactCaptureError):
        return str(exc)
    return "artifact could not be captured"
