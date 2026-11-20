"""Admin management of client API keys."""

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..auth import require_basic_auth
from ..config import Client, get_clients
from ..execution_http import authorize_execution
from ..interactions import get_scoped_session_turns

router = APIRouter(prefix="/admin", dependencies=[Depends(require_basic_auth)])


class CreateKey(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    client: str = Field(min_length=1, max_length=128)


def _serialize_key(row: dict) -> dict:
    return {
        **{k: v for k, v in row.items() if k != "caller"},
        "client": row.get("caller"),
    }


def _client_policies(clients: dict[str, Client]) -> list[dict]:
    policies = []
    for name in sorted(clients):
        policy = clients[name]
        policies.append({
            "name": name,
            "models": list(getattr(policy, "models", ()) or ()),
            "places": list(getattr(policy, "places", ()) or ()),
            "max_surface": getattr(policy, "max_surface", None),
            "workspace_roots": [str(root) for root in (getattr(policy, "workspace_roots", ()) or ())],
            "workspace_roots_count": len(getattr(policy, "workspace_roots", ()) or ()),
        })
    return policies


@router.get("/api-keys")
async def list_keys(request: Request):
    clients = get_clients()
    keys = await request.app.state.api_key_store.list()
    return {"keys": [_serialize_key(row) for row in keys],
            "clients": sorted(clients),
            "client_policies": _client_policies(clients)}


@router.post("/api-keys")
async def create_key(body: CreateKey, request: Request):
    clients = get_clients()
    if body.client not in clients:
        raise HTTPException(status_code=422, detail="Unknown client")
    try:
        row, key = await request.app.state.api_key_store.create(body.name, body.client)
    except (ValueError, asyncpg.UniqueViolationError):
        raise HTTPException(status_code=409, detail="Key name already exists") from None
    return JSONResponse(
        jsonable_encoder({**_serialize_key(row), "key": key}),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api-keys/{name}/revoke")
async def revoke_key(name: str, request: Request):
    row = await request.app.state.api_key_store.revoke(name)
    if row is None:
        raise HTTPException(status_code=404, detail="Key not found")
    return _serialize_key(row)


@router.delete("/api-keys/{name}")
async def delete_key(name: str, request: Request):
    if not await request.app.state.api_key_store.delete(name):
        raise HTTPException(status_code=409, detail="Key not found or not revoked")
    return {"status": "ok"}


client_router = APIRouter(prefix="/v1", dependencies=[Depends(authorize_execution)])


@client_router.get("/sessions/{conversation_key:path}")
async def session_detail(
    conversation_key: str, request: Request, runtime_session_id: str | None = None,
    provider: str | None = None, claude_session_id: str | None = None,
):
    session_id = runtime_session_id or claude_session_id
    turns = await get_scoped_session_turns(conversation_key, request.state.api_key_name, session_id, provider)
    return {"conversation_key": conversation_key, "runtime_session_id": session_id,
            "provider": provider, "claude_session_id": claude_session_id, "turns": turns}
