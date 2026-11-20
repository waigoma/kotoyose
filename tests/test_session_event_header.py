"""応答ヘッダ X-Session-Event の回帰テスト。

呼び出し側 (ai-task-hub) は「既存の X-Conversation-Key を送ったのに created が
返った」ことだけを手掛かりに kotoyose 側の文脈喪失を判ずる。したがって 4 値が
正しく出ることと、streaming / エラーパスでも欠けないことが要件である。
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from kotoyose.app import app
from kotoyose.routes.chat import SESSION_EVENT_HEADER
from kotoyose.session import session_cache_identities, sessions

from tests.fixtures.api_keys import TEST_API_HEADERS

pytestmark = pytest.mark.usefixtures("client_api_key", "configured_chat_alias")

_IDENTITY = ("claude", "kotoyose-chat", "digest")



async def _fake_stream(client, prompt: str):
    yield "はい"
    yield (None, None, None)


async def _post(headers: dict[str, str] | None = None, **overrides):
    """同期パスを 1 往復させて応答を返す。"""
    body = {
        "model": "kotoyose-chat",
        "messages": [{"role": "user", "content": "ごきげんよう"}],
        "stream": False,
    }
    body.update(overrides)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS) as ac:
        return await ac.post("/v1/chat/completions", json=body, headers=headers)


@pytest.fixture
def sync_chat():
    """同期パスの外部依存 (SDK・interactions・session 永続化) を潰す。"""
    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.chat.get_or_resume_client") as mock_resume,
        patch(
            "kotoyose.routes.chat.ask_claude",
            new=AsyncMock(return_value=("お答え", None, "sess_test")),
        ),
        patch("kotoyose.routes.chat.record_received", new=AsyncMock(return_value=1)),
        patch("kotoyose.routes.chat.record_completed", new=AsyncMock()),
        patch("kotoyose.routes.chat.record_failed", new=AsyncMock()),
        patch("kotoyose.routes.chat._safe_upsert_session", new=AsyncMock()),
        patch("kotoyose.routes.chat._rotate_hash_key", new=AsyncMock(return_value="k")),
    ):
        yield mock_resume


@pytest.fixture
def seeded_cache():
    """cache_hit を成立させるため sessions / identity を仕込み、後で必ず片付ける。"""
    key = "channel:cache-hit"
    cached = AsyncMock()
    cached.invalidated = False
    sessions[key] = cached
    session_cache_identities[key] = _IDENTITY
    try:
        yield key
    finally:
        sessions.pop(key, None)
        session_cache_identities.pop(key, None)


@pytest.mark.asyncio
async def test_created_when_key_is_unknown(sync_chat):
    """未知の鍵は created。呼び出し側はこれを文脈喪失の signal として使う。"""
    sync_chat.return_value = (AsyncMock(), False)

    res = await _post(headers={"X-Conversation-Key": "channel:brand-new"})

    assert res.status_code == 200
    assert res.headers[SESSION_EVENT_HEADER] == "created"


@pytest.mark.asyncio
async def test_resumed_when_session_store_had_the_key(sync_chat):
    """DB から session id を引けたら resumed。喪失ではない。"""
    sync_chat.return_value = (AsyncMock(), True)

    res = await _post(headers={"X-Conversation-Key": "channel:known"})

    assert res.headers[SESSION_EVENT_HEADER] == "resumed"


@pytest.mark.asyncio
async def test_cache_hit_when_client_is_still_in_memory(sync_chat, seeded_cache):
    """インメモリの client を使い回した場合は cache_hit。"""
    sync_chat.return_value = (AsyncMock(), False)

    with patch("kotoyose.routes.chat._cache_identity", return_value=_IDENTITY):
        res = await _post(headers={"X-Conversation-Key": seeded_cache})

    assert res.headers[SESSION_EVENT_HEADER] == "cache_hit"


@pytest.mark.asyncio
async def test_first_when_no_conversation_key_is_sent(sync_chat):
    """鍵を送らない初回 (ハッシュパス) は first。"""
    sync_chat.return_value = (AsyncMock(), False)

    res = await _post()

    assert res.headers[SESSION_EVENT_HEADER] == "first"


@pytest.mark.asyncio
async def test_header_is_present_on_the_streaming_response():
    """SSE でもヘッダは落ちない。session_event はストリーム開始前に確定している。"""
    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.chat.get_or_resume_client") as mock_resume,
        patch("kotoyose.routes.chat.stream_claude", new=_fake_stream),
        patch("kotoyose.routes.chat.record_received", new=AsyncMock(return_value=1)),
        patch("kotoyose.routes.chat.record_completed", new=AsyncMock()),
        patch("kotoyose.routes.chat._safe_upsert_session", new=AsyncMock()),
    ):
        mock_resume.return_value = (AsyncMock(), False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS) as ac:
            async with ac.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "kotoyose-chat",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
                headers={"X-Conversation-Key": "channel:streamed"},
            ) as res:
                assert res.status_code == 200
                assert res.headers[SESSION_EVENT_HEADER] == "created"
                async for _ in res.aiter_lines():
                    pass


@pytest.mark.asyncio
async def test_header_is_present_on_the_sdk_error_response():
    """SDK が倒れた 500 にも載せる (admin UI と curl から追えるように)。"""
    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.chat.get_or_resume_client") as mock_resume,
        patch("kotoyose.routes.chat.ask_claude", new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch("kotoyose.routes.chat.record_received", new=AsyncMock(return_value=1)),
        patch("kotoyose.routes.chat.record_failed", new=AsyncMock()),
    ):
        mock_resume.return_value = (AsyncMock(), False)
        res = await _post(headers={"X-Conversation-Key": "channel:exploded"})

    assert res.status_code == 500
    assert res.headers[SESSION_EVENT_HEADER] == "created"
