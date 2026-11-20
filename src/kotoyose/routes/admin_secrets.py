"""Admin HTTP API for encrypted secrets metadata and write-only management."""

from __future__ import annotations

import re
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import auth
from ..auth import admin_username, require_basic_auth
from ..config import SECRET_SETTING_FIELDS, _MCP_REQUEST_HEADER_TEMPLATE, get_config, parse_secret_reference
from ..secret_store import (
    SecretStore,
    SecretStoreDisabledError,
    SecretStoreError,
    _mask_last4,
    get_secret_store,
    validate_secret_name,
)

log = structlog.get_logger()

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_basic_auth)],
)


class PutSecretRequest(BaseModel):
    """Write-only payload to set or replace a secret."""

    value: str = Field(min_length=1, repr=False)
    purpose: str = ""


def _active_store(request: Request | None = None) -> SecretStore:
    store = getattr(getattr(request, "app", None), "state", None)
    candidate = getattr(store, "secret_store", None) if store is not None else None
    return candidate if isinstance(candidate, SecretStore) else get_secret_store()


def require_secret_store(request: Request) -> SecretStore:
    if auth.API_KEY is None:
        raise HTTPException(
            status_code=503,
            detail="secret store disabled: admin basic auth is disabled (set KOTOYOSE_TOKEN)",
        )
    store = _active_store(request)
    if not store.is_configured:
        detail = (
            f"secret store not configured ({store.key_error})"
            if store.key_error
            else "secret store not configured"
        )
        raise HTTPException(status_code=503, detail=detail)
    return store


_require_store = require_secret_store


def _suggest_secret_name(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-._").lower()
    return cleaned[:128] or "secret-key"


def _unmanaged_references() -> list[dict[str, str]]:
    """Collect env: and literal credential references that can be moved to Secrets."""
    cfg = get_config()
    items: list[dict[str, str]] = []

    for conn_name, conn in sorted(cfg.connections.items()):
        if conn.env_name:
            items.append({
                "kind": "connection",
                "target": conn_name,
                "label": f"connection:{conn_name} ({conn.provider})",
                "current": conn.secret,
                "suggested_name": _suggest_secret_name(conn.env_name),
            })

    def scan_mcp(owner_label: str, mcp_name: str, server: dict[str, Any]) -> None:
        for section in ("env", "headers"):
            mapping = server.get(section)
            if not isinstance(mapping, dict):
                continue
            for key, val in sorted(mapping.items()):
                if isinstance(val, str) and val and not _MCP_REQUEST_HEADER_TEMPLATE.search(val):
                    items.append({
                        "kind": "mcp",
                        "target": f"{owner_label}.{section}.{key}",
                        "label": f"{owner_label} {section}.{key}",
                        "current": f"literal (••••{_mask_last4(val)})",
                        "suggested_name": _suggest_secret_name(f"{mcp_name}-{key}"),
                    })

    for mcp_name, mcp_def in sorted(cfg.mcps.items()):
        scan_mcp(f"mcp:{mcp_name}", mcp_name, mcp_def.server)

    for alias_name, preset in sorted(cfg.models.items()):
        for srv_name, srv in sorted(preset.mcp_servers.items()):
            scan_mcp(f"alias:{alias_name}.mcp:{srv_name}", srv_name, srv)

    for field in sorted(SECRET_SETTING_FIELDS):
        val = getattr(cfg.settings, field, None)
        if isinstance(val, str) and val and parse_secret_reference(val) is None:
            items.append({
                "kind": "settings",
                "target": f"settings.{field}",
                "label": f"settings.{field}",
                "current": f"literal (••••{_mask_last4(val)})",
                "suggested_name": _suggest_secret_name(field),
            })

    return items


@router.get("/secrets", summary="List secret metadata (never values)")
async def list_secrets(request: Request):
    """Return metadata and usage for stored and referenced secrets, never plaintext values."""
    store = _require_store(request)
    try:
        stored = await store.list()
    except SecretStoreDisabledError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    users_by_name = get_config().secret_users()
    by_name: dict[str, dict[str, Any]] = {}
    for item in stored:
        name = item["name"]
        by_name[name] = {
            **item,
            "used_by": users_by_name.get(name, []),
        }

    for ref_name, used_by in users_by_name.items():
        if ref_name not in by_name:
            by_name[ref_name] = {
                "name": ref_name,
                "purpose": "",
                "set": False,
                "last4": "",
                "created_at": None,
                "updated_at": None,
                "updated_by": None,
                "version": 0,
                "key_id": None,
                "used_by": used_by,
            }

    return {
        "key_id": store.key_id,
        "secrets": [by_name[name] for name in sorted(by_name)],
        "unmanaged": _unmanaged_references(),
    }


async def write_secret_value(
    request: Request,
    name: str,
    value: str,
    *,
    purpose: str = "",
    actor: str = "admin",
    emit_log: bool = True,
) -> dict[str, Any]:
    """Encrypt and store (or replace) a secret through the admin secret store write path."""
    store = require_secret_store(request)
    try:
        validate_secret_name(name)
        meta = await store.set(
            name,
            value,
            purpose=purpose,
            updated_by=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SecretStoreDisabledError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except SecretStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    users = get_config().secret_users().get(name, [])
    if emit_log:
        log.info("admin.secret.updated", name=name, version=meta["version"])
    return {**meta, "used_by": users}


@router.put("/secrets/{name}", summary="Set or replace a secret (write-only)")
async def put_secret(
    name: str,
    body: PutSecretRequest,
    request: Request,
    actor: str = Depends(admin_username),
):
    """Encrypt and store (or rotate version of) a secret. Never returns or logs plaintext."""
    return await write_secret_value(
        request,
        name,
        body.value,
        purpose=body.purpose,
        actor=actor,
        emit_log=True,
    )


@router.delete("/secrets/{name}", summary="Delete an unreferenced secret")
async def delete_secret(name: str, request: Request):
    """Delete a secret unless it is still referenced by routes, MCPs, or settings."""
    store = _require_store(request)
    try:
        validate_secret_name(name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    users = get_config().secret_users().get(name, [])
    if users:
        raise HTTPException(
            status_code=409,
            detail=f"Secret '{name}' is referenced by: {', '.join(users)}",
        )

    try:
        removed = await store.delete(name)
    except SecretStoreDisabledError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail=f"Secret '{name}' not found")

    log.info("admin.secret.deleted", name=name)
    return {"status": "ok", "name": name}
