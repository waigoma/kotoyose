"""Reusable PTY login session core and print-CLI interactive login manager."""

from __future__ import annotations

import asyncio
import codecs
from collections.abc import Awaitable, Callable, Sequence
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import shutil
import signal
import struct
import tempfile
import termios
from typing import Any, Literal
from urllib.parse import urlsplit
import uuid

import structlog

from .config import Connection, HarnessConfig, get_config
from .connection_state_store import ConnectionStateStore, get_connection_state_store
from .connections_probe import probe_connection
from .print_cli_home import PrintCliHomeBusyError, acquire_login_home
from .print_cli_hooks import LoginFlow, LoginFlowKind, get_login_flow, register_login_flow
from .print_cli_profiles import (
    PrintCliLoginSpec,
    PrintCliProfile,
    build_print_cli_env,
    get_print_cli_profile,
    resolve_print_cli_binary,
)

log = structlog.get_logger()

DEFAULT_START_TIMEOUT_SECONDS = 20.0
DEFAULT_SUBMIT_TIMEOUT_SECONDS = 60.0
_CLOSE_REAP_TIMEOUT_SECONDS = 2.0
_MAX_OUTPUT_BUFFER_CHARS = 4096
_CLI_OUTPUT_BUFFER_CHARS = 16384
_ERROR_TEXT_MAX_LEN = 200
_URL_SETTLE_SECONDS = 0.15
_PROMPT_SETTLE_SECONDS = 0.05
_PTY_ENV_KEYS = ("PATH", "HOME", "LANG", "TERM")

_ANSI_SEQUENCE_RE = re.compile(
    r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])"
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_REDACT_TOKEN_RE = re.compile(
    r"(?:sk-ant-[0-9A-Za-z_-]+|ya29\.[0-9A-Za-z_.-]+|Bearer\s+\S+)",
    re.IGNORECASE,
)
_REDACT_URL_RE = re.compile(r"https?://\S+")


class CliLoginConflictError(RuntimeError):
    """Raised when a CLI PTY login session is already active for a connection."""


class CliLoginNotSupportedError(ValueError):
    """Raised when CLI login is requested for an incompatible connection or missing binary."""


class CliLoginInvalidCodeError(ValueError):
    """Raised when the pasted authorization code fails input validation."""


class CliLoginSessionNotFoundError(KeyError):
    """Raised when a CLI login session ID does not exist for the connection."""


class CliLoginStartupError(RuntimeError):
    """Raised when the CLI fails to start or exits before printing an OAuth URL."""


def strip_ansi(text: str) -> str:
    """Strip ANSI/OSC/CSI escape sequences and normalize carriage returns."""
    cleaned = _ANSI_SEQUENCE_RE.sub("", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL_CHARS_RE.sub("", cleaned)


def extract_matching_url(
    text: str,
    url_pattern: str | re.Pattern[str],
    *,
    require_terminated: bool = True,
) -> str | None:
    """Return the first URL matching `url_pattern` from stripped PTY output."""
    cleaned = strip_ansi(text)
    base_pattern = url_pattern if isinstance(url_pattern, re.Pattern) else re.compile(url_pattern)
    if require_terminated:
        term_pattern = re.compile(f"({base_pattern.pattern})(?=\\s)", base_pattern.flags)
        match = term_pattern.search(cleaned)
        return match.group(1) if match is not None else None
    match = base_pattern.search(cleaned)
    return match.group(0) if match is not None else None


def validate_login_code(code: str) -> str:
    """Validate pasted OAuth code: 1-512 printable chars, no whitespace or newlines."""
    if not isinstance(code, str) or not (1 <= len(code) <= 512):
        raise CliLoginInvalidCodeError("Code must be between 1 and 512 characters")
    if any(ch.isspace() or not ch.isprintable() for ch in code):
        raise CliLoginInvalidCodeError("Code must contain only printable non-whitespace characters")
    return code


def sanitize_error_line(text: str, *, fallback: str) -> str:
    """Extract a short, redacted error line without tokens or OAuth URLs."""
    cleaned = strip_ansi(text)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    for line in reversed(lines):
        redacted = _REDACT_TOKEN_RE.sub("[redacted]", line)
        redacted = _REDACT_URL_RE.sub("[url]", redacted)
        collapsed = " ".join(redacted.split())
        if collapsed:
            return collapsed[:_ERROR_TEXT_MAX_LEN]
    return fallback[:_ERROR_TEXT_MAX_LEN]


def build_pty_env(
    login_home_env: str,
    login_home: Path,
    *,
    allowlist: Sequence[str] = _PTY_ENV_KEYS,
) -> dict[str, str]:
    """Build minimal PTY environment with `BROWSER=true` and only `login_home_env` added."""
    env = {k: os.environ[k] for k in allowlist if k in os.environ}
    env.setdefault("TERM", "xterm-256color")
    env["BROWSER"] = "true"
    env[login_home_env] = str(login_home)
    return env


def ensure_login_home_0700(path: Path) -> None:
    """Create `login_home` with mode 0700 if missing, without opening files inside it."""
    if not path.exists():
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)


