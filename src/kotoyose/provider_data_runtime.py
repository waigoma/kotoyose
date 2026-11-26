"""Pinned installation and declared-command launch for data providers.

A child process starts only after the approval store accepts the exact bytes.
Login and status use the normal account home. ACP uses the declared isolation
when required. PATH and credential variables are omitted from the launch environment.
"""

from collections.abc import Callable
from contextlib import ExitStack, suppress
import ctypes
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import select
import stat
import subprocess
import tempfile
import threading
import time
from typing import BinaryIO

from .acp_spec import AcpSpec
from .cli_install_spec import platform_key
from .config_paths import default_data_dir
from .print_cli_grok_profile import GROK_ACP_READ_ARGV, GROK_ACP_WRITE_ARGV
from .provider_data import UNRESTRICTED, Declaration
from .provider_data_storage import _windows, atomic_write, locked, private_directory, private_read, restrict_owner_directory

MAX_ARTIFACT_BYTES = 256 * 1024 * 1024
_CAPTURE_LIMIT = 64 * 1024
_ENV_KEYS = frozenset({"HOME", "USERPROFILE", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "TZ"})
_WINDOWS_ENV_KEYS = {"SYSTEMROOT", "APPDATA", "LOCALAPPDATA"}
# Windows unrestricted Grok runs with the user's rights, so withholding these adds no protection;
# without them its shell tool cannot start PowerShell or find git, node, python ("program not found").
_WINDOWS_COMMAND_ENV_KEYS = frozenset({
    "PATH", "PATHEXT", "COMSPEC", "WINDIR", "SYSTEMDRIVE", "PSMODULEPATH",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "PROGRAMDATA",
    "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "OS",
    "COMPUTERNAME", "USERNAME", "USERDOMAIN",
})
_LOGIN_LOCATION = frozenset({"HOME", "USERPROFILE"})
_SECRET_ENV = re.compile(r"(?i)(key|token|secret|password|credential)")
LANDLOCK_PLATFORMS = frozenset({"linux-x64", "linux-arm64"})
_LANDLOCK_CREATE_RULESET_NR = 444  # Linux x86_64 / aarch64
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_LANDLOCK_NETWORK_ABI = 4  # Linux 6.7 introduced Landlock network rules.
_LIVE: dict[str, dict[int, Callable[[], None]]] = {}
_LIVE_LOCK = threading.Lock()


class DeclarationRejected(ValueError):
    """The declaration is not approved for execution on this machine."""


def data_env() -> dict[str, str]:
    """Login location, locale, and temp paths for a data-provider process.

    Credential-like names are dropped from the inherited environment. ``HOME`` and
    ``USERPROFILE`` stay so the official client keeps the OS user's login location.
    ``PATH`` is not passed. The process still runs with the user's own rights.
    """
    env = {}
    allowed = _ENV_KEYS | _WINDOWS_ENV_KEYS if _windows() else _ENV_KEYS
    for key, value in os.environ.items():
        if not value:
            continue
        if key in _LOGIN_LOCATION:
            env[key] = value
            continue
        if _SECRET_ENV.search(key) or key not in allowed:
            continue
        env[key] = value
    if _windows():
        from .provider_data_windows import account_folders

        profile, local = account_folders()
        env.update(HOME=str(profile), USERPROFILE=str(profile), LOCALAPPDATA=str(local))
    elif "HOME" not in env:
        env["HOME"] = str(Path.home())
    return env


def _private_group(gid: int, account) -> bool:
    """Ubuntu などの user private group (利用者と同名で、ほかに member がいない primary group) か。

    その group の書き込み権は利用者本人だけのものなので、~/.grok が 775 でも安全とみなす。
    """
    import grp

    if gid != account.pw_gid:
        return False
    try:
        group = grp.getgrgid(gid)
    except (KeyError, OSError):
        return False
    return group.gr_name == account.pw_name and set(group.gr_mem) <= {account.pw_name}


