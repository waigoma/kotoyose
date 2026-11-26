"""Provider packages and CLI availability in the child runner's Python environment."""

import importlib
from importlib.metadata import PackageNotFoundError, distribution, version
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .child_catalog import catalog_document, component_rows
from .cli_install_spec import installed_binary_version, managed_install_paths, platform_key
from .print_cli_profiles import get_print_cli_profile


def providers() -> list[dict]:
    """現在の組み込み宣言からカタログ行を返す。"""
    return component_rows()


def cli_choice(home: Path, provider: str, *, default: str = "bundled") -> str:
    try:
        settings = json.loads((home / "app.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    if not isinstance(settings, dict):
        return default
    choice = settings.get(f"{provider}_cli", default)
    return "system" if choice == "system" else "bundled"


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _path_cli(names: list[str]) -> Path | None:
    # Only search PATH explicitly. On Windows some which() versions also search the current directory.
    paths = [Path(entry) for entry in os.get_exec_path() if entry and Path(entry).is_absolute()]
    for name in names:
        for directory in paths:
            path = directory / name
            if _executable(path):
                return path
    return None


def _windows_system_cli(row: dict) -> Path | None:
    """Search every PATH entry for ``.exe`` before any ``.cmd``."""
    native = _path_cli([name for name in row["windows_cli"] if name.endswith(".exe")])
    if native:
        return native
    profile = os.environ.get("USERPROFILE")
    if profile and row.get("windows_profile_cli"):
        path = Path(profile) / row["windows_profile_cli"]
        if path.is_absolute() and _executable(path):
            return path
    return _path_cli([name for name in row["windows_cli"] if name.endswith(".cmd")])


def system_cli(row: dict) -> Path | None:
    if sys.platform != "win32":
        return _path_cli([row["cli"]])
    return _windows_system_cli(row)


def bundled_cli(row: dict) -> Path | None:
    relative = row["bundled_cli"] + (".exe" if sys.platform == "win32" else "")
    try:
        path = Path(distribution(row["cli_package"]).locate_file(relative))
    except PackageNotFoundError:
        return None
    return path.resolve() if _executable(path) else None


def selected_cli(home: Path, row: dict, *, default: str = "bundled") -> Path | None:
    if row.get("profile"):
        return system_cli(row) if cli_choice(home, row["id"], default=default) == "system" else managed_cli(home, row)
    return system_cli(row) if cli_choice(home, row["id"], default=default) == "system" else bundled_cli(row)


def managed_cli(home: Path, row: dict) -> Path | None:
    """子機が管理する、現在の OS に対応した固定版を探す。"""
    managed = row.get("managed")
    if not managed or platform_key() not in managed["platforms"]:
        return None
    profile = get_print_cli_profile(row["profile"])
    release, binary = managed_install_paths(profile.id, profile.install, profile.binary, tools_root=home / "tools")
    return binary if installed_binary_version(binary, release) else None


def _version(path: Path) -> str | None:
    args = [str(path), "--version"]
    try:
        if sys.platform == "win32":
            from .child_windows import resolve_windows_shim

            executable, arguments = resolve_windows_shim(args[0], args[1:])
            if not Path(executable).is_absolute():
                node = _path_cli(["node.exe"]) if executable == "node" else None
                if node is None:
                    return None
                executable = str(node)
            args = [executable, *arguments]
        # Use regular files so a descendant inheriting stdout cannot keep this probe open.
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                args, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                cwd=path.parent, timeout=4, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            if result.returncode == 0:
                output.seek(0)
                lines = output.read(1024).decode("utf-8", errors="replace").strip().splitlines()
                if lines:
                    return lines[0]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


def sdk_importable(row: dict) -> bool:
    try:
        importlib.import_module(row["module"])
    except Exception:
        return False
    return True


def probe(home: Path, *, versions: bool) -> list[dict]:
    from .child_turn import supports_component_turn

    result = []
    rows = providers()
    catalog_version = catalog_document(rows)["version"]
    platform = platform_key()
    for row in rows:
        choice = cli_choice(home, row["id"])
        installed = {}
        for package in row["packages"]:
            try:
                installed[package] = version(package)
            except PackageNotFoundError:
                installed[package] = None
        sdk = bool(row["packages"])
        managed_available = platform in (row.get("managed") or {}).get("platforms", ())
        local = sys.platform in (row.get("login") or {}).get("transports", {})
        system = system_cli(row) if sdk or local or managed_available else None
        bundled = bundled_cli(row) if sdk else managed_cli(home, row)
        result.append({
            "id": row["id"],
            "catalog_version": catalog_version,
            "packages": installed,
            "system_cli": system is not None,
            "system_version": _version(system) if system and versions else None,
            "bundled_cli": bundled is not None,
            "choice": choice,
            "turns_supported": sdk and supports_component_turn(row["driver"]),
            "sdk_importable": sdk and sdk_importable(row),
            "managed_available": managed_available,
        })
    return result
