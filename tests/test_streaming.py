"""
ストリーミング SSE チャンクのフォーマット検証テスト。

SDK の stream_claude をモックし、StreamingResponse が
正しい SSE 形式 (data: {...}\n\n) を出力するかを確認する。
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

from kotoyose.app import app

from tests.fixtures.api_keys import TEST_API_HEADERS

pytestmark = pytest.mark.usefixtures("client_api_key", "configured_chat_alias")



async def _fake_stream(client, prompt: str):
    for chunk in ["Hello", ", ", "world!"]:
        yield chunk
    yield (None, None, None)


@pytest.mark.asyncio
async def test_streaming_sse_format():
    """stream=true で SSE 行が正しい形式で返る。"""
    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.chat.get_or_resume_client") as mock_resume,
        patch("kotoyose.routes.chat.stream_claude", new=_fake_stream),
        patch("kotoyose.routes.chat._rotate_hash_key", new=AsyncMock()),
    ):
        mock_resume.return_value = (AsyncMock(), False)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS
        ) as ac:
            async with ac.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "kotoyose-chat",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            ) as response:
                assert response.status_code == 200
                assert "text/event-stream" in response.headers["content-type"]

                lines = []
                async for line in response.aiter_lines():
                    if line:
                        lines.append(line)

    # Filter data lines
    data_lines = [l for l in lines if l.startswith("data: ")]
    assert len(data_lines) >= 3  # at least: first chunk, content chunks, [DONE]

    # Last data line must be [DONE]
    assert data_lines[-1] == "data: [DONE]"

    # All non-[DONE] data lines must be valid JSON
    for line in data_lines[:-1]:
        payload = json.loads(line[len("data: "):])
        assert "choices" in payload
        assert payload["object"] == "chat.completion.chunk"


@pytest.mark.asyncio
async def test_non_streaming_still_works():
    """stream=false (デフォルト) で通常の JSON レスポンスが返る。"""
    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.chat.get_or_resume_client") as mock_resume,
        patch("kotoyose.routes.chat.ask_claude", new=AsyncMock(return_value=("hi!", None, None))),
        patch("kotoyose.routes.chat._rotate_hash_key", new=AsyncMock()),
    ):
        mock_resume.return_value = (AsyncMock(), False)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS
        ) as ac:
            resp = await ac.post(
                "/v1/chat/completions",
                json={
                    "model": "kotoyose-chat",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["object"] == "chat.completion"
        assert body["choices"][0]["message"]["content"] == "hi!"


@pytest.mark.asyncio
async def test_first_turn_sync_error_disconnects_uncached_client():
    """初回応答の失敗時に、cache 未登録 runtime を残さない。"""
    client = AsyncMock()
    with (
        patch("kotoyose.auth.API_KEY", None),
        patch(
            "kotoyose.routes.chat.get_or_resume_client",
            new=AsyncMock(return_value=(client, False)),
        ),
        patch(
            "kotoyose.routes.chat.ask_claude",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch("kotoyose.routes.chat.record_failed", new=AsyncMock()),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS
        ) as ac:
            response = await ac.post(
                "/v1/chat/completions",
                json={
                    "model": "kotoyose-chat",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": False,
                },
            )

    assert response.status_code == 500
    client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_first_turn_stream_error_disconnects_uncached_client():
    """初回 stream の失敗時も、cache 未登録 runtime を残さない。"""
    client = AsyncMock()

    async def failing_stream(_client, _prompt):
        raise RuntimeError("boom")
        yield  # pragma: no cover

    with (
        patch("kotoyose.auth.API_KEY", None),
        patch(
            "kotoyose.routes.chat.get_or_resume_client",
            new=AsyncMock(return_value=(client, False)),
        ),
        patch("kotoyose.routes.chat.stream_claude", new=failing_stream),
        patch("kotoyose.routes.chat.record_failed", new=AsyncMock()),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS
        ) as ac:
            response = await ac.post(
                "/v1/chat/completions",
                json={
                    "model": "kotoyose-chat",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            )

    assert response.status_code == 200
    assert "[Error: stream interrupted]" in response.text
    client.disconnect.assert_awaited_once()


@pytest.mark.parametrize("event_header", [None, "0", "1"])
async def test_streaming_runtime_events_opt_in(event_header):
    from kotoyose.runtime.base import RuntimeEvent

    async def stream_runtime(client, prompt):
        yield "before"
        yield RuntimeEvent("tool_call", "read", "pending", {"tool_call_id": "call", "title": "cat a.py", "private": True})
        yield RuntimeEvent("tool_use", "Bash", "x" * 300, {"tool_use_id": "use"})
        yield RuntimeEvent("thinking", summary="hidden")
        yield "after"
        yield RuntimeEvent("tool_call_update", summary="completed", payload={"tool_call_id": "call"})
        yield RuntimeEvent("tool_result", "Bash", "error: boom", {"tool_use_id": "use", "is_error": True})
        yield RuntimeEvent("tool_call", payload={"tool_call_id": "x" * 257})
        yield (None, {"input_tokens": 5, "output_tokens": 2, "cost_usd": 0.1}, None)

    with (
        patch("kotoyose.routes.chat.get_or_resume_client", new=AsyncMock(return_value=(AsyncMock(), False))),
        patch("kotoyose.routes.chat.stream_claude", new=stream_runtime),
        patch("kotoyose.routes.chat.guard_enabled", return_value=True),
        patch("kotoyose.routes.chat._rotate_hash_key", new=AsyncMock()),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS,
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={} if event_header is None else {"X-Kotoyose-Events": event_header},
                json={"model": "kotoyose-chat", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            )
    assert response.status_code == 200
    lines = response.text.splitlines()
    assert lines[-2] == "data: [DONE]"
    chunks = [json.loads(line[6:]) for line in lines if line.startswith("data: {")]
    if event_header != "1":
        assert len(chunks) == 3
        assert chunks[1]["choices"][0]["delta"]["content"] == "beforeafter"
        assert "usage" not in chunks[-1]
        assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
        return

    assert chunks[1]["choices"][0]["delta"]["content"] == "before"
    assert chunks[2] == {
        "object": "kotoyose.event", "kind": "tool_call", "tool_call_id": "call",
        "title": "cat a.py", "tool_kind": "read", "status": "pending",
    }
    assert chunks[3] == {
        "object": "kotoyose.event", "kind": "tool_call", "tool_call_id": "use",
        "title": ("Bash: " + "x" * 300)[:200], "tool_kind": "execute", "status": "in_progress",
    }
    assert chunks[4]["choices"][0]["delta"]["content"] == "after"
    assert chunks[5] == {
        "object": "kotoyose.event", "kind": "tool_call_update", "tool_call_id": "call",
        "title": "tool", "tool_kind": "other", "status": "completed",
    }
    assert chunks[6] == {
        "object": "kotoyose.event", "kind": "tool_call_update", "tool_call_id": "use",
        "tool_kind": "execute", "status": "failed",
    }
    chunks = chunks[:6] + chunks[7:]
    assert chunks[6]["usage"]["prompt_tokens"] == 5
    assert chunks[6]["usage"]["cost_usd"] == 0.1
