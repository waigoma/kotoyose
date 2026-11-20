"""Provider process の PID 再利用を検出し、安全な pidfd 停止だけを提供する。"""

from __future__ import annotations

import asyncio
import ctypes
import errno
import math
import os
import select
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


ProcessStopResult = Literal["confirmed", "unconfirmed"]
_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
_PidfdOpen = Callable[[int, int], int]
_PidfdSendSignal = Callable[[int, int], None]


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """PID の再利用を識別するための provider process 属性。"""

    pid: int
    start_ticks: int
    boot_id: str
    uid: int


def _read_identity(pid: int) -> ProcessIdentity:
    proc = Path(f"/proc/{pid}")
    stat_line = (proc / "stat").read_text(encoding="ascii")
    _, separator, remainder = stat_line.rpartition(")")
    if not separator:
        raise ValueError("process stat is malformed")
    fields = remainder.strip().split()
    # remainder の先頭は field 3 (state) なので、field 22 starttime は index 19。
    if len(fields) <= 19:
        raise ValueError("process stat has too few fields")
    try:
        start_ticks = int(fields[19])
    except ValueError as exc:
        raise ValueError("process start time is malformed") from exc
    boot_id = _BOOT_ID.read_text(encoding="ascii").strip()
    if not boot_id:
        raise ValueError("kernel boot id is empty")
    return ProcessIdentity(pid=pid, start_ticks=start_ticks, boot_id=boot_id, uid=proc.stat().st_uid)


