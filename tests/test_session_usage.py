"""ask_claude / stream_claude が usage 情報と claude_session_id を返すことを確認するテスト。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from kotoyose.activity import bind_event_queue
from kotoyose.runtime.base import RuntimeEvent, RuntimeSession, RuntimeTurnError
from kotoyose.runtime.claude import ClaudeRuntimeSession
from kotoyose.session import ask_claude, stream_claude


def _result(usage=None, total_cost_usd=None, session_id=""):
    """Construct the actual installed SDK terminal contract, without a provider."""
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id=session_id, usage=usage, total_cost_usd=total_cost_usd,
    )


@pytest.mark.asyncio
async def test_ask_claude_returns_text_usage_and_session_id():
    """AssistantMessage.session_id があれば (text, usage, session_id) を返す。"""
    async def fake_receive():
        yield AssistantMessage(
            content=[TextBlock(text="お答え")],
            model="haiku",
            session_id="sess_abc123",
        )
        yield _result({"input_tokens": 10, "output_tokens": 3})

    fake_client = MagicMock()
    fake_client.query = AsyncMock()
    fake_client.receive_response = fake_receive
    runtime = ClaudeRuntimeSession(client=fake_client)

    assert isinstance(runtime, RuntimeSession)
    text, usage, session_id = await ask_claude(runtime, "hi")
    assert text == "お答え"
    assert usage == {"input_tokens": 10, "output_tokens": 3}
    assert session_id == "sess_abc123"


@pytest.mark.asyncio
async def test_ask_claude_rejects_missing_terminal_result():
    """A truncated SDK stream is failure, even when it emitted partial text."""
    async def fake_receive():
        yield AssistantMessage(content=[TextBlock(text="hi")], model="haiku")

    fake_client = MagicMock()
    fake_client.query = AsyncMock()
    fake_client.receive_response = fake_receive
    runtime = ClaudeRuntimeSession(client=fake_client)

    with pytest.raises(RuntimeTurnError) as error:
        await ask_claude(runtime, "hi")
    assert error.value.partial_result.text == "hi"
    assert error.value.partial_result.usage is None
    assert error.value.partial_result.runtime_session_id is None
    assert runtime.invalidated


@pytest.mark.asyncio
async def test_ask_claude_uses_authoritative_result_session_id():
    """The terminal result supersedes any provisional assistant identity."""
    async def fake_receive():
        yield AssistantMessage(content=[TextBlock(text="A")], model="haiku", session_id="sess_first")
        yield AssistantMessage(content=[TextBlock(text="B")], model="haiku", session_id="sess_second")
        yield _result(session_id="sess_terminal")

    fake_client = MagicMock()
    fake_client.query = AsyncMock()
    fake_client.receive_response = fake_receive
    runtime = ClaudeRuntimeSession(client=fake_client)

    _, _, session_id = await ask_claude(runtime, "hi")
    assert session_id == "sess_terminal"


@pytest.mark.asyncio
async def test_stream_claude_yields_chunks_then_returns_usage_and_session_id():
    """stream_claude は文字列 chunk を yield しつつ最後に (None, usage, session_id) タプルを yield する。"""
    async def fake_receive():
        yield AssistantMessage(content=[TextBlock(text="お")], model="haiku", session_id="sess_xyz")
        yield AssistantMessage(content=[TextBlock(text="答え")], model="haiku", session_id="sess_xyz")
        yield _result({"input_tokens": 5, "output_tokens": 2})

    fake_client = MagicMock()
    fake_client.query = AsyncMock()
    fake_client.receive_response = fake_receive
    runtime = ClaudeRuntimeSession(client=fake_client)

    chunks = []
    final_usage = None
    final_session_id = None
    async for item in stream_claude(runtime, "hi"):
        if isinstance(item, tuple):
            _, final_usage, final_session_id = item
        elif isinstance(item, RuntimeEvent):
            continue
        else:
            chunks.append(item)
    assert chunks == ["お", "答え"]
    assert final_usage == {"input_tokens": 5, "output_tokens": 2}
    assert final_session_id == "sess_xyz"


@pytest.mark.asyncio
async def test_ask_claude_includes_sdk_total_cost_in_usage():
    """Claude SDK の total_cost_usd を OpenAI 互換 response 用に渡す。"""
    async def fake_receive():
        yield _result({"input_tokens": 5, "output_tokens": 2}, total_cost_usd=0.0123)

    fake_client = MagicMock()
    fake_client.query = AsyncMock()
    fake_client.receive_response = fake_receive

    _, usage, _ = await ask_claude(ClaudeRuntimeSession(client=fake_client), "hi")

    assert usage == {"input_tokens": 5, "output_tokens": 2, "cost_usd": 0.0123}


@pytest.mark.asyncio
async def test_claude_idle_timeout_is_extended_by_each_message():
    """継続して message を受信する Claude 応答は idle timeout を超過しない。"""
    async def fake_receive():
        for text in ("A", "B", "C"):
            await asyncio.sleep(0.01)
            yield AssistantMessage(content=[TextBlock(text=text)], model="haiku")
        yield _result()

    fake_client = MagicMock()
    fake_client.query = AsyncMock()
    fake_client.receive_response = fake_receive

    result = await ClaudeRuntimeSession(client=fake_client, timeout_seconds=0.02).ask("hi")

    assert result.text == "ABC"


@pytest.mark.asyncio
async def test_claude_idle_timeout_invalidates_and_disconnects_client():
    """無応答 timeout の client は次ターンで再利用できないよう無効化する。"""
    async def fake_receive():
        await asyncio.sleep(0.05)
        if False:
            yield None

    fake_client = MagicMock()
    fake_client.query = AsyncMock()
    fake_client.disconnect = AsyncMock()
    fake_client.receive_response = fake_receive
    runtime = ClaudeRuntimeSession(client=fake_client, timeout_seconds=0.01)

    with pytest.raises(RuntimeError, match="idle timeout"):
        await runtime.ask("hi")

    assert runtime.invalidated is True
    fake_client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_claude_stream_does_not_timeout_while_sse_client_is_slow():
    """SSE consumer の送信待ちは Claude の idle timeout として扱わない。"""
    async def fake_receive():
        yield AssistantMessage(content=[TextBlock(text="A")], model="haiku")
        yield AssistantMessage(content=[TextBlock(text="B")], model="haiku")
        yield _result()

    fake_client = MagicMock()
    fake_client.query = AsyncMock()
    fake_client.disconnect = AsyncMock()
    fake_client.receive_response = fake_receive
    runtime = ClaudeRuntimeSession(client=fake_client, timeout_seconds=0.01)
    response = runtime.stream("hi")

    while isinstance(item := await anext(response), RuntimeEvent):
        pass
    assert item == "A"
    await asyncio.sleep(0.02)
    while isinstance(item := await anext(response), RuntimeEvent):
        pass
    assert item == "B"
    assert runtime.invalidated is False
    fake_client.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_claude_runtime_emits_structured_activity_events():
    """tool use/result、text、turn を本文 chunk と独立した event に変換する。"""
    async def fake_receive():
        yield AssistantMessage(
            content=[
                TextBlock(text="確認します"),
                ToolUseBlock(id="tool-1", name="Bash", input={"command": "pnpm -r test"}),
            ],
            model="sonnet",
        )
        yield UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="tool-1",
                    content="all tests passed",
                    is_error=False,
                )
            ]
        )
        yield _result()

    fake_client = MagicMock()
    fake_client.query = AsyncMock()
    fake_client.receive_response = fake_receive

    items = [item async for item in ClaudeRuntimeSession(client=fake_client).stream("test")]
    events = [item for item in items if isinstance(item, RuntimeEvent)]

    assert [event.kind for event in events] == ["turn", "text", "tool_use", "tool_result"]
    assert events[0].payload == {"turn": 1}
    assert events[2].tool_name == "Bash"
    assert events[2].summary == '{"command": "pnpm -r test"}'
    assert events[3].tool_name == "Bash"
    assert events[3].payload["is_error"] is False
    assert "確認します" in items


@pytest.mark.asyncio
async def test_runtime_event_summaries_are_single_line_and_truncated():
    """event の summary と payload 断片は改行を除き 500 文字以内に制限する。"""
    async def fake_receive():
        yield AssistantMessage(
            content=[ToolUseBlock(id="tool-1", name="Write", input={"content": "a\n" * 400})],
            model="sonnet",
        )
        yield _result()

    fake_client = MagicMock()
    fake_client.query = AsyncMock()
    fake_client.receive_response = fake_receive

    items = [item async for item in ClaudeRuntimeSession(client=fake_client).stream("test")]
    event = next(item for item in items if isinstance(item, RuntimeEvent) and item.kind == "tool_use")
    assert len(event.summary) <= 500
    assert "\n" not in event.summary
    assert len(event.payload["input"]) <= 500


@pytest.mark.asyncio
async def test_ask_claude_emits_runtime_events_to_bound_queue():
    """bind_event_queue 下の ask_claude は RuntimeEvent を欠落させず queue へ送る。"""
    async def fake_receive():
        yield AssistantMessage(
            content=[
                TextBlock(text="調査中"),
                ToolUseBlock(id="tool-ask-1", name="Read", input={"file_path": "/tmp/a.txt"}),
            ],
            model="sonnet",
            session_id="sess_ask_events",
        )
        yield UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="tool-ask-1",
                    content="contents",
                    is_error=False,
                )
            ]
        )
        yield _result({"input_tokens": 8, "output_tokens": 5}, session_id="sess_ask_events")

    fake_client = MagicMock()
    fake_client.query = AsyncMock()
    fake_client.receive_response = fake_receive
    runtime = ClaudeRuntimeSession(client=fake_client)

    queue: asyncio.Queue[RuntimeEvent | None] = asyncio.Queue()
    with bind_event_queue(queue):
        text, usage, session_id = await ask_claude(runtime, "read it")

    events: list[RuntimeEvent] = []
    while not queue.empty():
        item = queue.get_nowait()
        if item is not None:
            events.append(item)

    assert text == "調査中"
    assert usage == {"input_tokens": 8, "output_tokens": 5}
    assert session_id == "sess_ask_events"
    assert [event.kind for event in events] == ["turn", "text", "tool_use", "tool_result"]
    assert events[2].tool_name == "Read"
    assert events[3].tool_name == "Read"
