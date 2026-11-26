"""Resolve the CLI shipped in the installed Claude Agent SDK."""

from contextlib import contextmanager
from contextvars import ContextVar
from importlib.util import find_spec
import os
from pathlib import Path
import sys


def bundled_executable_name(platform: str | None = None) -> str:
    """Windows は wheel の `claude.exe`。npm の `claude.cmd` は SDK が起動を拒否する。"""
    system = sys.platform if platform is None else platform
    return "claude.exe" if system == "win32" else "claude"


def bundled_claude_cli() -> Path:
    sdk = find_spec("claude_agent_sdk")
    if sdk is not None and sdk.origin is not None:
        cli = Path(sdk.origin).resolve().parent / "_bundled" / bundled_executable_name()
        if cli.is_file() and os.access(cli, os.X_OK):
            return cli
    raise ValueError(
        "Claude Agent SDK bundled CLI is missing or not executable. "
        "Install the locked Claude Agent SDK wheel or set drivers.claude.agent-sdk.cli_binary."
    )


claude_cli_override: ContextVar[Path | None] = ContextVar("claude_cli_override", default=None)


@contextmanager
def use_claude_cli(path: Path | None):
    previous = claude_cli_override.set(path)
    try:
        yield
    finally:
        claude_cli_override.reset(previous)


def child_claude_cli(home: Path) -> Path:
    return child_system_claude_cli(home) or bundled_claude_cli()


def child_system_claude_cli(home: Path | None) -> Path | None:
    from .child_components import cli_choice, providers, system_cli

    if home is None or cli_choice(home, "claude") != "system":
        return None
    row = next(row for row in providers() if row["id"] == "claude")
    cli = system_cli(row)
    if cli is None:
        raise ValueError("The selected system Claude CLI is unavailable.")
    return cli
