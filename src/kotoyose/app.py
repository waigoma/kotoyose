"""
FastAPI アプリケーションの組み立て。

起動時: structlog 設定 → DB 初期化 → TTL タスク開始
終了時: TTL タスク停止 → 全セッション切断 → DB 接続プール解放
"""

import asyncio
import os
from pathlib import Path

import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import auth, db, process_env
from .api_key_store import ApiKeyStore
from .child_identity import default_host_child_home
from .child_server import ChildHub, child_listen_config
from .config import SESSION_TTL_SECONDS, get_config, missing_data_policy_message
from .config_paths import resolve_config_file_path, resolve_execution_config_path
from .claude_login import shutdown_claude_login_sessions
from .cli_login import shutdown_cli_login_sessions
from .codex_login import shutdown_login_sessions
from .connection_state_store import ConnectionStateStore, set_connection_state_store
from .connections_probe import connection_probe_loop
from .conversation.compose import create_conversations
from .db import close_db, init_db
from .delegation_mcp import DelegationMCP, delegation_route
from .execution_bootstrap import start_execution_service
from .execution_resources import ExecutionError
from .logging_setup import configure_logging
from .middleware import RequestIDMiddleware
from .models import ErrorDetail, ErrorResponse
from .routes import (
    admin, admin_children, admin_clients, admin_imports, admin_provider_data, admin_secrets, admin_settings, admin_ui,
    api_keys, chat, conversations, executions, health, models_api, observability, playground, session_continue,
    session_export,
)
from .secret_store import SecretKeyError, SecretStore, set_secret_store
from .session import evict_expired_sessions, session_cache_identities, sessions, session_last_used

log = structlog.get_logger()

_TTL_CHECK_INTERVAL = 60  # seconds


_EFFORT_WARM_DELAY = 10.0
_EFFORT_WARM_INTERVAL = 4 * 3600.0


async def _effort_warm_loop(app_state) -> None:
    """起動の少し後と、その後は一定間隔で、alias の model の使える effort を取得しておく。"""
    from .model_discovery import warm_reasoning_efforts

    await asyncio.sleep(_EFFORT_WARM_DELAY)
    while True:
        try:
            warmed = await warm_reasoning_efforts(
                get_config(), execution_service=getattr(app_state, "execution_service", None),
            )
            log.info("model_discovery.efforts_warmed", connections=warmed)
        except Exception as exc:  # noqa: BLE001 - the loop must keep running
            log.warning("model_discovery.effort_warm_loop_failed", error_type=type(exc).__name__)
        await asyncio.sleep(_EFFORT_WARM_INTERVAL)


