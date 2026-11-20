"""Native agent attempt の Recent Requests 記録。実行経路から DB 待機を切り離す。"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

import structlog

from . import db
from .activity import start_event_writer
from .interactions import record_completed, record_failed, record_received
from .runtime.base import RuntimeEvent
from .output_guard import guard_enabled, redact_runtime_event


log = structlog.get_logger()
_tasks: set[asyncio.Task] = set()
_TEXT_LIMIT = 16_384
_EVENT_LIMIT = 5_000


def _input_prompt(snapshot: dict) -> str:
    """admission が解決して agent に渡す input 本文だけを、順序と上限を保って記録する。"""
    parts: list[str] = []
    remaining = _TEXT_LIMIT
    for item in snapshot.get("inputs", []):
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if not isinstance(kind, str) or not kind:
            continue
        body = item.get("text")
        if kind != "image" and isinstance(body, str) and body:
            part = f"[{kind}]\n{body}"
        else:
            ref = item.get("content_ref") or item.get("artifact_ref") or item.get("resource_ref")
            if not isinstance(ref, str) or not ref:
                continue
            size = item.get("size")
            size_label = str(size) if isinstance(size, int) and size >= 0 else "?"
            part = f"[{kind}: {ref[:128]} ({size_label} bytes)]"
        prefix = "\n\n" if parts else ""
        chunk = (prefix + part)[:remaining]
        parts.append(chunk)
        remaining -= len(chunk)
        if remaining == 0:
            break
    return "".join(parts) or "[agent attempt]"


class AgentInteraction:
    """Attempt の観測情報を bounded queue に載せ、別 task で interaction へ保存する。"""

    def __init__(self, attempt: dict) -> None:
        self.attempt = attempt
        self._events: deque[RuntimeEvent] = deque(maxlen=_EVENT_LIMIT)
        self._emitted = 0
        self._ready = asyncio.Event()
        self._finished: tuple[str, str, str, bytes, dict] | None = None
        task = asyncio.create_task(self._write())
        _tasks.add(task)

        def release(done: asyncio.Task) -> None:
            _tasks.discard(done)
            if not done.cancelled():
                error = done.exception()
                if error is not None:
                    log.warning("agent_interaction.writer_failed", error_type=type(error).__name__)

        task.add_done_callback(release)

    def emit(self, event: RuntimeEvent) -> None:
        if self._finished is not None or self._emitted >= _EVENT_LIMIT:
            return
        if guard_enabled():
            event = redact_runtime_event(event)
        self._emitted += 1
        self._events.append(RuntimeEvent(
            kind=str(event.kind)[:100],
            tool_name=str(event.tool_name)[:100] if event.tool_name else None,
            summary=str(event.summary)[:500] if event.summary else None,
        ))
        self._ready.set()

    def chat_progress(self, kind: str, payload: dict) -> None:
        if kind != "provider_progress":
            return
        if payload.get("kind") == "text":
            self.emit(RuntimeEvent(kind="text", summary=payload.get("text", "")))
        else:
            self.emit(RuntimeEvent(
                kind=payload.get("kind", "provider_progress"),
                tool_name=payload.get("tool_name"),
                summary=payload.get("summary"),
            ))

    def finish(self, status: str, reason: str, stop: str, content: bytes, usage: dict) -> None:
        if self._finished is None:
            self._finished = (status, reason, stop, content, usage)
            self._ready.set()

    async def _write(self) -> None:
        snapshot = self.attempt["snapshot"]
        prompt = _input_prompt(snapshot)
        interaction_id = await record_received(
            request_id=f"native-agent:{self.attempt['id']}:{self.attempt['generation']}",
            conversation_key=f"attempt:{self.attempt['id']}",
            # The interaction's model is the alias the caller asked for, as on the chat path;
            # the provider-side model id stays in metadata.
            model=snapshot.get("alias") or snapshot["model"],
            provider=snapshot.get("provider", "claude"),
            source="native-agent",
            prompt=prompt,
            streamed=True,
            metadata={
                "principal": self.attempt["principal"],
                "generation": self.attempt["generation"],
                "provider_model": snapshot["model"],
            },
            idempotent=True,
            api_key_name=snapshot.get("api_key_name", "!internal"),
        )
        if interaction_id is None:
            if db._pool is None:
                log.warning("agent_interaction.database_unavailable")
            return
        writer = start_event_writer(interaction_id)
        while True:
            await self._ready.wait()
            self._ready.clear()
            while self._events:
                writer.put_nowait(self._events.popleft())
            if self._finished is not None:
                break
        writer.put_nowait(None)
        status, reason, stop, content, usage = self._finished
        metadata: dict[str, Any] = {}
        if usage.get("cost") is not None:
            metadata["cost_usd"] = usage["cost"]
        tokens_in = usage.get("input_tokens")
        tokens_out = usage.get("output_tokens")
        tokens_source = usage.get("source") if tokens_in is not None or tokens_out is not None else None
        if status == "succeeded" and stop == "confirmed":
            await record_completed(
                interaction_id, content.decode("utf-8", errors="replace"),
                tokens_in, tokens_out, tokens_source or "unknown", None,
                provider=snapshot.get("provider", "claude"), metadata=metadata,
            )
        else:
            detail = f"{status}: {reason}"
            if stop != "confirmed":
                detail += f" (stop {stop})"
            await record_failed(
                interaction_id, detail, tokens_in=tokens_in, tokens_out=tokens_out,
                tokens_source=tokens_source, metadata=metadata,
            )