def _real_grok_home() -> Path:
    if _windows():
        from .provider_data_windows import real_grok_home

        try:
            return real_grok_home()
        except (ValueError, OSError) as exc:
            raise DeclarationRejected(f"Grok requires a private USERPROFILE\\.grok without reparse points: {exc}") from exc
    import pwd

    uid = os.getuid()
    try:
        account = pwd.getpwuid(uid)
        real_home = Path(account.pw_dir)
    except (KeyError, OSError) as exc:
        raise DeclarationRejected("Grok login requires an OS account home") from exc
    if not real_home.is_absolute() or ".." in real_home.parts:
        raise DeclarationRejected("Grok login requires an absolute OS account home without '..'")
    if (home := os.environ.get("HOME")) is not None and Path(home) != real_home:
        raise DeclarationRejected("Grok login requires HOME to match the OS account home")
    real_home = Path("/", *real_home.parts[1:])
    grok_home = real_home / ".grok"
    for path in reversed((grok_home, *grok_home.parents)):
        try:
            info = path.lstat()
        except OSError as exc:
            raise DeclarationRejected("Grok login requires an existing OS account home and ~/.grok directory") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise DeclarationRejected("Grok login path must be a directory without symlinks")
        writable = info.st_mode & (0o002 if _private_group(info.st_gid, account) else 0o022)
        if path in (real_home, grok_home) and (info.st_uid != uid or writable):
            raise DeclarationRejected("Grok login requires HOME and ~/.grok to be owned by the current user and not group/world-writable")
    return grok_home


def checked_write_cwd(cwd: Path, child_home: Path) -> Path:
    grok_home = _real_grok_home()
    try:
        resolved = cwd.resolve(strict=True)
        child_home = child_home.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DeclarationRejected("data provider write requires resolvable workspace and child home directories") from exc
    if not resolved.is_dir():
        raise DeclarationRejected("data provider write requires a workspace directory")
    if grok_home.parent.is_relative_to(resolved):
        raise DeclarationRejected("data provider write cwd contains or equals the OS account home (HOME)")
    for protected, label in ((child_home, "the child home"), (grok_home, "GROK_HOME")):
        if protected.is_relative_to(resolved) or resolved.is_relative_to(protected):
            raise DeclarationRejected(f"data provider write cwd overlaps {label}")
    return resolved


def _landlock_available() -> bool:
    if platform_key() not in LANDLOCK_PLATFORMS:
        return False
    try:
        syscall = ctypes.CDLL(None, use_errno=True).syscall
        syscall.argtypes = (ctypes.c_long,)
        syscall.restype = ctypes.c_long
        return syscall(
            _LANDLOCK_CREATE_RULESET_NR, ctypes.c_void_p(), ctypes.c_size_t(0),
            ctypes.c_uint(_LANDLOCK_CREATE_RULESET_VERSION),
        ) >= _LANDLOCK_NETWORK_ABI
    except (AttributeError, OSError, ctypes.ArgumentError):
        return False


def require_acp_landlock(spec: AcpSpec | None) -> None:
    """Grok の sandbox に依存する宣言 argv で Landlock を確認できなければ DeclarationRejected を送出する。"""
    if spec is not None and spec.argv in (GROK_ACP_READ_ARGV, GROK_ACP_WRITE_ARGV) and not _landlock_available():
        raise DeclarationRejected("data provider ACP requires Linux Landlock ABI 4 or later (kernel 6.7+) for the Grok sandbox")


def acp_data_env(row: Declaration, child_home: Path | None, leases: ExitStack) -> dict[str, str]:
    isolation = row.isolation
    windows = _windows()
    if windows and (isolation is None or child_home is None):
        raise DeclarationRejected("Windows Grok ACP requires child-managed isolation")
    env = data_env()
    if isolation is None:
        return env

    grok_home = _real_grok_home()
    # mkdtemp creates an exclusive, owner-only root. On Windows secure its ACL
    # in the same owner-controlled tree as the approval.
    parent = private_directory(child_home / "provider-data" / row.id) if windows else None
    private_home = Path(leases.enter_context(tempfile.TemporaryDirectory(prefix="kotoyose-acp-home-", dir=parent)))
    for key in ("USERPROFILE", "TMPDIR", "TMP", "TEMP"):
        env.pop(key, None)
    env["HOME"] = str(private_home)
    if windows:
        if not env.get("SYSTEMROOT"):
            raise DeclarationRejected("Windows Grok requires SYSTEMROOT")
        for key, path in {
            "USERPROFILE": private_home,
            "APPDATA": private_home / "AppData" / "Roaming", "LOCALAPPDATA": private_home / "AppData" / "Local",
            "TEMP": private_home / "Temp", "TMP": private_home / "Temp",
        }.items():
            env[key] = str(private_directory(path))
        for key, value in os.environ.items():
            upper = key.upper()
            if value and upper in _WINDOWS_COMMAND_ENV_KEYS and not _SECRET_ENV.search(upper):
                env[upper] = value
    env.update(dict.fromkeys(isolation.keep_home_env, str(grok_home)))
    env.update(isolation.env)
    return env


