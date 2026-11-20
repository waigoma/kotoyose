"""停止後に開いた fd を保持し、Artifact を全量展開せず検証・保存する。"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import BinaryIO, Iterator

CHUNK_BYTES = 1024 * 1024


class ArtifactFileError(ValueError):
    """収集中の変更や上限超過。元の workspace ファイルは変更しない。"""


@dataclass
class ArtifactFile:
    """controller が所有する read-only fd。利用後は close が必要。"""

    stream: BinaryIO
    size: int
    digest: str
    identity: tuple[int, int, int, int]

    @classmethod
    def capture(cls, fd: int, limit: int) -> ArtifactFile:
        """fd を複製し、有限chunkでhashを計算する。失敗時は複製だけ閉じる。"""
        stream = os.fdopen(os.dup(fd), "rb", buffering=0)
        before = os.fstat(fd)
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        item = cls(stream, before.st_size, "", identity)
        try:
            if item.size > limit:
                raise ArtifactFileError("artifact exceeds byte limit")
            digest = hashlib.sha256()
            size = 0
            for chunk in item.chunks():
                size += len(chunk)
                if size > limit:
                    raise ArtifactFileError("artifact exceeds byte limit")
                digest.update(chunk)
            item.digest = digest.hexdigest()
            return item
        except BaseException:
            item.close()
            raise

    def chunks(self) -> Iterator[bytes]:
        """同じ fd を再検証して読む。path の再解決や全量 buffer は使わない。"""
        self.stream.seek(0)
        size = 0
        while chunk := self.stream.read(CHUNK_BYTES):
            size += len(chunk)
            if size > self.size:
                raise ArtifactFileError("artifact changed while being read")
            yield chunk
        after = os.fstat(self.stream.fileno())
        if size != self.size or self.identity != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise ArtifactFileError("artifact changed while being read")

    def close(self) -> None:
        """元ファイルを削除せず、controller の fd だけ解放する。"""
        self.stream.close()
