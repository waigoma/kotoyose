"""
管理 Web UI とログイン・セッション管理エンドポイント。

GET  /admin/login                    — コンソール用ログインページ。
GET  /admin/login/console.css        — ログインページ用の console.css。
POST /admin/login                    — 資格情報検証とセッション Cookie 発行。
POST /admin/logout                   — セッション破棄と Cookie 削除。
GET  /admin/session                  — 現在の認証ユーザーとセッション有効期限。
GET  /admin/ui                       — 新しい管理 console (static/console) の HTML を返す。未認証時は /admin/login へリダイレクト。
GET  /admin/ui/console/{hash}/{path} — console のアセット。hash が現在の内容と一致するときだけ返す。
"""

import hashlib
import html
from pathlib import Path
import secrets
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasicCredentials

from .. import auth
from ..auth import (
    ADMIN_SESSION_ABSOLUTE_SECONDS,
    ADMIN_SESSION_COOKIE,
    _basic,
    create_admin_session,
    delete_admin_session,
    get_valid_admin_session,
    is_https_request,
    is_login_rate_limited,
    record_login_failure,
    require_basic_auth,
    sanitize_next,
    session_expires_at,
    try_authenticate_admin,
    verify_admin_csrf,
)

log = structlog.get_logger()

router = APIRouter(tags=["admin"])

_STATIC_ROOT = Path(__file__).resolve().parent.parent / "static"
_CONSOLE_DIR = _STATIC_ROOT / "console"
_CONSOLE_CSS_PATH = _CONSOLE_DIR / "console.css"
_CONSOLE_INDEX_PATH = _CONSOLE_DIR / "index.html"
_CONSOLE_LOGIN_PATH = _CONSOLE_DIR / "login.html"


def _console_files() -> list[Path]:
    """console のフォルダ内のファイル (順序固定)。"""
    return sorted(path for path in _CONSOLE_DIR.rglob("*") if path.is_file())


def _console_version() -> str:
    """console の全ファイルの相対パスと内容から、URL 用のハッシュを作る。

    どれか 1 つでも変わると、アセットの URL が変わって古いキャッシュを使わない。
    """
    digest = hashlib.sha256()
    for path in _console_files():
        digest.update(path.relative_to(_CONSOLE_DIR).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


_CONSOLE_VERSION = _console_version()
_CONSOLE_BASE = f"/admin/ui/console/{_CONSOLE_VERSION}"
_ASSET_HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable",
}


def _render_login_html(
    *,
    next_url: str = "/admin/ui",
    error: str | None = None,
    username: str = "",
) -> str:
    notice_html = ""
    if auth.API_KEY is None:
        notice_html = '<p class="hint">Authentication is disabled (KOTOYOSE_TOKEN is unset).</p>'
    error_html = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    template = _CONSOLE_LOGIN_PATH.read_text(encoding="utf-8")
    return (
        template.replace("__CONSOLE_VERSION__", _CONSOLE_VERSION)
        .replace("__NEXT__", html.escape(sanitize_next(next_url), quote=True))
        .replace("__USERNAME__", html.escape(username, quote=True))
        .replace("__ERROR__", error_html)
        .replace("__NOTICE__", notice_html)
    )


