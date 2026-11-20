"""route / pool alias / risk ack の管理エンドポイント。

- POST/PUT/DELETE /admin/routes/{name}, POST /admin/routes/{name}/rename
- POST/DELETE     /admin/routes/{name}/ack   : 承諾の記録と取り消し (admin の Basic 認証だけ)
- POST/PUT/DELETE /admin/pools/{name}
- GET             /admin/pools/state : pool 候補の状態 (管理 UI の読み取り専用。/v1/pools と同じ view)

保存の経路は MCP などと同じ (複製 → 変更 → save_config の全体検証 → 原子的な書き込みと再読込)。
承諾を書く経路は、この router の `require_basic_auth` を通った admin だけが届く。
Bearer の agent / caller token では 401 になる。
"""

from datetime import datetime, timezone
import asyncio
from typing import Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from ..data_policy import connection_data_policy
from ..agent_sanction import route_digest, route_reasons
from ..auth import admin_username, require_basic_auth, verify_admin_csrf
from ..claude_login import (
    ClaudeLoginConflictError,
    ClaudeLoginInvalidCodeError,
    ClaudeLoginNotSupportedError,
    ClaudeLoginSessionNotFoundError,
    ClaudeLoginStartupError,
    cancel_claude_login_session,
    get_claude_login_status,
    start_claude_login_session,
    submit_claude_login_code,
    validate_claude_code,
)
from ..cli_installer import CliInstallConflict, CliInstallError, install_tool, tool_status
from ..cli_login import (
    CliLoginConflictError,
    CliLoginInvalidCodeError,
    CliLoginNotSupportedError,
    CliLoginSessionNotFoundError,
    CliLoginStartupError,
    cancel_cli_login_session,
    get_cli_login_status,
    start_cli_login_session,
    submit_cli_login_code,
    validate_login_code,
)
from ..codex_login import (
    LoginConflictError,
    LoginNotSupportedError,
    cancel_codex_login,
    get_codex_login_status,
    start_codex_device_login,
)
from ..config import AgentRoute, Connection, PoolAlias, ProviderOrder, RiskAck, get_clients, get_config
from ..connection_state_store import default_connection_state, get_connection_state_store
from ..connections_probe import (
    _CLI_VERSION_CACHE,  # noqa: F401
    probe_cli_version as _probe_cli_version,
    probe_connection,
)
from ..print_cli_home import PrintCliHomeBusyError
from ..print_cli_profiles import PRINT_CLI_PROFILES
from .admin import _save_config_or_422, _save_resource
from .admin_catalog import route_sanction_view
from .admin_secrets import require_secret_store, write_secret_value
from .models_api import pool_view

log = structlog.get_logger()

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_basic_auth)],
)


class ProviderOrderRequest(BaseModel):
    provider_order: ProviderOrder


@router.put("/settings/provider-order", summary="管理 UI の provider 表示順を更新する")
async def update_provider_order(body: ProviderOrderRequest):
    new_cfg = get_config().model_copy(deep=True)
    new_cfg.settings.provider_order = body.provider_order
    _save_config_or_422(new_cfg)
    return {"status": "ok", "provider_order": body.provider_order}


class RouteRenameRequest(BaseModel):
    new_name: str = Field(min_length=1)


class AckRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class ClaudeCodeRequest(BaseModel):
    """Pasted OAuth code for official Claude CLI login/setup-token (never logged)."""

    code: str = Field(repr=False)

    @field_validator("code")
    @classmethod
    def _check_code(cls, value: str) -> str:
        return validate_claude_code(value)


class CliCodeRequest(BaseModel):
    """Pasted authorization code for interactive print-CLI login (never logged)."""

    code: str = Field(repr=False)

    @field_validator("code")
    @classmethod
    def _check_code(cls, value: str) -> str:
        return validate_login_code(value)


def _connection_routes(cfg, conn_id: str) -> list[str]:
    """指定 connection を参照する route 名一覧。"""
    return sorted(route_name for route_name, route in cfg.routes.items() if route.connection == conn_id)


