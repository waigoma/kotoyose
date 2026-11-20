"""
認証ユーティリティ。

管理 UI、管理 API、Playground は管理セッションまたは HTTP Basic で認証する。
`KOTOYOSE_TOKEN` 未設定時はローカル開発用に管理認証を省略する。
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import secrets
import threading
import time
from urllib.parse import urlsplit

import structlog
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import API_KEY, BASIC_AUTH_USER

_basic = HTTPBasic(auto_error=False, realm="kotoyose-admin")
log = structlog.get_logger()

_BASIC_REALM = "kotoyose-admin"
_BASIC_CHALLENGE = {"WWW-Authenticate": f'Basic realm="{_BASIC_REALM}"'}

ADMIN_SESSION_COOKIE = "kotoyose_admin"
ADMIN_CSRF_HEADER = "X-Kotoyose-Admin"
ADMIN_SESSION_IDLE_SECONDS = 12 * 3600
ADMIN_SESSION_ABSOLUTE_SECONDS = 7 * 24 * 3600
LOGIN_RATE_LIMIT_MAX_FAILURES = 5
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 10 * 60
_CSRF_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass
class AdminSession:
    """In-memory admin console session."""

    session_id: str
    user: str
    created_at: float
    last_seen_at: float


_lock = threading.Lock()
_admin_sessions: dict[str, AdminSession] = {}
_login_failures: dict[str, list[float]] = {}


def _now() -> float:
    """Return current UTC epoch seconds (monkeypatchable in tests)."""
    return time.time()


def _is_session_expired(session: AdminSession, now: float) -> bool:
    if now - session.created_at >= ADMIN_SESSION_ABSOLUTE_SECONDS:
        return True
    if now - session.last_seen_at >= ADMIN_SESSION_IDLE_SECONDS:
        return True
    return False


def _prune_expired_sessions_locked(now: float) -> None:
    expired = [sid for sid, sess in _admin_sessions.items() if _is_session_expired(sess, now)]
    for sid in expired:
        del _admin_sessions[sid]


def create_admin_session(user: str) -> AdminSession:
    """Create and store a new in-memory admin session with a 32-byte random ID."""
    now = _now()
    session_id = secrets.token_urlsafe(32)
    session = AdminSession(
        session_id=session_id,
        user=user,
        created_at=now,
        last_seen_at=now,
    )
    with _lock:
        _prune_expired_sessions_locked(now)
        _admin_sessions[session_id] = session
    return session


def get_valid_admin_session(session_id: str | None, *, touch: bool = True) -> AdminSession | None:
    """Look up an active session, evicting it if idle or absolute TTL has elapsed."""
    if not session_id:
        return None
    now = _now()
    with _lock:
        session = _admin_sessions.get(session_id)
        if session is None:
            return None
        if _is_session_expired(session, now):
            del _admin_sessions[session_id]
            return None
        if touch:
            session.last_seen_at = now
        return session


def delete_admin_session(session_id: str | None) -> bool:
    """Delete an admin session if present."""
    if not session_id:
        return False
    with _lock:
        return _admin_sessions.pop(session_id, None) is not None


def session_expires_at(session: AdminSession) -> datetime:
    """Return the earliest of idle and absolute expiry timestamps in UTC."""
    expires_ts = min(
        session.last_seen_at + ADMIN_SESSION_IDLE_SECONDS,
        session.created_at + ADMIN_SESSION_ABSOLUTE_SECONDS,
    )
    return datetime.fromtimestamp(expires_ts, tz=timezone.utc)


def clear_admin_auth_state() -> None:
    """Clear all in-memory admin sessions and login rate-limit records."""
    with _lock:
        _admin_sessions.clear()
        _login_failures.clear()


def _prune_failures_locked(client_ip: str, now: float) -> list[float]:
    cutoff = now - LOGIN_RATE_LIMIT_WINDOW_SECONDS
    recent = [ts for ts in _login_failures.get(client_ip, ()) if ts > cutoff]
    if recent:
        _login_failures[client_ip] = recent
    else:
        _login_failures.pop(client_ip, None)
    return recent


def is_login_rate_limited(client_ip: str) -> bool:
    """Return True if client_ip has reached the failed login limit in the active window."""
    now = _now()
    with _lock:
        return len(_prune_failures_locked(client_ip, now)) >= LOGIN_RATE_LIMIT_MAX_FAILURES


def record_login_failure(client_ip: str) -> None:
    """Record a failed login timestamp for client_ip."""
    now = _now()
    with _lock:
        recent = _prune_failures_locked(client_ip, now)
        recent.append(now)
        _login_failures[client_ip] = recent


def is_https_request(request: Request) -> bool:
    """Return True when the request arrived over HTTPS (directly or via proxy)."""
    if request.url.scheme.lower() == "https":
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return forwarded_proto == "https"


def sanitize_next(next_url: str | None, default: str = "/admin/ui") -> str:
    """Allow only same-site relative redirect targets."""
    if not next_url or not isinstance(next_url, str):
        return default
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in next_url):
        return default
    if not next_url.startswith("/") or next_url.startswith("//") or next_url.startswith("/\\"):
        return default
    parsed = urlsplit(next_url)
    if parsed.scheme or parsed.netloc or "\\" in parsed.path:
        return default
    if parsed.path.startswith("/admin/logout"):
        return default
    return next_url


def _origin_parts(url_or_origin: str) -> tuple[str, str, int] | None:
    parsed = urlsplit(url_or_origin.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return (scheme, parsed.hostname.lower(), port)


def _request_origin_parts(request: Request) -> tuple[str, str, int] | None:
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    scheme = proto if proto in {"http", "https"} else request.url.scheme.lower()
    host = (request.headers.get("host") or request.url.netloc or "").strip()
    if not host:
        return None
    return _origin_parts(f"{scheme}://{host}")


def verify_admin_csrf(request: Request) -> None:
    """Enforce custom header and same-origin check on state-changing cookie requests."""
    if request.method.upper() not in _CSRF_METHODS:
        return
    if request.headers.get("x-kotoyose-admin") != "1":
        log.warning("auth.csrf.failed", reason="missing_or_invalid_header", method=request.method, path=request.url.path)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF verification failed",
        )
    origin_hdr = request.headers.get("origin")
    source_hdr = origin_hdr if origin_hdr is not None else request.headers.get("referer")
    expected = _request_origin_parts(request)
    actual = _origin_parts(source_hdr) if source_hdr else None
    if expected is None or actual is None or actual != expected:
        log.warning("auth.csrf.failed", reason="origin_mismatch", method=request.method, path=request.url.path)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF origin mismatch",
        )


def _basic_credentials_valid(credentials: HTTPBasicCredentials) -> bool:
    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        BASIC_AUTH_USER.encode("utf-8"),
    )
    pass_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        API_KEY.encode("utf-8"),
    )
    return user_ok and pass_ok


def try_authenticate_admin(
    request: Request,
    credentials: HTTPBasicCredentials | None = None,
) -> bool:
    """Authenticate an admin request via dev bypass, HTTP Basic, or session cookie.

    Enforces CSRF checks on state-changing cookie-authenticated requests.
    Populates request.state.admin_user, admin_auth_scheme, and admin_session_expires_at.
    """
    if API_KEY is None:
        # 環境変数未設定 = 認証なし (開発用)
        request.state.admin_user = "auth-disabled"
        request.state.admin_auth_scheme = "disabled"
        request.state.admin_session_expires_at = None
        return True

    if credentials is not None:
        if _basic_credentials_valid(credentials):
            request.state.admin_user = credentials.username
            request.state.admin_auth_scheme = "basic"
            request.state.admin_session_expires_at = None
            log.info("auth.basic.ok", user=credentials.username)
            return True
        return False

    session_id = request.cookies.get(ADMIN_SESSION_COOKIE)
    session = get_valid_admin_session(session_id, touch=True)
    if session is not None:
        verify_admin_csrf(request)
        request.state.admin_user = session.user
        request.state.admin_auth_scheme = "cookie"
        request.state.admin_session_expires_at = session_expires_at(session).isoformat()
        return True

    return False


def require_basic_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_basic),
) -> None:
    """管理用エンドポイントをセッション cookie または HTTP Basic で認証する。

    API_KEY (KOTOYOSE_TOKEN) が未設定なら認証なし。
    認証失敗時は WWW-Authenticate: Basic realm="kotoyose-admin" の 401 を返す。
    console の fetch (X-Kotoyose-Admin 付き) と session cookie を持つ要求には challenge を
    付けない。付けるとブラウザが独自の認証ダイアログを出し、ログイン画面へ戻れない。
    """
    if try_authenticate_admin(request, credentials):
        return

    reason = "invalid_credentials" if credentials is not None else "missing_credentials"
    log.warning("auth.basic.failed", reason=reason, path=request.url.path)
    from_console = (request.headers.get("x-kotoyose-admin") == "1"
                    or ADMIN_SESSION_COOKIE in request.cookies)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers=None if from_console else _BASIC_CHALLENGE,
    )


def admin_username(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_basic),
) -> str:
    """認証済みの admin の名前 (記録の `acknowledged_by` 用)。

    router 全体の require_basic_auth が先に資格情報を検証する。ここは名前を読むだけで、
    Bearer (agent / caller の token) は読まない。認証が無効な開発環境では固定の名前を返す。
    """
    if API_KEY is None:
        return "auth-disabled"
    admin_user = getattr(request.state, "admin_user", None)
    if isinstance(admin_user, str) and admin_user:
        return admin_user
    if credentials is not None:
        return credentials.username
    return "auth-disabled"
