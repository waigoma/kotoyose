"""Resolve the CLI shipped in the installed Claude Agent SDK."""

from importlib.util import find_spec
import os
from pathlib import Path
import sys


def bundled_claude_cli() -> Path:
    sdk = find_spec("claude_agent_sdk")
    if sdk is not None and sdk.origin is not None:
        executable = "claude.exe" if sys.platform == "win32" else "claude"
        cli = Path(sdk.origin).resolve().parent / "_bundled" / executable
        if cli.is_file() and os.access(cli, os.X_OK):
            return cli
    raise ValueError(
        "Claude Agent SDK bundled CLI is missing or not executable. "
        "Install the locked Claude Agent SDK wheel or set drivers.claude.agent-sdk.cli_binary."
    )