def _connection_view(
    cfg,
    conn_id: str,
    conn: Connection,
    state: dict[str, Any] | None = None,
    states: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    data = conn.model_dump(mode="json")
    data["effective_data_policy"] = connection_data_policy(conn)
    if conn.kind == "subscription":
        data.setdefault("home", conn.home or "default")
    data.setdefault("show_quota", conn.show_quota)
    effective_state = dict(state if state is not None else default_connection_state(conn_id))
    if conn.quota_from:
        source_state = (states or {}).get(conn.quota_from) or default_connection_state(conn.quota_from)
        effective_state["quota"] = list(source_state.get("quota") or [])
        effective_state["quota_source"] = source_state.get("quota_source")
        effective_state["quota_via"] = conn.quota_from
        if effective_state.get("checked_at") is None:
            effective_state["checked_at"] = source_state.get("checked_at")
    return {
        "id": conn_id,
        **data,
        "routes": _connection_routes(cfg, conn_id),
        "state": effective_state,
    }


def _resolve_state_store(request: Request | None):
    if request is not None:
        store = getattr(request.app.state, "connection_state_store", None)
        if store is not None:
            return store
    return get_connection_state_store()


@router.get("/tools", summary="管理対象の CLI と導入状態")
def list_tools():
    return {"tools": [tool_status(profile) for profile in PRINT_CLI_PROFILES.values() if profile.install]}


@router.post("/tools/{profile}/install", summary="検証済みの CLI を導入する")
def install_cli_tool(profile: str, request: Request):
    if getattr(request.state, "admin_auth_scheme", None) not in {"basic", "cookie"}:
        raise HTTPException(status_code=403, detail="Installing a CLI requires owner authentication")
    if request.state.admin_auth_scheme == "basic":
        verify_admin_csrf(request)
    declaration = PRINT_CLI_PROFILES.get(profile)
    if declaration is None or declaration.install is None:
        raise HTTPException(status_code=404, detail="No managed distribution for this profile")
    try:
        return install_tool(declaration)
    except CliInstallConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CliInstallError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/connections", summary="connection 一覧")
async def list_connections(request: Request):
    cfg = get_config()
    store = _resolve_state_store(request)
    states = await store.list()
    return {
        "connections": [
            _connection_view(cfg, conn_id, conn, states.get(conn_id), states=states)
            for conn_id, conn in sorted(cfg.connections.items())
        ]
    }


async def _run_connection_probe(conn_id: str, request: Request, *, force_refresh: bool) -> dict[str, Any]:
    cfg = get_config()
    conn = cfg.connections.get(conn_id)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"connection '{conn_id}' not found")
    store = _resolve_state_store(request)
    execution_service = getattr(request.app.state, "execution_service", None)
    try:
        state = await probe_connection(
            conn_id,
            cfg=cfg,
            execution_service=execution_service,
            store=store,
            force_refresh=force_refresh,
        )
    except PrintCliHomeBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    states = {conn.quota_from: await store.get(conn.quota_from)} if conn.quota_from else None
    clean_states = {k: v for k, v in (states or {}).items() if v is not None}
    log.info("admin.connection.probed", connection=conn_id, auth=state.get("auth"), force_refresh=force_refresh)
    return {
        "status": "ok",
        "connection": _connection_view(cfg, conn_id, conn, state, states=clean_states),
        "state": state,
    }


@router.post("/connections/{conn_id}/probe", summary="connection の状態を今すぐ確認する")
async def probe_connection_now(conn_id: str, request: Request):
    return await _run_connection_probe(conn_id, request, force_refresh=False)


@router.post("/connections/{conn_id}/refresh", summary="Codex connection の token refresh と状態確認を今すぐ実行する")
async def refresh_connection_now(conn_id: str, request: Request):
    cfg = get_config()
    conn = cfg.connections.get(conn_id)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"connection '{conn_id}' not found")
    if not (conn.provider == "codex" and conn.kind == "subscription"):
        raise HTTPException(
            status_code=400,
            detail="Manual refresh is only supported for Codex subscription connections",
        )
    return await _run_connection_probe(conn_id, request, force_refresh=True)


@router.post("/connections/{conn_id}/login", summary="Codex device-code login を開始する")
async def start_connection_login(conn_id: str, request: Request):
    cfg = get_config()
    store = _resolve_state_store(request)
    execution_service = getattr(request.app.state, "execution_service", None)
    try:
        result = await start_codex_device_login(
            conn_id,
            cfg=cfg,
            execution_service=execution_service,
            store=store,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"connection '{conn_id}' not found")
    except LoginNotSupportedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LoginConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    log.info("admin.connection.login_started", connection=conn_id, session_id=result["session_id"])
    return result