def capture_process_identity(pid: int) -> ProcessIdentity:
    """``/proc`` から PID、starttime、boot ID、UID を読む。

    ``pid`` は provider transport が取得した正の PID に限る。procfs の消失・権限
    エラーは内部情報を含まない ``RuntimeError`` として返す。
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("pid must be a positive integer")
    try:
        return _read_identity(pid)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("process identity is malformed") from exc
    except OSError as exc:
        raise RuntimeError("process identity is unavailable") from exc


def _proc_exists(pid: int) -> bool | None:
    try:
        os.stat(f"/proc/{pid}")
        return True
    except FileNotFoundError:
        try:
            os.stat("/proc")
        except OSError:
            return None
        return False
    except OSError:
        return None


async def _poll_pidfd(fd: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            ready, _, _ = select.select([fd], [], [], 0)
        except OSError:
            return False
        if ready:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.01, remaining))


async def _wait_pidfd(fd: int, timeout: float) -> bool:
    """pidfd の readable 通知を event loop で待ち、非対応 loop では短い poll に落とす。"""
    if timeout <= 0:
        return await _poll_pidfd(fd, 0)
    loop = asyncio.get_running_loop()
    exited = loop.create_future()

    def ready() -> None:
        if not exited.done():
            exited.set_result(True)

    try:
        loop.add_reader(fd, ready)
    except (NotImplementedError, OSError, ValueError):
        return await _poll_pidfd(fd, timeout)
    try:
        try:
            await asyncio.wait_for(asyncio.shield(exited), timeout)
        except asyncio.TimeoutError:
            return False
        return True
    finally:
        loop.remove_reader(fd)


def _libc_pidfd_backend() -> tuple[_PidfdOpen | None, _PidfdSendSignal | None]:
    """高水準 API がない Linux build 向けに、export 済み libc 関数を型付きで束ねる。"""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        open_symbol = libc.pidfd_open
        send_symbol = libc.pidfd_send_signal
    except (AttributeError, OSError):
        return None, None
    open_symbol.argtypes = [ctypes.c_int, ctypes.c_uint]
    open_symbol.restype = ctypes.c_int
    send_symbol.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    send_symbol.restype = ctypes.c_int

    def open_pidfd(pid: int, flags: int) -> int:
        fd = open_symbol(pid, flags)
        if fd < 0:
            error = ctypes.get_errno() or errno.EIO
            raise OSError(error, os.strerror(error))
        return fd

    def send_signal(pidfd: int, signal_number: int) -> None:
        result = send_symbol(pidfd, signal_number, None, 0)
        if result < 0:
            error = ctypes.get_errno() or errno.EIO
            raise OSError(error, os.strerror(error))

    return open_pidfd, send_signal


def _pidfd_backend() -> tuple[_PidfdOpen | None, _PidfdSendSignal | None]:
    """pidfd の実装を選ぶ。高水準 API 不足時だけ同名 libc export を補完する。"""
    open_pidfd = getattr(os, "pidfd_open", None)
    send_signal = getattr(signal, "pidfd_send_signal", None)
    if callable(open_pidfd) and callable(send_signal):
        return open_pidfd, send_signal
    libc_open, libc_send = _libc_pidfd_backend()
    return (
        open_pidfd if callable(open_pidfd) else libc_open,
        send_signal if callable(send_signal) else libc_send,
    )


async def stop_identified_process(
    identity: ProcessIdentity, grace_seconds: float
) -> ProcessStopResult:
    """identity を再照合した process を pidfd で TERM、必要なら KILL する。

    PID が消滅または boot / starttime が変わっていた場合は対象 process が gone と
    確認できるため ``confirmed`` を返す。pidfd、権限、procfs、停止確認に問題が
    ある場合は PID 再利用を避けて kill fallback を行わず ``unconfirmed`` とする。
    """
    if (
        not isinstance(identity, ProcessIdentity)
        or not isinstance(identity.pid, int)
        or isinstance(identity.pid, bool)
        or identity.pid <= 0
        or not isinstance(identity.start_ticks, int)
        or isinstance(identity.start_ticks, bool)
        or identity.start_ticks < 0
        or not isinstance(identity.boot_id, str)
        or not identity.boot_id
        or not isinstance(identity.uid, int)
        or isinstance(identity.uid, bool)
        or identity.uid < 0
        or not isinstance(grace_seconds, (int, float))
        or isinstance(grace_seconds, bool)
        or not math.isfinite(grace_seconds)
        or grace_seconds < 0
    ):
        return "unconfirmed"

    try:
        current = capture_process_identity(identity.pid)
    except (ValueError, RuntimeError):
        return "confirmed" if _proc_exists(identity.pid) is False else "unconfirmed"
    if current.boot_id != identity.boot_id or current.start_ticks != identity.start_ticks:
        return "confirmed"
    try:
        current_uid = os.getuid()
    except OSError:
        return "unconfirmed"
    if current.uid != identity.uid or current.uid != current_uid:
        return "unconfirmed"

    open_pidfd, send_signal = _pidfd_backend()
    if open_pidfd is None or send_signal is None:
        return "unconfirmed"
    try:
        pidfd = open_pidfd(identity.pid, 0)
    except (ProcessLookupError, FileNotFoundError):
        return "confirmed"
    except (PermissionError, OSError):
        return "unconfirmed"

    try:
        try:
            reopened = capture_process_identity(identity.pid)
        except (ValueError, RuntimeError):
            return "confirmed" if _proc_exists(identity.pid) is False else "unconfirmed"
        if reopened.boot_id != identity.boot_id or reopened.start_ticks != identity.start_ticks:
            return "confirmed"
        if reopened.uid != identity.uid or reopened.uid != current_uid:
            return "unconfirmed"
        try:
            send_signal(pidfd, signal.SIGTERM)
        except (ProcessLookupError, FileNotFoundError):
            return "confirmed"
        except (PermissionError, OSError):
            return "unconfirmed"
        if await _wait_pidfd(pidfd, float(grace_seconds)):
            return "confirmed"
        try:
            send_signal(pidfd, signal.SIGKILL)
        except (ProcessLookupError, FileNotFoundError):
            return "confirmed"
        except (PermissionError, OSError):
            return "unconfirmed"
        return "confirmed" if await _wait_pidfd(pidfd, 5.0) else "unconfirmed"
    except (TypeError, ValueError, OSError, RuntimeError):
        return "unconfirmed"
    finally:
        try:
            os.close(pidfd)
        except (OSError, ValueError):
            pass