@dataclass
class PtyLoginSession:
    """In-memory state for a single interactive CLI PTY session."""

    session_id: str
    connection_id: str
    url: str
    expires_at: datetime
    cli_binary: Path
    login_home: Path | None = field(default=None, kw_only=True)
    profile_id: str | None = field(default=None, kw_only=True)
    temp_cwd: Path | None = field(default=None, kw_only=True)
    flow: LoginFlowKind | None = field(default=None, kw_only=True)
    user_code: str | None = field(default=None, kw_only=True, repr=False)
    _release_home: Callable[[], None] | None = field(default=None, kw_only=True, repr=False)
    state: Literal["waiting_for_code", "waiting_for_browser", "submitting", "completed", "failed", "expired", "cancelled"] = field(
        default="waiting_for_code", kw_only=True
    )
    error: str | None = field(default=None, kw_only=True)
    result: dict[str, Any] | None = field(default=None, kw_only=True)
    _max_output_buffer_chars: int = field(default=_MAX_OUTPUT_BUFFER_CHARS, repr=False, kw_only=True)
    _process: asyncio.subprocess.Process | None = field(default=None, repr=False, kw_only=True)
    _master_fd: int | None = field(default=None, repr=False, kw_only=True)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False, kw_only=True)
    _decoder: Any = field(default=None, repr=False, kw_only=True)
    _output_buf: str = field(default="", repr=False, kw_only=True)
    _eof_event: asyncio.Event | None = field(default=None, repr=False, kw_only=True)
    _data_event: asyncio.Event | None = field(default=None, repr=False, kw_only=True)
    _expiry_task: asyncio.Task[None] | None = field(default=None, repr=False, kw_only=True)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, kw_only=True)

    def append_output_bytes(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self._decoder is None:
            self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        text = self._decoder.decode(chunk, final=False)
        if text:
            combined = self._output_buf + text
            limit = self._max_output_buffer_chars
            if len(combined) > limit:
                combined = combined[-limit:]
            self._output_buf = combined

    def wipe_buffers(self) -> None:
        self._output_buf = ""
        self._decoder = None

    def public_view(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "connection_id": self.connection_id,
            "state": self.state,
            "url": self.url if self.state in {"waiting_for_code", "waiting_for_browser"} else None,
            "expires_at": self.expires_at.isoformat(),
            "error": self.error,
        }
        if self.flow is not None:
            payload["flow"] = self.flow
        if self.flow == "device_code" and self.state == "waiting_for_browser":
            payload["user_code"] = self.user_code
        if self.state == "completed" and self.result is not None:
            payload.update(self.result)
        return payload


_SESSIONS: dict[str, PtyLoginSession] = {}
_ACTIVE_BY_CONNECTION: dict[str, str] = {}


def close_master_fd(session: PtyLoginSession) -> None:
    fd = session._master_fd
    if fd is None:
        return
    session._master_fd = None
    if session._loop is not None:
        with contextlib.suppress(RuntimeError, ValueError):
            session._loop.remove_reader(fd)
    with contextlib.suppress(OSError):
        os.close(fd)


async def terminate_pty_process(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None:
        return
    if proc.returncode is None:
        pid = proc.pid
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=0.4)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(TimeoutError, BaseException):
                await asyncio.wait_for(proc.wait(), timeout=_CLOSE_REAP_TIMEOUT_SECONDS)


async def cleanup_session_resources(
    session: PtyLoginSession,
    *,
    active_by_connection: dict[str, str] | None = None,
    extra_temp_dirs: Sequence[Path | None] = (),
    cancel_expiry_task: bool = True,
) -> None:
    if cancel_expiry_task and session._expiry_task is not None:
        current = asyncio.current_task()
        if session._expiry_task is not current and not session._expiry_task.done():
            session._expiry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await session._expiry_task
        session._expiry_task = None

    proc = session._process
    session._process = None
    await terminate_pty_process(proc)
    close_master_fd(session)
    session.wipe_buffers()

    for temp_dir in extra_temp_dirs:
        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
    if session.temp_cwd is not None:
        shutil.rmtree(session.temp_cwd, ignore_errors=True)
        session.temp_cwd = None

    if session._release_home is not None:
        session.url = ""
        session.user_code = None
        session._release_home()
        session._release_home = None

    registry = _ACTIVE_BY_CONNECTION if active_by_connection is None else active_by_connection
    if registry.get(session.connection_id) == session.session_id:
        registry.pop(session.connection_id, None)


def attach_pty_reader(session: PtyLoginSession, master_fd: int) -> None:
    loop = asyncio.get_running_loop()
    session._loop = loop
    session._master_fd = master_fd
    session._eof_event = asyncio.Event()
    session._data_event = asyncio.Event()
    os.set_blocking(master_fd, False)

    def _on_readable() -> None:
        if session._master_fd != master_fd:
            return
        while True:
            try:
                chunk = os.read(master_fd, 4096)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF):
                    close_master_fd(session)
                    if session._eof_event is not None:
                        session._eof_event.set()
                    if session._data_event is not None:
                        session._data_event.set()
                    return
                close_master_fd(session)
                if session._eof_event is not None:
                    session._eof_event.set()
                if session._data_event is not None:
                    session._data_event.set()
                return
            if not chunk:
                close_master_fd(session)
                if session._eof_event is not None:
                    session._eof_event.set()
                if session._data_event is not None:
                    session._data_event.set()
                return
            session.append_output_bytes(chunk)
            if session._data_event is not None:
                session._data_event.set()

    loop.add_reader(master_fd, _on_readable)