@router.get("/connections/{conn_id}/login/{session_id}", summary="Codex device-code login 状態を確認する")
async def poll_connection_login(conn_id: str, session_id: str, request: Request):
    cfg = get_config()
    if conn_id not in cfg.connections:
        raise HTTPException(status_code=404, detail=f"connection '{conn_id}' not found")
    try:
        status_view = await get_codex_login_status(conn_id, session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"login session '{session_id}' not found")
    if status_view.get("status") == "completed":
        store = _resolve_state_store(request)
        status_view["state"] = await store.get(conn_id) or default_connection_state(conn_id)
    return status_view


@router.delete("/connections/{conn_id}/login/{session_id}", summary="Codex device-code login をキャンセルする")
async def delete_connection_login(conn_id: str, session_id: str):
    cfg = get_config()
    if conn_id not in cfg.connections:
        raise HTTPException(status_code=404, detail=f"connection '{conn_id}' not found")
    try:
        res = await cancel_codex_login(conn_id, session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"login session '{session_id}' not found")
    log.info("admin.connection.login_cancelled", connection=conn_id, session_id=session_id)
    return res


async def _start_claude_flow(
    conn_id: str,
    request: Request,
    *,
    kind: Literal["login", "setup_token"],
) -> dict[str, Any]:
    cfg = get_config()
    if kind == "setup_token":
        require_secret_store(request)
    execution_service = getattr(request.app.state, "execution_service", None)
    try:
        return await start_claude_login_session(
            conn_id,
            kind=kind,
            cfg=cfg,
            execution_service=execution_service,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"connection '{conn_id}' not found")
    except ClaudeLoginNotSupportedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ClaudeLoginConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ClaudeLoginStartupError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


async def _submit_claude_code_flow(
    conn_id: str,
    session_id: str,
    body: ClaudeCodeRequest,
    request: Request,
    *,
    kind: Literal["login", "setup_token"],
    actor: str,
) -> dict[str, Any]:
    cfg = get_config()
    if conn_id not in cfg.connections:
        raise HTTPException(status_code=404, detail=f"connection '{conn_id}' not found")
    store = _resolve_state_store(request)
    execution_service = getattr(request.app.state, "execution_service", None)

    async def _save_secret(secret_name: str, token_value: str) -> dict[str, Any]:
        return await write_secret_value(
            request,
            secret_name,
            token_value,
            purpose="Claude subscription OAuth token",
            actor=actor,
            emit_log=False,
        )

    try:
        return await submit_claude_login_code(
            conn_id,
            session_id,
            body.code,
            kind=kind,
            cfg=cfg,
            execution_service=execution_service,
            state_store=store,
            save_secret=_save_secret if kind == "setup_token" else None,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"login session '{session_id}' not found")
    except ClaudeLoginInvalidCodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ClaudeLoginConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


async def _get_claude_flow_status(
    conn_id: str,
    session_id: str,
    *,
    kind: Literal["login", "setup_token"],
) -> dict[str, Any]:
    cfg = get_config()
    if conn_id not in cfg.connections:
        raise HTTPException(status_code=404, detail=f"connection '{conn_id}' not found")
    try:
        return await get_claude_login_status(conn_id, session_id, kind=kind)
    except ClaudeLoginSessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"login session '{session_id}' not found")


async def _cancel_claude_flow(
    conn_id: str,
    session_id: str,
    *,
    kind: Literal["login", "setup_token"],
) -> dict[str, Any]:
    cfg = get_config()
    if conn_id not in cfg.connections:
        raise HTTPException(status_code=404, detail=f"connection '{conn_id}' not found")
    try:
        return await cancel_claude_login_session(conn_id, session_id, kind=kind)
    except ClaudeLoginSessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"login session '{session_id}' not found")


@router.post("/connections/{conn_id}/claude-login", summary="Claude CLI login (config_dir) を開始する")
async def start_claude_connection_login(conn_id: str, request: Request):
    return await _start_claude_flow(conn_id, request, kind="login")


@router.post("/connections/{conn_id}/claude-login/{session_id}/code", summary="Claude CLI login に OAuth code を送信する")
async def submit_claude_connection_login_code(
    conn_id: str,
    session_id: str,
    body: ClaudeCodeRequest,
    request: Request,
    actor: str = Depends(admin_username),
):
    return await _submit_claude_code_flow(
        conn_id, session_id, body, request, kind="login", actor=actor
    )


