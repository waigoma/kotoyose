"""Codex の child ローカルな起動条件と、host から受け取れる設定。"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
import os
from pathlib import Path
import shutil
import sys

from .child_windows import resolve_windows_shim
from .provider_spec import CODEX_CHILD_DRIVER as CODEX_CHILD_DRIVER
from .provider_spec import CODEX_CHILD_OPTIONS, CODEX_SANDBOX_CAP

CODEX_SANDBOX_MODES = CODEX_SANDBOX_CAP.order
DEFAULT_CODEX_SANDBOX_MAX = CODEX_SANDBOX_CAP.policy_default


def codex_turn_options(options: object) -> dict:
    """wire で扱う Codex オプションを、SDK の turn 制御に必要な値に限る。"""
    return CODEX_CHILD_OPTIONS.wire(options)


def child_codex_turn_options(options: object, sandbox_max: str) -> dict:
    """child 側で検証済みの上限を適用し、承認要求を拒否する SDK 設定を返す。"""
    return CODEX_CHILD_OPTIONS.local(codex_turn_options(options), sandbox_max=sandbox_max, display_name="Codex")


def _bundled_binary() -> str | None:
    try:
        from codex_cli_bin import bundled_codex_path

        return shutil.which(str(bundled_codex_path()))
    except (ImportError, OSError):
        return None


def _windows_executable(name: str) -> str | None:
    if os.path.dirname(name):
        return shutil.which(name)
    # Windows の shutil.which は名前だけだと PATH の前にカレントディレクトリを探す場合がある。
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if directory and (found := shutil.which(os.path.join(directory, name))):
            return found
    return None


@dataclass(frozen=True)
class ChildCodexLaunch:
    """同梱時は SDK の起動既定値を使い、PATH 版は解決済みの argv で起動する。"""

    argv: tuple[str, ...]
    bundled: bool


def resolve_child_codex(home: Path) -> ChildCodexLaunch:
    """`app.json` の `codex_cli` で選んだ CLI を返す。選んだ側が無ければ FileNotFoundError。"""
    from .child_components import cli_choice

    if cli_choice(home, "codex") != "system":
        if binary := _bundled_binary():
            return ChildCodexLaunch((binary,), bundled=True)
        raise FileNotFoundError("Codex CLI is unavailable on the child")
    windows = sys.platform == "win32"
    which = _windows_executable if windows else shutil.which
    for name in ("codex.exe", "codex.cmd", "codex.bat") if windows else ("codex",):
        if not (binary := which(name)):
            continue
        binary = os.path.abspath(binary)
        if windows:
            try:
                executable, args = resolve_windows_shim(binary, [])
                if args and not Path(args[0]).is_file():
                    continue
            except (OSError, ValueError):
                continue
            if not (resolved := which("node.exe" if executable == "node" else executable)):
                continue
            return ChildCodexLaunch((os.path.abspath(resolved), *(os.path.abspath(arg) for arg in args)), bundled=False)
        return ChildCodexLaunch((binary,), bundled=False)
    raise FileNotFoundError("Codex CLI is unavailable on the child")


def child_codex_installed(home: Path) -> bool:
    """SDK 配布物と指定された CLI の両方を、プロセスを起動せず確認する。"""
    try:
        metadata.distribution("openai-codex")
        resolve_child_codex(home)
    except (metadata.PackageNotFoundError, FileNotFoundError):
        return False
    return True
