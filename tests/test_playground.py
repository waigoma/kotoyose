"""Playground 内部 API の設定解決・MCP 検出・SSE 形式を検証する。"""

import json
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from kotoyose.app import app
from kotoyose.config import HarnessConfig, ModelPreset


def _config() -> HarnessConfig:
    """テスト用の実 MCP 設定を含む HarnessConfig を作る。"""
    return HarnessConfig(
        models={
            "playground": ModelPreset(
                model="playground-model",
                mcp_servers={"memory": {"type": "http", "url": "http://mcp.test/mcp"}},
            )
        },
    )


async def test_playground_presets_use_loaded_config():
    """preset 一覧が既存 HarnessConfig の runtime 解決結果を返す。"""
    with patch("kotoyose.auth.API_KEY", None), patch(
        "kotoyose.routes.playground.get_config", return_value=_config()
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get("/internal/playground/presets")

    assert response.status_code == 200
    assert response.json()["presets"][0] == {
        "id": "playground",
        "provider": "claude",
        "model": "playground-model",
        "mcp_servers": ["memory"],
    }


async def test_playground_mcp_tools_use_sdk_status():
    """MCP endpoint が接続済み SDK client の get_mcp_status を変換する。"""
    class FakeClient:
        provider = "claude"

        def __init__(self):
            self.client = self
            self.disconnect = AsyncMock()

        async def get_mcp_status(self):
            return {
                "mcpServers": [
                    {
                        "name": "memory",
                        "status": "connected",
                        "tools": [{"name": "search", "description": "Search memory"}],
                    }
                ]
            }

    fake = FakeClient()
    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.playground.get_config", return_value=_config()),
        patch(
            "kotoyose.routes.playground.get_or_resume_client",
            new=AsyncMock(return_value=(fake, False)),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get("/internal/playground/mcp-tools?model=playground")

    assert response.status_code == 200
    assert response.json()["servers"][0]["tools"] == [
        {"name": "search", "description": "Search memory"}
    ]
    fake.disconnect.assert_awaited_once()


async def test_playground_chat_streams_runtime_chunks():
    """単発 chat が既存 stream_claude の chunk を SSE へ変換する。"""
    class FakeClient:
        disconnect = AsyncMock()

    async def fake_stream(client, prompt):
        assert prompt == "hello"
        yield "world"
        yield (None, {"output_tokens": 1}, "session-id")

    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.playground.get_config", return_value=_config()),
        patch(
            "kotoyose.routes.playground.get_or_resume_client",
            new=AsyncMock(return_value=(FakeClient(), False)),
        ),
        patch("kotoyose.routes.playground.stream_claude", new=fake_stream),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.post(
                "/internal/playground/chat",
                json={"model": "playground", "message": "hello"},
            )

    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events == [
        {"type": "start", "model": "playground"},
        {"type": "text", "text": "world"},
        {"type": "done", "usage": {"output_tokens": 1}},
    ]
