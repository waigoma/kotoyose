"""Official Claude Code CLI PTY login and setup-token session manager."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Literal
import uuid

import structlog

from .claude_cli import bundled_claude_cli
from .cli_login import (
    DEFAULT_START_TIMEOUT_SECONDS,
    DEFAULT_SUBMIT_TIMEOUT_SECONDS,
    CliLoginConflictError,
    CliLoginInvalidCodeError,
    CliLoginNotSupportedError,
    CliLoginSessionNotFoundError,
    CliLoginStartupError,
    PtyLoginSession,
    attach_pty_reader,
    build_pty_env,
    cleanup_session_resources,
    ensure_login_home_0700,
    expire_if_elapsed,
    extract_matching_url,
    sanitize_error_line,
    spawn_pty_process,
    strip_ansi,
    validate_login_code,
    wait_for_pty_url,
    watch_expiry,
    write_pty_line,
)
from .config import Connection, HarnessConfig, get_config
from .connection_state_store import ConnectionStateStore, get_connection_state_store
from .connections_probe import (
    classify_expiry_auth,
    probe_connection,
    resolve_connection_cli_binary,
    run_claude_auth_status,
)
from .secret_store import SecretStore, get_secret_store

log = structlog.get_logger()

DEFAULT_CLAUDE_LOGIN_TTL_SECONDS = 10 * 60

_CLAUDE_SUBSCRIPTION_PROVIDERS = frozenset({"claude", "anthropic"})
_CLAUDE_COMMAND_LOGIN: tuple[str, ...] = ("auth", "login", "--claudeai")
_CLAUDE_COMMAND_SETUP_TOKEN: tuple[str, ...] = ("setup-token",)
_ALLOWED_CLAUDE_COMMANDS = frozenset({_CLAUDE_COMMAND_LOGIN, _CLAUDE_COMMAND_SETUP_TOKEN})

_OAUTH_URL_RE = re.compile(
    r"https://(?:claude\.com|claude\.ai|platform\.claude\.com)/[^\s<>\"'\x00-\x1f\x7f]+"
)
_SETUP_TOKEN_RE = re.compile(r"sk-ant-oat[0-9A-Za-z_-]+")


class ClaudeLoginConflictError(CliLoginConflictError):
    """Raised when a Claude PTY login/setup-token session is already active for a connection."""


class ClaudeLoginNotSupportedError(CliLoginNotSupportedError):
    """Raised when Claude login/setup-token is requested for an incompatible connection or missing CLI."""


class ClaudeLoginInvalidCodeError(CliLoginInvalidCodeError):
    """Raised when the pasted authorization code fails input validation."""


class ClaudeLoginSessionNotFoundError(CliLoginSessionNotFoundError):
    """Raised when a Claude login/setup-token session ID does not exist for the connection."""


class ClaudeLoginStartupError(CliLoginStartupError):
    """Raised when the Claude CLI fails to start or exits before printing an OAuth URL."""


def extract_claude_oauth_url(text: str, *, require_terminated: bool = True) -> str | None:
    """Return the first official Claude OAuth URL from stripped PTY output."""
    return extract_matching_url(text, _OAUTH_URL_RE, require_terminated=require_terminated)


def extract_setup_token(text: str) -> str | None:
    """Extract the `sk-ant-oat...` setup-token from stripped CLI output."""
    cleaned = strip_ansi(text)
    for line in cleaned.splitlines():
        match = _SETUP_TOKEN_RE.search(line)
        if match is not None:
            return match.group(0)
    return None


def validate_claude_code(code: str) -> str:
    """Validate pasted OAuth code: 1-512 printable chars, no whitespace or newlines."""
    try:
        return validate_login_code(code)
    except CliLoginInvalidCodeError as exc:
        raise ClaudeLoginInvalidCodeError(str(exc)) from exc


def _resolve_claude_cli(
    cfg: HarnessConfig,
    conn_id: str,
    conn: Connection,
    execution_service: Any = None,
) -> Path:
    cli_binary = resolve_connection_cli_binary(cfg, conn_id, conn, execution_service)
    try:
        return cli_binary if cli_binary is not None else bundled_claude_cli()
    except ValueError as exc:
        raise ClaudeLoginNotSupportedError(str(exc)) from exc


def _build_pty_env(config_dir: Path) -> dict[str, str]:
    """Build minimal PTY environment with `BROWSER=true` and no inherited API/OAuth tokens."""
    return build_pty_env("CLAUDE_CONFIG_DIR", config_dir)


@dataclass
class ClaudeLoginSession(PtyLoginSession):
    """In-memory state for a single interactive Claude CLI PTY session."""

    kind: Literal["login", "setup_token"]
    config_dir: Path
    temp_config_dir: Path | None = None

    def public_view(self) -> dict[str, Any]:
        payload = super().public_view()
        payload["kind"] = self.kind
        return payload


_SESSIONS: dict[str, ClaudeLoginSession] = {}
_ACTIVE_BY_CONNECTION: dict[str, str] = {}
_SESSION_LOCK = asyncio.Lock()


async def _cleanup_session_resources(
    session: ClaudeLoginSession,
    *,
    cancel_expiry_task: bool = True,
) -> None:
    temp_dirs = (session.temp_config_dir,)
    session.temp_config_dir = None
    await cleanup_session_resources(
        session,
        active_by_connection=_ACTIVE_BY_CONNECTION,
        extra_temp_dirs=temp_dirs,
        cancel_expiry_task=cancel_expiry_task,
    )


def _log_claude_failed(session: PtyLoginSession) -> None:
    log.info("claude_login.failed", connection=session.connection_id, session_id=session.session_id)


async def _expire_if_elapsed(session: ClaudeLoginSession, now: datetime | None = None) -> bool:
    return await expire_if_elapsed(
        session,
        cleanup=lambda s: _cleanup_session_resources(s),  # type: ignore[arg-type]
        on_expire=_log_claude_failed,
        now=now,
    )


async def _watch_expiry(session: ClaudeLoginSession) -> None:
    await watch_expiry(
        session,
        cleanup=lambda s: _cleanup_session_resources(s, cancel_expiry_task=False),  # type: ignore[arg-type]
        on_expire=_log_claude_failed,
    )


def _validate_claude_connection(
    cfg: HarnessConfig,
    conn_id: str,
    kind: Literal["login", "setup_token"],
) -> Connection:
    conn = cfg.connections.get(conn_id)
    if conn is None:
        raise KeyError(conn_id)
    if conn.kind != "subscription" or conn.provider.lower() not in _CLAUDE_SUBSCRIPTION_PROVIDERS:
        raise ClaudeLoginNotSupportedError(
            "Claude CLI login is only supported for Claude subscription connections"
        )
    if kind == "login":
        if conn.config_dir is None:
            raise ClaudeLoginNotSupportedError(
                "Claude auth login requires a connection with config_dir"
            )
    elif kind == "setup_token":
        if conn.config_dir is not None or conn.secret_name is None:
            raise ClaudeLoginNotSupportedError(
                "Claude setup-token requires a secret-backed Claude subscription connection"
            )
    return conn


async def start_claude_login_session(
    conn_id: str,
    *,
    kind: Literal["login", "setup_token"],
    cfg: HarnessConfig | None = None,
    execution_service: Any = None,
    ttl_seconds: float | None = None,
    start_timeout: float | None = None,
) -> dict[str, Any]:
    """Start an interactive Claude CLI PTY session (`auth login --claudeai` or `setup-token`)."""
    active_cfg = cfg or get_config()
    conn = _validate_claude_connection(active_cfg, conn_id, kind)
    cli_binary = _resolve_claude_cli(active_cfg, conn_id, conn, execution_service)

    async with _SESSION_LOCK:
        active_id = _ACTIVE_BY_CONNECTION.get(conn_id)
        if active_id is not None:
            existing = _SESSIONS.get(active_id)
            if existing is not None:
                await _expire_if_elapsed(existing)
                if existing.state in {"waiting_for_code", "submitting"}:
                    raise ClaudeLoginConflictError(
                        f"Claude login already in progress for connection '{conn_id}'"
                    )
            _ACTIVE_BY_CONNECTION.pop(conn_id, None)

        temp_config_dir: Path | None = None
        temp_cwd = Path(tempfile.mkdtemp(prefix="kotoyose-claude-pty-cwd-"))
        if kind == "login":
            assert conn.config_dir is not None
            config_dir = conn.config_dir
            ensure_login_home_0700(config_dir)
            cmd_args = _CLAUDE_COMMAND_LOGIN
        else:
            temp_config_dir = Path(tempfile.mkdtemp(prefix="kotoyose-claude-setup-token-"))
            os.chmod(temp_config_dir, 0o700)
            config_dir = temp_config_dir
            cmd_args = _CLAUDE_COMMAND_SETUP_TOKEN

        if cmd_args not in _ALLOWED_CLAUDE_COMMANDS:
            raise ValueError(f"Disallowed Claude CLI command: {cmd_args!r}")

        effective_ttl = DEFAULT_CLAUDE_LOGIN_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        effective_start_timeout = DEFAULT_START_TIMEOUT_SECONDS if start_timeout is None else start_timeout
        session = ClaudeLoginSession(
            session_id=uuid.uuid4().hex,
            connection_id=conn_id,
            kind=kind,
            url="",
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=effective_ttl),
            cli_binary=cli_binary,
            config_dir=config_dir,
            login_home=config_dir,
            temp_config_dir=temp_config_dir,
            temp_cwd=temp_cwd,
        )

        try:
            proc, master_fd = await spawn_pty_process(
                cli_binary,
                cmd_args,
                env=_build_pty_env(config_dir),
                cwd=temp_cwd,
            )
            session._process = proc
            attach_pty_reader(session, master_fd)
            try:
                url = await wait_for_pty_url(
                    session,
                    _OAUTH_URL_RE,
                    timeout_seconds=effective_start_timeout,
                    cli_label="Claude CLI",
                )
            except CliLoginStartupError as exc:
                raise ClaudeLoginStartupError(str(exc)) from exc
            session.url = url
            session.wipe_buffers()
        except BaseException:
            await _cleanup_session_resources(session, cancel_expiry_task=False)
            raise

        _SESSIONS[session.session_id] = session
        _ACTIVE_BY_CONNECTION[conn_id] = session.session_id
        session._expiry_task = asyncio.create_task(_watch_expiry(session))
        log.info("claude_login.started", connection=conn_id, session_id=session.session_id)
        return {
            "session_id": session.session_id,
            "url": session.url,
            "expires_at": session.expires_at.isoformat(),
        }


async def submit_claude_login_code(
    conn_id: str,
    session_id: str,
    code: str,
    *,
    kind: Literal["login", "setup_token"],
    cfg: HarnessConfig | None = None,
    execution_service: Any = None,
    state_store: ConnectionStateStore | None = None,
    secret_store: SecretStore | None = None,
    save_secret: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
    submit_timeout: float | None = None,
) -> dict[str, Any]:
    """Send the pasted OAuth code to the PTY and wait up to 60s for completion."""
    validated_code = validate_claude_code(code)
    session = _SESSIONS.get(session_id)
    if session is None or session.connection_id != conn_id or session.kind != kind:
        raise ClaudeLoginSessionNotFoundError(session_id)

    active_cfg = cfg or get_config()
    conn = active_cfg.connections.get(conn_id)
    if conn is None:
        raise KeyError(conn_id)

    async with session._lock:
        if await _expire_if_elapsed(session):
            return {"state": "failed", "error": session.error or "Login session expired"}
        if session.state != "waiting_for_code":
            raise ClaudeLoginConflictError(f"Login session is already {session.state}")

        session.state = "submitting"
        # Wipe initial banner / OAuth URL before reading post-code CLI output.
        session.wipe_buffers()

        proc = session._process
        if proc is None or session._master_fd is None:
            session.state = "failed"
            session.error = "Claude CLI process is no longer running"
            await _cleanup_session_resources(session)
            log.info("claude_login.failed", connection=conn_id, session_id=session_id)
            return {"state": "failed", "error": session.error}

        try:
            await write_pty_line(session, validated_code)
        except OSError as exc:
            session.state = "failed"
            session.error = f"Failed to send code to Claude CLI: {type(exc).__name__}"
            await _cleanup_session_resources(session)
            log.info("claude_login.failed", connection=conn_id, session_id=session_id)
            return {"state": "failed", "error": session.error}
        finally:
            validated_code = ""

        effective_timeout = DEFAULT_SUBMIT_TIMEOUT_SECONDS if submit_timeout is None else submit_timeout
        try:
            await asyncio.wait_for(proc.wait(), timeout=effective_timeout)
            if session._eof_event is not None and not session._eof_event.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(session._eof_event.wait(), timeout=0.5)
        except TimeoutError:
            session.state = "failed"
            session.error = "Claude CLI timed out waiting for authentication"
            await _cleanup_session_resources(session)
            log.info("claude_login.failed", connection=conn_id, session_id=session_id)
            return {"state": "failed", "error": session.error}

        if session.state == "cancelled":
            await _cleanup_session_resources(session)
            return {"state": "failed", "error": "Login cancelled"}

        returncode = proc.returncode
        raw_output = session._output_buf
        session.wipe_buffers()

        try:
            if returncode != 0:
                sanitized = sanitize_error_line(
                    raw_output,
                    fallback=f"Claude CLI exited with status {returncode}",
                )
                session.state = "failed"
                session.error = sanitized
                log.info("claude_login.failed", connection=conn_id, session_id=session_id)
                return {"state": "failed", "error": sanitized}

            if kind == "login":
                raw_output = ""
                status_env = _build_pty_env(session.config_dir)
                status_env.pop("CLAUDE_CONFIG_DIR", None)
                status_info, _ = await run_claude_auth_status(
                    session.cli_binary,
                    session.config_dir,
                    extra_env=status_env,
                )
                if status_info is None or not status_info["logged_in"]:
                    session.state = "failed"
                    session.error = "Claude login did not complete"
                    log.info("claude_login.failed", connection=conn_id, session_id=session_id)
                    return {"state": "failed", "error": session.error}

                target_store = state_store or get_connection_state_store()
                now = datetime.now(timezone.utc)
                auth_state = classify_expiry_auth(status_info["expires_dt"], logged_in=True, now=now)
                await target_store.upsert(
                    conn_id,
                    auth=auth_state,
                    expires_at=status_info["expires_dt"],
                    plan=status_info["plan"],
                    last_error=None,
                    checked_at=now,
                )
                try:
                    await probe_connection(
                        conn_id,
                        cfg=active_cfg,
                        execution_service=execution_service,
                        store=target_store,
                    )
                except Exception:
                    pass

                result = {
                    "state": "completed",
                    "logged_in": True,
                    "login_expires_at": status_info["login_expires_at"],
                    "plan": status_info["plan"],
                    "email": status_info["email"],
                }
                session.state = "completed"
                session.error = None
                session.result = result
                log.info("claude_login.completed", connection=conn_id, session_id=session_id)
                return result

            # setup_token flow
            token = extract_setup_token(raw_output)
            raw_output = ""
            if not token:
                session.state = "failed"
                session.error = "No setup-token found in Claude CLI output"
                log.info("claude_login.failed", connection=conn_id, session_id=session_id)
                return {"state": "failed", "error": session.error}

            secret_name = conn.secret_name
            if not secret_name:
                token = ""
                session.state = "failed"
                session.error = "Connection has no secret store target"
                log.info("claude_login.failed", connection=conn_id, session_id=session_id)
                return {"state": "failed", "error": session.error}

            try:
                if save_secret is not None:
                    meta = await save_secret(secret_name, token)
                else:
                    target_secret_store = secret_store or get_secret_store()
                    meta = await target_secret_store.set(
                        secret_name,
                        token,
                        purpose="Claude subscription OAuth token",
                    )
            finally:
                token = ""

            target_store = state_store or get_connection_state_store()
            await target_store.upsert(
                conn_id,
                auth="ok",
                last_error=None,
                checked_at=datetime.now(timezone.utc),
            )

            result = {
                "state": "completed",
                "secret": meta["name"],
                "updated_at": meta["updated_at"],
            }
            session.state = "completed"
            session.error = None
            session.result = result
            log.info("claude_login.completed", connection=conn_id, session_id=session_id)
            return result
        finally:
            raw_output = ""
            await _cleanup_session_resources(session)


async def get_claude_login_status(
    conn_id: str,
    session_id: str,
    *,
    kind: Literal["login", "setup_token"],
) -> dict[str, Any]:
    """Return current session status (`waiting_for_code`/`submitting`/`completed`/`failed`/`expired`/`cancelled`)."""
    session = _SESSIONS.get(session_id)
    if session is None or session.connection_id != conn_id or session.kind != kind:
        raise ClaudeLoginSessionNotFoundError(session_id)
    await _expire_if_elapsed(session)
    return session.public_view()


async def cancel_claude_login_session(
    conn_id: str,
    session_id: str,
    *,
    kind: Literal["login", "setup_token"],
) -> dict[str, Any]:
    """Cancel an active Claude PTY login/setup-token session and kill its process."""
    session = _SESSIONS.get(session_id)
    if session is None or session.connection_id != conn_id or session.kind != kind:
        raise ClaudeLoginSessionNotFoundError(session_id)
    if session.state in {"waiting_for_code", "submitting"}:
        session.state = "cancelled"
        session.error = "Login cancelled"
        await _cleanup_session_resources(session)
        log.info("claude_login.cancelled", connection=conn_id, session_id=session_id)
    return session.public_view()


async def shutdown_claude_login_sessions() -> None:
    """Kill and clean up all active Claude PTY sessions during app shutdown."""
    for session in list(_SESSIONS.values()):
        await _cleanup_session_resources(session)
    _ACTIVE_BY_CONNECTION.clear()
    _SESSIONS.clear()
