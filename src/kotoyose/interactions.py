"""
kotoyose.interactions テーブルへの record / 集計操作。

1 リクエスト = 1 行で chat.py から以下を呼ぶ:
- record_received: 受付時 INSERT (status='in_progress')
- record_completed: 完了時 UPDATE (status='completed')
- record_failed: 失敗時 UPDATE (status='failed')

DATABASE_URL 未設定の環境では全関数が no-op 相当 (record_received は None を返す)。
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import structlog

from . import db

log = structlog.get_logger()


def _event_json_default(value: Any) -> Any:
    """SDK 固有型を Timeline 保存用の JSON 値へ変換する。"""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (set, frozenset)):
        return list(value)
    # Runtime event は補助的な観測情報です。未知の SDK 型 1 個で Timeline 全体を
    # 欠落させないよう、最後は人間が読める文字列表現を保存します。
    return str(value)


def _with_output_throughput(row: Any) -> dict:
    """完了 interaction に E2E output tok/s の派生値を付けて返す。"""
    item = dict(row)
    tokens_out = item.get("tokens_out")
    duration_ms = item.get("duration_ms")
    measurable = (
        item.get("status") == "completed"
        and isinstance(tokens_out, (int, float))
        and not isinstance(tokens_out, bool)
        and tokens_out >= 0
        and isinstance(duration_ms, (int, float))
        and not isinstance(duration_ms, bool)
        and duration_ms > 0
    )
    item["output_tokens_per_second"] = (
        round(tokens_out * 1000 / duration_ms, 2) if measurable else None
    )
    return item


def _get_pool():
    """db._pool を取得 (テストでパッチしやすくするための間接化)。"""
    return db._pool


async def record_received(
    request_id: str,
    conversation_key: str | None,
    model: str,
    source: str | None,
    prompt: str,
    streamed: bool,
    provider: str = "claude",
    metadata: dict | None = None,
    idempotent: bool = False,
    api_key_name: str | None = None,
) -> int | None:
    """
    リクエスト受付時に INSERT する。返り値は新規行の id (no-op 時は None)。
    """
    pool = _get_pool()
    if pool is None:
        return None
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO kotoyose.interactions
                (request_id, conversation_key, provider, model, source,
                 prompt, prompt_chars, streamed, status, metadata, api_key_name)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'in_progress', $9::jsonb, $10)
            """ + ("ON CONFLICT (request_id) DO NOTHING" if idempotent else "") + """
            RETURNING id
            """,
            request_id,
            conversation_key,
            provider,
            model,
            source,
            prompt,
            len(prompt),
            streamed,
            json.dumps(metadata or {}),
            api_key_name,
        )
        return row["id"] if row else None
    except Exception as e:
        log.warning("interactions.record_received.failed", error=str(e))
        return None