async def spawn_pty_process(
    cli_binary: Path,
    command_args: tuple[str, ...],
    *,
    env: dict[str, str],
    cwd: Path,
) -> tuple[asyncio.subprocess.Process, int]:
    """Start a CLI command inside a fresh PTY (never a shell)."""
    master_fd, slave_fd = pty.openpty()
    try:
        with contextlib.suppress(OSError):
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 1024, 0, 0))
        with contextlib.suppress(termios.error, OSError):
            attrs = termios.tcgetattr(slave_fd)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)

        process = await asyncio.create_subprocess_exec(
            str(cli_binary),
            *command_args,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=str(cwd),
            env=env,
            start_new_session=True,
        )
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(master_fd)
        raise
    finally:
        with contextlib.suppress(OSError):
            os.close(slave_fd)

    return process, master_fd


def _extract_official_url(
    text: str, pattern: str | re.Pattern[str], hosts: tuple[str, ...], *, require_terminated: bool,
) -> str | None:
    # Match the whole URL before applying the profile pattern so a prefix cannot
    # hide an unapproved origin (for example, an allowed host followed by @).
    cleaned = strip_ansi(text)
    for match in re.finditer(r"""https://[^\s<>"']+""", cleaned):
        if require_terminated and match.end() == len(cleaned):
            continue
        url = match.group()
        if "\\" in url or not re.fullmatch(pattern, url):
            continue
        try:
            parsed = urlsplit(url)
            if parsed.hostname in hosts and parsed.username is None and parsed.port in (None, 443):
                return url
        except ValueError:
            continue
    return None


def _public_login_error(spec: PrintCliLoginSpec | None, output: str, *, fallback: str) -> str:
    line = sanitize_error_line(output, fallback=fallback)
    if spec is not None:
        for allowed in spec.public_error_lines:
            if line.casefold() == allowed.casefold():
                return allowed
    return fallback


