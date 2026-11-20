"""Chat streaming application service の単体テスト。"""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from kotoyose.chat_completion import CompletionPorts, TurnContext
from kotoyose.chat_streaming import StreamContext, StreamingPorts, stream_chat_response
from kotoyose.models import ChatMessage


def _context() -> StreamContext:
    return StreamContext(
        turn=TurnContext(
            client=AsyncMock(),
            latest_text="hello",
            messages=[ChatMessage(role="user", content="hello")],
            model="kotoyose-chat",
            provider="claude",
            interaction_id=7,
            session_event="created",
            conversation_key="conversation-1",
            lookup_key="conversation-1",
            session_identity=("claude", "kotoyose-chat", "digest"),
        ),
        prompt="hello",
    )


def _ports(stream_runtime) -> StreamingPorts:
    return StreamingPorts(
        stream_runtime=stream_runtime,
        start_event_writer=lambda _: asyncio.Queue(),
        record_failed=AsyncMock(),
        disconnect_client=AsyncMock(),
        completion=CompletionPorts(
            record_completed=AsyncMock(),
            backfill_conversation_key=AsyncMock(),
            rotate_hash_key=AsyncMock(return_value="rotated"),
            upsert_session=AsyncMock(),
            extract_tokens=lambda usage, prompt, answer: (5, 2, "sdk"),
        ),
    )


def _uncached_context() -> StreamContext:
    context = _context()
    return replace(
        context,
        turn=replace(context.turn, conversation_key=None, lookup_key=None),
    )


@pytest.mark.asyncio
async def test_stream_chat_response_completes_ledger_and_session():
    """正常 stream は終端 chunk 後に interaction と固定 session を確定する。"""
    async def stream_runtime(client, prompt):
        yield "answer"
        yield (None, {"input_tokens": 5, "output_tokens": 2}, "session-1")

    context = _context()
    ports = _ports(stream_runtime)

    chunks = [chunk async for chunk in stream_chat_response(context, ports)]

    assert '"role":"assistant"' in chunks[0]
    assert '"content":"answer"' in chunks[1]
    assert chunks[-1] == "data: [DONE]\n\n"
    ports.completion.record_completed.assert_awaited_once()
    ports.completion.upsert_session.assert_awaited_once()
    ports.record_failed.assert_not_awaited()
    ports.disconnect_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_chat_response_converts_runtime_error_to_terminal_sse():
    """Runtime 例外は ledger を failed にし、中断 sentinel と DONE を返す。"""
    async def stream_runtime(client, prompt):
        if False:
            yield ""
        raise RuntimeError("provider down")

    ports = _ports(stream_runtime)

    chunks = [chunk async for chunk in stream_chat_response(_context(), ports)]

    assert any("stream interrupted" in chunk for chunk in chunks)
    assert chunks[-1] == "data: [DONE]\n\n"
    ports.record_failed.assert_awaited_once_with(7, "provider down")
    ports.completion.record_completed.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_chat_response_records_cancelled_runtime_detached():
    """Runtime cancellation でも interaction を failed にし client を解放する。"""
    failure_recorded = asyncio.Event()

    async def stream_runtime(client, prompt):
        yield "partial"
        raise asyncio.CancelledError

    async def record_failed(interaction_id, message):
        failure_recorded.set()

    ports = replace(
        _ports(stream_runtime),
        record_failed=record_failed,
    )
    chunks: list[str] = []

    with pytest.raises(asyncio.CancelledError):
        async for chunk in stream_chat_response(_uncached_context(), ports):
            chunks.append(chunk)

    await asyncio.wait_for(failure_recorded.wait(), timeout=1)
    assert any("stream interrupted" in chunk for chunk in chunks)
    assert chunks[-1] == "data: [DONE]\n\n"
    ports.disconnect_client.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_chat_response_records_generator_close_detached():
    """Client disconnect による generator close でも帳簿と client を片付ける。"""
    failure_recorded = asyncio.Event()

    async def stream_runtime(client, prompt):
        yield "partial"
        await asyncio.Event().wait()

    async def record_failed(interaction_id, message):
        failure_recorded.set()

    ports = replace(
        _ports(stream_runtime),
        record_failed=record_failed,
    )
    iterator = stream_chat_response(_uncached_context(), ports)

    await anext(iterator)
    await anext(iterator)
    await iterator.aclose()

    await asyncio.wait_for(failure_recorded.wait(), timeout=1)
    ports.disconnect_client.assert_awaited_once()
    ports.completion.record_completed.assert_not_awaited()
