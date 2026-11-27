"""Admin CRUD endpoints for Clients in config.yml."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
import structlog

from ..auth import require_basic_auth
from ..config import CLIENT_NAME_PATTERN, Client, get_clients, get_config
from ..execution_resources import agent_model_allowed, agent_place_blockers
from .admin import _save_config_or_422

log = structlog.get_logger()
_CLIENT_NAME_RE = re.compile(CLIENT_NAME_PATTERN)

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_basic_auth)],
)


def _client_payload(name: str, client: Client, keys: list[dict]) -> dict:
    bound = [
        {
            "name": row["name"],
            "prefix": row.get("prefix"),
            "last4": row.get("last4"),
            "revoked_at": row.get("revoked_at"),
        }
        for row in keys
        if row.get("caller") == name
    ]
    active_keys = [row["name"] for row in bound if row.get("revoked_at") is None]
    data = client.model_dump(mode="json")
    data.setdefault("max_surface", client.max_surface)
    return {
        "name": name,
        **data,
        "scopes": list(client.scopes),
        "workspace_roots": [str(path) for path in client.workspace_roots],
        "active_keys": active_keys,
        "keys": bound,
    }


@router.get("/clients", summary="List configured clients")
async def list_clients(request: Request):
    store = getattr(request.app.state, "api_key_store", None)
    keys = await store.list() if store is not None else []
    config = get_config()
    clients = get_clients(config)
    settings = getattr(getattr(request.app.state, "execution_service", None), "config", None)
    aliases = sorted(config.models.keys() | config.pools.keys())
    return {
        "clients": [
            {
                **_client_payload(name, client, keys),
                "place_blocked_models": {
                    alias: places for alias in aliases
                    if agent_model_allowed(alias, client)
                    and (places := agent_place_blockers(alias, client, settings, config=config))
                },
            }
            for name, client in sorted(clients.items())
        ],
    }


@router.put("/clients/{name}", summary="Create or update a client")
async def put_client(name: str, body: Client):
    if not _CLIENT_NAME_RE.fullmatch(name):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid client name '{name}': must match {CLIENT_NAME_PATTERN}",
        )
    cfg = get_config().model_copy(deep=True)
    created = name not in cfg.clients
    cfg.clients[name] = body
    _save_config_or_422(cfg)
    saved = get_config().clients[name]
    log.info("admin.client.saved", client=name, created=created)
    return {
        "status": "ok",
        "name": name,
        "created": created,
        "client": _client_payload(name, saved, []),
    }


@router.delete("/clients/{name}", summary="Delete a client")
async def delete_client(name: str, request: Request):
    cfg = get_config()
    if name not in cfg.clients:
        raise HTTPException(status_code=404, detail=f"Client '{name}' not found")
    store = getattr(request.app.state, "api_key_store", None)
    keys = await store.list() if store is not None else []
    active_keys = sorted(
        row["name"]
        for row in keys
        if row.get("caller") == name and row.get("revoked_at") is None
    )
    if active_keys:
        detail = f"Client '{name}' cannot be deleted while bound to active API keys: {', '.join(active_keys)}"
        return JSONResponse(
            status_code=409,
            content={
                "detail": detail,
                "keys": active_keys,
                "error": {"message": detail, "type": "conflict", "keys": active_keys},
            },
        )
    new_cfg = cfg.model_copy(deep=True)
    del new_cfg.clients[name]
    _save_config_or_422(new_cfg)
    log.info("admin.client.deleted", client=name)
    return {"status": "ok", "name": name}
