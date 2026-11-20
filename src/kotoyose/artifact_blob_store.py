"""Artifact の意味を保存 backend から分離する immutable bytes の境界。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


class ArtifactStorageError(Exception):
    """未保存・破損・保存先不一致を、成功 evidence として扱わせない。"""


class ArtifactMissingError(ArtifactStorageError):
    """backend が対象 key の不在を明示的に証明した場合だけ使う。"""


@dataclass(frozen=True, slots=True)
class ArtifactBlob:
    """認可済み Artifact の内容と、backend でも検証する metadata。"""

    key: str
    digest: str
    size: int
    media_type: str

    @classmethod
    def from_content(cls, ref: str, content: bytes, media_type: str) -> ArtifactBlob:
        """Caller やファイル名を storage path に含めずに保存 identity を導く。"""
        return cls(hashlib.sha256(ref.encode()).hexdigest(), hashlib.sha256(content).hexdigest(),
                   len(content), media_type)

    def verify(self, content: bytes) -> bytes:
        """同じ locator の応答でも、metadata と実 bytes が一致する場合だけ返す。"""
        if len(content) != self.size or hashlib.sha256(content).hexdigest() != self.digest:
            raise ArtifactStorageError("Artifact bytes do not match the saved digest and size")
        return content


class ArtifactBlobStore(Protocol):
    """認可は journal 側で行い、backend は保存済み identity の bytes だけを扱う。"""

    @property
    def identity(self) -> str:
        """接続先の変更を検出する、秘密情報を含まない設定 digest。"""
        ...

    async def put(self, blob: ArtifactBlob, content: bytes) -> None:
        """既存内容を上書きせず、同内容の再送を read-back で照合する。"""
        ...

    async def read(self, blob: ArtifactBlob) -> bytes:
        """保存場所に依存する内部 URL や credential を公開せずに取得する。"""
        ...

    async def delete(self, blob: ArtifactBlob) -> None:
        """GC が immutable key の bytes を削除する。"""
        ...


class ArtifactBlobRegistry:
    """過去の backend を残したまま、新規保存先を明示的に変更する。"""

    def __init__(self, default: str, backends: dict[str, ArtifactBlobStore]):
        if default != "postgres" and default not in backends:
            raise ValueError("Default artifact backend is not configured")
        if "postgres" in backends:
            raise ValueError("postgres is reserved for transaction-owned inline bytes")
        self.default = default
        self.backends = dict(backends)

    def resolve(self, name: str, identity: str | None = None) -> ArtifactBlobStore:
        """未知の backend や同名設定の差替えを、別保存先へ fallback させない。"""
        backend = self.backends.get(name)
        if backend is None or (identity is not None and backend.identity != identity):
            raise ArtifactStorageError("Saved artifact backend is unavailable or changed")
        return backend
