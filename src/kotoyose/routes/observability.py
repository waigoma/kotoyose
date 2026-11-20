"""
オブザーバビリティ API エンドポイント。

GET  /admin/interactions               一覧 (フィルタ可)
GET  /admin/interactions/{id}          詳細
GET  /admin/sessions                   conversation_key 集計
GET  /admin/sessions/{key}             セッションの全ターン
GET  /admin/stats                      24h / 7d 集計
"""

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..activity import public_activity_data, stream_activity
from ..auth import require_basic_auth
from ..interactions import (
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
) -> dict:
    return await list_interactions(
        limit=limit, offset=offset,
        model=model, source=source, status=status,
        conversation_key=conversation_key, since=since, until=until,
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
    offset: int = Query(0, ge=0),
) -> dict:
    return await list_sessions(limit=limit, offset=offset)


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
    return {
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
async def get_stats() -> dict:
    return await compute_stats()


@router.get(
    "/stats/hourly",
    summary="時間別統計",
    description="過去 N 時間 (既定 24h) の 1 時間ごとの集計。",
)
async def get_hourly_stats(hours: int = Query(24, ge=1, le=168)) -> dict:
    return await compute_hourly_stats(hours=hours)
