"""Antigravity CLI の version と実行互換性を診断する。"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .. import process_env

MIN_ANTIGRAVITY_CLI_VERSION = (1, 1, 24)
_VERSION_PROBE_TIMEOUT_SECONDS = 5.0
_PROCESS_REAP_TIMEOUT_SECONDS = 1.0

ProbeErrorCode = Literal[
    "command_not_found",
    "spawn_failed",
    "timeout",
    "nonzero_exit",
    "version_unknown",
    "version_too_old",
]


@dataclass(frozen=True)
class AntigravityCliDiagnostic:
    """Admin と runtime が共有する Antigravity CLI 診断結果。

    Attributes:
        executable: 解決済みの実行ファイル path。未検出時は None。
        detected_version: 検出した semantic version。解析不能時は None。
        minimum_version: Kotoyose が要求する最低 version。
        compatible: CLI を runtime で利用できる場合は True。
        error_code: 非互換の定型理由。互換時は None。
    """

    executable: str | None
    detected_version: tuple[int, int, int] | None
    minimum_version: tuple[int, int, int]
    compatible: bool
    error_code: ProbeErrorCode | None


async def probe_antigravity_cli(
    command: Sequence[str] = ("agy", "--version"),
) -> AntigravityCliDiagnostic:
    """Antigravity CLI を検出し、最低 version との互換性を返す。

    Args:
        command: Version を表示する subprocess command。

    Returns:
        Raw stdout / stderr を含まない構造化診断。
    """
    executable = shutil.which(command[0]) if command else None
    if executable is None:
        return _diagnostic(None, None, "command_not_found")

    resolved_command = [executable, *command[1:]]
    try:
        process = await asyncio.create_subprocess_exec(
            *resolved_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=process_env.build_minimal_env(),
        )
    except OSError:
        return _diagnostic(executable, None, "spawn_failed")

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=_VERSION_PROBE_TIMEOUT_SECONDS
        )
    except TimeoutError:
        await _stop_process(process)
        return _diagnostic(executable, None, "timeout")
    except asyncio.CancelledError:
        await _stop_process(process)
        raise
    except OSError:
        await _stop_process(process)
        return _diagnostic(executable, None, "spawn_failed")

    output = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    version = _parse_cli_version(output)
    if process.returncode != 0:
        return _diagnostic(executable, version, "nonzero_exit")
    if version is None:
        return _diagnostic(executable, None, "version_unknown")
    if version < MIN_ANTIGRAVITY_CLI_VERSION:
        return _diagnostic(executable, version, "version_too_old")
    return _diagnostic(executable, version, None)


def _diagnostic(
    executable: str | None,
    version: tuple[int, int, int] | None,
    error_code: ProbeErrorCode | None,
) -> AntigravityCliDiagnostic:
    return AntigravityCliDiagnostic(
        executable=executable,
        detected_version=version,
        minimum_version=MIN_ANTIGRAVITY_CLI_VERSION,
        compatible=error_code is None,
        error_code=error_code,
    )


def _parse_cli_version(output: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)", output)
    return tuple(int(part) for part in match.groups()) if match else None


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    """中断した probe の子 process を停止して回収する。"""
    with contextlib.suppress(ProcessLookupError):
        process.kill()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        async with asyncio.timeout(_PROCESS_REAP_TIMEOUT_SECONDS):
            await asyncio.shield(process.wait())