async def wait_for_pty_url(
    session: PtyLoginSession,
    url_pattern: str | re.Pattern[str],
    *,
    timeout_seconds: float,
    cli_label: str = "CLI",
    code_prompt_pattern: str | re.Pattern[str] | None = None,
    login_spec: PrintCliLoginSpec | None = None,
    user_code_pattern: str | None = None,
) -> str:
    """Wait until the PTY prints a matching sign-in URL or fail fast if the CLI exits."""
    assert session._data_event is not None
    assert session._eof_event is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.05, timeout_seconds)
    unterminated_since: float | None = None
    found_url: str | None = None

    def find_url(*, require_terminated: bool) -> str | None:
        if login_spec is None:
            return extract_matching_url(session._output_buf, url_pattern, require_terminated=require_terminated)
        return _extract_official_url(
            session._output_buf, url_pattern, login_spec.url_hosts, require_terminated=require_terminated,
        )

    code_re = re.compile(user_code_pattern) if user_code_pattern is not None else None
    while True:
        ready = code_re is None or session.user_code is not None
        if not ready:
            match = code_re.search(strip_ansi(session._output_buf))
            if match is not None and re.fullmatch(r"[A-Za-z0-9-]{1,32}", match.group(1) or ""):
                session.user_code = match.group(1)
                ready = True
        terminated_url = find_url(require_terminated=True)
        if terminated_url is not None:
            found_url = terminated_url
        if found_url is not None and ready:
            break

        candidate_url = find_url(require_terminated=False) if found_url is None else None
        if candidate_url is not None:
            if session._eof_event.is_set():
                found_url = candidate_url
                if ready:
                    break
            now = loop.time()
            if unterminated_since is None:
                unterminated_since = now
            elif now - unterminated_since >= _URL_SETTLE_SECONDS:
                found_url = candidate_url
                if ready:
                    break

        if session._eof_event.is_set():
            proc = session._process
            exit_code = None
            if proc is not None:
                with contextlib.suppress(TimeoutError):
                    exit_code = await asyncio.wait_for(proc.wait(), timeout=0.5)
            fallback = (
                f"{cli_label} exited with status {exit_code}"
                if exit_code is not None
                else f"{cli_label} exited before printing sign-in URL"
            )
            error = (
                sanitize_error_line(session._output_buf, fallback=fallback) if login_spec is None
                else _public_login_error(login_spec, session._output_buf, fallback=fallback)
            )
            raise CliLoginStartupError(error)

        remaining = deadline - loop.time()
        if remaining <= 0:
            expected = "device code" if found_url and not ready else "sign-in URL"
            raise CliLoginStartupError(f"Timed out waiting for {cli_label} {expected}")

        wait_slice = remaining
        if unterminated_since is not None and found_url is None:
            wait_slice = min(wait_slice, max(0.01, _URL_SETTLE_SECONDS - (loop.time() - unterminated_since)))

        session._data_event.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(session._data_event.wait(), timeout=wait_slice)

    if code_prompt_pattern is not None and not session._eof_event.is_set():
        prompt_re = (
            code_prompt_pattern
            if isinstance(code_prompt_pattern, re.Pattern)
            else re.compile(code_prompt_pattern)
        )
        prompt_deadline = min(deadline, loop.time() + _PROMPT_SETTLE_SECONDS)
        while not prompt_re.search(strip_ansi(session._output_buf)) and not session._eof_event.is_set():
            rem = prompt_deadline - loop.time()
            if rem <= 0:
                break
            session._data_event.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(session._data_event.wait(), timeout=rem)

    return found_url


async def write_pty_line(session: PtyLoginSession, line: str) -> None:
    fd = session._master_fd
    if fd is None:
        raise OSError("PTY is closed")
    data = (line + "\r").encode("utf-8")
    view = memoryview(data)
    written = 0
    while written < len(view):
        try:
            n = os.write(fd, view[written:])
            if n <= 0:
                raise OSError("Short write to PTY")
            written += n
        except BlockingIOError:
            await asyncio.sleep(0.01)


async def expire_if_elapsed(
    session: PtyLoginSession,
    *,
    cleanup: Callable[[PtyLoginSession], Awaitable[None]] | None = None,
    on_expire: Callable[[PtyLoginSession], None] | None = None,
    now: datetime | None = None,
) -> bool:
    ref = now or datetime.now(timezone.utc)
    if session.state == "waiting_for_code" and ref >= session.expires_at:
        session.state = "expired"
        session.error = "Login session expired"
        if cleanup is not None:
            await cleanup(session)
        else:
            await cleanup_session_resources(session)
        if on_expire is not None:
            on_expire(session)
        return True
    return False