def read_permission_policy():
    """ACP policy for a read declaration: allow read-like tools and deny write and exec."""
    from .acp_turn import AcpPermissionPolicy

    return AcpPermissionPolicy(
        "read", "host",
        ("read", "search", "think"),
        ("edit", "delete", "move", "execute", "fetch", "switch_mode", "other"),
    )


def track_process(declaration_id: str, pid: int, kill: Callable[[], None]) -> None:
    with _LIVE_LOCK:
        _LIVE.setdefault(declaration_id, {})[pid] = kill


def untrack_process(declaration_id: str, pid: int) -> None:
    with _LIVE_LOCK:
        found = _LIVE.get(declaration_id)
        if found is None:
            return
        found.pop(pid, None)
        if not found:
            _LIVE.pop(declaration_id, None)


def stop_process(declaration_id: str, pid: int) -> None:
    """Stop one declared process and its group. Session close uses this."""
    with _LIVE_LOCK:
        found = _LIVE.get(declaration_id)
        kill = None if found is None else found.pop(pid, None)
        if found is not None and not found:
            _LIVE.pop(declaration_id, None)
    if kill is not None:
        with suppress(OSError):
            kill()


def stop_declaration(declaration_id: str) -> None:
    """Stop every tracked process started from this declaration."""
    with _LIVE_LOCK:
        killers = list(_LIVE.pop(declaration_id, {}).values())
    for kill in killers:
        with suppress(OSError):
            kill()


def _kill_tree(pid: int) -> None:
    if os.name == "nt" or pid <= 0:
        return
    from .child_process_tree import kill_process_group
    from .host_place import kill_session

    with suppress(OSError):
        kill_process_group(pid)
    with suppress(OSError):
        kill_session(pid)


def register_declared_process(declaration_id: str, pid: int) -> None:
    """Record a declared process so cancel, timeout, and revoke stop its tree."""
    if _windows():
        from .child_process_tree import bound_windows_job, kill_pids

        pids = bound_windows_job()
        if pid not in pids:
            raise OSError("declared Windows process was not tracked")
        track_process(declaration_id, pid, lambda: kill_pids(pids))
    else:
        track_process(declaration_id, pid, lambda: _kill_tree(pid))


def _root(child_home: Path | None) -> Path:
    if child_home is not None:
        restrict_owner_directory(child_home)
    return child_home if child_home is not None else default_data_dir()


def _release(row: Declaration, child_home: Path | None) -> Path:
    platform = row.platform(platform_key())
    if platform is None:
        raise DeclarationRejected("no artifact for this platform")
    digest = platform.artifact.digest.removeprefix("sha256:")
    return _root(child_home).absolute() / "tools" / "provider-data" / row.profile_id / digest


def _approval_capability(row: Declaration, child_home: Path | None) -> str:
    platform = row.platform(platform_key())
    if platform is not None and platform.supports("read"):
        return "read"
    if platform is not None and platform.supports(UNRESTRICTED) and child_home is not None:
        return UNRESTRICTED
    raise DeclarationRejected("no runnable declaration for this platform and placement")


def ensure_executable(row: Declaration, child_home: Path | None) -> None:
    """Refuse before any process starts when the child has not approved these bytes."""
    capability = _approval_capability(row, child_home)
    if child_home is not None or os.environ.get("KOTOYOSE_CHILD_RUNNER"):
        if child_home is None:
            raise DeclarationRejected("unapproved provider declaration")
        from .provider_data_child import approved_for

        approved_for(child_home, row, capability)
        return
    from .provider_data_host import host_catalog

    if not any(item.id == row.id and item.sha256 == row.sha256 for item in host_catalog().rows):
        raise DeclarationRejected("provider declaration is not in the host catalog")


