"""スコープ付き API キーによるセッション履歴の読み取り専用公開。"""

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..execution_resources import ExecutionError, authenticate_client
from ..interactions import export_interactions
from ..session_import.models import EXPORT_MAX_BODY_BYTES, EXPORT_MAX_LIMIT, ensure_utc, format_utc_iso

router = APIRouter(prefix="/v1", tags=["sessions"])
_bearer = HTTPBearer(auto_error=False)


async def authorize_session_export(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Client に紐づく export スコープを検証し、認証情報を state に設定する。"""
    token = credentials.credentials if credentials is not None else None
    store = getattr(request.app.state, "api_key_store", None)
    key = await store.authenticate(token) if store is not None and token and token.startswith("kys_") else None
    unauthorized = HTTPException(
        status_code=401, detail="Invalid or missing API key", headers={"WWW-Authenticate": "Bearer"},
    )
    if key is None or not key.get("caller"):
        raise unauthorized
    try:
        principal, policy = authenticate_client(key["caller"])
    except ExecutionError as exc:
        if exc.status != 401:
            raise
        raise unauthorized from None
    if "sessions:export" not in (getattr(policy, "scopes", None) or ()):
        raise HTTPException(status_code=403, detail="API key lacks 'sessions:export' scope")
    request.state.api_key_name = key["name"]
    request.state.api_key_caller = key["caller"]
    request.state.execution_principal = principal
    request.state.execution_policy = policy


def _serialize_row(row: dict) -> dict:
    deleted = row.get("deleted_at") is not None
    metadata = row.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return {
        **{name: row[name] for name in (
            "id", "request_id", "conversation_key", "provider", "runtime_session_id", "model", "source",
        )},
        "received_at": format_utc_iso(row["received_at"]),
        "completed_at": format_utc_iso(row["completed_at"]),
        "prompt": None if deleted else row["prompt"],
        "response": None if deleted else row["response"],
        "deleted": deleted,
        "metadata": metadata if isinstance(metadata, dict) else {},
    }


def _envelope(items: list[dict], has_more: bool) -> dict:
    cursor = {"completed_at": items[-1]["completed_at"], "id": items[-1]["id"]} if items else None
    return {"schema_version": "1", "items": items, "next_cursor": cursor, "has_more": has_more}


@router.get("/sessions/export", dependencies=[Depends(authorize_session_export)])
async def export_sessions(
    include_subagents: bool = Query(False),
    after_completed_at: datetime | None = Query(default=None),
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=EXPORT_MAX_LIMIT, ge=1, le=EXPORT_MAX_LIMIT),
) -> dict:
    """完了履歴を UTC カーソルでページ化する。削除行の本文は返さない。

    件数と応答サイズで区切るが、進捗を保証するため先頭の 1 件は必ず含める。
    """
    if after_id > 0 and after_completed_at is None:
        raise HTTPException(status_code=422, detail="after_completed_at is required when after_id is provided")
    rows = await export_interactions(
        after_completed_at=ensure_utc(after_completed_at) if after_completed_at is not None else None,
        after_id=after_id, limit=limit + 1,
        **({"include_subagents": True} if include_subagents else {}),
    )
    has_more = len(rows) > limit
    items: list[dict] = []
    # JSONResponse と同じエンコードで、カーソル・外枠・UTF-8 のバイト数も含める。
    item_bytes = 0
    for row in rows[:limit]:
        item = _serialize_row(row)
        encoded_size = len(JSONResponse(item).body)
        candidate = _envelope([item], has_more)
        size = len(JSONResponse(candidate).body) + item_bytes + len(items)
        if items and size > EXPORT_MAX_BODY_BYTES:
            has_more = True
            break
        items.append(item)
        item_bytes += encoded_size
    return _envelope(items, has_more)