@router.get("/connections/{conn_id}/claude-login/{session_id}", summary="Claude CLI login 状態を確認する")
async def poll_claude_connection_login(conn_id: str, session_id: str):
    return await _get_claude_flow_status(conn_id, session_id, kind="login")


@router.delete("/connections/{conn_id}/claude-login/{session_id}", summary="Claude CLI login をキャンセルする")
async def delete_claude_connection_login(conn_id: str, session_id: str):
    return await _cancel_claude_flow(conn_id, session_id, kind="login")


@router.post("/connections/{conn_id}/claude-setup-token", summary="Claude CLI setup-token 発行を開始する")
async def start_claude_connection_setup_token(conn_id: str, request: Request):
    return await _start_claude_flow(conn_id, request, kind="setup_token")


@router.post("/connections/{conn_id}/claude-setup-token/{session_id}/code", summary="Claude CLI setup-token に OAuth code を送信する")
async def submit_claude_connection_setup_token_code(
    conn_id: str,
    session_id: str,
    body: ClaudeCodeRequest,
    request: Request,
    actor: str = Depends(admin_username),
):
    return await _submit_claude_code_flow(
        conn_id, session_id, body, request, kind="setup_token", actor=actor
    )


@router.get("/connections/{conn_id}/claude-setup-token/{session_id}", summary="Claude CLI setup-token 状態を確認する")
async def poll_claude_connection_setup_token(conn_id: str, session_id: str):
    return await _get_claude_flow_status(conn_id, session_id, kind="setup_token")


@router.delete("/connections/{conn_id}/claude-setup-token/{session_id}", summary="Claude CLI setup-token をキャンセルする")
async def delete_claude_connection_setup_token(conn_id: str, session_id: str):
    return await _cancel_claude_flow(conn_id, session_id, kind="setup_token")


@router.post("/connections/{conn_id}/cli-login", summary="Print-CLI login (config_dir) を開始する")
async def start_print_cli_connection_login(conn_id: str, request: Request):
    cfg = get_config()
    execution_service = getattr(request.app.state, "execution_service", None)
    try:
        return await start_cli_login_session(
            conn_id,
            cfg=cfg,
            execution_service=execution_service,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"connection '{conn_id}' not found")
    except CliLoginNotSupportedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except CliLoginConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except CliLoginStartupError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/connections/{conn_id}/cli-login/{session_id}/code", summary="Print-CLI login に code を送信する")
async def submit_print_cli_connection_login_code(
    conn_id: str,
    session_id: str,
    body: CliCodeRequest,
    request: Request,
):
    cfg = get_config()
    if conn_id not in cfg.connections:
        raise HTTPException(status_code=404, detail=f"connection '{conn_id}' not found")
    store = _resolve_state_store(request)
    execution_service = getattr(request.app.state, "execution_service", None)
    try:
        return await submit_cli_login_code(
            conn_id,
            session_id,
            body.code,
            cfg=cfg,
            execution_service=execution_service,
            state_store=store,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"login session '{session_id}' not found")
    except CliLoginNotSupportedError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except CliLoginInvalidCodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except CliLoginConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/connections/{conn_id}/cli-login/{session_id}", summary="Print-CLI login 状態を確認する")
async def poll_print_cli_connection_login(conn_id: str, session_id: str):
    cfg = get_config()
    if conn_id not in cfg.connections:
        raise HTTPException(status_code=404, detail=f"connection '{conn_id}' not found")
    try:
        return await get_cli_login_status(conn_id, session_id)
    except CliLoginSessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"login session '{session_id}' not found")


@router.delete("/connections/{conn_id}/cli-login/{session_id}", summary="Print-CLI login をキャンセルする")
async def delete_print_cli_connection_login(conn_id: str, session_id: str):
    cfg = get_config()
    if conn_id not in cfg.connections:
        raise HTTPException(status_code=404, detail=f"connection '{conn_id}' not found")
    try:
        return await cancel_cli_login_session(conn_id, session_id)
    except CliLoginSessionNotFoundError:
        raise HTTPException(status_code=404, detail=f"login session '{session_id}' not found")


