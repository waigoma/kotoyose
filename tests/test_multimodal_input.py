"""OpenAI 互換の画像 content parts を runtime 入力へ変換するテスト。"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from openai_codex import ImageInput, TextInput
from cursor_sdk import UserMessage

from kotoyose.app import app
from kotoyose.config import HarnessConfig
from kotoyose.conversation_key import derive_lookup_key, derive_storage_key
from kotoyose.models import ChatMessage
from kotoyose.runtime.base import RuntimeImageInput, RuntimeTextInput
from kotoyose.runtime.claude import _claude_query_input
from kotoyose.runtime.codex import _codex_turn_input
from kotoyose.runtime.cursor import _cursor_user_message

from tests.fixtures.api_keys import TEST_API_HEADERS

pytestmark = pytest.mark.usefixtures("client_api_key", "configured_chat_alias")


IMAGE_DATA_URL = "data:image/png;base64,iVBORw0KGgo="


def _multimodal_message(text: str = "この画像を説明して") -> ChatMessage:
    """代表的な OpenWebUI / OpenAI 互換画像メッセージを作る。"""
    return ChatMessage.model_validate(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {
                    "type": "image_url",
                    "image_url": {"url": IMAGE_DATA_URL, "detail": "high"},
                },
            ],
        }
    )


def test_chat_message_converts_content_parts_without_exposing_image_as_text():
    """テキストと画像を順序どおり変換し、text_content には画像を含めない。"""
    message = _multimodal_message()
    prompt = message.runtime_prompt()

    assert prompt == [
        RuntimeTextInput("この画像を説明して"),
        RuntimeImageInput(IMAGE_DATA_URL, "high"),
    ]
    assert message.text_content() == "この画像を説明して"


def test_text_only_content_parts_collapse_to_plain_runtime_prompt():
    """画像のない content parts は全 provider が扱える文字列へ畳む。"""
    message = ChatMessage.model_validate(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "前半"},
                {"type": "text", "text": "後半"},
            ],
        }
    )

    assert message.runtime_prompt() == "前半\n後半"


def test_chat_message_rejects_unsupported_or_malformed_image_urls():
    """file URL と壊れた base64 は provider 呼び出し前に拒否する。"""
    for url in ("file:///tmp/private.png", "data:image/png;base64,***"):
        with pytest.raises(ValueError, match="image_url.url"):
            ChatMessage.model_validate(
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": url}}
                    ],
                }
            )


def test_chat_message_rejects_oversized_image(monkeypatch):
    """上限を超える base64 画像は provider 呼び出し前に拒否する。"""
    monkeypatch.setattr("kotoyose.models._MAX_IMAGE_DATA_BYTES", 2)
    with pytest.raises(ValueError, match="20 MiB image limit"):
        ChatMessage.model_validate(
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,QUJD"},
                    }
                ],
            }
        )


def test_chat_message_rejects_more_than_twenty_images():
    """1 message に 21 画像を詰めた入力を拒否する。"""
    with pytest.raises(ValueError, match="more than 20 images"):
        ChatMessage.model_validate(
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": IMAGE_DATA_URL},
                    }
                    for _ in range(21)
                ],
            }
        )


async def test_claude_input_uses_streaming_image_content_block():
    """Claude には data URL を Anthropic base64 image source として渡す。"""
    query_input = _claude_query_input(_multimodal_message().runtime_prompt())
    messages = [message async for message in query_input]

    assert messages == [
        {
            "type": "user",
            "session_id": "",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "この画像を説明して"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "iVBORw0KGgo=",
                        },
                    },
                ],
            },
            "parent_tool_use_id": None,
        }
    ]


def test_codex_and_cursor_receive_native_image_inputs():
    """Codex / Cursor でも各 SDK の画像型へ変換する。"""
    prompt = _multimodal_message().runtime_prompt()

    codex_input = _codex_turn_input(prompt)
    assert isinstance(codex_input[0], TextInput)
    assert isinstance(codex_input[1], ImageInput)
    assert codex_input[1].url == IMAGE_DATA_URL

    cursor_input = _cursor_user_message(prompt)
    assert isinstance(cursor_input, UserMessage)
    assert cursor_input.text == "この画像を説明して"
    assert cursor_input.images[0].mime_type == "image/png"
    assert cursor_input.images[0].data == "iVBORw0KGgo="


def test_multimodal_history_keeps_hash_rotation_invariant():
    """画像付き履歴でも前 turn の storage key と次 turn の lookup key が一致する。"""
    first = [_multimodal_message()]
    storage_key = derive_storage_key(first, "猫です")
    next_turn = [
        *first,
        ChatMessage(role="assistant", content="猫です"),
        ChatMessage(role="user", content="色は？"),
    ]

    assert derive_lookup_key(next_turn) == storage_key


@pytest.mark.asyncio
@pytest.mark.usefixtures("configured_chat_alias")
async def test_chat_route_forwards_images_but_redacts_them_from_interactions():
    """API は画像を runtime へ渡し、interaction prompt には本体を保存しない。"""
    captured: dict[str, object] = {}

    async def fake_ask(client, prompt):
        captured["prompt"] = prompt
        return "画像の説明", None, None

    record_received = AsyncMock(return_value=1)
    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.chat.get_or_resume_client", new=AsyncMock(return_value=(AsyncMock(), False))),
        patch("kotoyose.routes.chat.ask_claude", new=fake_ask),
        patch("kotoyose.routes.chat.record_received", new=record_received),
        patch("kotoyose.routes.chat.record_completed", new=AsyncMock()),
        patch("kotoyose.routes.chat._rotate_hash_key", new=AsyncMock()),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "kotoyose-chat",
                    "messages": [_multimodal_message().model_dump(mode="json")],
                },
            )

    assert response.status_code == 200
    assert captured["prompt"] == _multimodal_message().runtime_prompt()
    saved_prompt = record_received.await_args.kwargs["prompt"]
    assert saved_prompt == "この画像を説明して\n[image]"
    assert "base64" not in saved_prompt


@pytest.mark.asyncio
async def test_chat_route_rejects_images_for_provider_without_safe_image_path():
    """Antigravity では画像を黙って捨てず、明示的な 400 を返す。"""
    config = HarnessConfig(models={"agy-model": {"provider": "antigravity"}})
    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.chat.get_config", return_value=config),
        patch("kotoyose.routes.chat.get_or_resume_client", new=AsyncMock()) as dispatch,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "agy-model",
                    "messages": [_multimodal_message().model_dump(mode="json")],
                },
            )

    dispatch.assert_not_awaited()
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "does not support image input" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_chat_route_rejects_historical_image_for_unsupported_provider():
    """最新 turn が text-only でも履歴内画像を非対応 provider で見逃さない。"""
    config = HarnessConfig(models={"antigravity-model": {"provider": "antigravity"}})
    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.chat.get_config", return_value=config),
        patch("kotoyose.routes.chat.get_or_resume_client", new=AsyncMock()) as dispatch,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "antigravity-model",
                    "messages": [
                        _multimodal_message().model_dump(mode="json"),
                        {"role": "assistant", "content": "画像の説明"},
                        {"role": "user", "content": "続きを教えて"},
                    ],
                },
            )

    dispatch.assert_not_awaited()
    assert response.status_code == 400
    assert "does not support image input" in response.json()["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex", "cursor"])
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_chat_route_rejects_remote_image_url_for_local_provider(provider, scheme):
    """Codex が未対応の remote URL を SDK の不透明な 500 にせず 400 にする。"""
    config = HarnessConfig(models={f"{provider}-model": {"provider": provider}})
    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.chat.get_config", return_value=config),
        patch("kotoyose.routes.chat.get_or_resume_client", new=AsyncMock()) as dispatch,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS
        ) as client:
            response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": f"{provider}-model",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"{scheme}://example.com/image.png"},
                                }
                            ],
                        }
                    ],
                },
            )

    dispatch.assert_not_awaited()
    assert response.status_code == 400
    assert "base64 data URL" in response.json()["error"]["message"]
