"""Publication and transfer of the operator's child packages."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable, Generator, Iterator, Mapping
from dataclasses import asdict, dataclass, field
import hashlib
from io import FileIO
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from threading import Lock

from .child_lock import child_lock
from .config_paths import default_data_dir

FEATURE = "app-update/1"
MAX_SIZE = 64 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
MAX_DOWNLOADS = 4
DOWNLOAD_COOLDOWN = 60
TRANSFER_TIMEOUT = 180
_SIDECAR_LIMIT = 1024
_VERSION = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
_BUILD = re.compile(r"[0-9a-f]{40}(?:-dirty-[0-9a-f]{12})?")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FILENAMES = {
    "windows-x64": "KotoyoseChild-Setup-{version}.exe",
    "linux-x64": "kotoyose-child-{version}-linux-x64.tar.gz",
    "linux-arm64": "kotoyose-child-{version}-linux-arm64.tar.gz",
}
_log = logging.getLogger(__name__)


def package_name(version: str, platform: str) -> str:
    return _FILENAMES[platform].format(version=version)


def _open_regular(path: Path) -> FileIO:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                         | getattr(os, "O_BINARY", 0))
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError("expected a regular update file")
    return FileIO(descriptor, "rb")


def _signature(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def directory(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    explicit = env.get("KOTOYOSE_CHILD_UPDATES_DIR", "").strip()
    return Path(explicit).expanduser() if explicit else default_data_dir(env) / "child-updates"


def build_id(value: object) -> str | None:
    return value if isinstance(value, str) and (value == "unknown" or _BUILD.fullmatch(value)) else None


@dataclass(frozen=True)
class Offer:
    version: str
    build_id: str
    sha256: str
    size: int

    def payload(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls, path: Path) -> Offer:
        with _open_regular(path) as sidecar:
            raw = sidecar.read(_SIDECAR_LIMIT + 1)
        if len(raw) > _SIDECAR_LIMIT:
            raise ValueError("update sidecar is too large")
        row = json.loads(raw)
        if not isinstance(row, dict) or row.keys() != {"version", "build_id", "sha256", "size"}:
            raise ValueError("invalid update sidecar")
        offer = cls(**row)
        if (not isinstance(offer.version, str) or len(offer.version) > 48 or not _VERSION.fullmatch(offer.version)
                or not isinstance(offer.build_id, str) or not _BUILD.fullmatch(offer.build_id)
                or not isinstance(offer.sha256, str) or not _SHA256.fullmatch(offer.sha256)
                or type(offer.size) is not int or not 0 < offer.size <= MAX_SIZE):
            raise ValueError("invalid update metadata")
        return offer


@dataclass
class Publication:
    offer: Offer
    path: Path
    _verified: tuple[tuple[int, int, int, int], bool] | None = field(default=None, init=False, compare=False, repr=False)
    _verify_lock: Lock = field(default_factory=Lock, init=False, compare=False, repr=False)

    @staticmethod
    def _sidecar(root: Path, platform: str | None) -> Path | None:
        if platform not in _FILENAMES:
            return None
        platform_root = root / platform
        current = platform_root / "current"
        if platform == "windows-x64" and not current.is_symlink():
            # Use the original publication until the operator republishes Windows.
            platform_root = root
        try:
            snapshot = (platform_root / "current").resolve(strict=True)
            if snapshot.parent != platform_root.resolve() or not snapshot.name.startswith(".release-"):
                return None
            return snapshot / "offer.json"
        except (OSError, ValueError, RuntimeError):
            pass
        return None

    @classmethod
    def _load(cls, sidecar: Path, platform: str) -> Publication:
        offer = Offer.load(sidecar)
        path = sidecar.with_name(package_name(offer.version, platform))
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != offer.size:
            raise ValueError("invalid published update file")
        return cls(offer, path)

    @classmethod
    def current(cls, root: Path, platform: str | None = "windows-x64") -> Publication | None:
        sidecar = cls._sidecar(root, platform)
        if sidecar is not None and platform is not None:
            try:
                return cls._load(sidecar, platform)
            except (OSError, ValueError):
                pass
        return None

    def _open_verified(self) -> FileIO:
        installer = _open_regular(self.path)
        try:
            signature = _signature(os.fstat(installer.fileno()))
            if signature[2] != self.offer.size:
                raise ValueError("update size changed")
            with self._verify_lock:
                if self._verified is None or self._verified[0] != signature:
                    digest = hashlib.sha256()
                    remaining = self.offer.size
                    while remaining and (chunk := installer.read(min(remaining, CHUNK_SIZE))):
                        digest.update(chunk)
                        remaining -= len(chunk)
                    if _signature(os.fstat(installer.fileno())) != signature:
                        raise ValueError("update changed during verification")
                    self._verified = (signature, remaining == 0 and digest.hexdigest() == self.offer.sha256)
                    installer.seek(0)
                if not self._verified[1]:
                    raise ValueError("update contents changed")
        except BaseException:
            installer.close()
            raise
        return installer

    def _chunks(self) -> Generator[bytes]:
        with self._open_verified() as installer:
            remaining = self.offer.size
            while remaining:
                chunk = installer.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise ValueError("update was truncated while streaming")
                remaining -= len(chunk)
                yield chunk

    async def stream(self, send: Callable[[str, dict], Awaitable[bool]]) -> bool:
        chunks = self._chunks()
        loop = asyncio.get_running_loop()
        worker = None

        def finish_read(result):
            result.exception()
            chunks.close()

        try:
            async with asyncio.timeout(TRANSFER_TIMEOUT):
                offset = 0
                while True:
                    worker = loop.run_in_executor(None, next, chunks, None)
                    chunk = await asyncio.shield(worker)
                    worker = None
                    if chunk is None:
                        return await send("app_update_end", {})
                    if not await send("app_update_chunk", {
                        "offset": offset, "data": base64.b64encode(chunk).decode("ascii"),
                    }):
                        return False
                    offset += len(chunk)
        except (OSError, ValueError, TimeoutError):
            return False
        finally:
            if worker is None:
                chunks.close()
            else:
                # A cancelled await must leave the generator open until the thread exits.
                worker.add_done_callback(finish_read)


class Publications:
    def __init__(self):
        self._lock = Lock()
        self._cache: dict[str, tuple[Path, tuple[int, int, int, int], Publication | None]] = {}

    async def get(self, root: Path, platform: str | None) -> Publication | None:
        if platform is None or platform not in _FILENAMES:
            return None
        return await asyncio.to_thread(self._get, root, platform)

    def _get(self, root: Path, platform: str) -> Publication | None:
        with self._lock:
            sidecar = Publication._sidecar(root, platform)
            try:
                info = sidecar.lstat() if sidecar is not None else None
            except OSError:
                info = None
            if sidecar is None or info is None or not stat.S_ISREG(info.st_mode):
                self._cache.pop(platform, None)
                return None
            signature = _signature(info)
            cached = self._cache.get(platform)
            if cached is not None and cached[:2] == (sidecar, signature):
                return cached[2]
            try:
                publication = Publication._load(sidecar, platform)
            except (OSError, ValueError):
                publication = None
            self._cache[platform] = (sidecar, signature, publication)
            return publication


def publish(dist: Path, root: Path, version: str, platform: str = "windows-x64", *, allow_dirty: bool = False) -> Offer:
    if len(version) > 48 or not _VERSION.fullmatch(version):
        raise ValueError("invalid installer version")
    if platform not in _FILENAMES:
        raise ValueError("invalid child platform")
    name = package_name(version, platform)
    offer = Offer.load(dist / f"{name}.json")
    _check_dirty(offer, allow_dirty)
    if version != offer.version:
        raise ValueError("sidecar version and package filename differ")
    root = root / platform
    with child_lock(root):
        _publish_snapshot(dist, root, name, offer)
    return offer


def _publish_snapshot(dist: Path, root: Path, name: str, offer: Offer) -> None:
    snapshot = Path(tempfile.mkdtemp(prefix=".release-", dir=root))
    try:
        with _open_regular(dist / name) as source, (snapshot / name).open("xb") as target:
            sha256 = hashlib.sha256()
            size = 0
            while chunk := source.read(CHUNK_SIZE):
                size += len(chunk)
                if size > offer.size:
                    raise ValueError("installer exceeds its published size")
                sha256.update(chunk)
                target.write(chunk)
            if size != offer.size or sha256.hexdigest() != offer.sha256:
                raise ValueError("installer differs from its sidecar")
        (snapshot / "offer.json").write_text(json.dumps(offer.payload()) + "\n", encoding="utf-8")
        pointer = snapshot / "pointer"
        pointer.symlink_to(snapshot.name, target_is_directory=True)
        os.replace(pointer, root / "current")
    except BaseException:
        shutil.rmtree(snapshot)
        raise
    for old in root.glob(".release-*"):
        if old != snapshot and not old.is_symlink() and old.is_dir():
            try:
                shutil.rmtree(old)
            except OSError as error:
                _log.warning("could not prune old child publication %s: %s", old.name, error)


def _check_dirty(offer: Offer, allow_dirty: bool) -> None:
    if "-dirty" in offer.build_id and not allow_dirty:
        raise ValueError("dirty child build requires --allow-dirty")


def publish_available(dist: Path, root: Path, version: str, *, allow_dirty: bool = False) -> Iterator[tuple[str, Offer]]:
    platforms = []
    for platform in _FILENAMES:
        name = package_name(version, platform)
        paths = (dist / name, dist / f"{name}.json")
        if any(path.exists() or path.is_symlink() for path in paths):
            _check_dirty(Offer.load(paths[1]), allow_dirty)
            platforms.append((name, platform))
    if not platforms:
        raise ValueError(f"no child packages for {version}")
    for name, platform in platforms:
        yield name, publish(dist, root, version, platform, allow_dirty=allow_dirty)
