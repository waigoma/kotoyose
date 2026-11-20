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