def _login_error_response(*, is_json: bool, status_code: int, message: str, next_url: str, username: str = ""):
    if is_json:
        return JSONResponse({"detail": message}, status_code=status_code, headers={"Cache-Control": "no-store"})
    return HTMLResponse(
        content=_render_login_html(next_url=next_url, error=message, username=username),
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(next: str | None = None) -> HTMLResponse:
    """管理コンソールのログイン画面を返す。"""
    return HTMLResponse(
        content=_render_login_html(next_url=sanitize_next(next)),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/admin/login/console.css", include_in_schema=False)
async def login_console_css() -> FileResponse:
    """ログイン画面が共有する console.css を返す。"""
    return FileResponse(
        _CONSOLE_CSS_PATH,
        media_type="text/css",
        headers=_ASSET_HEADERS,
    )


@router.post("/admin/login", include_in_schema=False)
async def login_submit(request: Request):
    """フォームまたは JSON 資格情報を検証し、セッション Cookie を発行する。"""
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    accept = (request.headers.get("accept") or "").lower()
    is_json = content_type == "application/json"

    username = ""
    password = ""
    next_raw: str | None = None

    if is_json:
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"detail": "Invalid JSON payload."}, status_code=400, headers={"Cache-Control": "no-store"})
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "Invalid JSON payload."}, status_code=400, headers={"Cache-Control": "no-store"})
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        raw_next_val = payload.get("next")
        next_raw = raw_next_val if isinstance(raw_next_val, str) else None
    elif content_type in {"application/x-www-form-urlencoded", "multipart/form-data"}:
        form = await request.form()
        username = str(form.get("username") or "")
        password = str(form.get("password") or "")
        raw_next_val = form.get("next")
        next_raw = raw_next_val if isinstance(raw_next_val, str) else None
    else:
        is_json = "application/json" in accept

    safe_next = sanitize_next(next_raw)
    client_ip = request.client.host if request.client else "unknown"

    if auth.API_KEY is None:
        return _login_error_response(
            is_json=is_json,
            status_code=401,
            message="Authentication is disabled (KOTOYOSE_TOKEN is unset).",
            next_url=safe_next,
            username=username,
        )

    if is_login_rate_limited(client_ip):
        log.warning("auth.login.rate_limited", client_ip=client_ip)
        return _login_error_response(
            is_json=is_json,
            status_code=429,
            message="Too many failed login attempts. Try again later.",
            next_url=safe_next,
            username=username,
        )

    user_ok = secrets.compare_digest(
        username.encode("utf-8"),
        auth.BASIC_AUTH_USER.encode("utf-8"),
    )
    pass_ok = secrets.compare_digest(
        password.encode("utf-8"),
        auth.API_KEY.encode("utf-8"),
    )
    if not (user_ok and pass_ok):
        record_login_failure(client_ip)
        log.warning("auth.login.failed", client_ip=client_ip, username=username)
        return _login_error_response(
            is_json=is_json,
            status_code=401,
            message="Invalid username or password.",
            next_url=safe_next,
            username=username,
        )

    session = create_admin_session(auth.BASIC_AUTH_USER)
    expires_at = session_expires_at(session).isoformat()
    log.info("auth.login.ok", client_ip=client_ip, user=session.user)

    if is_json:
        response = JSONResponse(
            {"user": session.user, "expires_at": expires_at, "next": safe_next},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )
    else:
        response = RedirectResponse(
            url=safe_next,
            status_code=303,
            headers={"Cache-Control": "no-store"},
        )

    response.set_cookie(
        key=ADMIN_SESSION_COOKIE,
        value=session.session_id,
        max_age=ADMIN_SESSION_ABSOLUTE_SECONDS,
        path="/admin",
        httponly=True,
        samesite="strict",
        secure=is_https_request(request),
    )
    return response


@router.post("/admin/logout", include_in_schema=False)
async def logout_submit(request: Request):
    """現在のセッションと Cookie を削除する。"""
    session_id = request.cookies.get(ADMIN_SESSION_COOKIE)
    session = get_valid_admin_session(session_id, touch=False)
    if session is not None:
        verify_admin_csrf(request)
        delete_admin_session(session_id)
        log.info("auth.logout.ok", user=session.user)

    response = JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})
    response.delete_cookie(
        key=ADMIN_SESSION_COOKIE,
        path="/admin",
        httponly=True,
        samesite="strict",
        secure=is_https_request(request),
    )
    return response


@router.get("/admin/session", dependencies=[Depends(require_basic_auth)], include_in_schema=False)
async def session_info(request: Request) -> JSONResponse:
    """認証済み管理者のユーザー名とセッション有効期限を返す。"""
    user = getattr(request.state, "admin_user", "auth-disabled")
    expires_at = getattr(request.state, "admin_session_expires_at", None)
    return JSONResponse(
        {"user": user, "expires_at": expires_at},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/admin/ui", response_class=HTMLResponse, include_in_schema=False)
async def admin_ui(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_basic),
):
    """認証済みの管理 console の HTML を返す。未認証時は /admin/login へリダイレクト。

    Returns:
        内容ハッシュ付き URL のアセットを参照する HTML レスポンス、またはログインへのリダイレクト。
    """
    if not try_authenticate_admin(request, credentials):
        next_target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        safe_next = sanitize_next(next_target)
        return RedirectResponse(
            url=f"/admin/login?next={quote(safe_next, safe='/')}",
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )
    html_body = _CONSOLE_INDEX_PATH.read_text(encoding="utf-8").replace("__ASSET_BASE__", _CONSOLE_BASE)
    return HTMLResponse(
        content=html_body,
        headers={"Cache-Control": "no-cache"},
    )


@router.get(
    "/admin/ui/console/{version}/{path:path}",
    dependencies=[Depends(require_basic_auth)],
    include_in_schema=False,
)
async def console_asset(version: str, path: str) -> FileResponse:
    """console のアセットを返す。版が現在の内容のハッシュと違えば 404。

    index.html / login.html は HTML テンプレートなので、ここでは配らない。

    Args:
        version: URL に埋め込まれた console の内容ハッシュ。
        path: console フォルダ内の相対パス。

    Returns:
        長期キャッシュ可能なアセットのレスポンス。
    """
    if version != _CONSOLE_VERSION:
        raise HTTPException(status_code=404, detail="asset version not found")
    target = (_CONSOLE_DIR / path).resolve()
    if (
        not target.is_relative_to(_CONSOLE_DIR)
        or not target.is_file()
        or target in {_CONSOLE_INDEX_PATH, _CONSOLE_LOGIN_PATH}
    ):
        raise HTTPException(status_code=404, detail="asset not found")
    return FileResponse(target, headers=_ASSET_HEADERS)
