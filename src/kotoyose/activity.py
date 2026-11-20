"""Runtime activity event の保存と公開 stream を担う application service。"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any

from .output_guard import guard_enabled, redact_runtime_event
from .interactions import get_interaction, list_interaction_events, record_event

if TYPE_CHECKING:
    from .runtime.base import RuntimeEvent


_EVENT_LIMIT = 5000
_event_writer_tasks: set[asyncio.Task] = set()
_active_event_queue: contextvars.ContextVar[asyncio.Queue[RuntimeEvent | None] | None] = (
    contextvars.ContextVar("kotoyose_active_event_queue", default=None)
)


@contextlib.contextmanager
def bind_event_queue(
    queue: asyncio.Queue[RuntimeEvent | None] | None,
):
    """同期 turn の実行中に RuntimeEvent の送出先 queue を束縛する。"""
    token = _active_event_queue.set(queue)
    try:
        yield queue
    finally:
        _active_event_queue.reset(token)


def emit_runtime_event(event: RuntimeEvent) -> None:
    """現在束縛されている event writer queue があれば RuntimeEvent を投入する。"""
    queue = _active_event_queue.get()
    if queue is not None:
        queue.put_nowait(event)


def start_event_writer(
    interaction_id: int | None,
) -> asyncio.Queue[RuntimeEvent | None]:
    """Interaction ごとの event を seq 順に background task で保存する。

    Args:
        interaction_id: 保存先 interaction。None の場合は DB 側で無視される。

    Returns:
        Producer が RuntimeEvent を投入し、終了時に None を投入する queue。
    """
    queue: asyncio.Queue[RuntimeEvent | None] = asyncio.Queue()
    redact = guard_enabled()

    async def write_events() -> None:
        seq = 0
        truncated = False
        while (event := await queue.get()) is not None:
            if seq < _EVENT_LIMIT - 1:
                if redact:
                    event = redact_runtime_event(event)
                await record_event(
                    interaction_id,
                    seq,
                    event.kind,
                    event.tool_name,
                    event.summary,
                    event.payload,
                )
                seq += 1
            elif not truncated:
                truncated = True
                await record_event(
                    interaction_id,
                    seq,
                    "error",
                    None,
                    f"activity event limit reached ({_EVENT_LIMIT})",
                    {"limit": _EVENT_LIMIT},
                )
                seq += 1

    task = asyncio.create_task(write_events())
    _event_writer_tasks.add(task)

    def release_task(done: asyncio.Task) -> None:
        _event_writer_tasks.discard(done)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(release_task)
    return queue


def public_activity_data(row: Mapping[str, Any]) -> dict[str, Any]:
    """DB row から SSE / JSON API で公開してよい field だけを返す。

    Args:
        row: Interaction event の DB row。

    Returns:
        内部 payload を除いた JSON 化可能な event data。
    """
    timestamp = row.get("ts")
    return {
        "seq": row["seq"],
        "ts": (
            timestamp.isoformat()
            if hasattr(timestamp, "isoformat")
            else str(timestamp)
        ),
        "kind": row["kind"],
        "tool_name": row.get("tool_name"),
        "summary": row.get("summary"),
    }


async def stream_activity(
    interaction_id: int,
    after_seq: int,
    *,
    live: bool = True,
) -> AsyncIterator[str]:
    """蓄積済み event を SSE 化し、進行中なら terminal 状態まで追跡する。

    Args:
        interaction_id: 対象 interaction ID。
        after_seq: この sequence より後の event だけを返す。
        live: False なら現在蓄積済みの event だけを返す。

    Yields:
        `activity` event として wire へ送る SSE 文字列。
    """
    cursor = after_seq
    terminal_empty_polls = 0
    while True:
        rows = await list_interaction_events(interaction_id, after_seq=cursor)
        for row in rows:
            cursor = max(cursor, row["seq"])
            yield "event: activity\ndata: " + json.dumps(
                public_activity_data(row), ensure_ascii=False
            ) + "\n\n"
        if not live:
            return
        interaction = await get_interaction(interaction_id)
        if interaction is None or interaction.get("status") != "in_progress":
            terminal_empty_polls = terminal_empty_polls + 1 if not rows else 0
            if terminal_empty_polls >= 2:
                return
        await asyncio.sleep(0.25)

