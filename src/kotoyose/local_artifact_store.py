"""Local FS に immutable Artifact を fsync / atomic create で保存する。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import threading
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from uuid import uuid4

from .artifact_blob_store import ArtifactBlob, ArtifactMissingError, ArtifactStorageError
from .artifact_file import ArtifactFile, ArtifactFileError, CHUNK_BYTES

MAX_INLINE_READ_BYTES = 64 * 1024 * 1024


class LocalArtifactStore:
    """root を private に固定し、ArtifactRef を filesystem path として解釈しない。"""

    def __init__(self, root: Path):
        if not root.is_absolute():
            raise ValueError("Artifact root must be absolute")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.resolve(strict=True) != root or root.stat().st_mode & 0o077:
            raise ValueError("Artifact root must be private and must not contain symlinks")
        self.root = root
        self._root_identity = (root.stat().st_dev, root.stat().st_ino)
        self._identity = hashlib.sha256(f"local_fs:1:{root}".encode()).hexdigest()
        self._write_lock = asyncio.Lock()

    @property
    def identity(self) -> str:
        """管理者が保存 root を差替えた場合、過去の locator を無効とする。"""
        return self._identity

    def _open_root(self, blob: ArtifactBlob) -> int:
        if re.fullmatch(r"[0-9a-f]{64}", blob.key) is None:
            raise ArtifactStorageError("Invalid artifact storage key")
        try:
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise ArtifactStorageError("Artifact storage root is unavailable") from exc
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != self._root_identity or info.st_mode & 0o077:
            os.close(fd)
            raise ArtifactStorageError("Artifact root identity or permissions changed")
        return fd

    def _read(self, blob: ArtifactBlob) -> bytes:
        if blob.size > MAX_INLINE_READ_BYTES:
            raise ArtifactStorageError("Large artifacts require streaming reads")
        root_fd = self._open_root(blob)
        try:
            fd = os.open(blob.key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                # root は controller 専用。並行 upload の atomic link が残る短時間も同じ bytes を読める。
                if not stat.S_ISREG(info.st_mode) or info.st_size != blob.size:
                    raise ArtifactStorageError("Artifact file identity or size is invalid")
                return blob.verify(stream.read(blob.size + 1))
        finally:
            os.close(root_fd)

    def _put(self, blob: ArtifactBlob, content: bytes) -> None:
        blob.verify(content)
        root_fd = self._open_root(blob)
        temporary = f".upload-{uuid4().hex}"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=root_fd)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, blob.key, src_dir_fd=root_fd, dst_dir_fd=root_fd,
                        follow_symlinks=False)
            except FileExistsError:
                pass
            os.unlink(temporary, dir_fd=root_fd)
            os.fsync(root_fd)
            self._read(blob)
        finally:
            try:
                os.unlink(temporary, dir_fd=root_fd)
            except FileNotFoundError:
                pass
            os.close(root_fd)

    async def put(self, blob: ArtifactBlob, content: bytes) -> None:
        """並行保存と再送を同じ immutable key に収束させる。"""
        try:
            async with self._write_lock:
                await asyncio.to_thread(self._put, blob, content)
        except OSError as exc:
            raise ArtifactStorageError("Local artifact could not be saved") from exc

    def check_capacity(self, required: int, minimum_free: int) -> None:
        """予約量と最低空き容量を検査する。削除や割当の変更は行わない。"""
        info = os.statvfs(self.root)
        if info.f_bavail * info.f_frsize < required + minimum_free:
            raise ArtifactStorageError("Artifact disk capacity is insufficient")

    def _put_chunks(self, blob: ArtifactBlob, chunks: Iterable[bytes], minimum_free: int,
                    cancelled: threading.Event) -> None:
        root_fd = self._open_root(blob)
        temporary = f".upload-{uuid4().hex}"
        try:
            self.check_capacity(blob.size, minimum_free)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
            digest, size = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as stream:
                for chunk in chunks:
                    if cancelled.is_set():
                        raise ArtifactStorageError("Artifact stream was cancelled")
                    size += len(chunk)
                    if size > blob.size:
                        raise ArtifactStorageError("Artifact stream exceeds saved size")
                    self.check_capacity(len(chunk), minimum_free)
                    digest.update(chunk)
                    stream.write(chunk)
                if cancelled.is_set():
                    raise ArtifactStorageError("Artifact stream was cancelled")
                if size != blob.size or digest.hexdigest() != blob.digest:
                    raise ArtifactStorageError("Artifact stream digest or size changed")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, blob.key, src_dir_fd=root_fd, dst_dir_fd=root_fd, follow_symlinks=False)
            except FileExistsError:
                # 再送で異なる bytes を同じ key に採用しない。全量展開せず既存内容を照合する。
                fd = os.open(blob.key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
                with os.fdopen(fd, "rb") as existing:
                    if not stat.S_ISREG(os.fstat(existing.fileno()).st_mode):
                        raise ArtifactStorageError("Artifact file is not regular")
                    actual, count = hashlib.sha256(), 0
                    while chunk := existing.read(CHUNK_BYTES):
                        count += len(chunk)
                        if count > blob.size:
                            raise ArtifactStorageError("Existing artifact size changed")
                        actual.update(chunk)
                    if count != blob.size or actual.hexdigest() != blob.digest:
                        raise ArtifactStorageError("Existing artifact digest changed")
            os.unlink(temporary, dir_fd=root_fd)
            os.fsync(root_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=root_fd)
            except FileNotFoundError:
                pass
            os.close(root_fd)

    async def put_file(self, blob: ArtifactBlob, source: ArtifactFile, minimum_free: int) -> None:
        """検証済みfdから保存する。失敗時は一時uploadだけ破棄し、原本を保持する。"""
        try:
            async with self._write_lock:
                cancelled = threading.Event()
                writer = asyncio.create_task(asyncio.to_thread(
                    self._put_chunks, blob, source.chunks(), minimum_free, cancelled))
                try:
                    await asyncio.shield(writer)
                except asyncio.CancelledError:
                    # fdを閉じたり次のuploadを許す前に、所有threadの一時fileを回収する。
                    cancelled.set()
                    try:
                        await writer
                    except (ArtifactStorageError, ArtifactFileError, OSError):
                        pass
                    raise
        except (OSError, ArtifactFileError) as exc:
            raise ArtifactStorageError("Local artifact stream could not be saved") from exc

    async def stream(self, blob: ArtifactBlob) -> AsyncIterator[bytes]:
        """有限chunkで読み、終端でdigestを照合する。不一致時は正常EOFを返さない。"""
        root_fd = self._open_root(blob)
        stream = None
        try:
            fd = os.open(blob.key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
            stream = os.fdopen(fd, "rb")
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size != blob.size:
                raise ArtifactStorageError("Artifact file identity or size changed")
            size, digest = 0, hashlib.sha256()
            pending = None
            while chunk := await asyncio.to_thread(stream.read, CHUNK_BYTES):
                size += len(chunk)
                if size > blob.size:
                    raise ArtifactStorageError("Artifact stream exceeds saved size")
                digest.update(chunk)
                if pending is not None:
                    yield pending
                pending = chunk
            if size != blob.size or digest.hexdigest() != blob.digest:
                raise ArtifactStorageError("Artifact stream digest changed")
            # 最終chunkを検証前に送るとContent-Lengthだけで成功扱いされ得るため保留する。
            if pending is not None:
                yield pending
        except FileNotFoundError as exc:
            raise ArtifactMissingError("Local artifact is missing") from exc
        finally:
            if stream is not None:
                stream.close()
            os.close(root_fd)

    async def read(self, blob: ArtifactBlob) -> bytes:
        """認可済み metadata に一致する通常ファイルだけを bounded read する。"""
        try:
            return await asyncio.to_thread(self._read, blob)
        except FileNotFoundError as exc:
            raise ArtifactMissingError("Local artifact is missing") from exc
        except OSError as exc:
            raise ArtifactStorageError("Local artifact could not be read") from exc

    def _delete(self, blob: ArtifactBlob) -> None:
        root_fd = self._open_root(blob)
        try:
            os.unlink(blob.key, dir_fd=root_fd)
            os.fsync(root_fd)
        except FileNotFoundError:
            return
        finally:
            os.close(root_fd)

    async def delete(self, blob: ArtifactBlob) -> None:
        """GC 対象を key と root identity で限定して削除する。"""
        try:
            await asyncio.to_thread(self._delete, blob)
        except OSError as exc:
            raise ArtifactStorageError("Local artifact could not be deleted") from exc