_DIGEST_CACHE: dict[str, tuple[tuple[int, int, int, int], str]] = {}


def _cached_digest(path: Path, info: os.stat_result) -> str:
    """100 MB を超えるバイナリを hello や表示のたびに読み直さない。inode・大きさ・時刻が同じなら前の値を使う。"""
    key = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    cached = _DIGEST_CACHE.get(str(path))
    if cached is not None and cached[0] == key:
        return cached[1]
    digest = _digest(path)
    _DIGEST_CACHE[str(path)] = (key, digest)
    return digest


def installed_path(row: Declaration, child_home: Path | None) -> Path | None:
    try:
        platform = row.platform(platform_key())
        if platform is None:
            return None
        release = _release(row, child_home)
        if not release.is_dir() or release.is_symlink():
            return None
        private_directory(release)
        record = json.loads(private_read(release / "manifest.json", 4096))
        path = release / platform.binary
        if _windows():
            from .provider_data_windows import storage_file

            info = storage_file(path)
        else:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o500:
                return None
        if (
            not isinstance(record, dict)
            or record.get("artifact") != platform.artifact.digest
            or record.get("version") != row.install.version
            or record.get("executable") != platform.artifact.digest.removeprefix("sha256:")
        ):
            return None
        if _windows() or platform.supports(UNRESTRICTED):
            digest = _digest(path)
        else:
            digest = _cached_digest(path, info)
        return path if record["executable"] == digest else None
    except (KeyError, OSError, TypeError, ValueError):
        return None


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _harden_windows_executable(path: Path) -> None:
    with suppress(OSError):
        path.chmod(stat.S_IREAD | stat.S_IEXEC)


def managed_binary(row: Declaration, child_home: Path | None) -> Path:
    ensure_executable(row, child_home)
    path = installed_path(row, child_home)
    if path is None:
        raise DeclarationRejected("pinned CLI is not installed or failed integrity verification")
    if _windows():
        _harden_windows_executable(path)
    return path


def install_bytes(row: Declaration, payload: bytes, *, child_home: Path | None, source: str) -> Path:
    return install_stream(row, BytesIO(payload), child_home=child_home, source=source)


def install_stream(row: Declaration, payload: BinaryIO, *, child_home: Path | None, source: str) -> Path:
    """Verify the pin before enabling execution and publishing the release."""
    ensure_executable(row, child_home)
    platform = row.platform(platform_key())
    assert platform is not None
    if platform.artifact.format != "raw":
        raise ValueError("archive install is not enabled for this local artifact")
    release = _release(row, child_home)
    with locked(release.parent):
        with tempfile.TemporaryDirectory(dir=release.parent, prefix=".install-") as temporary:
            stage = Path(temporary)
            binary = stage / platform.binary
            download = stage / "artifact.part" if _windows() else binary
            with download.open("xb") as destination:
                size = 0
                while chunk := payload.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_ARTIFACT_BYTES:
                        raise ValueError("artifact exceeds its size limit")
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            actual = _digest(download)
            if not size or "sha256:" + actual != platform.artifact.digest:
                raise ValueError("artifact SHA-256 mismatch")
            ensure_executable(row, child_home)
            if existing := installed_path(row, child_home):
                return existing
            if release.exists() or release.is_symlink():
                raise ValueError("managed release already exists with invalid contents")
            if _windows():
                from .provider_data_windows import storage_file

                storage_file(download)
                download.rename(binary)
            else:
                binary.chmod(0o500)
            output = _run(binary, ("--version",), timeout=15, child_home=child_home, row=row, check=True)
            text = output.stdout[:1024].decode("utf-8", errors="replace")
            if not re.search(rf"(?<![\w.-])v?{re.escape(row.install.version)}(?![\w.-])", text):
                raise ValueError("artifact reported an unexpected version")
            if _digest(binary) != actual:
                raise ValueError("staged artifact changed during the version check")
            ensure_executable(row, child_home)
            atomic_write(stage / "manifest.json", json.dumps({
                "url": source, "version": row.install.version, "artifact": platform.artifact.digest,
                "executable": actual, "platform": platform.artifact.platform,
                "verified_at": datetime.now(timezone.utc).isoformat(),
            }).encode(), child=child_home is not None)
            stage.rename(release)
        installed = release / platform.binary
        if _windows():
            _harden_windows_executable(installed)
    if installed_path(row, child_home) != installed:
        raise ValueError("installed artifact failed integrity verification")
    return installed


