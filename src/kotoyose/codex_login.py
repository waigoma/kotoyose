"""Official Codex device-code login session manager using `app-server` RPCs."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal
import uuid

import structlog

from .config import HarnessConfig, get_config
from .connection_state_store import ConnectionStateStore
from .connections_probe import (
    _build_codex_probe_config,
    codex_home_and_auth_path,
    probe_connection,
    resolve_connection_cli_binary,
)

log = structlog.get_logger()

DEFAULT_LOGIN_TTL_SECONDS = 15 * 60
_START_TIMEOUT_SECONDS = 20.0
_CLOSE_TIMEOUT_SECONDS = 2.0


class LoginConflictError(RuntimeError):
    """Raised when a device-code login is already active for a connection."""


class LoginNotSupportedError(ValueError):
    """Raised when device-code login is requested for an unsupported connection or missing CLI."""


@dataclass
class CodexLoginSession:
    """In-memory state for an interactive Codex device-code login attempt."""

    session_id: str
    connection_id: str
    verification_url: str
    user_code: str
    expires_at: datetime
    status: Literal["waiting", "completed", "failed", "expired"] = "waiting"
    error: str | None = None
    codex: Any = field(default=None, repr=False)
    handle: Any = field(default=None, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    cancelled: bool = False

    def public_view(self) -> dict[str, Any]:
        """Serialize public status fields without any tokens or credentials."""
        return {
            "session_id": self.session_id,
            "connection_id": self.connection_id,
            "status": self.status,
            "verification_url": self.verification_url,
            "user_code": self.user_code,
            "expires_at": self.expires_at.isoformat(),
            "error": self.error,
        }


_LOGIN_SESSIONS: dict[str, CodexLoginSession] = {}
_ACTIVE_BY_CONNECTION: dict[str, str] = {}
_LOGIN_LOCK = asyncio.Lock()


async def _close_codex_client(codex: Any) -> None:
    if codex is None:
        return
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(codex.close(), timeout=_CLOSE_TIMEOUT_SECONDS)


def _expire_if_elapsed(session: CodexLoginSession, now: datetime | None = None) -> None:
    ref = now or datetime.now(timezone.utc)
    if session.status == "waiting" and ref >= session.expires_at:
        session.status = "expired"
        if session.error is None:
            session.error = "Device code expired"


async def _watch_login_session(
    session: CodexLoginSession,
    *,
    cfg: HarnessConfig,
    execution_service: Any = None,
    store: ConnectionStateStore | None = None,
) -> None:
    try:
        remaining = max(0.0, (session.expires_at - datetime.now(timezone.utc)).total_seconds())
        async with asyncio.timeout(remaining):
            notif = await session.handle.wait()
        await _close_codex_client(session.codex)
        session.codex = None
        if getattr(notif, "success", False):
            # Probe first so state badges are fresh by the time callers observe "completed".
            try:
                await probe_connection(
                    session.connection_id,
                    cfg=cfg,
                    execution_service=execution_service,
                    store=store,
                )
            except Exception as exc:
                log.warning(
                    "connections.login_post_probe_failed",
                    connection=session.connection_id,
                    error_type=type(exc).__name__,
                )
            session.status = "completed"
            session.error = None
        else:
            session.status = "failed"
            session.error = getattr(notif, "error", None) or "Login failed"
    except TimeoutError:
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(session.handle.cancel(), timeout=2.0)
        session.status = "expired"
        session.error = "Device code expired"
    except asyncio.CancelledError:
        if session.status == "waiting":
            session.status = "failed"
            session.error = "Login cancelled"
    except Exception as exc:
        if session.status == "waiting":
            session.status = "failed"
            session.error = f"Login failed: {type(exc).__name__}"
    finally:
        await _close_codex_client(session.codex)
        session.codex = None
        if _ACTIVE_BY_CONNECTION.get(session.connection_id) == session.session_id:
            _ACTIVE_BY_CONNECTION.pop(session.connection_id, None)


async def start_codex_device_login(
    conn_id: str,
    *,
    cfg: HarnessConfig | None = None,
    execution_service: Any = None,
    store: ConnectionStateStore | None = None,
    ttl_seconds: float | None = None,
) -> dict[str, Any]:
    """Start an official `account/login/start` (`chatgptDeviceCode`) flow for `conn_id`."""
    active_cfg = cfg or get_config()
    conn = active_cfg.connections.get(conn_id)
    if conn is None:
        raise KeyError(conn_id)
    if conn.provider != "codex" or conn.kind != "subscription":
        raise LoginNotSupportedError(
            "Device-code login is only supported for Codex subscription connections"
        )
    cli_binary = resolve_connection_cli_binary(active_cfg, conn_id, conn, execution_service)
    if cli_binary is None:
        raise LoginNotSupportedError("Codex CLI binary is not configured")

    from openai_codex import AsyncCodex

    codex_home, _ = codex_home_and_auth_path(conn)
    if codex_home:
        Path(codex_home).mkdir(parents=True, exist_ok=True)
    codex_cfg = _build_codex_probe_config(cli_binary, codex_home)

    async with _LOGIN_LOCK:
        active_id = _ACTIVE_BY_CONNECTION.get(conn_id)
        if active_id is not None:
            existing = _LOGIN_SESSIONS.get(active_id)
            if existing is not None:
                _expire_if_elapsed(existing)
                if existing.status == "waiting":
                    raise LoginConflictError(f"Login already in progress for connection '{conn_id}'")
            _ACTIVE_BY_CONNECTION.pop(conn_id, None)

        codex = AsyncCodex(codex_cfg)
        try:
            handle = await asyncio.wait_for(
                codex.login_chatgpt_device_code(),
                timeout=_START_TIMEOUT_SECONDS,
            )
        except Exception:
            await _close_codex_client(codex)
            raise

        now = datetime.now(timezone.utc)
        effective_ttl = DEFAULT_LOGIN_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        expires_at = now + timedelta(seconds=effective_ttl)
        session = CodexLoginSession(
            session_id=uuid.uuid4().hex,
            connection_id=conn_id,
            verification_url=handle.verification_url,
            user_code=handle.user_code,
            expires_at=expires_at,
            codex=codex,
            handle=handle,
        )
        _LOGIN_SESSIONS[session.session_id] = session
        _ACTIVE_BY_CONNECTION[conn_id] = session.session_id
        session.task = asyncio.create_task(
            _watch_login_session(
                session,
                cfg=active_cfg,
                execution_service=execution_service,
                store=store,
            )
        )
        return {
            "session_id": session.session_id,
            "verification_url": session.verification_url,
            "user_code": session.user_code,
            "expires_at": session.expires_at.isoformat(),
        }


async def get_codex_login_status(conn_id: str, session_id: str) -> dict[str, Any]:
    """Return current status (`waiting`/`completed`/`failed`/`expired`) for a login session."""
    session = _LOGIN_SESSIONS.get(session_id)
    if session is None or session.connection_id != conn_id:
        raise KeyError(session_id)
    _expire_if_elapsed(session)
    if session.status == "expired" and session.task is not None and not session.task.done():
        session.task.cancel()
    return session.public_view()


async def cancel_codex_login(conn_id: str, session_id: str) -> dict[str, Any]:
    """Cancel an active Codex device-code login session via `account/login/cancel`."""
    session = _LOGIN_SESSIONS.get(session_id)
    if session is None or session.connection_id != conn_id:
        raise KeyError(session_id)
    if session.status == "waiting":
        session.cancelled = True
        session.status = "failed"
        session.error = "Login cancelled"
        if session.handle is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(session.handle.cancel(), timeout=2.0)
        if session.task is not None and not session.task.done():
            session.task.cancel()
            with contextlib.suppress(BaseException):
                await session.task
        await _close_codex_client(session.codex)
        session.codex = None
        if _ACTIVE_BY_CONNECTION.get(conn_id) == session_id:
            _ACTIVE_BY_CONNECTION.pop(conn_id, None)
    return session.public_view()


async def shutdown_login_sessions() -> None:
    """Cancel and close all active Codex login sessions during app shutdown."""
    for session in list(_LOGIN_SESSIONS.values()):
        if session.task is not None and not session.task.done():
            session.task.cancel()
            with contextlib.suppress(BaseException):
                await session.task
        await _close_codex_client(session.codex)
        session.codex = None
    _ACTIVE_BY_CONNECTION.clear()
    _LOGIN_SESSIONS.clear()