@router.post("/connections/{conn_id}", summary="connection 追加")
async def create_connection(conn_id: str, connection: Connection, request: Request):
    result = await _save_resource("connections", conn_id, connection, False)
    log.info("admin.connection.created", connection=conn_id)
    cfg = get_config()
    store = _resolve_state_store(request)
    state = await store.get(conn_id)
    conn = cfg.connections[conn_id]
    states = {conn.quota_from: v} if conn.quota_from and (v := await store.get(conn.quota_from)) is not None else None
    return {**result, "connection": _connection_view(cfg, conn_id, conn, state, states=states)}


@router.put("/connections/{conn_id}", summary="connection 更新")
async def update_connection(conn_id: str, connection: Connection, request: Request):
    result = await _save_resource("connections", conn_id, connection, True)
    log.info("admin.connection.updated", connection=conn_id)
    cfg = get_config()
    store = _resolve_state_store(request)
    state = await store.get(conn_id)
    conn = cfg.connections[conn_id]
    states = {conn.quota_from: v} if conn.quota_from and (v := await store.get(conn.quota_from)) is not None else None
    return {**result, "connection": _connection_view(cfg, conn_id, conn, state, states=states)}


@router.delete("/connections/{conn_id}", summary="connection 削除")
async def delete_connection(conn_id: str, request: Request):
    cfg = get_config()
    if conn_id not in cfg.connections:
        raise HTTPException(status_code=404, detail=f"connection '{conn_id}' not found")
    if references := _connection_routes(cfg, conn_id):
        raise HTTPException(
            status_code=409,
            detail=f"connection '{conn_id}' is referenced by routes: {', '.join(references)}",
        )
    quota_refs = sorted(cid for cid, c in cfg.connections.items() if c.quota_from == conn_id)
    if quota_refs:
        raise HTTPException(
            status_code=409,
            detail=f"connection '{conn_id}' is referenced by quota_from on: {', '.join(quota_refs)}",
        )
    new_cfg = cfg.model_copy(deep=True)
    del new_cfg.connections[conn_id]
    _save_config_or_422(new_cfg)
    await _resolve_state_store(request).delete(conn_id)
    log.info("admin.connection.deleted", connection=conn_id)
    return {"status": "ok", "id": conn_id}


def _route_references(cfg, name: str) -> list[str]:
    """route を使う alias と、その alias を候補に持つ pool alias。"""
    aliases = [alias for alias, preset in cfg.models.items() if preset.route == name]
    pools = [f"pool:{pool_name}" for pool_name, pool in cfg.pools.items()
             if any(candidate in aliases for candidate in pool.candidates)]
    return aliases + pools


@router.post("/routes/{name}", summary="route 追加")
async def create_route(name: str, route: AgentRoute):
    result = await _save_resource("routes", name, route, False)
    log.info("admin.route.created", route=name)
    return {**result, **route_sanction_view(get_config(), name)}


@router.put("/routes/{name}", summary="route 更新")
async def update_route(name: str, route: AgentRoute):
    result = await _save_resource("routes", name, route, True)
    log.info("admin.route.updated", route=name)
    return {**result, **route_sanction_view(get_config(), name)}


@router.delete("/routes/{name}", summary="route 削除")
async def delete_route(name: str):
    cfg = get_config()
    if name not in cfg.routes:
        raise HTTPException(status_code=404, detail=f"route '{name}' not found")
    if references := _route_references(cfg, name):
        raise HTTPException(status_code=409, detail=f"route '{name}' is referenced by: {', '.join(references)}")
    new_cfg = cfg.model_copy(deep=True)
    del new_cfg.routes[name]
    # 消えた route への承諾が残ると、次の読込が落ちる。承諾は route と一緒に消す。
    removed = len(new_cfg.risk_acks)
    new_cfg.risk_acks = [ack for ack in new_cfg.risk_acks if ack.route != name]
    _save_config_or_422(new_cfg)
    log.info("admin.route.deleted", route=name)
    return {"status": "ok", "name": name, "removed_acks": removed - len(new_cfg.risk_acks)}


