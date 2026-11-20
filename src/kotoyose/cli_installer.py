"""Install a single binary from a pinned, integrity-checked distribution."""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import hmac
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import tarfile
from tempfile import TemporaryDirectory
import time

import httpx

from .cli_install_spec import CliInstallSpec, NpmPlatformBinary, install_path, installed_binary_version
from .print_cli_profiles import PrintCliProfile

_MAX_DOWNLOAD = 128 * 1024**2
_MAX_BINARY = 256 * 1024**2
_DOWNLOAD_SECONDS = 600  # a raw vendor binary can be ~200 MB
_BROTLI_SCRIPT = """
const fs = require('node:fs');
const zlib = require('node:zlib');
process.stdout.write(zlib.brotliDecompressSync(fs.readFileSync(0), {
  maxOutputLength: Number(process.argv[1])
}));
"""


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


def _platform_key() -> str:
    architecture = {"x86_64": "x64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    return f"{platform.system().lower()}-{architecture}"


def _download(url: str, *, max_size: int = _MAX_DOWNLOAD) -> bytes:
    deadline = time.monotonic() + _DOWNLOAD_SECONDS
    data = bytearray()
    with httpx.stream("GET", url, timeout=15, trust_env=False, headers={"Accept-Encoding": "identity"}) as response:
        response.raise_for_status()
        for chunk in response.iter_raw():
            if time.monotonic() > deadline:
                raise CliInstallError("CLI distribution download timed out")
            if len(data) + len(chunk) > max_size:
                raise CliInstallError("CLI distribution exceeds the download size limit")
            data.extend(chunk)
    return bytes(data)


def _pin(package: NpmPlatformBinary) -> bytes:
    prefix, _, encoded = package.integrity.partition("-")
    try:
        digest = base64.b64decode(encoded, validate=True)
    except binascii.Error:
        digest = b""
    if prefix != "sha512" or len(digest) != hashlib.sha512().digest_size:
        raise CliInstallError(f"SHA-512 integrity is unset or invalid for {package.platform}")
    return digest


def _extract(data: bytes, member_name: str) -> bytes:
    binary = None
    with tarfile.open(fileobj=io.BytesIO(data), mode="r|gz") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise CliInstallError("Unsafe CLI distribution member")
            if member.name != member_name:
                continue
            if binary is not None or not member.isfile() or not 0 < member.size <= _MAX_BINARY:
                raise CliInstallError("Invalid CLI binary member")
            with archive.extractfile(member) as stream:
                binary = stream.read(_MAX_BINARY + 1)
    if not binary:
        raise CliInstallError("CLI binary member is missing")
    return binary


def _fetch_npm(spec: CliInstallSpec, target: str) -> bytes:
    if spec.compression not in (None, "brotli"):
        raise CliInstallError("Unsupported CLI distribution format")
    package = next((entry for entry in spec.packages if entry.platform == target), None)
    if package is None:
        raise CliInstallError(f"CLI installation is unsupported on {target}")
    expected = _pin(package)
    if not re.fullmatch(r"(?:@[a-z0-9._-]+/)?[a-z0-9][a-z0-9._-]*", package.package):
        raise CliInstallError("Invalid npm package name")
    name = package.package.rsplit("/", 1)[-1]
    url = f"https://registry.npmjs.org/{package.package}/-/{name}-{spec.version}.tgz"
    data = _download(url)
    if not hmac.compare_digest(hashlib.sha512(data).digest(), expected):
        raise CliInstallError("CLI distribution integrity mismatch")
    binary = _extract(data, spec.member)
    if spec.compression == "brotli":
        # Resolve node from the server's PATH (nvm, user installs, CI tool caches), then run it
        # by absolute path with an empty environment: an empty env alone only searches /bin:/usr/bin.
        node = shutil.which("node")
        if node is None:
            raise CliInstallError("Installing this CLI needs Node.js (node) on the server's PATH")
        binary = subprocess.run(
            [node, "-e", _BROTLI_SCRIPT, str(_MAX_BINARY)], input=binary,
            capture_output=True, check=True, timeout=30, env={},
        ).stdout
    return binary


def _fetch_direct(spec: CliInstallSpec, target: str) -> bytes:
    entry = next((entry for entry in spec.direct_binaries if entry.platform == target), None)
    if entry is None or not re.fullmatch(r"[0-9a-fA-F]{64}", entry.sha256):
        raise CliInstallError(f"CLI installation is not available for this platform ({target})")

    expected = bytes.fromhex(entry.sha256)
    last_error = None
    for template in entry.url_templates:
        url = template.format(version=spec.version, os=entry.os, arch=entry.arch)
        try:
            binary = _download(url, max_size=_MAX_BINARY)
        except (httpx.HTTPError, CliInstallError) as exc:
            last_error = exc
            continue
        if not hmac.compare_digest(hashlib.sha256(binary).digest(), expected):
            raise CliInstallError("CLI distribution integrity mismatch")
        return binary
    if last_error is not None:
        raise last_error
    raise CliInstallError("CLI distribution has no download URLs")


def _distribution(spec: CliInstallSpec) -> bytes:
    target = _platform_key()
    if spec.method == "npm_platform_binary":
        binary = _fetch_npm(spec, target)
    elif spec.method == "direct_binary":
        binary = _fetch_direct(spec, target)
    else:
        raise CliInstallError("Unsupported CLI distribution format")
    if not binary:
        raise CliInstallError("CLI distribution contains an empty binary")
    return binary


def tool_status(profile: PrintCliProfile) -> ToolStatus:
    spec = profile.install
    if spec is None:
        raise CliInstallError("CLI profile has no managed distribution")
    path = install_path(profile.id, spec.version, profile.binary)
    version = installed_binary_version(path)
    return ToolStatus(profile.id, profile.provider, spec.version, bool(version), str(path) if version else None, version)


def _other_version_running(path: Path) -> bool:
    # Isolated processes may see the binary at the bind-mount destination.
    other_binaries = set()
    for candidate in path.parent.parent.glob(f"*/{path.name}"):
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


def _publish(profile: PrintCliProfile, spec: CliInstallSpec, path: Path, stage: Path) -> ToolStatus:
    release = stage / "release"
    release.mkdir()
    binary = release / path.name
    binary.write_bytes(_distribution(spec))
    binary.chmod(0o755)
    home = stage / "home"
    home.mkdir()
    result = subprocess.run(
        [str(binary), "--version"], cwd=home, env={"HOME": str(home)},
        stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=15,
    )
    version = result.stdout.decode(errors="replace").strip()
    if not re.search(rf"(?<![\w.])v?{re.escape(spec.version)}(?![\w.])", version):
        raise CliInstallError("CLI binary reported an unexpected version")
    status = ToolStatus(profile.id, profile.provider, spec.version, True, str(path), version[:1024])
    (release / "manifest.json").write_text(json.dumps(asdict(status)) + "\n", encoding="utf-8")
    if _other_version_running(path):
        raise CliInstallConflict("A different managed CLI version is in use")
    release.rename(path.parent)
    return status


def install_tool(profile: PrintCliProfile) -> ToolStatus:
    """Publish the verified executable and its manifest together on the same filesystem."""
    spec = profile.install
    if spec is None:
        raise CliInstallError("CLI profile has no managed distribution")
    path = install_path(profile.id, spec.version, profile.binary)
    profile_dir = path.parent.parent
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
        with (profile_dir / ".install.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CliInstallConflict("CLI installation already in progress") from exc
            if _other_version_running(path):
                raise CliInstallConflict("A different managed CLI version is in use")
            status = tool_status(profile)
            if status.installed:
                return status
            if path.parent.exists():
                raise CliInstallConflict("CLI version directory already exists without a valid installation")
            with TemporaryDirectory(dir=profile_dir, prefix=".install-") as directory:
                return _publish(profile, spec, path, Path(directory))
    except (OSError, EOFError, httpx.HTTPError, tarfile.TarError, subprocess.SubprocessError) as exc:
        raise CliInstallError(f"CLI installation failed ({type(exc).__name__})") from exc
