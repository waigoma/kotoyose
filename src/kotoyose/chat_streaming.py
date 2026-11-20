"""Chat streaming の wire 出力と完了状態を統括する application service。"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import structlog

from .chat_completion import CompletionPorts, TurnContext, finalize_chat_turn, record_turn_failure
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

        full: list[str] = []
        pending = ""
        usage: dict | None = None
        runtime_session_id: str | None = None
        final_text: str | None = None
        runtime_error_emitted = False
        runtime_iterator = ports.stream_runtime(context.turn.client, context.prompt)
        try:
            async for item in runtime_iterator:
                if isinstance(item, tuple):
                    final_text, usage, runtime_session_id = item
                    continue
                if isinstance(item, RuntimeEvent):
                    runtime_error_emitted = (
                        runtime_error_emitted or item.kind == "error"
                    )
                    event_queue.put_nowait(item)
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
                        yield text_chunk(released)
                else:
                    yield text_chunk(item)
            answer = final_text if final_text is not None else "".join(full)
            if context.inspect_output:
                check_answer(answer)
                if pending:
                    yield text_chunk(pending)
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
            try:
                close = getattr(runtime_iterator, "aclose", None)
                if close is not None:
                    await close()
            finally:
                event_queue.put_nowait(None)

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
