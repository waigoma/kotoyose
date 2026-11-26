"""所有者が選んだ CLI の管理操作を、子機の tools ディレクトリー内で実行する。"""

import argparse
from pathlib import Path

from .child_catalog import component_rows
from .cli_install_spec import platform_key
from .cli_installer import CliInstallError, install_tool, remove_tool
from .print_cli_profiles import get_print_cli_profile


def change_tool(home: Path, provider: str, action: str) -> None:
    """対象 OS の固定 pin だけを導入または削除する。"""
    row = next((row for row in component_rows() if row["id"] == provider), None)
    managed = row.get("managed") if row else None
    if not managed or platform_key() not in managed["platforms"]:
        raise CliInstallError("Managed CLI is unavailable on this platform")
    profile = get_print_cli_profile(row["profile"])
    tools_root = home.resolve() / "tools"
    directory = tools_root / profile.id
    if directory.resolve() != directory:
        raise CliInstallError("Invalid managed CLI directory")
    operation = {"install": install_tool, "remove": remove_tool}[action]
    operation(profile, tools_root=tools_root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("home", type=Path)
    parser.add_argument("provider")
    parser.add_argument("action", choices=("install", "remove"))
    args = parser.parse_args()
    if not args.home.is_absolute():
        parser.error("home must be absolute")
    try:
        change_tool(args.home, args.provider, args.action)
    except (CliInstallError, OSError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
