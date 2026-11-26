"""Standard-library-only declarations for managed CLI binaries."""

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
from typing import Literal

from .config_paths import default_data_dir


@dataclass(frozen=True)
class CliArtifact:
    """Platform pin and ordered URLs with vendor-specific template values."""

    platform: str
    os: str
    arch: str
    url_templates: tuple[str, ...]
    digest: str = "unset"
    format: Literal["raw", "tar.gz"] = "raw"
    member: str | None = None
    directory: str | None = None
    entrypoint: str | None = None

    def __post_init__(self) -> None:
        if self.digest != "unset" and not re.fullmatch(
            r"sha256:[0-9a-fA-F]{64}|sha512:[0-9a-fA-F]{128}", self.digest
        ):
            raise ValueError("Invalid CLI artifact digest")
        layout = (self.member is not None, self.directory is not None, self.entrypoint is not None)
        if (self.format, layout) not in (
            ("raw", (False, False, False)),
            ("tar.gz", (True, False, False)),
            ("tar.gz", (False, True, True)),
        ):
            raise ValueError("Invalid CLI artifact layout")
        for name in (self.member, self.directory, self.entrypoint):
            if name is not None:
                path = PurePosixPath(name)
                if not path.parts or path.is_absolute() or ".." in path.parts:
                    raise ValueError("Invalid CLI artifact path")


@dataclass(frozen=True)
class CliInstallSpec:
    version: str
    artifacts: tuple[CliArtifact, ...]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9]+)*", self.version):
            raise ValueError("CLI installation requires an exact version")
        if len({entry.platform for entry in self.artifacts}) != len(self.artifacts):
            raise ValueError("Duplicate CLI artifact platform")

    def artifact_for(self, platform: str) -> CliArtifact | None:
        return next((entry for entry in self.artifacts if entry.platform == platform), None)


def platform_key() -> str:
    architecture = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    return f"{platform.system().lower()}-{architecture}"


def managed_install_paths(
    profile_id: str, spec: CliInstallSpec, binary: str, *, tools_root: Path | None = None,
) -> tuple[Path, Path]:
    """Return the pinned release and executable paths under the selected tools root."""
    fallback = install_path(profile_id, spec.version, binary, tools_root=tools_root)
    release = fallback.parent
    artifact = spec.artifact_for(platform_key())
    if artifact is not None and artifact.entrypoint is not None:
        return release, release / artifact.entrypoint
    return release, fallback


def install_path(profile_id: str, version: str, binary: str, *, tools_root: Path | None = None) -> Path:
    """Build a managed path from validated identifiers, using the host root by default."""
    if any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", part) for part in (profile_id, version, binary)):
        raise ValueError("Invalid managed CLI path component")
    root = tools_root if tools_root is not None else default_data_dir().absolute() / "tools"
    return root / profile_id / version / binary


def installed_binary_version(path: Path, release: Path) -> str | None:
    if not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK):
        return None
    try:
        record = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("version") != release.name:
        return None
    version = record.get("binary_version")
    return version if isinstance(version, str) and version else None