def install_local(row: Declaration, artifact: Path, *, child_home: Path | None = None) -> Path:
    """Install a local file after checking the declaration pin. Used for the fake CLI and tests."""
    path = artifact.absolute()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_BYTES:
        raise ValueError("local artifact must be a regular file")
    with path.open("rb") as payload:
        return install_stream(row, payload, child_home=child_home, source=f"file:{path.name}")


def _capture(proc: subprocess.Popen, timeout: float) -> bytes:
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    if _windows():
        ready = _windows_pipe_ready(fd)
    else:
        os.set_blocking(fd, False)

        def ready(remaining):
            return 16384 if select.select([fd], [], [], min(remaining, 0.2))[0] else None

    chunks: list[bytes] = []
    total = 0
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        available = ready(remaining)
        if available is None:
            if proc.poll() is None:
                continue
            # The process can write its final output and exit during the readiness wait.
            available = ready(0)
            if available is None:
                break
        try:
            data = os.read(fd, available) if available else b""
        except BlockingIOError:
            continue
        if not data:
            break
        total += len(data)
        if total > _CAPTURE_LIMIT:
            raise ValueError("provider command output exceeded the capture limit")
        chunks.append(data)
    return b"".join(chunks)


def _windows_pipe_ready(fd: int):
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    peek = kernel.PeekNamedPipe
    peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                     ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    handle = msvcrt.get_osfhandle(fd)

    def ready(remaining):
        available = wintypes.DWORD()
        if not peek(handle, None, 0, None, ctypes.byref(available), None):
            error = ctypes.get_last_error()
            if error == 109:  # ERROR_BROKEN_PIPE
                return 0
            raise ctypes.WinError(error)
        if not available.value:
            time.sleep(min(remaining, 0.02))
            return None
        return min(available.value, 16384)

    return ready


def _run(binary: Path, argv: tuple[str, ...], *, timeout: int, child_home, row: Declaration, check: bool) -> subprocess.CompletedProcess:
    ensure_executable(row, child_home)
    with ExitStack() as scope:
        temporary = scope.enter_context(tempfile.TemporaryDirectory(prefix="kotoyose-provider-op-"))
        options: dict = {
            "cwd": temporary, "env": data_env(), "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL,
        }
        if _windows():
            from .child_process_tree import kill_pids, windows_job_scope

            pids = scope.enter_context(windows_job_scope())
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            pids = None
            options["start_new_session"] = True
        proc = subprocess.Popen([str(binary), *argv], **options)

        def kill() -> None:
            if pids is not None:
                kill_pids(pids)
            else:
                _kill_tree(proc.pid)
            with suppress(OSError):
                proc.kill()

        track_process(row.id, proc.pid, kill)
        try:
            try:
                stdout = _capture(proc, timeout)
                code = proc.wait(timeout=5)
            except (TimeoutError, subprocess.TimeoutExpired):
                kill()
                with suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
                raise ValueError("provider command timed out") from None
            except ValueError:
                kill()
                with suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
                raise
        finally:
            untrack_process(row.id, proc.pid)
            if proc.stdout is not None:
                proc.stdout.close()
    if code >= 126 or code < 0:
        raise ValueError("provider command could not execute")
    if check and code != 0:
        raise ValueError("provider command failed")
    return subprocess.CompletedProcess([str(binary), *argv], code, stdout, b"")


def operation(row: Declaration, action: str, *, child_home: Path | None = None) -> bool:
    """Run a declared login or status command. Returns whether the process exited 0."""
    argv, timeout = {
        "login": (row.login.command, row.login.cli_timeout_seconds),
        "status": (row.login.status_command, row.status_timeout),
    }[action]
    binary = managed_binary(row, child_home)
    result = _run(binary, argv, timeout=timeout, child_home=child_home, row=row, check=False)
    return result.returncode == 0
