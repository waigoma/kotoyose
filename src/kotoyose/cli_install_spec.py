"""Standard-library-only declarations for managed CLI binaries."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re

from .config_paths import default_data_dir


@dataclass(frozen=True)
class NpmPlatformBinary:
    platform: str
    package: str
    integrity: str


@dataclass(frozen=True)
class CliInstallSpec:
    version: str
    packages: tuple[NpmPlatformBinary, ...]
    member: str
    compression: str | None = None
    method: str = "npm_platform_binary"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", self.version):
            raise ValueError("CLI installation requires an exact numeric version")


def install_path(profile_id: str, version: str, binary: str) -> Path:
    if any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", part) for part in (profile_id, version, binary)):
        raise ValueError("Invalid managed CLI path component")
    return default_data_dir().absolute() / "tools" / profile_id / version / binary


def installed_binary_version(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink() or not os.access(path, os.X_OK):
        return None
    try:
        record = json.loads(path.with_name("manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("version") != path.parent.name:
        return None
    version = record.get("binary_version")
    return version if isinstance(version, str) and version else None
