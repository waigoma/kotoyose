"""Chat streaming の wire 出力と完了状態を統括する application service。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass

import structlog
from anyio import CancelScope

from . import delegation
from .chat_approvals import TOOL_KIND_BY_NAME, PermissionRequest, TurnApprovals, bind_turn_approvals
from .chat_completion import CompletionPorts, TurnContext, finalize_chat_turn, record_turn_failure
from .chat_usage import build_response_usage
from .config import get_config
from .connections_probe import connection_for_model, is_auth_failure_error, record_connection_auth_failure
from .output_guard import CredentialLeakSuspected, LEAK_REASON, check_answer, redact_event
from .models import (
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
)
from .runtime.base import RuntimeEvent, RuntimePrompt, RuntimeSession, RuntimeTurnError


log = structlog.get_logger()

# Longer than any credential shape the guard detects, so a shape is always
# complete inside the held-back tail before any of it is released.
_HOLDBACK_CHARS = 8192
STREAM_INTERRUPTED_SENTINEL = "\n[Error: stream interrupted]"
_terminal_status_tasks: set[asyncio.Future] = set()

RuntimeStreamItem = str | RuntimeEvent | tuple[None, dict | None, str | None]


@dataclass(frozen=True, slots=True)
class StreamContext:
    """1 回の streaming chat 実行に必要な解決済み context。"""

    turn: TurnContext
    prompt: RuntimePrompt
    inspect_output: bool = False
    include_events: bool = False
    approvals: TurnApprovals | None = None
    deliver_approvals: bool = False


@dataclass(frozen=True, slots=True)
class StreamingPorts:
    """Streaming use case が外部状態へアクセスするための port 集合。"""

    stream_runtime: Callable[
        [RuntimeSession, RuntimePrompt], AsyncIterator[RuntimeStreamItem]
    ]
    start_event_writer: Callable[
        [int | None], asyncio.Queue[RuntimeEvent | None]
    ]
    record_failed: Callable[[int | None, str], Awaitable[object]]
    disconnect_client: Callable[[RuntimeSession], Awaitable[object]]
    completion: CompletionPorts


@dataclass(slots=True)
class _StreamState:
    """Generator 終了時に帳簿と session cleanup の要否を判定する状態。"""

    terminal: bool = False
    session_registered: bool = False


def _interrupted_tail(chunk_id: str, created: int, model: str) -> list[str]:
    """中断 sentinel、stop reason、DONE を含む終端 SSE を返す。"""
    error_chunk = ChatCompletionChunk(
        id=chunk_id,
        created=created,
        model=model,
        choices=[
            ChatCompletionChunkChoice(
                delta=ChatCompletionChunkDelta(content=STREAM_INTERRUPTED_SENTINEL),
                finish_reason="stop",
            )
        ],
    )
    return [f"data: {error_chunk.model_dump_json()}\n\n", "data: [DONE]\n\n"]


_ACP_TOOL_KINDS = {"read", "edit", "delete", "move", "search", "execute", "think", "fetch"}
_ACP_TOOL_STATUS = {"pending", "in_progress", "completed", "failed"}
ACTIVITY_KINDS = frozenset({"tool_call", "tool_call_update", "tool_use", "tool_result"})


def _event_sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


async def _iter_parent_progress(runtime_iterator, context: StreamContext):
    """親 runtime を 1 件ずつ取り、待ちのあいだに届いた子の進捗を先に返す。

    先読みしない。資格情報を検知したあとに runtime を引き続けると、閉じる前に
    次の本文まで読んでしまう。iterator を閉じるのはここだけにする。
    """
    queue: asyncio.Queue = asyncio.Queue()
    key = delegation.parent_stream_key(context.turn.client) if context.include_events else None
    delegation.register_parent_stream(key, queue)
    if context.approvals is not None and context.deliver_approvals:
        context.approvals.queue = queue
    runtime = runtime_iterator.__aiter__()
    pending: asyncio.Task | None = None
    progress_task: asyncio.Task | None = None

    async def next_item():
        with bind_turn_approvals(context.approvals):
            return await anext(runtime)

    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(next_item())
            progress_task = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait(
                {pending, progress_task}, return_when=asyncio.FIRST_COMPLETED,
            )
            if progress_task in done and pending not in done:
                yield progress_task.result()
                continue
            if progress_task not in done:
                progress_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await progress_task
            else:
                queue.put_nowait(progress_task.result())
            item_task, pending = pending, None
            try:
                yield item_task.result()
            except StopAsyncIteration:
                break
    finally:
        if context.approvals is not None:
            context.approvals.close()
        delegation.unregister_parent_stream(key, queue)
        if progress_task is not None and not progress_task.done():
            progress_task.cancel()
        with CancelScope(shield=True):
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            close = getattr(runtime_iterator, "aclose", None)
            if close is not None:
                await close()


def _activity_chunk(event: RuntimeEvent, inspect_output: bool) -> str | None:
    """道具の進捗を、runtime によらない ACP の tool call の形で SSE に載せる。"""
    payload = event.payload or {}
    if event.kind in ("tool_call", "tool_call_update"):
        # cli.acp: tool_name は ACP の ToolKind、summary は状態。
        tool_id = payload.get("tool_call_id")
        status = event.summary if event.summary in _ACP_TOOL_STATUS else None
        tool_kind = event.tool_name if event.tool_name in _ACP_TOOL_KINDS else "other"
        title = payload.get("title") if isinstance(payload.get("title"), str) else event.tool_name or "tool"
        kind = event.kind
    else:
        # Claude / Codex / Cursor SDK: tool_use が開始、tool_result が終了。summary は引数か結果の要約。
        tool_id = payload.get("tool_use_id")
        name = event.tool_name or "tool"
        tool_kind = TOOL_KIND_BY_NAME.get(name.lower(), "other")
        if event.kind == "tool_use":
            kind, status = "tool_call", "in_progress"
            title = f"{name}: {event.summary}" if event.summary else name
        else:
            kind, status = "tool_call_update", "failed" if payload.get("is_error") else "completed"
            title = None
    if not isinstance(tool_id, str) or not 0 < len(tool_id) <= 256:
        return None
    if title is not None:
        title = (redact_event(title) if inspect_output else title)[:200]
    fields = {"tool_call_id": tool_id, "title": title, "tool_kind": tool_kind, "status": status,
              "summary": payload.get("permission_denial") if event.kind == "tool_call_update" else None}
    data = {"object": "kotoyose.event", "kind": kind}
    data.update({key: value for key, value in fields.items() if value is not None})
    return f"data: {json.dumps(data)}\n\n"


def _record_failed_detached(
    ports: StreamingPorts,
    interaction_id: int | None,
    message: str,
) -> None:
    """Caller の cancellation から切り離して interaction を failed にする。"""
    if interaction_id is None:
        return
    task = asyncio.ensure_future(ports.record_failed(interaction_id, message))
    _terminal_status_tasks.add(task)

    def release_task(done: asyncio.Future) -> None:
        _terminal_status_tasks.discard(done)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(release_task)


def stream_chat_response(
    context: StreamContext,
    ports: StreamingPorts,
) -> AsyncIterator[str]:
    """Runtime stream を OpenAI 互換 SSE に変換し、帳簿と session を終端する。

    Args:
        context: Route で解決済みの request / session context。
        ports: Runtime、interaction、session 永続化への port。

    Returns:
        StreamingResponse の body に渡す SSE async iterator。
    """
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    event_queue = ports.start_event_writer(context.turn.interaction_id)

    async def body(state: _StreamState) -> AsyncIterator[str]:
        first = ChatCompletionChunk(
            id=chunk_id,
            created=created,
            model=context.turn.model,
            choices=[
                ChatCompletionChunkChoice(
                    delta=ChatCompletionChunkDelta(role="assistant")
                )
            ],
        )
        yield f"data: {first.model_dump_json()}\n\n"

        def text_chunk(text: str) -> str:
            chunk = ChatCompletionChunk(
                id=chunk_id,
                created=created,
                model=context.turn.model,
                choices=[ChatCompletionChunkChoice(delta=ChatCompletionChunkDelta(content=text))],
            )
            return f"data: {chunk.model_dump_json()}\n\n"

        held_events: deque[tuple[int, str]] = deque()
        released_chars = 0

        def ordered_chunks(text: str) -> Iterator[str]:
            nonlocal released_chars
            end = released_chars + len(text)
            while held_events and held_events[0][0] <= end:
                offset, event = held_events.popleft()
                split = offset - released_chars
                if split:
                    yield text_chunk(text[:split])
                yield event
                text = text[split:]
                released_chars = offset
            if text:
                yield text_chunk(text)
            released_chars = end

        full: list[str] = []
        pending = ""
        usage: dict | None = None
        runtime_session_id: str | None = None
        final_text: str | None = None
        runtime_error_emitted = False
        runtime_iterator = ports.stream_runtime(context.turn.client, context.prompt)
        items = _iter_parent_progress(runtime_iterator, context)
        try:
            async for item in items:
                if isinstance(item, BaseException):
                    raise item
                if isinstance(item, PermissionRequest):
                    yield _event_sse(item.wire())
                    continue
                if isinstance(item, delegation.ParentStreamEvent):
                    event = _event_sse(item.data)
                    if context.inspect_output and pending:
                        held_events.append((released_chars + len(pending), event))
                    else:
                        yield event
                    continue
                if isinstance(item, tuple):
                    final_text, usage, runtime_session_id = item
                    continue
                if isinstance(item, RuntimeEvent):
                    runtime_error_emitted = (
                        runtime_error_emitted or item.kind == "error"
                    )
                    event_queue.put_nowait(item)
                    event = (_activity_chunk(item, context.inspect_output)
                             if context.include_events and item.kind in ACTIVITY_KINDS else None)
                    if event is not None:
                        if context.inspect_output and pending:
                            held_events.append((released_chars + len(pending), event))
                        else:
                            yield event
                    continue
                full.append(item)
                if context.inspect_output:
                    # Hold back only the tail that could still be the start of a
                    # credential shape; everything before it has been checked
                    # together with what follows, so it can stream now.
                    pending += item
                    check_answer(pending)
                    if len(pending) > _HOLDBACK_CHARS:
                        released, pending = pending[:-_HOLDBACK_CHARS], pending[-_HOLDBACK_CHARS:]
                        for chunk in ordered_chunks(released):
                            yield chunk
                else:
                    yield text_chunk(item)
            answer = final_text if final_text is not None else "".join(full)
            if context.inspect_output:
                check_answer(answer)
                for chunk in ordered_chunks(pending):
                    yield chunk
        except Exception as exc:
            message = redact_event(str(exc)) if context.inspect_output else str(exc)
            log.error("sdk.error", error=message)
            if is_auth_failure_error(exc):
                conn_id = connection_for_model(get_config(), context.turn.model)
                await record_connection_auth_failure(conn_id, error="authentication_failed")
            if not runtime_error_emitted:
                event_queue.put_nowait(
                    RuntimeEvent(kind="error", summary=message[:500])
                )
            await record_turn_failure(ports.record_failed, context.turn.interaction_id, exc)
            state.terminal = True
            if isinstance(exc, (CredentialLeakSuspected, RuntimeTurnError)):
                if isinstance(exc, CredentialLeakSuspected):
                    code = LEAK_REASON
                else:
                    code = exc.terminal_reason or "provider_failed"
                if context.inspect_output:
                    code = redact_event(code)
                error = {"error": {"message": message, "type": "provider_error", "code": code}}
                if isinstance(exc, RuntimeTurnError) and exc.retryable is False:
                    error["error"]["retryable"] = False
                yield f"data: {json.dumps(error)}\n\n"
                yield "data: [DONE]\n\n"
            else:
                for piece in _interrupted_tail(chunk_id, created, context.turn.model):
                    yield piece
            return
        finally:
            event_queue.put_nowait(None)
            with CancelScope(shield=True):
                await items.aclose()

        final = ChatCompletionChunk(
            id=chunk_id,
            created=created,
            model=context.turn.model,
            choices=[
                ChatCompletionChunkChoice(
                    delta=ChatCompletionChunkDelta(),
                    finish_reason="stop",
                )
            ],
        )
        if context.include_events and usage:
            tokens_in, tokens_out, _ = ports.completion.extract_tokens(
                usage, context.turn.latest_text, answer,
            )
            payload = final.model_dump()
            payload["usage"] = build_response_usage(usage, tokens_in, tokens_out).model_dump()
            yield f"data: {json.dumps(payload)}\n\n"
        else:
            yield f"data: {final.model_dump_json()}\n\n"
        yield "data: [DONE]\n\n"

        completion = await finalize_chat_turn(
            context.turn,
            ports.completion,
            answer=answer,
            usage=usage,
            runtime_session_id=runtime_session_id,
        )
        state.session_registered = completion.session_registered

        log.info("response.completed", answer_length=len(answer), streamed=True)
        state.terminal = True

    async def guarded_body() -> AsyncIterator[str]:
        state = _StreamState(
            session_registered=context.turn.lookup_key is not None
        )
        iterator = body(state)
        try:
            async for piece in iterator:
                yield piece
        except GeneratorExit:
            # Client が去った後は wire へ追加 yield できない。
            raise
        except BaseException:
            # Runtime cancellation では caller が待ち続けないよう終端 SSE を試みる。
            for piece in _interrupted_tail(chunk_id, created, context.turn.model):
                yield piece
            raise
        finally:
            if context.approvals is not None:
                context.approvals.close()
            with CancelScope(shield=True):
                await iterator.aclose()
            if not state.terminal:
                _record_failed_detached(
                    ports,
                    context.turn.interaction_id,
                    "stream ended without a terminal status "
                    "(client disconnect or runtime cancellation)",
                )
            if not state.session_registered:
                await ports.disconnect_client(context.turn.client)

    return guarded_body()