async def _ttl_loop() -> None:
    """TTL 超過セッションを定期的に evict する。"""
    while True:
        await asyncio.sleep(_TTL_CHECK_INTERVAL)
        await evict_expired_sessions(SESSION_TTL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """アプリの起動/終了フック。"""
    configure_logging()
    scrub_extra = process_env.collect_config_env_names(get_config()) + process_env.collect_execution_config_file_env_names(
        resolve_execution_config_path()
    )
    process_env.scrub(scrub_extra)
    await init_db()
    try:
        app.state.api_key_store = ApiKeyStore(db._pool)
        if auth.API_KEY is None:
            creds_dir = (process_env.get("CREDENTIALS_DIRECTORY") or "").strip()
            cred_path = Path(creds_dir) / "kotoyose-master" if creds_dir else None
            has_explicit_key = bool(
                (cred_path is not None and (cred_path.exists() or cred_path.is_symlink()))
                or (process_env.get("KOTOYOSE_MASTER_KEY_FILE") or "").strip()
            )
            reason = (
                "admin basic auth is disabled (KOTOYOSE_TOKEN unset); "
                "refusing to enable secret store without authentication"
            )
            if has_explicit_key:
                log.error("secret_store.disabled", error=reason)
            else:
                log.warning("secret_store.disabled", error=reason)
            secret_store = SecretStore(db._pool, key_error=reason)
        else:
            try:
                secret_store = SecretStore.from_env(
                    db._pool,
                    allow_default=True,
                    create_default=True,
                )
            except SecretKeyError as exc:
                log.error("secret_store.disabled", error=str(exc))
                secret_store = SecretStore(db._pool, key_error=str(exc))
        app.state.secret_store = secret_store
        set_secret_store(secret_store)
        connection_state_store = ConnectionStateStore(db._pool)
        app.state.connection_state_store = connection_state_store
        set_connection_state_store(connection_state_store)
        app.state.conversations = create_conversations()
        app.state.execution_service = await start_execution_service()
    except BaseException:
        set_connection_state_store(None)
        await close_db()
        raise
    from .session_store import get_default_store
    log.info(
        "session_store.ready",
        enabled=get_default_store() is not None,
    )
    ttl_task = asyncio.create_task(_ttl_loop())
    probe_task = asyncio.create_task(connection_probe_loop(app_state=app.state))
    effort_task = asyncio.create_task(_effort_warm_loop(app.state))
    try:
        listen = child_listen_config()
        if listen is not None and getattr(app.state, "child_hub", None) is None:
            app.state.child_hub = ChildHub(default_host_child_home())
        if listen is not None and app.state.child_hub is not None and not app.state.child_hub.listening:
            await app.state.child_hub.start(*listen)
        if resolve_config_file_path() == os.devnull:
            from .provider_data_host import reload_catalog

            reload_catalog()
        log.info("app.started")
        yield
    finally:
        hub = getattr(app.state, "child_hub", None)
        if hub is not None:
            await hub.stop()
        probe_task.cancel()
        try:
            await probe_task
        except asyncio.CancelledError:
            pass
        effort_task.cancel()
        try:
            await effort_task
        except asyncio.CancelledError:
            pass
        await shutdown_login_sessions()
        await shutdown_claude_login_sessions()
        await shutdown_cli_login_sessions()
        set_connection_state_store(None)
        if app.state.conversations is not None:
            await app.state.conversations.close()
        if app.state.execution_service is not None:
            try:
                await app.state.execution_service.close()
            except Exception as exc:
                log.error("execution.shutdown_failed", error_type=type(exc).__name__)
            finally:
                app.state.execution_service = None
        ttl_task.cancel()
        try:
            await ttl_task
        except asyncio.CancelledError:
            pass
        for key, client in list(sessions.items()):
            try:
                await client.disconnect()
            except Exception as e:
                log.warning(
                    "session.disconnect.failed",
                    conversation_key=key,
                    error=str(e),
                )
        sessions.clear()
        session_last_used.clear()
        session_cache_identities.clear()
        await close_db()
        log.info("app.stopped")


@asynccontextmanager
async def app_lifespan(app: FastAPI):
    app.state.delegation_mcp = DelegationMCP()
    async with app.state.delegation_mcp.manager.run(), lifespan(app):
        yield


app = FastAPI(
    title="Kotoyose",
    description=(
        "Claude を OpenAI 互換 API でラップする AI 実行ハーネス。\n\n"
        "Open WebUI / n8n / curl など、OpenAI 互換クライアントからそのまま接続できます。\n\n"
        "## 認証\n\n"
        "モデル API は `Authorization: Bearer <Client API key>` で認証します。\n"
        "管理用 HTTP Basic 認証のパスワードには `KOTOYOSE_TOKEN` を使います。\n\n"
        "## 会話キー\n\n"
        "- `X-Conversation-Key` ヘッダ: n8n / Discord / curl から明示指定\n"
        "- ヘッダなし: messages 履歴のハッシュで自動生成 (Open WebUI 用)\n\n"
        "## ストリーミング\n\n"
        '`"stream": true` で SSE (Server-Sent Events) ストリーミング応答が利用可能です。'
    ),
    version="0.2.0",
    lifespan=app_lifespan,
)
app.state.api_key_store = ApiKeyStore()
app.state.secret_store = None
app.state.child_hub = None
app.add_middleware(RequestIDMiddleware)
app.router.routes.append(delegation_route())
app.include_router(health.router)
app.include_router(models_api.router)
app.include_router(chat.router)
app.include_router(conversations.router)
app.include_router(executions.router)
app.include_router(admin.router)
app.include_router(admin_clients.router)
app.include_router(admin_secrets.router)
app.include_router(admin_settings.router)
app.include_router(admin_provider_data.router)
app.include_router(admin_children.router)
app.include_router(admin_imports.router)
app.include_router(api_keys.router)
# session_export は /v1/sessions/export を持つ。api_keys.client_router の
# /v1/sessions/{conversation_key:path} に先に拾われないよう、必ずそれより前に登録する。
app.include_router(session_export.router)
app.include_router(session_continue.client_router)
app.include_router(api_keys.client_router)
app.include_router(admin_ui.router)
# /admin/sessions/continue と handoff-preview は、observability の
# /admin/sessions/{conversation_key:path} より前に登録する。
app.include_router(session_continue.admin_router)
app.include_router(observability.router)
app.include_router(playground.router)
app.include_router(playground.ui_router)


@app.exception_handler(ExecutionError)
async def execution_exception_handler(request: Request, exc: ExecutionError):
    """実行境界の拒否理由と安全な再送可否を返す。"""
    return JSONResponse(status_code=exc.status, content={"error": {
        "type": exc.code, "code": exc.code, "message": str(exc),
        "request_id": getattr(request.state, "request_id", "unknown"),
        "retryable": exc.status >= 500 and exc.status != 501,
        "safe_to_retry": exc.safe_to_retry,
        **({"reason_code": exc.reason_code} if exc.reason_code is not None else {}),
        **({"recovery_at": exc.recovery_at} if getattr(exc, "recovery_at", None) is not None else {}),
    }})


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """HTTPException を OpenAI 互換エラー形式で返す。"""
    request_id = getattr(request.state, "request_id", "unknown")
    log.warning("http.error", status_code=exc.status_code, detail=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error=ErrorDetail(
                message=str(exc.detail),
                type="api_error",
                request_id=request_id,
            )
        ).model_dump(),
        # WWW-Authenticate などの認証チャレンジヘッダを保持する
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Pydantic 検証失敗を OpenAI 互換エラー形式で返す。"""
    request_id = getattr(request.state, "request_id", "unknown")

    # exc.errors() の各エントリから loc と msg を抽出
    # loc: ("body", "preset", "mcp_servers") → "body.preset.mcp_servers"
    # msg: 検証エラーメッセージ (e.g., "Input should be a valid dictionary")
    error_messages = []
    for error in exc.errors():
        conn_id = request.path_params.get("conn_id")
        if conn_id is not None and error["type"] == "missing" and error["loc"] == ("body", "data_policy"):
            error_messages.append(missing_data_policy_message(conn_id))
            continue
        loc_parts = [str(part) for part in error.get("loc", [])]
        loc_str = ".".join(loc_parts) if loc_parts else "unknown"
        msg = error.get("msg", "Validation error")
        error_messages.append(f"{loc_str}: {msg}")

    # 複数エラーは ; で繋ぐ
    message = "; ".join(error_messages) if error_messages else "Validation error"

    log.warning(
        "validation.error",
        status_code=422,
        error_count=len(exc.errors()),
        message=message,
    )
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(
            error=ErrorDetail(
                message=message,
                type="invalid_request_error",
                request_id=request_id,
            )
        ).model_dump(),
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """未捕捉例外を 500 で返す。"""
    request_id = getattr(request.state, "request_id", "unknown")
    log.error("unhandled.exception", error=str(exc), exc_info=True)
    return JSONResponse(
        status_code=500,
        content=ErrorResponse(
            error=ErrorDetail(
                message="Internal server error",
                type="internal_error",
                request_id=request_id,
            )
        ).model_dump(),
    )
