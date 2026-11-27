"""
オブザーバビリティ API エンドポイント。

GET  /admin/interactions               一覧 (フィルタ可)
GET  /admin/interactions/{id}          詳細
GET  /admin/sessions                   conversation_key 集計
GET  /admin/sessions/{key}             セッションの全ターン
GET  /admin/stats                      24h / 7d 集計
"""

import json

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from ..activity import public_activity_data, stream_activity
from ..auth import require_basic_auth
from ..interactions import (
    _summarize_prompt_title,
    get_session_title,
    set_session_title,
    normalize_custom_title,
    compute_hourly_stats,
    compute_stats,
    get_interaction,
    get_session_turns,
    list_interaction_events,
    list_interactions,
    list_sessions,
)

log = structlog.get_logger()

router = APIRouter(
    prefix="/admin",
    tags=["observability"],
    dependencies=[Depends(require_basic_auth)],
)


@router.get(
    "/interactions",
    summary="リクエスト一覧",
    description="kotoyose.interactions の時系列フラットリスト。フィルタ可。",
)
async def get_interactions(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    model: str | None = None,
    source: str | None = None,
    status: str | None = None,
    conversation_key: str | None = None,
    since: str | None = None,
    until: str | None = None,
    include_subagents: bool = Query(False),
) -> dict:
    return await list_interactions(
        limit=limit, offset=offset,
        model=model, source=source, status=status,
        conversation_key=conversation_key, since=since, until=until,
        **({"include_subagents": True} if include_subagents else {}),
    )


@router.get(
    "/interactions/{interaction_id}",
    summary="リクエスト詳細",
    description="本文 (prompt / response) を含む全フィールド。",
)
async def get_interaction_detail(interaction_id: int) -> dict:
    row = await get_interaction(interaction_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"interaction {interaction_id} not found")
    return row


@router.get("/interactions/{interaction_id}/events")
async def get_interaction_events(
    interaction_id: int,
    after_seq: int = Query(-1, ge=-1),
) -> dict:
    """interaction の蓄積済み activity event を返す。"""
    if await get_interaction(interaction_id) is None:
        raise HTTPException(status_code=404, detail=f"interaction {interaction_id} not found")
    rows = await list_interaction_events(interaction_id, after_seq=after_seq)
    return {"items": [public_activity_data(row) for row in rows]}


@router.get("/interactions/{interaction_id}/events/stream")
async def stream_interaction_events(
    interaction_id: int,
    after_seq: int = Query(-1, ge=-1),
):
    """interaction の activity event を SSE で live tail する。"""
    interaction = await get_interaction(interaction_id)
    if interaction is None:
        raise HTTPException(status_code=404, detail=f"interaction {interaction_id} not found")
    return StreamingResponse(
        stream_activity(
            interaction_id,
            after_seq,
            live=interaction.get("status") == "in_progress",
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/sessions",
    summary="セッション集計",
    description="conversation_key ごとのターン数・トークン累積等。",
)
async def get_sessions(
    limit: int = Query(50, ge=1, le=500),
    include_subagents: bool = Query(False),
    offset: int = Query(0, ge=0),
    q: str | None = Query(default=None),
) -> dict:
    extra: dict = {}
    if include_subagents:
        extra["include_subagents"] = True
    if q and q.strip():
        extra["q"] = q.strip()
    return await list_sessions(limit=limit, offset=offset, **extra)


class SessionTitleBody(BaseModel):
    """会話キーと正規化後 160 文字以内のタイトル。空文字・null はリセット。"""

    conversation_key: str = Field(min_length=1)
    title: str | None

    @field_validator("conversation_key")
    @classmethod
    def nonempty_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("conversation_key must not be empty")
        return value

    @field_validator("title")
    @classmethod
    def normalized_title(cls, value: str | None) -> str | None:
        return normalize_custom_title(value)


@router.put("/sessions/title")
async def put_session_title(body: SessionTitleBody) -> dict:
    """タイトルを保存する。空ならリセットし、DB 未接続時も null を返す。"""
    saved = await set_session_title(body.conversation_key, body.title)
    return {"conversation_key": body.conversation_key, "title": saved}


@router.get(
    "/sessions/{conversation_key:path}",
    summary="セッション詳細 (全ターン)",
    description=(
        "該当 conversation_key の全ターンを時系列昇順で返す。\n"
        "`runtime_session_id` と `provider` を指定すると特定のセッション分のみ返す。"
    ),
)
async def get_session_detail(
    conversation_key: str,
    runtime_session_id: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    claude_session_id: str | None = Query(default=None),
) -> dict:
    # claude_session_id は旧 UI / 外部 client の query parameter との後方互換。
    session_id = runtime_session_id or claude_session_id
    turns = await get_session_turns(
        conversation_key, runtime_session_id=session_id, provider=provider
    )
    custom_title = await get_session_title(conversation_key)
    native_title = None
    for turn in reversed(turns):
        metadata = turn.get("metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except ValueError:
                continue
        if isinstance(metadata, dict) and isinstance(metadata.get("session_title"), str) and metadata["session_title"]:
            native_title = metadata["session_title"]
            break
    first_prompt = next((turn.get("prompt") for turn in turns if (turn.get("prompt") or "").strip()), None)
    title = custom_title or native_title or _summarize_prompt_title(first_prompt)
    return {
        "title": title,
        "custom_title": custom_title,
        "conversation_key": conversation_key,
        "runtime_session_id": session_id,
        "provider": provider,
        # 旧 query parameter を指定した client は従来どおり response からも読める。
        "claude_session_id": claude_session_id,
        "turns": turns,
    }


@router.get(
    "/stats",
    summary="統計サマリ",
    description="past 24h / 7d の集計。",
)
async def get_stats(include_imported: bool = Query(False)) -> dict:
    if include_imported:
        return await compute_stats(include_imported=True)
    return await compute_stats()


@router.get(
    "/stats/hourly",
    summary="時間別統計",
    description="過去 N 時間 (既定 24h) の 1 時間ごとの集計。",
)
async def get_hourly_stats(
    hours: int = Query(24, ge=1, le=168), include_imported: bool = Query(False),
) -> dict:
    if include_imported:
        return await compute_hourly_stats(hours=hours, include_imported=True)
    return await compute_hourly_stats(hours=hours)
