"""Host-side Place runner boundary for chat and non-container execution."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
import contextlib
from dataclasses import replace
import os
from pathlib import Path
import signal
from typing import TYPE_CHECKING, Any

from . import process_env
from .place_conformance import can_claim_host_place
from .process_identity import ProcessIdentity, capture_process_identity

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions


class HostPlace:
    """Host-backed Place enforcing adapter conformance, scrubbed env, and tool opt-out."""

    @staticmethod
    def validate_driver(driver: str, *, allow_host_tools: bool = False) -> None:
        """Reject adapters that cannot guarantee host built-in tool disablement."""
        if not allow_host_tools and not can_claim_host_place(driver):
            raise ValueError(
                f"Driver {driver!r} cannot enforce host tool disablement; use place='jail'"
            )

    @staticmethod
    def constrain_claude_options(
        options: ClaudeAgentOptions,
        *,
        allow_host_tools: bool = False,
    ) -> ClaudeAgentOptions:
        """Apply host confinement invariants to resolved Claude SDK options.

        Any secret variable moved into ``process_env`` that still exists in
        ``os.environ`` (for example in test fixtures) is masked out unless
        explicitly provided in ``options.env``. When built-in host tools are
        disabled (``allow_host_tools=False``), both ``tools`` and
        ``setting_sources`` are forced empty so workspace config files cannot
        re-enable hooks or tools.
        """
        cleaned_env = dict(options.env)
        for secret_name in process_env.names():
            if secret_name in os.environ and secret_name not in cleaned_env:
                cleaned_env[secret_name] = ""

        if not allow_host_tools:
            return replace(
                options,
                env=cleaned_env,
                tools=[],
                setting_sources=[],
            )
        if cleaned_env != options.env:
            return replace(options, env=cleaned_env)
        return options

    @staticmethod
    def build_subprocess_env(
        extra_keys: Iterable[str] = (),
        *,
        overrides: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Build an allowlisted subprocess env stripped of scrubbed Kotoyose secrets."""
        return process_env.build_minimal_env(extra_keys, overrides=overrides)


async def watch_work_timeout(remaining: Callable[[], float]) -> None:
    """Poll active work budget until remaining seconds reach zero."""
    while True:
        seconds = remaining()
        if seconds <= 0:
            return
        await asyncio.sleep(min(seconds, 0.25))


_watch_work_timeout = watch_work_timeout


def session_processes(session_id: int, *, proc_root: Path = Path("/proc")) -> list[int]:
    """PIDs whose session id is `session_id` (the leader started with start_new_session).

    CLIs such as Cursor's `agent` start helper daemons (worker-server, language servers) that leave
    the leader's process group with setpgid but stay in its session; killpg alone misses them.
    """
    pids: list[int] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text()
        except OSError:
            continue
        # The command name may contain spaces/parentheses; fields resume after the last ')'.
        fields = raw[raw.rfind(")") + 2:].split()
        if len(fields) > 3 and fields[3] == str(session_id):
            pids.append(int(entry.name))
    return pids


def kill_session(session_id: int, sig: int = getattr(signal, "SIGKILL", signal.SIGTERM)) -> int:
    """Signal every process left in the session; returns how many were signalled."""
    count = 0
    for pid in session_processes(session_id):
        if pid == os.getpid():
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, sig)
            count += 1
    return count


async def shutdown_process_group(process: asyncio.subprocess.Process, grace: float) -> str:
    """Send TERM -> grace -> KILL to the process group and confirm exit."""
    try:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGCONT)
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), grace)
        except TimeoutError:
            pass
        # Kill surviving children in the same group even if the leader exited first.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        # Helpers that moved to their own process group but stayed in the session.
        kill_session(process.pid)
        await asyncio.wait_for(process.wait(), 5)
        return "confirmed" if process.returncode is not None else "unconfirmed"
    except (OSError, TimeoutError):
        return "unconfirmed"


_shutdown = shutdown_process_group


async def watch_process_group_limits(
    group: int,
    job: Any,
    *,
    path_cls: Any = Path,
    killpg_fn: Callable[[int, int], Any] | None = None,
) -> str:
    """Monitor host process-group RSS/threads and throttle CPU via SIGSTOP/SIGCONT."""
    send_group_signal = killpg_fn or os.killpg
    started = asyncio.get_running_loop().time()
    ticks = os.sysconf("SC_CLK_TCK")
    page_size = os.sysconf("SC_PAGE_SIZE")
    peak_cpu = 0.0
    while True:
        memory = pids = cpu = 0
        for entry in path_cls("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / "stat").read_text().rpartition(")")[2].split()
                if int(fields[2]) != group:
                    continue
                memory += int(fields[21]) * page_size
                pids += int(fields[17])
                cpu += sum(int(fields[index]) for index in (11, 12, 13, 14))
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
        if memory > job.memory_bytes:
            return "memory_limit_exceeded"
        if pids > job.pids_limit:
            return "pids_limit_exceeded"
        peak_cpu = max(peak_cpu, cpu / ticks)
        delay = peak_cpu / job.cpus - (asyncio.get_running_loop().time() - started)
        if delay > 0:
            try:
                with contextlib.suppress(ProcessLookupError):
                    send_group_signal(group, signal.SIGSTOP)
                await asyncio.sleep(min(delay, 0.1))
            finally:
                with contextlib.suppress(ProcessLookupError):
                    send_group_signal(group, signal.SIGCONT)
        else:
            await asyncio.sleep(0.05)


_watch_limits = watch_process_group_limits


def _recovered_group_exists(identity: ProcessIdentity) -> bool:
    """Verify recovered process-group ownership, including leaderless groups."""
    try:
        os.killpg(identity.pid, 0)
    except ProcessLookupError:
        return False
    try:
        current = capture_process_identity(identity.pid)
    except RuntimeError:
        try:
            os.stat(f"/proc/{identity.pid}")
        except FileNotFoundError:
            if (
                identity.boot_id == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                and identity.uid == os.getuid()
            ):
                return True
        raise
    if current != identity or current.uid != os.getuid():
        raise ValueError("Recovered process group identity does not match")
    return True


async def stop_recovered_group(identity: ProcessIdentity, grace: float) -> str:
    """Send TERM/KILL to a recovered process group and confirm complete exit."""
    try:
        if not _recovered_group_exists(identity):
            return "confirmed"
        with contextlib.suppress(ProcessLookupError):
            os.killpg(identity.pid, signal.SIGCONT)
            os.killpg(identity.pid, signal.SIGTERM)
        await asyncio.sleep(grace)
        if not _recovered_group_exists(identity):
            return "confirmed"
        with contextlib.suppress(ProcessLookupError):
            os.killpg(identity.pid, signal.SIGKILL)
        deadline = asyncio.get_running_loop().time() + 5
        while True:
            if not _recovered_group_exists(identity):
                return "confirmed"
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.01)
    except (OSError, RuntimeError, ValueError):
        pass
    return "unconfirmed"