async def record_completed(
    interaction_id: int | None,
    response: str,
    tokens_in: int | None,
    tokens_out: int | None,
    tokens_source: str,
    session_event: str | None,
    claude_session_id: str | None = None,
    provider: str = "claude",
    runtime_session_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """完了時の UPDATE。duration は received_at との差で自動計算。"""
    pool = _get_pool()
    if pool is None or interaction_id is None:
        return
    try:
        await pool.execute(
            """
            UPDATE kotoyose.interactions
            SET status = 'completed',
                response = $2,
                response_chars = $3,
                tokens_in = $4,
                tokens_out = $5,
                tokens_source = $6,
                session_event = $7,
                claude_session_id = $8,
                provider = $9,
                runtime_session_id = $10,
                metadata = metadata || $11::jsonb,
                completed_at = NOW(),
                duration_ms = EXTRACT(EPOCH FROM (NOW() - received_at)) * 1000
            WHERE id = $1
            """,
            interaction_id,
            response,
            len(response),
            tokens_in,
            tokens_out,
            tokens_source,
            session_event,
            claude_session_id,
            provider,
            runtime_session_id or claude_session_id,
            json.dumps(metadata or {}),
        )
    except Exception as e:
        log.warning("interactions.record_completed.failed", error=str(e))


async def record_failed(
    interaction_id: int | None, error_message: str, *,
    tokens_in: int | None = None, tokens_out: int | None = None,
    tokens_source: str | None = None, metadata: dict | None = None,
) -> None:
    """失敗時の UPDATE。"""
    pool = _get_pool()
    if pool is None or interaction_id is None:
        return
    try:
        await pool.execute(
            """
            UPDATE kotoyose.interactions
            SET status = 'failed',
                error_message = $2,
                tokens_in = $3,
                tokens_out = $4,
                tokens_source = $5,
                metadata = metadata || $6::jsonb,
                completed_at = NOW(),
                duration_ms = EXTRACT(EPOCH FROM (NOW() - received_at)) * 1000
            WHERE id = $1
            """,
            interaction_id,
            error_message,
            tokens_in,
            tokens_out,
            tokens_source,
            json.dumps(metadata or {}),
        )
    except Exception as e:
        log.warning("interactions.record_failed.failed", error=str(e))


async def backfill_conversation_key(interaction_id: int | None, conversation_key: str) -> None:
    """v2 ハッシュパス 1 ターン目: NULL だった conversation_key を storage_key で補完する。

    COALESCE により既存値 (v1 ヘッダパス・2 ターン目以降) は上書きしない。
    """
    pool = _get_pool()
    if pool is None or interaction_id is None:
        return
    try:
        await pool.execute(
            """
            UPDATE kotoyose.interactions
            SET conversation_key = COALESCE(conversation_key, $2)
            WHERE id = $1
            """,
            interaction_id,
            conversation_key,
        )
    except Exception as e:
        log.warning("interactions.backfill_conversation_key.failed", error=str(e))


async def record_event(
    interaction_id: int | None,
    seq: int,
    kind: str,
    tool_name: str | None,
    summary: str | None,
    payload: dict | None,
) -> None:
    """構造化 runtime event を保存する。DB 障害は応答へ波及させない。"""
    pool = _get_pool()
    if pool is None or interaction_id is None:
        return
    try:
        await pool.execute(
            """
            INSERT INTO kotoyose.interaction_events
                (interaction_id, seq, kind, tool_name, summary, payload)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb)
            ON CONFLICT (interaction_id, seq) DO NOTHING
            """,
            interaction_id,
            seq,
            kind,
            tool_name,
            summary[:500] if summary else None,
            json.dumps(payload, ensure_ascii=False, default=_event_json_default)
            if payload is not None
            else None,
        )
    except Exception as e:
        log.warning(
            "interactions.record_event.failed",
            interaction_id=interaction_id,
            seq=seq,
            error=str(e),
        )


async def list_interaction_events(
    interaction_id: int,
    *,
    after_seq: int = -1,
) -> list[dict]:
    """interaction のイベントを指定 seq より後から昇順で返す。"""
    pool = _get_pool()
    if pool is None:
        return []
    rows = await pool.fetch(
        """
        SELECT seq, ts, kind, tool_name, summary, payload
        FROM kotoyose.interaction_events
        WHERE interaction_id = $1 AND seq > $2
        ORDER BY seq ASC
        """,
        interaction_id,
        after_seq,
    )
    return [dict(row) for row in rows]


async def get_interaction_for_conversation(conversation_key: str, api_key_name: str | None = None) -> dict | None:
    """会話キーの進行中 interaction、なければ最新 interaction を返す。"""
    pool = _get_pool()
    if pool is None:
        return None
    row = await pool.fetchrow(
        """
        SELECT id, status, model
        FROM kotoyose.interactions
        WHERE conversation_key = $1
          AND api_key_name IS NOT DISTINCT FROM $2
          AND deleted_at IS NULL
        ORDER BY (status = 'in_progress') DESC, received_at DESC
        LIMIT 1
        """,
        conversation_key, api_key_name,
    )
    return dict(row) if row else None


# ── クエリ系 (admin API 用) ─────────────────────────────────────


_LIST_BASE_COLS = """
    i.id, i.request_id, i.conversation_key, i.provider, i.runtime_session_id,
    i.claude_session_id, i.model, i.source, i.received_at, i.completed_at,
    i.duration_ms, i.prompt_chars, i.response_chars, i.tokens_in, i.tokens_out,
    i.tokens_source, i.streamed, i.session_event, i.status, i.error_message,
    LEFT(i.prompt, 200) AS prompt_preview,
    LEFT(COALESCE(i.response, ''), 200) AS response_preview,
    COALESCE(activity.event_count, 0) AS event_count,
    activity.latest_kind, activity.latest_tool_name, activity.latest_summary
"""


async def list_interactions(
    *,
    limit: int = 50,
    offset: int = 0,
    model: str | None = None,
    source: str | None = None,
    status: str | None = None,
    conversation_key: str | None = None,
    since: str | None = None,
    until: str | None = None,
    include_deleted: bool = False,
) -> dict:
    """フィルタ付き一覧。{items, total, limit, offset}。
    include_deleted=False で soft-delete された行を除外する。
    """
    pool = _get_pool()
    if pool is None:
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    where_parts: list[str] = []
    params: list = []
    if not include_deleted:
        where_parts.append("deleted_at IS NULL")
    if model:
        params.append(model); where_parts.append(f"model = ${len(params)}")
    if source:
        params.append(source); where_parts.append(f"source = ${len(params)}")
    if status:
        params.append(status); where_parts.append(f"status = ${len(params)}")
    if conversation_key:
        params.append(conversation_key); where_parts.append(f"conversation_key = ${len(params)}")
    if since:
        params.append(since); where_parts.append(f"received_at >= ${len(params)}::timestamptz")
    if until:
        params.append(until); where_parts.append(f"received_at <= ${len(params)}::timestamptz")

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    list_sql = f"""
        SELECT {_LIST_BASE_COLS}
        FROM kotoyose.interactions i
        LEFT JOIN LATERAL (
            SELECT
                COUNT(*) OVER () AS event_count,
                e.kind AS latest_kind,
                e.tool_name AS latest_tool_name,
                e.summary AS latest_summary
            FROM kotoyose.interaction_events e
            WHERE e.interaction_id = i.id
            ORDER BY e.seq DESC
            LIMIT 1
        ) activity ON TRUE
        {where_sql}
        ORDER BY received_at DESC
        LIMIT {int(limit)} OFFSET {int(offset)}
    """
    count_sql = f"SELECT COUNT(*) FROM kotoyose.interactions {where_sql}"

    rows = await pool.fetch(list_sql, *params)
    total = await pool.fetchval(count_sql, *params)

    return {
        "items": [_with_output_throughput(r) for r in rows],
        "total": total or 0,
        "limit": limit,
        "offset": offset,
    }


async def get_interaction(interaction_id: int) -> dict | None:
    """1 件詳細。本文を含む全フィールド。"""
    pool = _get_pool()
    if pool is None:
        return None
    row = await pool.fetchrow(
        """
        SELECT *
        FROM kotoyose.interactions
        WHERE id = $1
        """,
        interaction_id,
    )
    return _with_output_throughput(row) if row else None


async def list_sessions(
    *,
    limit: int = 50,
    offset: int = 0,
    include_deleted: bool = False,
) -> dict:
    """(conversation_key, provider, runtime_session_id) グルーピングで集計。
    clear 後の新セッションは別行として表示されるよう、セッション ID も GROUP BY に含める。
    runtime_session_id が NULL の旧行は claude_session_id を fallback とする。
    include_deleted=False で soft-delete されたセッション (= 全ターンが deleted_at に印付き)
    を除外する。
    """
    pool = _get_pool()
    if pool is None:
        return {"items": [], "total": 0, "limit": limit, "offset": offset}

    deleted_filter = "" if include_deleted else " AND deleted_at IS NULL"

    rows = await pool.fetch(
        f"""
        SELECT
            conversation_key,
            provider,
            COALESCE(runtime_session_id, claude_session_id) AS runtime_session_id,
            claude_session_id,
            (ARRAY_AGG(model ORDER BY received_at DESC))[1] AS model,
            MIN(received_at) AS first_seen_at,
            MAX(received_at) AS last_seen_at,
            COUNT(*) AS turn_count,
            COALESCE(SUM(tokens_in), 0) AS total_tokens_in,
            COALESCE(SUM(tokens_out), 0) AS total_tokens_out,
            ARRAY(SELECT DISTINCT source FROM kotoyose.interactions i2
                  WHERE i2.conversation_key = kotoyose.interactions.conversation_key
                    AND i2.provider = kotoyose.interactions.provider
                    AND COALESCE(i2.runtime_session_id, i2.claude_session_id)
                        IS NOT DISTINCT FROM COALESCE(
                            kotoyose.interactions.runtime_session_id,
                            kotoyose.interactions.claude_session_id
                        )
                    AND i2.source IS NOT NULL
                    {deleted_filter.replace("deleted_at", "i2.deleted_at")}) AS sources,
            EXISTS(
                SELECT 1 FROM kotoyose.sessions s
                WHERE s.conversation_key = kotoyose.interactions.conversation_key
                  AND s.provider = kotoyose.interactions.provider
                  AND COALESCE(s.runtime_session_id, s.claude_session_id)
                      IS NOT DISTINCT FROM COALESCE(
                          kotoyose.interactions.runtime_session_id,
                          kotoyose.interactions.claude_session_id
                      )
            ) AS is_active
        FROM kotoyose.interactions
        WHERE conversation_key IS NOT NULL{deleted_filter}
        GROUP BY conversation_key, provider, runtime_session_id, claude_session_id
        ORDER BY last_seen_at DESC
        LIMIT {int(limit)} OFFSET {int(offset)}
        """,
    )
    total = await pool.fetchval(
        f"""
        SELECT COUNT(*)
        FROM (
            SELECT conversation_key, provider, runtime_session_id, claude_session_id
            FROM kotoyose.interactions
            WHERE conversation_key IS NOT NULL{deleted_filter}
            GROUP BY conversation_key, provider, runtime_session_id, claude_session_id
        ) sub
        """
    )
    return {
        "items": [dict(r) for r in rows],
        "total": total or 0,
        "limit": limit,
        "offset": offset,
    }


async def get_session_turns(
    conversation_key: str,
    runtime_session_id: str | None = None,
    provider: str | None = None,
    include_deleted: bool = False,
) -> list[dict]:
    """1 セッションの全ターンを時系列昇順で。
    runtime_session_id を指定すると、その provider 固有 session 分のみ返す。
    runtime_session_id が NULL の旧行は claude_session_id を fallback とする。
    include_deleted=False で soft-delete された行を除外する。
    """
    pool = _get_pool()
    if pool is None:
        return []
    deleted_filter = "" if include_deleted else " AND deleted_at IS NULL"
    # provider だけが指定された場合は、ID 未記録の同一 provider に絞る。
    # 両方未指定の従来 API 呼び出しだけは conversation 全体を返す。
    if runtime_session_id is not None or provider is not None:
        rows = await pool.fetch(
            f"""
            SELECT *
            FROM kotoyose.interactions
            WHERE conversation_key = $1
              AND COALESCE(runtime_session_id, claude_session_id) IS NOT DISTINCT FROM $2
              AND ($3::text IS NULL OR provider = $3){deleted_filter}
            ORDER BY received_at ASC
            """,
            conversation_key,
            runtime_session_id,
            provider,
        )
    else:
        rows = await pool.fetch(
            f"""
            SELECT *
            FROM kotoyose.interactions
            WHERE conversation_key = $1{deleted_filter}
            ORDER BY received_at ASC
            """,
            conversation_key,
        )
    return [_with_output_throughput(r) for r in rows]


async def get_scoped_session_turns(
    conversation_key: str, api_key_name: str | None,
    runtime_session_id: str | None = None, provider: str | None = None,
) -> list[dict]:
    pool = _get_pool()
    if pool is None:
        return []
    rows = await pool.fetch(
        """SELECT * FROM kotoyose.interactions
        WHERE conversation_key=$1 AND api_key_name IS NOT DISTINCT FROM $2
          AND deleted_at IS NULL
          AND (NOT $3::boolean OR COALESCE(runtime_session_id, claude_session_id) IS NOT DISTINCT FROM $4)
          AND ($5::text IS NULL OR provider=$5)
        ORDER BY received_at ASC""",
        conversation_key, api_key_name, runtime_session_id is not None or provider is not None,
        runtime_session_id, provider,
    )
    return [_with_output_throughput(row) for row in rows]


async def soft_delete_session(
    conversation_key: str,
    runtime_session_id: str | None,
    provider: str | None = None,
) -> int:
    """指定 (conversation_key, provider, runtime_session_id) の interactions を soft-delete する。

    deleted_at を NOW() で印付けし、対象行数を返す。冪等 (再実行で 0 を返す)。
    runtime_session_id=None の場合は ID 未記録行だけを対象にする。
    """
    pool = _get_pool()
    if pool is None:
        return 0
    result = await pool.execute(
        """
        UPDATE kotoyose.interactions
        SET deleted_at = NOW()
        WHERE conversation_key = $1
          AND COALESCE(runtime_session_id, claude_session_id) IS NOT DISTINCT FROM $2
          AND ($3::text IS NULL OR provider = $3)
          AND deleted_at IS NULL
        """,
        conversation_key,
        runtime_session_id,
        provider,
    )
    # asyncpg returns "UPDATE N"
    parts = result.split()
    return int(parts[1]) if len(parts) >= 2 else 0


async def compute_stats() -> dict:
    """past 24h / 7d の集計。"""
    pool = _get_pool()
    if pool is None:
        return {"past_24h": _empty_stats(), "past_7d": _empty_stats()}

    async def _bucket(hours: int) -> dict:
        agg = await pool.fetchrow(
            f"""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status = 'completed') AS completed,
                COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                COALESCE(SUM(tokens_in), 0) AS tokens_in,
                COALESCE(SUM(tokens_out), 0) AS tokens_out,
                AVG(duration_ms) AS avg_duration_ms
            FROM kotoyose.interactions
            WHERE received_at >= NOW() - INTERVAL '{hours} hours'
            """,
        )
        by_model = await pool.fetch(
            f"""
            SELECT model, COUNT(*) AS n FROM kotoyose.interactions
            WHERE received_at >= NOW() - INTERVAL '{hours} hours'
            GROUP BY model
            """
        )
        by_source = await pool.fetch(
            f"""
            SELECT COALESCE(source, '(none)') AS source, COUNT(*) AS n
            FROM kotoyose.interactions
            WHERE received_at >= NOW() - INTERVAL '{hours} hours'
            GROUP BY source
            """
        )
        return {
            "total_requests": agg["total"] or 0,
            "successful": agg["completed"] or 0,
            "failed": agg["failed"] or 0,
            "total_tokens_in": agg["tokens_in"] or 0,
            "total_tokens_out": agg["tokens_out"] or 0,
            "avg_duration_ms": int(agg["avg_duration_ms"] or 0),
            "by_model": {r["model"]: r["n"] for r in by_model},
            "by_source": {r["source"]: r["n"] for r in by_source},
        }

    return {"past_24h": await _bucket(24), "past_7d": await _bucket(168)}


def _empty_stats() -> dict:
    return {
        "total_requests": 0,
        "successful": 0,
        "failed": 0,
        "total_tokens_in": 0,
        "total_tokens_out": 0,
        "avg_duration_ms": 0,
        "by_model": {},
        "by_source": {},
    }


def _normalize_utc_hour(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    elif isinstance(value, datetime):
        dt = value
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0)


async def compute_hourly_stats(hours: int = 24) -> dict:
    """直近 `hours` 時間 (UTC 時間境界) の 1 時間ごとのリクエスト・トークン集計。"""
    current_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    slots = [current_hour - timedelta(hours=offset) for offset in range(hours - 1, -1, -1)]
    buckets: dict[datetime, dict] = {
        slot: {
            "hour": slot.isoformat(),
            "requests": 0,
            "successful": 0,
            "failed": 0,
            "tokens_in": 0,
            "tokens_out": 0,
        }
        for slot in slots
    }

    pool = _get_pool()
    if pool is None:
        return {"hours": hours, "items": list(buckets.values())}

    rows = await pool.fetch(
        """
        SELECT
            date_trunc('hour', received_at AT TIME ZONE 'UTC') AS hour,
            COUNT(*) AS requests,
            COUNT(*) FILTER (WHERE status = 'completed') AS successful,
            COUNT(*) FILTER (WHERE status = 'failed') AS failed,
            COALESCE(SUM(tokens_in), 0) AS tokens_in,
            COALESCE(SUM(tokens_out), 0) AS tokens_out
        FROM kotoyose.interactions
        WHERE received_at >= $1
          AND deleted_at IS NULL
        GROUP BY 1
        ORDER BY 1 ASC
        """,
        slots[0],
    )
    for row in rows:
        bucket_hour = _normalize_utc_hour(row.get("hour"))
        if bucket_hour is None or bucket_hour not in buckets:
            continue
        buckets[bucket_hour].update(
            {
                "requests": int(row["requests"] or 0),
                "successful": int(row["successful"] or 0),
                "failed": int(row["failed"] or 0),
                "tokens_in": int(row["tokens_in"] or 0),
                "tokens_out": int(row["tokens_out"] or 0),
            }
        )

    return {"hours": hours, "items": list(buckets.values())}