@router.post("/routes/{name}/rename", summary="route の名前変更")
async def rename_route(name: str, body: RouteRenameRequest):
    """route 名と、それを指す alias・risk_acks を 1 回の保存で変更する。

    承諾の digest は route の名前を含まないので、改名しても承諾は有効のまま。
    """
    cfg = get_config()
    if name not in cfg.routes:
        raise HTTPException(status_code=404, detail=f"route '{name}' not found")
    if body.new_name == name:
        return {"status": "ok", "name": name}
    if body.new_name in cfg.routes:
        raise HTTPException(status_code=409, detail=f"route '{body.new_name}' already exists")
    new_cfg = cfg.model_copy(deep=True)
    new_cfg.routes[body.new_name] = new_cfg.routes.pop(name)
    for preset in new_cfg.models.values():
        if preset.route == name:
            preset.route = body.new_name
    for ack in new_cfg.risk_acks:
        if ack.route == name:
            ack.route = body.new_name
    _save_config_or_422(new_cfg)
    log.info("admin.route.renamed", old=name, new=body.new_name)
    return {"status": "ok", "name": body.new_name}


@router.post("/routes/{name}/ack", summary="route の risk を承諾")
async def acknowledge_route(name: str, body: AckRequest, user: str = Depends(admin_username)):
    cfg = get_config()
    route = cfg.routes.get(name)
    if route is None:
        raise HTTPException(status_code=404, detail=f"route '{name}' not found")
    connection = cfg.connections.get(route.connection)
    reasons = route_reasons(route, connection)
    if not reasons:
        raise HTTPException(status_code=409, detail=f"route '{name}' is official; no acknowledgement is needed")
    digest = route_digest(route, connection)
    if any(ack.route == name and ack.digest == digest for ack in cfg.risk_acks):
        raise HTTPException(status_code=409, detail=f"route '{name}' is already acknowledged")
    new_cfg = cfg.model_copy(deep=True)
    # 古い (digest の合わない) 承諾は、新しい承諾で置き換える。
    new_cfg.risk_acks = [ack for ack in new_cfg.risk_acks if ack.route != name]
    new_cfg.risk_acks.append(RiskAck(
        route=name, digest=digest, reasons=reasons, acknowledged_by=user,
        at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        note=(body.note or "").strip() or None,
    ))
    _save_config_or_422(new_cfg)
    log.info("admin.route.acknowledged", route=name, acknowledged_by=user, reasons=reasons)
    return {"status": "ok", "name": name, **route_sanction_view(get_config(), name)}


@router.delete("/routes/{name}/ack", summary="route の承諾を取り消す")
async def revoke_route_ack(name: str):
    cfg = get_config()
    if name not in cfg.routes:
        raise HTTPException(status_code=404, detail=f"route '{name}' not found")
    if not any(ack.route == name for ack in cfg.risk_acks):
        raise HTTPException(status_code=404, detail=f"route '{name}' has no acknowledgement")
    new_cfg = cfg.model_copy(deep=True)
    new_cfg.risk_acks = [ack for ack in new_cfg.risk_acks if ack.route != name]
    _save_config_or_422(new_cfg)
    log.info("admin.route.ack_revoked", route=name)
    return {"status": "ok", "name": name, **route_sanction_view(get_config(), name)}


@router.get("/pools/state", summary="pool 候補の状態 (管理 UI 用・読み取りのみ)")
async def pool_state(request: Request):
    """pool alias ごとの候補の状態。/v1/pools と同じ view を、admin の Basic 認証の下で返す。"""
    service = getattr(request.app.state, "execution_service", None)
    return {"pools": await pool_view(service, get_config()) or {}}


def _check_pool_name(cfg, name: str, *, creating: bool) -> None:
    if creating and (name in cfg.pools or name in cfg.models):
        raise HTTPException(status_code=409, detail=f"alias '{name}' already exists")


@router.post("/pools/{name}", summary="pool alias 追加")
async def create_pool(name: str, pool: PoolAlias):
    _check_pool_name(get_config(), name, creating=True)
    new_cfg = get_config().model_copy(deep=True)
    new_cfg.pools[name] = pool
    _save_config_or_422(new_cfg)
    log.info("admin.pool.created", pool=name)
    return {"status": "ok", "name": name}


@router.put("/pools/{name}", summary="pool alias 更新")
async def update_pool(name: str, pool: PoolAlias):
    cfg = get_config()
    if name not in cfg.pools:
        raise HTTPException(status_code=404, detail=f"pool '{name}' not found")
    new_cfg = cfg.model_copy(deep=True)
    new_cfg.pools[name] = pool
    _save_config_or_422(new_cfg)
    log.info("admin.pool.updated", pool=name)
    return {"status": "ok", "name": name}


