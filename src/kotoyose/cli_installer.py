"""Install a CLI from a pinned, integrity-checked artifact."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import fcntl
import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
from tempfile import TemporaryDirectory
import time

import httpx

from . import cli_install_spec as install_spec
from .cli_install_spec import CliArtifact, CliInstallSpec, installed_binary_version, managed_install_paths
from .print_cli_profiles import PrintCliProfile

_MAX_DOWNLOAD = 256 * 1024**2
_MAX_FILE = 512 * 1024**2
_MAX_ARCHIVE = 768 * 1024**2
_MAX_MEMBERS = 20_000
_DOWNLOAD_SECONDS = 600


class CliInstallError(ValueError):
    pass


class CliInstallConflict(CliInstallError):
    pass


@dataclass(frozen=True)
class ToolStatus:
    profile: str
    provider: str
    version: str
    installed: bool = False
    path: str | None = None
    binary_version: str | None = None


def _download(url: str, destination: Path, algorithm: str) -> str:
    deadline = time.monotonic() + _DOWNLOAD_SECONDS
    digest = hashlib.new(algorithm)
    size = 0
    with destination.open("wb") as output, httpx.stream(
        "GET", url, timeout=15, trust_env=False, follow_redirects=True,
        headers={"Accept-Encoding": "identity"},
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_raw():
            size += len(chunk)
            if time.monotonic() > deadline:
                raise CliInstallError("CLI distribution download timed out")
            if size > _MAX_DOWNLOAD:
                raise CliInstallError("CLI distribution exceeds the download size limit")
            output.write(chunk)
            digest.update(chunk)
    if size == 0:
        raise CliInstallError("CLI distribution is empty")
    return digest.hexdigest()


def _fetch(spec: CliInstallSpec, artifact: CliArtifact, destination: Path) -> None:
    algorithm, expected = artifact.digest.split(":")
    last_error = None
    for template in artifact.url_templates:
        url = template.format(version=spec.version, os=artifact.os, arch=artifact.arch)
        try:
            actual = _download(url, destination, algorithm)
        except (httpx.HTTPError, CliInstallError) as exc:
            last_error = exc
            continue
        if not hmac.compare_digest(actual, expected.lower()):
            raise CliInstallError("CLI distribution integrity mismatch")
        return
    if last_error is not None:
        raise last_error
    raise CliInstallError("CLI distribution has no download URLs")


class _BoundedArchive:
    def __init__(self, stream: gzip.GzipFile):
        self.stream = stream
        self.remaining = _MAX_ARCHIVE

    def read(self, size: int) -> bytes:
        data = self.stream.read(min(size, self.remaining + 1))
        self.remaining -= len(data)
        if self.remaining < 0:
            raise CliInstallError("CLI distribution exceeds the expanded size limit")
        return data


def _extract(payload: Path, artifact: CliArtifact, release: Path, binary: Path) -> None:
    directory = PurePosixPath(artifact.directory) if artifact.directory is not None else None
    with gzip.open(payload, "rb") as stream, tarfile.open(
        fileobj=_BoundedArchive(stream), mode="r|",
    ) as archive:
        for count, member in enumerate(archive, 1):
            name = PurePosixPath(member.name)
            if (
                name.is_absolute() or ".." in name.parts or member.sparse is not None
                or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE)
            ):
                raise CliInstallError("Unsafe CLI distribution member")
            if count > _MAX_MEMBERS or not 0 <= member.size <= _MAX_FILE:
                raise CliInstallError("CLI distribution exceeds the archive member limits")

            if directory is None:
                if name != PurePosixPath(artifact.member):
                    continue
                if not member.isfile() or member.size == 0 or binary.exists():
                    raise CliInstallError("Invalid CLI binary member")
                destination = binary
            else:
                if not name.is_relative_to(directory):
                    continue
                relative = name.relative_to(directory)
                if (not relative.parts and not member.isdir()) or relative.parts[:1] == ("manifest.json",):
                    raise CliInstallError("Invalid CLI package layout")
                destination = release / relative

            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, destination.open("xb") as output:
                shutil.copyfileobj(source, output)
            destination.chmod(0o755 if member.mode & 0o111 else 0o644)

    if not binary.is_file() or binary.stat().st_size == 0:
        raise CliInstallError("CLI binary member is missing or empty")


def tool_status(profile: PrintCliProfile) -> ToolStatus:
    spec = profile.install
    if spec is None:
        raise CliInstallError("CLI profile has no managed distribution")
    release, path = managed_install_paths(profile.id, spec, profile.binary)
    version = installed_binary_version(path, release)
    return ToolStatus(profile.id, profile.provider, spec.version, bool(version), str(path) if version else None, version)


def _other_version_running(release: Path, path: Path) -> bool:
    # Isolated processes may see the binary at the bind-mount destination.
    other_binaries = set()
    for candidate in release.parent.glob(f"*/{path.relative_to(release)}"):
        if candidate == path:
            continue
        try:
            info = candidate.stat()
        except FileNotFoundError:
            continue
        other_binaries.add((info.st_dev, info.st_ino))
    if not other_binaries:
        return False
    for exe in Path("/proc").glob("[0-9]*/exe"):
        try:
            info = exe.stat()
        except OSError:
            continue
        if (info.st_dev, info.st_ino) in other_binaries:
            return True
    return False


def _publish(
    profile: PrintCliProfile, spec: CliInstallSpec, release: Path, path: Path, stage: Path,
) -> ToolStatus:
    target = install_spec.platform_key()
    artifact = spec.artifact_for(target)
    if artifact is None or artifact.digest == "unset":
        raise CliInstallError(f"CLI installation is not available for this platform ({target})")

    staged_release = stage / "release"
    staged_release.mkdir()
    binary = staged_release / path.relative_to(release)
    payload = stage / "artifact"
    _fetch(spec, artifact, payload)
    if artifact.format == "raw":
        payload.rename(binary)
    else:
        _extract(payload, artifact, staged_release, binary)
    binary.chmod(0o755)
    home = stage / "home"
    home.mkdir()
    result = subprocess.run(
        [str(binary), "--version"], cwd=home, env={"HOME": str(home)},
        stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=15,
    )
    version = result.stdout.decode(errors="replace").strip()
    if not re.search(rf"(?<![\w.-])v?{re.escape(spec.version)}(?![\w.-])", version):
        raise CliInstallError("CLI binary reported an unexpected version")
    status = ToolStatus(profile.id, profile.provider, spec.version, True, str(path), version[:1024])
    (staged_release / "manifest.json").write_text(json.dumps(asdict(status)) + "\n", encoding="utf-8")
    if _other_version_running(release, path):
        raise CliInstallConflict("A different managed CLI version is in use")
    staged_release.rename(release)
    return status


def install_tool(profile: PrintCliProfile) -> ToolStatus:
    """Publish the verified executable and its manifest together on the same filesystem."""
    spec = profile.install
    if spec is None:
        raise CliInstallError("CLI profile has no managed distribution")
    release, path = managed_install_paths(profile.id, spec, profile.binary)
    profile_dir = release.parent
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
        with (profile_dir / ".install.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CliInstallConflict("CLI installation already in progress") from exc
            if _other_version_running(release, path):
                raise CliInstallConflict("A different managed CLI version is in use")
            status = tool_status(profile)
            if status.installed:
                return status
            if release.exists():
                raise CliInstallConflict("CLI version directory already exists without a valid installation")
            with TemporaryDirectory(dir=profile_dir, prefix=".install-") as directory:
                return _publish(profile, spec, release, path, Path(directory))
    except (OSError, EOFError, httpx.HTTPError, tarfile.TarError, subprocess.SubprocessError) as exc:
        raise CliInstallError(f"CLI installation failed ({type(exc).__name__})") from exc