async def watch_expiry(
    session: PtyLoginSession,
    *,
    cleanup: Callable[[PtyLoginSession], Awaitable[None]] | None = None,
    on_expire: Callable[[PtyLoginSession], None] | None = None,
) -> None:
    try:
        remaining = max(0.0, (session.expires_at - datetime.now(timezone.utc)).total_seconds())
        await asyncio.sleep(remaining)
        async with session._lock:
            if session.state == "waiting_for_code":
                session.state = "expired"
                session.error = "Login session expired"
                if cleanup is not None:
                    await cleanup(session)
                else:
                    await cleanup_session_resources(session, cancel_expiry_task=False)
                if on_expire is not None:
                    on_expire(session)
    except asyncio.CancelledError:
        return


# ── Print-CLI login session management ───────────────────────────────────────


def _validate_print_cli_connection(
    cfg: HarnessConfig,
    conn_id: str,
) -> tuple[Connection, PrintCliProfile, PrintCliLoginSpec, LoginFlow]:
    conn = cfg.connections.get(conn_id)
    if conn is None:
        raise KeyError(conn_id)
    if conn.kind != "subscription":
        raise CliLoginNotSupportedError("CLI login is only supported for subscription connections")
    profile = cfg.print_cli_profile_for_connection(conn_id)
    if profile is None:
        raise CliLoginNotSupportedError(
            f"Set connections.{conn_id}.profile to a print CLI profile with login support"
        )
    if profile.login is None or profile.login_flow is None:
        raise CliLoginNotSupportedError(
            f"CLI login is not supported for provider '{conn.provider}'"
        )
    if conn.config_dir is None:
        raise CliLoginNotSupportedError(
            f"{profile.id} CLI login requires a connection with config_dir"
        )
    flow = get_login_flow(profile.login_flow)
    if not profile.login.url_hosts:
        raise CliLoginNotSupportedError("CLI login requires a sign-in URL host allowlist")
    if flow.kind != "paste_code" and not profile.login.status_command:
        raise CliLoginNotSupportedError("CLI login flow requires a status command")
    if flow.kind == "device_code" and (
        not profile.login.user_code_pattern or not re.compile(profile.login.user_code_pattern).groups
    ):
        raise CliLoginNotSupportedError("Device login requires a pattern that captures the user code")
    return conn, profile, profile.login, flow


def _extract_json_dict(raw_output: str) -> dict[str, Any] | None:
    cleaned = strip_ansi(raw_output).strip()
    if not cleaned:
        return None
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    idx = 0
    last_dict: dict[str, Any] | None = None
    while idx < len(cleaned):
        pos = cleaned.find("{", idx)
        if pos == -1:
            break
        try:
            parsed, end = decoder.raw_decode(cleaned, pos)
        except ValueError:
            idx = pos + 1
            continue
        if isinstance(parsed, dict):
            last_dict = parsed
        idx = end
    return last_dict


def _check_login_success(
    spec: PrintCliLoginSpec,
    returncode: int | None,
    raw_output: str,
) -> bool:
    payload = _extract_json_dict(raw_output) if spec.success_json_field is not None else None
    return spec.matches_status(returncode, payload, raw_output)


async def _check_cli_session_state(
    session: PtyLoginSession,
    *,
    cancel_expiry_task: bool = True,
    now: datetime | None = None,
) -> bool:
    """Transition `waiting_for_code` session to `expired` or `failed` if elapsed or CLI exited."""
    if session.state != "waiting_for_code":
        return False
    ref = now or datetime.now(timezone.utc)
    if ref >= session.expires_at:
        session.state = "expired"
        session.error = "Login session expired"
        await cleanup_session_resources(
            session,
            active_by_connection=_ACTIVE_BY_CONNECTION,
            cancel_expiry_task=cancel_expiry_task,
        )
        log.info("cli_login.failed", connection=session.connection_id, session_id=session.session_id)
        return True

    if session._eof_event is not None and session._eof_event.is_set():
        proc = session._process
        returncode = proc.returncode if proc is not None else None
        if proc is not None and returncode is None:
            with contextlib.suppress(TimeoutError):
                returncode = await asyncio.wait_for(proc.wait(), timeout=0.5)
        if session.state != "waiting_for_code":
            return True
        raw_output = strip_ansi(session._output_buf)
        lower_out = raw_output.lower()
        profile = get_print_cli_profile(session.profile_id) if session.profile_id is not None else None
        is_timeout = (
            (profile is not None and profile.classify_exit_code(returncode) == "timeout")
            or "timed out" in lower_out
            or "timeout" in lower_out
            or "expired" in lower_out
        )
        if is_timeout:
            session.state = "expired"
            session.error = "Login session expired"
        else:
            session.state = "failed"
            label = profile.provider if profile is not None else "CLI"
            session.error = _public_login_error(
                profile.login if profile is not None else None, raw_output,
                fallback=f"{label} CLI exited with status {returncode}",
            )
        await cleanup_session_resources(
            session,
            active_by_connection=_ACTIVE_BY_CONNECTION,
            cancel_expiry_task=cancel_expiry_task,
        )
        log.info("cli_login.failed", connection=session.connection_id, session_id=session.session_id)
        return True

    return False


