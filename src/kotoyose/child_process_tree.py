"""turn が起動したプロセスの木を、Linux ではプロセスグループ、Windows では Job Object で止める。

Windows の実装は kernel32 を差し替えて Linux でも試す。実際の DLL は win32 でだけ読む。
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from contextvars import ContextVar
import os
import signal
import subprocess
import sys
from threading import Lock
from typing import Any, Callable

# この turn task が起動した pid。テストプロセス自体は追わない。
TURN_PIDS: ContextVar[set[int] | None] = ContextVar("kotoyose_child_turn_pids", default=None)
# SDK が Popen に指定できない POSIX のセッション分離を、app-server 起動時だけ適用する。
TURN_NEW_SESSION: ContextVar[bool] = ContextVar("kotoyose_child_turn_new_session", default=False)
STRICT_WINDOWS_JOBS: ContextVar[bool] = ContextVar("kotoyose_child_strict_jobs", default=False)


class _TrackedPids(set[int]):
    """1 つの runtime の pid 集合。一度止めたあとの kill_pids は何もしない。"""

    def __init__(self) -> None:
        super().__init__()
        self.killed = False
        self.lock = Lock()
        self.process_groups: set[int] = set()


def new_turn_pids() -> _TrackedPids:
    """温存した session も、新しい runtime も、この集合を 1 つだけ持つ。"""
    return _TrackedPids()

CREATE_SUSPENDED = 0x00000004
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
# JobObjectExtendedLimitInformation
JOB_OBJECT_EXTENDED_LIMIT = 9

_DEPTH = 0
_RESTORE: list[Callable[[], None]] = []
_JOBS: dict[int, TurnJob] = {}


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def kill_on_close_information() -> JOBOBJECT_EXTENDED_LIMIT_INFORMATION:
    """最後のハンドルを閉じたとき、Job に入っているプロセスを終わらせる制限。"""
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    return info


def taskkill_argv(pid: int) -> list[str]:
    """Job に入れ損ねた木を、shell を経由せず止める。"""
    return ["taskkill.exe", "/PID", str(pid), "/T", "/F"]


class Kernel32Adapter:
    """kernel32 の Job Object API。テストは同じメソッドを持つ偽物を渡す。"""

    def __init__(self, dll: Any):
        self.dll = dll

    def create_job(self) -> int:
        handle = self.dll.CreateJobObjectW(None, None)
        if not handle:
            raise OSError("CreateJobObjectW failed")
        return handle

    def set_kill_on_close(self, handle: int, *, enabled: bool = True) -> None:
        info = kill_on_close_information() if enabled else JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if not self.dll.SetInformationJobObject(
            int(handle), JOB_OBJECT_EXTENDED_LIMIT, ctypes.byref(info), ctypes.sizeof(info),
        ):
            raise OSError("SetInformationJobObject failed")

    def assign(self, job: int, process: int) -> bool:
        return bool(self.dll.AssignProcessToJobObject(int(job), int(process)))

    def resume(self, thread: int) -> None:
        if self.dll.ResumeThread(int(thread)) == 0xFFFFFFFF:
            raise OSError("ResumeThread failed")

    def terminate(self, job: int, code: int) -> None:
        if not self.dll.TerminateJobObject(int(job), int(code)):
            raise OSError("TerminateJobObject failed")

    def close(self, handle: int) -> None:
        self.dll.CloseHandle(int(handle))

    def taskkill(self, pid: int) -> None:
        """待たない。event loop の中から呼んでも、taskkill の終了を待って止めない。"""
        token = TURN_PIDS.set(None)
        try:
            subprocess.Popen(
                taskkill_argv(pid),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            TURN_PIDS.reset(token)


class TurnJob:
    """1 つの turn が起動したプロセスだけを入れる Job。

    kotoyose-child 自身を 1 つの Job に入れると、温存した session と並行する turn を
    別々に止められない。起動したプロセスを、最初の命令の前にこの Job へ入れる。
    """

    def __init__(self, kernel: Any):
        self.kernel = kernel
        self.handle = kernel.create_job()
        try:
            kernel.set_kill_on_close(self.handle)
        except OSError:
            kernel.close(self.handle)
            raise
        self.pids: set[int] = set()
        self.failed_pids: set[int] = set()
        self._closed = False

    def creation_flags(self, flags: int) -> tuple[int, bool]:
        """戻り値の bool は、この呼び出しが CREATE_SUSPENDED を足したか。

        足した場合だけ ResumeThread する。呼び出し側が最初から停止を求めていたときは再開しない。
        """
        if flags & CREATE_SUSPENDED:
            return flags, False
        return flags | CREATE_SUSPENDED, True

    def attach(self, process: int, thread: int, pid: int, *, resume: bool) -> None:
        """割り当ててから再開する。割り当てに失敗しても、停止したままにはしない。"""
        try:
            assigned = bool(self.kernel.assign(self.handle, process))
        except OSError:
            assigned = False
        if not assigned:
            if STRICT_WINDOWS_JOBS.get():
                raise OSError("AssignProcessToJobObject failed")
            self.failed_pids.add(pid)
        if resume:
            self.kernel.resume(thread)
        self.pids.add(pid)

    def kill(self) -> None:
        """Job を終わらせてハンドルを閉じる。割り当てに失敗した pid だけ taskkill する。

        成功した pid は TerminateJobObject のあとで再利用されていることがある。
        そこへ taskkill すると、別のプロセスを止める。
        """
        if self._closed:
            return
        self._closed = True
        try:
            self.kernel.terminate(self.handle, 1)
        finally:
            self.kernel.close(self.handle)
            for pid in list(self.failed_pids):
                self.kernel.taskkill(pid)

    def release(self) -> None:
        self.kernel.set_kill_on_close(self.handle, enabled=False)
        self.kernel.close(self.handle)
        self._closed = True


def suspend_and_assign(real_create: Callable[..., tuple], job: TurnJob, args: tuple) -> tuple:
    """CreateProcess が返したスレッドを、Popen が閉じる前に Job へ入れる。

    subprocess.Popen は CreateProcess のスレッドハンドルを、呼び出し元へ返す前に閉じる。
    CREATE_SUSPENDED を Popen の引数に足すだけでは再開できない。CreateProcess の包みで
    割り当ててから ResumeThread する。主スレッドが動く前なので、孫は Job を継承する。
    """
    values = list(args)
    flags, resume = job.creation_flags(int(values[5]))
    values[5] = flags
    created = real_create(*values)
    process, thread, pid, _tid = created
    job.attach(process, thread, int(pid), resume=resume)
    return created


def windows_kernel() -> Kernel32Adapter:
    """win32 の kernel32。import 時には読まない。"""
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    dll.CreateJobObjectW.restype = ctypes.c_void_p
    dll.SetInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(JOBOBJECT_EXTENDED_LIMIT_INFORMATION), ctypes.c_uint32,
    ]
    dll.SetInformationJobObject.restype = ctypes.c_int
    dll.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    dll.AssignProcessToJobObject.restype = ctypes.c_int
    dll.ResumeThread.argtypes = [ctypes.c_void_p]
    dll.ResumeThread.restype = ctypes.c_uint32
    dll.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    dll.TerminateJobObject.restype = ctypes.c_int
    dll.CloseHandle.argtypes = [ctypes.c_void_p]
    dll.CloseHandle.restype = ctypes.c_int
    return Kernel32Adapter(dll)


def _job_for(pids: set[int]) -> TurnJob:
    job = _JOBS.get(id(pids))
    if job is None:
        job = TurnJob(windows_kernel())
        _JOBS[id(pids)] = job
    return job


def tracking_type(base: type) -> type:
    """Popen の子クラス。pid を TURN_PIDS に足すだけで、Job への割り当ては CreateProcess が行う。"""

    class TrackingPopen(base):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pids = TURN_PIDS.get()
            if isinstance(pids, _TrackedPids):
                # Codex SDK の Popen は worker thread で動く。取消後の起動と pid の登録漏れを防ぐ。
                with pids.lock:
                    if pids.killed:
                        raise OSError("child turn has stopped")
                    separate_group = sys.platform != "win32" and TURN_NEW_SESSION.get()
                    if separate_group:
                        kwargs["start_new_session"] = True
                    super().__init__(*args, **kwargs)
                    if self.pid:
                        pids.add(self.pid)
                        if separate_group:
                            pids.process_groups.add(self.pid)
                return
            super().__init__(*args, **kwargs)
            if pids is not None and (pid := getattr(self, "pid", None)):
                pids.add(pid)

    return TrackingPopen


def install_popen_tracking(*modules: Any) -> Callable[[], None]:
    """各モジュールの Popen 属性を追跡子クラスへ差し替える。

    asyncio.windows_utils.Popen は import のときに subprocess.Popen を継承する。
    subprocess.Popen を後から差し替えても、そちらのクラスは元の Popen のままなので、属性も差し替える。
    """
    originals = [(module, module.Popen) for module in modules]
    for module, original in originals:
        module.Popen = tracking_type(original)

    def restore() -> None:
        for module, original in originals:
            module.Popen = original

    return restore


def _install_create_process_hook() -> None:
    import _winapi

    original = _winapi.CreateProcess

    def hooked(*args: Any, **kwargs: Any) -> Any:
        pids = TURN_PIDS.get()
        if kwargs or pids is None:
            return original(*args, **kwargs)
        def create(*values: Any) -> tuple:
            nonlocal created
            created = original(*values)
            return created

        created = None
        try:
            return suspend_and_assign(create, _job_for(pids), args)
        except OSError:
            if created is not None:
                process, thread, _, _ = created
                try:
                    _winapi.TerminateProcess(process, 1)
                finally:
                    _winapi.CloseHandle(thread)
                    _winapi.CloseHandle(process)
            raise

    _winapi.CreateProcess = hooked

    def restore() -> None:
        _winapi.CreateProcess = original

    _RESTORE.append(restore)


def install_turn_popen() -> None:
    """turn の間だけプロセス起動を追跡する。入れ子の start は数える。"""
    global _DEPTH
    _DEPTH += 1
    if _DEPTH != 1:
        return
    modules: list[Any] = [subprocess]
    if sys.platform == "win32":
        import asyncio.windows_utils as windows_utils
        modules.append(windows_utils)
    _RESTORE.append(install_popen_tracking(*modules))
    if sys.platform == "win32":
        _install_create_process_hook()


def uninstall_turn_popen() -> None:
    """追跡を外す。まだ turn が残っているときは外さない。"""
    global _DEPTH
    _DEPTH = max(0, _DEPTH - 1)
    if _DEPTH != 0:
        return
    while _RESTORE:
        _RESTORE.pop()()


def kill_pid(pid: int) -> None:
    """プロセスグループを SIGKILL する。自分のグループとテストのグループは辿って個別に止める。"""
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return
    if pgid == pid and pgid != os.getpgrp():
        kill_process_group(pgid)
        return
    _kill_tree(pid)


def kill_process_group(pid: int) -> None:
    """Stop a process started in its own group and its descendants in other groups."""
    _kill_children(pid)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _kill_tree(pid: int) -> None:
    _kill_children(pid)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _kill_children(pid: int) -> None:
    from pathlib import Path

    children: set[int] = set()
    for path in Path(f"/proc/{pid}/task").glob("*/children"):
        try:
            children.update(int(item) for item in path.read_text(encoding="utf-8").split())
        except (OSError, ValueError):
            pass
    for child in children:
        _kill_tree(child)


def kill_pids(pids: set[int]) -> None:
    """この pid 集合の木を止める。同じ集合をもう一度渡しても、何もしない。

    Windows では集合に紐づく Job を終わらせる。Job に入れ損ねた pid だけ、その Job の
    taskkill が控えになる。Job を持たない集合を taskkill で掃かない。番号は再利用されている。
    """
    if isinstance(pids, _TrackedPids):
        with pids.lock:
            if pids.killed:
                return
            pids.killed = True
    if sys.platform == "win32":
        job = _JOBS.pop(id(pids), None)
        if job is not None:
            job.kill()
        return
    for pid in list(pids):
        if isinstance(pids, _TrackedPids) and pid in pids.process_groups:
            kill_process_group(pid)
        else:
            kill_pid(pid)


@contextmanager
def windows_job_scope():
    """Assign helper commands to a strict Job before their first instruction."""
    pids = new_turn_pids()
    strict = STRICT_WINDOWS_JOBS.set(True)
    tracking = TURN_PIDS.set(pids)
    try:
        install_turn_popen()
        yield pids
    finally:
        TURN_PIDS.reset(tracking)
        STRICT_WINDOWS_JOBS.reset(strict)
        uninstall_turn_popen()
        kill_pids(pids)


def release_windows_job(pids: set[int]) -> None:
    """Close a completed login's Job without stopping its remaining processes."""
    if (job := _JOBS.get(id(pids))) is not None:
        job.release()
        del _JOBS[id(pids)]


def bound_windows_job():
    pids = TURN_PIDS.get()
    if pids is None or not STRICT_WINDOWS_JOBS.get() or not _DEPTH:
        raise OSError("declared Windows process requires strict Job Object tracking")
    return pids