@router.delete("/pools/{name}", summary="pool alias 削除")
async def delete_pool(name: str):
    cfg = get_config()
    if name not in cfg.pools:
        raise HTTPException(status_code=404, detail=f"pool '{name}' not found")
    new_cfg = cfg.model_copy(deep=True)
    del new_cfg.pools[name]
    _save_config_or_422(new_cfg)
    log.info("admin.pool.deleted", pool=name)
    return {"status": "ok", "name": name}


def _machine_driver_view(install: Any, version_info: dict[str, str | None]) -> dict[str, Any]:
    """Safe view of DriverInstall: paths, digest prefixes; never file contents or token values."""
    cells: dict[str, dict[str, Any]] = {}
    for cell_name, cell in sorted((getattr(install, "cells", None) or {}).items()):
        egress = getattr(cell, "codex_egress_limits", None)
        digest = getattr(cell, "rootfs_digest", "") or ""
        cells[cell_name] = {
            "rootfs": str(cell.rootfs) if cell.rootfs is not None else None,
            "rootfs_digest_prefix": digest[:12],
            "image_id": getattr(cell, "image_id", None),
            "codex_egress_limits": egress.model_dump(mode="json") if egress is not None else None,
        }

    cli_binary = getattr(install, "cli_binary", None)
    return {
        "cli_binary": str(cli_binary) if cli_binary is not None else None,
        "cli_version": version_info.get("version"),
        "cli_version_note": version_info.get("note"),
        "cells": cells,
    }


def _machine_client_view(name: str, policy: Any) -> dict[str, Any]:
    """Safe view of Client ceilings."""
    return {
        "name": name,
        "places": list(getattr(policy, "places", []) or []),
        "max_surface": getattr(policy, "max_surface", None),
        "models": list(getattr(policy, "models", None) or getattr(policy, "agent_models", []) or []),
        "max_concurrent_runs": getattr(policy, "max_concurrent_runs", getattr(policy, "max_concurrent", None)),
        "max_run_seconds": getattr(policy, "max_run_seconds", getattr(policy, "max_seconds", None)),
        "max_memory_bytes": getattr(policy, "max_memory_bytes", None),
        "max_processes": getattr(policy, "max_processes", getattr(policy, "max_pids", None)),
        "max_cpus": getattr(policy, "max_cpus", None),
    }


def _machine_container_view(container: Any) -> dict[str, Any] | None:
    if container is None:
        return None
    return {
        "max_containers": container.max_containers,
        "workspace_memory_bytes": container.workspace_memory_bytes,
        "cpus": container.cpus,
        "pids_limit": container.pids_limit,
        "min_free_disk_bytes": container.min_free_disk_bytes,
        "agent_root": container.agent_root,
        "home_root": container.home_root,
        "egress_socket_root": container.egress_socket_root,
    }


@router.get("/machine", summary="ホスト実行設定の安全な読み取りビュー")
async def get_machine(request: Request):
    """Return a read-only, secret-free summary of NativeExecutionConfig and client policies."""
    service = getattr(request.app.state, "execution_service", None)
    clients_view = [
        _machine_client_view(name, policy)
        for name, policy in sorted(get_clients().items())
    ]
    if service is None:
        return {
            "enabled": False,
            "sanction_enforcement": "off",
            "native_worker": None,
            "drivers": {},
            "clients": clients_view,
            "agent_container": None,
        }

    config = service.config
    raw_drivers = dict(sorted((getattr(config, "drivers", None) or {}).items()))
    driver_names = list(raw_drivers.keys())
    version_results = await asyncio.gather(
        *(_probe_cli_version(getattr(raw_drivers[name], "cli_binary", None)) for name in driver_names),
    )
    drivers_view = {
        name: _machine_driver_view(raw_drivers[name], version_info)
        for name, version_info in zip(driver_names, version_results, strict=True)
    }

    return {
        "enabled": True,
        "sanction_enforcement": getattr(config, "sanction_enforcement", "off"),
        "native_worker": None,
        "drivers": drivers_view,
        "clients": clients_view,
        "agent_container": _machine_container_view(getattr(config, "agent_container", None)),
    }