async def _watch_cli_expiry_or_eof(session: PtyLoginSession) -> None:
    assert session._eof_event is not None
    remaining = max(0.0, (session.expires_at - datetime.now(timezone.utc)).total_seconds())
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(remaining):
            await session._eof_event.wait()
    if session.state == "waiting_for_code":
        async with session._lock:
            await _check_cli_session_state(session, cancel_expiry_task=False)


async def _spawn_login_command(
    session: PtyLoginSession, profile: PrintCliProfile, command: tuple[str, ...],
) -> None:
    env = build_print_cli_env(profile, session.login_home)
    if profile.login is not None and command == profile.login.command:
        env.update(profile.login_env)
    env.setdefault("TERM", "xterm-256color")
    env["BROWSER"] = profile.fixed_env.get("BROWSER", "true")
    assert session.temp_cwd is not None
    try:
        proc, master_fd = await spawn_pty_process(
            session.cli_binary, command, env=env, cwd=session.temp_cwd,
        )
    except OSError as exc:
        raise CliLoginStartupError(f"Could not launch {profile.id} CLI: {type(exc).__name__}") from exc
    session._process = proc
    attach_pty_reader(session, master_fd)


async def _start_login_flow(
    session: PtyLoginSession, profile: PrintCliProfile, spec: PrintCliLoginSpec, timeout: float,
) -> str:
    await _spawn_login_command(session, profile, spec.command)
    return await wait_for_pty_url(
        session, spec.url_pattern, timeout_seconds=timeout, cli_label=profile.id,
        code_prompt_pattern=spec.code_prompt_pattern if session.flow == "paste_code" else None,
        user_code_pattern=spec.user_code_pattern if session.flow == "device_code" else None,
        login_spec=spec,
    )


for _kind in ("paste_code", "browser_wait", "device_code"):
    register_login_flow(_kind, LoginFlow(_kind, _start_login_flow, _check_login_success))


async def _wait_for_login_command(session: PtyLoginSession) -> int:
    assert session._process is not None
    assert session._eof_event is not None
    returncode = await session._process.wait()
    # Drain the PTY before releasing it or inspecting the status response.
    await session._eof_event.wait()
    return returncode


async def _finish_browser_login(
    session: PtyLoginSession, profile: PrintCliProfile, spec: PrintCliLoginSpec, flow: LoginFlow,
    *, cfg: HarnessConfig | None = None, execution_service: Any = None,
) -> None:
    state: Literal["completed", "failed", "expired"] = "failed"
    error: str | None = None
    try:
        remaining = max(0.0, (session.expires_at - datetime.now(timezone.utc)).total_seconds())
        async with asyncio.timeout(remaining):
            returncode = await _wait_for_login_command(session)
            if profile.classify_exit_code(returncode) == "timeout":
                raise TimeoutError
            session.wipe_buffers()
            await _spawn_login_command(session, profile, spec.status_command)
            status = await _wait_for_login_command(session)
            if flow.succeeded(spec, status, session._output_buf):
                state = "completed"
            else:
                error = f"{profile.id} CLI did not confirm sign-in"
    except TimeoutError:
        state = "expired"
        error = "Login session expired"
    except CliLoginStartupError as exc:
        error = str(exc)
    finally:
        # Cancellation can race a timeout while the worker is reaping the CLI.
        cleanup = asyncio.create_task(cleanup_session_resources(session, cancel_expiry_task=False))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
    if session.state != "cancelled":
        session.state = state
        session.error = error
    if session.state == "completed":
        # Same as the paste_code path: refresh the connection state once the credential update
        # is complete (after cleanup released the login-home lock), so the console stops
        # showing the pre-login "unknown" until the next scheduled probe.
        with contextlib.suppress(Exception):
            await probe_connection(
                session.connection_id, cfg=cfg or get_config(), execution_service=execution_service,
                store=get_connection_state_store(),
            )
    log.info("cli_login.finished", connection=session.connection_id, session_id=session.session_id, state=session.state)


async def start_cli_login_session(
    conn_id: str,
    *,
    cfg: HarnessConfig | None = None,
    execution_service: Any = None,
    ttl_seconds: float | None = None,
    start_timeout: float | None = None,
) -> dict[str, Any]:
    """Start an interactive print-CLI PTY login session for a connection with `config_dir`."""
    active_cfg = cfg or get_config()
    conn, profile, spec, flow = _validate_print_cli_connection(active_cfg, conn_id)
    assert conn.config_dir is not None

    cli_binary = resolve_print_cli_binary(profile, getattr(execution_service, "config", None))
    if cli_binary is None:
        raise CliLoginNotSupportedError(
            f"{profile.binary} CLI binary is unavailable; set drivers.cli.print.profile_binaries.{profile.id}"
        )

    active_id = _ACTIVE_BY_CONNECTION.get(conn_id)
    if active_id is not None:
        existing = _SESSIONS.get(active_id)
        if existing is None:
            raise CliLoginConflictError(f"CLI login already in progress for connection '{conn_id}'")
        if existing.state == "waiting_for_code" and not existing._lock.locked():
            async with existing._lock:
                await _check_cli_session_state(existing)
        if _ACTIVE_BY_CONNECTION.get(conn_id) is not None:
            raise CliLoginConflictError(f"CLI login already in progress for connection '{conn_id}'")

    effective_ttl = float(spec.cli_timeout_seconds) if ttl_seconds is None else ttl_seconds
    effective_start_timeout = DEFAULT_START_TIMEOUT_SECONDS if start_timeout is None else start_timeout

    session = PtyLoginSession(
        session_id=uuid.uuid4().hex,
        connection_id=conn_id,
        url="",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=effective_ttl),
        cli_binary=cli_binary,
        login_home=conn.config_dir,
        profile_id=profile.id,
        flow=flow.kind,
        state="waiting_for_code" if flow.kind == "paste_code" else "waiting_for_browser",
        _max_output_buffer_chars=_CLI_OUTPUT_BUFFER_CHARS,
    )

    try:
        session._release_home = acquire_login_home(conn.config_dir)
    except PrintCliHomeBusyError as exc:
        raise CliLoginConflictError(str(exc)) from exc

    _ACTIVE_BY_CONNECTION[conn_id] = session.session_id
    try:
        ensure_login_home_0700(conn.config_dir)
        session.temp_cwd = Path(tempfile.mkdtemp(prefix=f"kotoyose-{profile.id}-pty-cwd-"))
        if flow.kind != "paste_code":
            effective_start_timeout = min(effective_start_timeout, effective_ttl)
        session.url = await flow.start(session, profile, spec, effective_start_timeout)
        if _extract_official_url(session.url, spec.url_pattern, spec.url_hosts, require_terminated=False) != session.url:
            raise CliLoginStartupError("CLI login requires an approved sign-in URL")
        # Wipe banner/OAuth URL (state/code_challenge) immediately after extracting URL.
        session.wipe_buffers()
    except BaseException:
        await cleanup_session_resources(
            session,
            active_by_connection=_ACTIVE_BY_CONNECTION,
            cancel_expiry_task=False,
        )
        raise

    _SESSIONS[session.session_id] = session
    watcher = (
        _watch_cli_expiry_or_eof(session) if flow.kind == "paste_code"
        else _finish_browser_login(session, profile, spec, flow, cfg=active_cfg, execution_service=execution_service)
    )
    session._expiry_task = asyncio.create_task(watcher)
    log.info("cli_login.started", connection=conn_id, session_id=session.session_id)
    return session.public_view()


async def submit_cli_login_code(
    conn_id: str,
    session_id: str,
    code: str,
    *,
    cfg: HarnessConfig | None = None,
    execution_service: Any = None,
    state_store: ConnectionStateStore | None = None,
    submit_timeout: float | None = None,
) -> dict[str, Any]:
    """Send the pasted authorization code to the PTY, check success, and re-run probe."""
    session = _SESSIONS.get(session_id)
    if session is None or session.connection_id != conn_id:
        raise CliLoginSessionNotFoundError(session_id)

    if session.flow != "paste_code":
        raise CliLoginInvalidCodeError("This login flow does not accept a code")
    validated_code = validate_login_code(code)

    active_cfg = cfg or get_config()
    _conn, profile, spec, flow = _validate_print_cli_connection(active_cfg, conn_id)

    if session.profile_id != profile.id:
        raise CliLoginConflictError("Connection profile changed during CLI login")

    async with session._lock:
        if await _check_cli_session_state(session):
            return {"state": session.state, "error": session.error or "Login session expired"}
        if session.state != "waiting_for_code":
            raise CliLoginConflictError(f"Login session is already {session.state}")

        session.state = "submitting"
        session.wipe_buffers()

        try:
            if session._process is None or session._master_fd is None:
                session.error = f"{profile.id} CLI process is no longer running"
            else:
                effective_timeout = DEFAULT_SUBMIT_TIMEOUT_SECONDS if submit_timeout is None else submit_timeout
                async with asyncio.timeout(effective_timeout):
                    await write_pty_line(session, validated_code)
                    validated_code = ""
                    returncode = await _wait_for_login_command(session)
                if not flow.succeeded(spec, returncode, session._output_buf):
                    session.error = _public_login_error(
                        spec, session._output_buf, fallback=f"{profile.id} CLI exited with status {returncode}",
                    )
        except TimeoutError:
            session.error = f"{profile.id} CLI timed out waiting for authentication"
        except OSError as exc:
            session.error = f"Failed to send code to {profile.id} CLI: {type(exc).__name__}"
        except asyncio.CancelledError:
            session.state = "cancelled"
            session.error = "Login cancelled"
            raise
        finally:
            await cleanup_session_resources(session)

        if session.state == "cancelled":
            return {"state": "failed", "error": "Login cancelled"}
        if session.error is not None:
            session.state = "failed"
            log.info("cli_login.failed", connection=conn_id, session_id=session_id)
            return {"state": "failed", "error": session.error}

        # The credential update is complete. Refresh quota as a shared probe.
        session.state = "completed"
        with contextlib.suppress(Exception):
            await probe_connection(
                conn_id, cfg=active_cfg, execution_service=execution_service,
                store=state_store or get_connection_state_store(),
            )
        log.info("cli_login.completed", connection=conn_id, session_id=session_id)
        return {"state": "completed"}


async def get_cli_login_status(conn_id: str, session_id: str) -> dict[str, Any]:
    """Return current print-CLI login session status."""
    session = _SESSIONS.get(session_id)
    if session is None or session.connection_id != conn_id:
        raise CliLoginSessionNotFoundError(session_id)
    async with session._lock:
        await _check_cli_session_state(session)
        return session.public_view()


async def cancel_cli_login_session(conn_id: str, session_id: str) -> dict[str, Any]:
    """Cancel an active print-CLI PTY login session and kill its process."""
    session = _SESSIONS.get(session_id)
    if session is None or session.connection_id != conn_id:
        raise CliLoginSessionNotFoundError(session_id)
    await _cancel_cli_session(session)
    return session.public_view()


async def _cancel_cli_session(session: PtyLoginSession) -> None:
    if session.state not in {"waiting_for_code", "waiting_for_browser", "submitting"}:
        return
    previous = session.state
    session.state = "cancelled"
    session.error = "Login cancelled"
    if previous == "submitting":
        # The submit handler owns cleanup and the lease while it waits for exit.
        await terminate_pty_process(session._process)
        async with session._lock:
            pass
    elif previous == "waiting_for_browser":
        task = session._expiry_task
        assert task is not None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        # A task cancelled before its first turn never enters its finally block.
        if session._release_home is not None:
            await cleanup_session_resources(session)
    else:
        await cleanup_session_resources(session)
    log.info("cli_login.cancelled", connection=session.connection_id, session_id=session.session_id)


async def shutdown_cli_login_sessions() -> None:
    """Kill and clean up all active print-CLI PTY sessions during app shutdown."""
    for session in list(_SESSIONS.values()):
        await _cancel_cli_session(session)
        if session._expiry_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await session._expiry_task
    _ACTIVE_BY_CONNECTION.clear()
    _SESSIONS.clear()
