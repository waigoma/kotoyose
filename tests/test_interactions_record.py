"""chat.py 経由で interaction が記録されることの統合テスト。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from httpx import AsyncClient, ASGITransport

from kotoyose import activity
from kotoyose.runtime.base import RuntimeEvent
from kotoyose.runtime.claude import ClaudeRuntimeSession

from tests.fixtures.api_keys import TEST_API_HEADERS

pytestmark = pytest.mark.usefixtures("client_api_key", "configured_chat_alias")



@pytest.mark.asyncio
async def test_chat_records_interaction_on_completion():
    """同期パスの完了時に record_received → record_completed が呼ばれる。"""
    from kotoyose.app import app

    received_calls = []
    completed_calls = []

    async def fake_received(**kw):
        received_calls.append(kw)
        return 42

    async def fake_completed(**kw):
        completed_calls.append(kw)

    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.chat.get_or_resume_client") as mock_get,
        patch(
            "kotoyose.routes.chat.ask_claude",
            new=AsyncMock(return_value=("お答え", {"input_tokens": 11, "output_tokens": 3}, "sess_test_001")),
        ),
        patch("kotoyose.routes.chat.record_received", new=fake_received),
        patch("kotoyose.routes.chat.record_completed", new=fake_completed),
        patch("kotoyose.routes.chat.record_failed", new=AsyncMock()),
    ):
        mock_get.return_value = (AsyncMock(), False)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS
        ) as ac:
            resp = await ac.post(
                "/v1/chat/completions",
                json={
                    "model": "kotoyose-chat",
                    "messages": [{"role": "user", "content": "ごきげんよう"}],
                    "stream": False,
                },
                headers={"X-Conversation-Key": "channel:test-record"},
            )
    assert resp.status_code == 200
    assert len(received_calls) == 1
    assert received_calls[0]["model"] == "kotoyose-chat"
    assert received_calls[0]["provider"] == "claude"
    assert received_calls[0]["prompt"] == "ごきげんよう"
    assert len(completed_calls) == 1
    assert completed_calls[0]["interaction_id"] == 42
    assert completed_calls[0]["response"] == "お答え"
    assert completed_calls[0]["tokens_in"] == 11
    assert completed_calls[0]["tokens_out"] == 3
    assert completed_calls[0]["tokens_source"] == "sdk"
    assert completed_calls[0]["provider"] == "claude"
    assert completed_calls[0]["runtime_session_id"] == "sess_test_001"
    # session_event は "created" (mock_get で新規 mock client が AsyncMock になり sessions には事前にない)
    assert completed_calls[0]["session_event"] == "created"


@pytest.mark.asyncio
async def test_chat_upsert_session_failure_does_not_break_response():
    """応答後の session 永続化が失敗しても chat response は 200 のまま返る。"""
    from kotoyose.app import app

    async def fake_received(**kw):
        return 43

    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.chat.get_or_resume_client") as mock_get,
        patch(
            "kotoyose.routes.chat.ask_claude",
            new=AsyncMock(return_value=("お答え", None, "sess_upsert_fail")),
        ),
        patch("kotoyose.routes.chat.record_received", new=fake_received),
        patch("kotoyose.routes.chat.record_completed", new=AsyncMock()),
        patch("kotoyose.routes.chat.record_failed", new=AsyncMock()),
            patch(
                "kotoyose.chat_session.upsert_session",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
    ):
        mock_get.return_value = (AsyncMock(), False)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS
        ) as ac:
            resp = await ac.post(
                "/v1/chat/completions",
                json={
                    "model": "kotoyose-chat",
                    "messages": [{"role": "user", "content": "ごきげんよう"}],
                    "stream": False,
                },
                headers={"X-Conversation-Key": "channel:upsert-fail"},
            )

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "お答え"


@pytest.mark.asyncio
async def test_chat_records_failed_on_sdk_error():
    """ask_claude が例外を上げると record_failed が呼ばれる。"""
    from kotoyose.app import app

    failed_calls = []

    async def fake_failed(interaction_id, error_message):
        failed_calls.append({"id": interaction_id, "msg": error_message})

    async def fake_received(**kw):
        return 99

    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.chat.get_or_resume_client") as mock_get,
        patch("kotoyose.routes.chat.ask_claude", new=AsyncMock(side_effect=RuntimeError("SDK boom"))),
        patch("kotoyose.routes.chat.record_received", new=fake_received),
        patch("kotoyose.routes.chat.record_completed", new=AsyncMock()),
        patch("kotoyose.routes.chat.record_failed", new=fake_failed),
    ):
        mock_get.return_value = (AsyncMock(), False)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS
        ) as ac:
            resp = await ac.post(
                "/v1/chat/completions",
                json={
                    "model": "kotoyose-chat",
                    "messages": [{"role": "user", "content": "boom"}],
                    "stream": False,
                },
                headers={"X-Conversation-Key": "channel:fail-test"},
            )
    assert resp.status_code == 500
    assert len(failed_calls) == 1
    assert failed_calls[0]["id"] == 99
    assert "SDK boom" in failed_calls[0]["msg"]


@pytest.mark.asyncio
async def test_chat_record_received_returns_none_does_not_break():
    """DB 未設定 (record_received が None を返す) でもレスポンスは 200。"""
    from kotoyose.app import app

    async def fake_received(**kw):
        return None

    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.chat.get_or_resume_client") as mock_get,
        patch(
            "kotoyose.routes.chat.ask_claude",
            new=AsyncMock(return_value=("ok", None, None)),
        ),
        patch("kotoyose.routes.chat.record_received", new=fake_received),
        patch("kotoyose.routes.chat.record_completed", new=AsyncMock()),
        patch("kotoyose.routes.chat.record_failed", new=AsyncMock()),
    ):
        mock_get.return_value = (AsyncMock(), False)
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
                headers={"X-Conversation-Key": "channel:no-db"},
            )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_chat_streaming_records_interaction_on_completion():
    """ストリーミングパスでも record_received → record_completed が呼ばれる。"""
    from kotoyose.app import app

    received_calls = []
    completed_calls = []

    async def fake_received(**kw):
        received_calls.append(kw)
        return 77

    async def fake_completed(**kw):
        completed_calls.append(kw)

    async def fake_stream(client, prompt):
        for chunk in ["お", "答", "え"]:
            yield chunk
        yield (None, {"input_tokens": 22, "output_tokens": 6}, "sess_test_002")

    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.chat.get_or_resume_client") as mock_get,
        patch("kotoyose.routes.chat.stream_claude", new=fake_stream),
        patch("kotoyose.routes.chat.record_received", new=fake_received),
        patch("kotoyose.routes.chat.record_completed", new=fake_completed),
        patch("kotoyose.routes.chat.record_failed", new=AsyncMock()),
    ):
        mock_get.return_value = (AsyncMock(), False)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS
        ) as ac:
            async with ac.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "kotoyose-chat",
                    "messages": [{"role": "user", "content": "ごきげんよう"}],
                    "stream": True,
                },
                headers={"X-Conversation-Key": "channel:stream-record"},
            ) as resp:
                assert resp.status_code == 200
                async for _ in resp.aiter_lines():
                    pass

    assert len(received_calls) == 1
    assert received_calls[0]["streamed"] is True
    assert len(completed_calls) == 1
    assert completed_calls[0]["interaction_id"] == 77
    assert completed_calls[0]["response"] == "お答え"
    assert completed_calls[0]["tokens_in"] == 22
    assert completed_calls[0]["tokens_out"] == 6
    assert completed_calls[0]["tokens_source"] == "sdk"


@pytest.mark.asyncio
async def test_event_db_failure_does_not_interrupt_streaming_response():
    """activity event の DB 書込み失敗時も本文と [DONE] まで返す。"""
    from kotoyose import activity
    from kotoyose.app import app

    async def fake_stream(client, prompt):
        yield RuntimeEvent(kind="tool_use", tool_name="Bash", summary="pytest")
        yield "done"
        yield (None, None, None)

    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.chat.get_or_resume_client", new=AsyncMock(return_value=(AsyncMock(), False))),
        patch("kotoyose.routes.chat.stream_claude", new=fake_stream),
        patch("kotoyose.routes.chat.record_received", new=AsyncMock(return_value=88)),
        patch("kotoyose.activity.record_event", new=AsyncMock(side_effect=RuntimeError("db down"))),
        patch("kotoyose.routes.chat.record_completed", new=AsyncMock()),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS) as ac:
            resp = await ac.post(
                "/v1/chat/completions",
                json={
                    "model": "kotoyose-chat",
                    "messages": [{"role": "user", "content": "run"}],
                    "stream": True,
                },
            )
        if activity._event_writer_tasks:
            await asyncio.gather(*activity._event_writer_tasks, return_exceptions=True)

    assert resp.status_code == 200
    assert '"content":"done"' in resp.text
    assert "data: [DONE]" in resp.text


@pytest.mark.asyncio
async def test_event_writer_caps_interaction_at_5000_with_final_error():
    """5,000 件目を打切り error とし、それ以降は保存しない。"""
    from kotoyose import activity

    saved = []

    async def fake_record(*args):
        saved.append(args)

    with patch("kotoyose.activity.record_event", new=fake_record):
        queue = activity.start_event_writer(99)
        for index in range(5002):
            queue.put_nowait(RuntimeEvent(kind="text", summary=str(index)))
        queue.put_nowait(None)
        await asyncio.gather(*list(activity._event_writer_tasks))

    assert len(saved) == 5000
    assert saved[-1][1] == 4999
    assert saved[-1][2] == "error"
    assert "5000" in saved[-1][4]


@pytest.mark.asyncio
async def test_chat_503_calls_record_failed():
    """SDKConnectionError (503) でも record_failed が呼ばれる (interaction を in_progress に放置しない)。"""
    from kotoyose.app import app
    from kotoyose.session import SDKConnectionError

    failed_calls = []

    async def fake_failed(interaction_id, error_message):
        failed_calls.append({"id": interaction_id, "msg": error_message})

    async def fake_received(**kw):
        return 100

    with (
        patch("kotoyose.auth.API_KEY", None),
        patch(
            "kotoyose.routes.chat.get_or_resume_client",
            new=AsyncMock(side_effect=SDKConnectionError("connect refused")),
        ),
        patch("kotoyose.routes.chat.record_received", new=fake_received),
        patch("kotoyose.routes.chat.record_completed", new=AsyncMock()),
        patch("kotoyose.routes.chat.record_failed", new=fake_failed),
    ):
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
                headers={"X-Conversation-Key": "channel:503-test"},
            )
    assert resp.status_code == 503
    assert len(failed_calls) == 1
    assert failed_calls[0]["id"] == 100
    assert "connect refused" in failed_calls[0]["msg"]


@pytest.mark.asyncio
async def test_chat_streaming_records_failed_when_cancelled():
    """cancel (BaseException) で終わった streaming でも帳簿を終端させる。

    idle timeout は runtime 側の `asyncio.timeout` によるタスク cancel で届く。
    CancelledError は BaseException 由来なので `except Exception` を通り抜け、
    record_failed が呼ばれずに interaction が in_progress のまま残っていた。
    """
    from kotoyose.app import app

    failed_calls = []
    completed_calls = []

    async def fake_received(**kw):
        return 99

    async def fake_completed(**kw):
        completed_calls.append(kw)

    recorded = asyncio.Event()

    async def fake_failed(interaction_id, error_message):
        failed_calls.append((interaction_id, error_message))
        recorded.set()

    async def fake_stream(client, prompt):
        yield "途中まで"
        raise asyncio.CancelledError()

    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.chat.get_or_resume_client") as mock_get,
        patch("kotoyose.routes.chat.stream_claude", new=fake_stream),
        patch("kotoyose.routes.chat.record_received", new=fake_received),
        patch("kotoyose.routes.chat.record_completed", new=fake_completed),
        patch("kotoyose.routes.chat.record_failed", new=fake_failed),
    ):
        mock_get.return_value = (AsyncMock(), False)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS
        ) as ac:
            try:
                async with ac.stream(
                    "POST",
                    "/v1/chat/completions",
                    json={
                        "model": "kotoyose-chat",
                        "messages": [{"role": "user", "content": "ごきげんよう"}],
                        "stream": True,
                    },
                    headers={"X-Conversation-Key": "channel:stream-cancel"},
                ) as resp:
                    async for _ in resp.aiter_lines():
                        pass
            except (asyncio.CancelledError, Exception):
                # 中断そのものは呼び出し側へ伝わってよい。帳簿が終端することを見る。
                pass

    # 終端の記録は呼び出し側の cancel から切り離した task で走るため、完了を待つ。
    await asyncio.wait_for(recorded.wait(), timeout=5)

    # 完了として記録してはならない。
    assert completed_calls == []
    # 中断でも失敗として終端すること。
    assert len(failed_calls) == 1, f"record_failed が呼ばれていない: {failed_calls}"
    assert failed_calls[0][0] == 99
    assert "terminal status" in failed_calls[0][1]


@pytest.mark.asyncio
async def test_chat_streaming_emits_terminal_chunk_when_cancelled():
    """中断で終わった streaming でも、呼び出し側へ終端の合図を送る。

    sentinel `[Error: stream interrupted]` と `[DONE]` は `except Exception` の中に
    しか無く、runtime の idle timeout (task cancel) 経路では送られていなかった。
    合図が届かないと、呼び出し側は自分の timeout まで待たされる。
    """
    from kotoyose.app import app

    async def fake_received(**kw):
        return 123

    async def fake_stream(client, prompt):
        yield "途中まで"
        raise asyncio.CancelledError()

    body = ""
    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.chat.get_or_resume_client") as mock_get,
        patch("kotoyose.routes.chat.stream_claude", new=fake_stream),
        patch("kotoyose.routes.chat.record_received", new=fake_received),
        patch("kotoyose.routes.chat.record_completed", new=AsyncMock()),
        patch("kotoyose.routes.chat.record_failed", new=AsyncMock()),
    ):
        mock_get.return_value = (AsyncMock(), False)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS
        ) as ac:
            try:
                async with ac.stream(
                    "POST",
                    "/v1/chat/completions",
                    json={
                        "model": "kotoyose-chat",
                        "messages": [{"role": "user", "content": "ごきげんよう"}],
                        "stream": True,
                    },
                    headers={"X-Conversation-Key": "channel:stream-cancel-tail"},
                ) as resp:
                    async for line in resp.aiter_lines():
                        body += line + "\n"
            except (asyncio.CancelledError, Exception):
                pass

    assert "[Error: stream interrupted]" in body, f"sentinel が届いていない: {body[-300:]}"
    assert "data: [DONE]" in body, f"[DONE] が届いていない: {body[-300:]}"


@pytest.mark.asyncio
async def test_chat_sync_records_runtime_tool_events():
    """非ストリーミング /v1/chat/completions でも RuntimeEvent が interaction_events に記録される。"""
    from kotoyose.app import app

    async def fake_receive():
        yield AssistantMessage(
            content=[
                TextBlock(text="確認します"),
                ToolUseBlock(id="tool-sync-1", name="Bash", input={"command": "ls -la"}),
            ],
            model="sonnet",
            session_id="sess_sync_events",
        )
        yield UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="tool-sync-1",
                    content="file1.txt",
                    is_error=False,
                )
            ]
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sess_sync_events",
            usage={"input_tokens": 9, "output_tokens": 4},
        )

    fake_client = MagicMock()
    fake_client.query = AsyncMock()
    fake_client.receive_response = fake_receive
    runtime = ClaudeRuntimeSession(client=fake_client)

    recorded_events = []

    async def fake_record_event(interaction_id, seq, kind, tool_name, summary, payload):
        recorded_events.append((interaction_id, seq, kind, tool_name, summary, payload))

    with (
        patch("kotoyose.auth.API_KEY", None),
        patch("kotoyose.routes.chat.get_or_resume_client", new=AsyncMock(return_value=(runtime, False))),
        patch("kotoyose.routes.chat.record_received", new=AsyncMock(return_value=55)),
        patch("kotoyose.routes.chat.record_completed", new=AsyncMock()),
        patch("kotoyose.routes.chat.record_failed", new=AsyncMock()),
        patch("kotoyose.activity.record_event", new=fake_record_event),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS) as ac:
            resp = await ac.post(
                "/v1/chat/completions",
                json={
                    "model": "kotoyose-chat",
                    "messages": [{"role": "user", "content": "確認して"}],
                    "stream": False,
                },
                headers={"X-Conversation-Key": "channel:sync-events"},
            )
        if activity._event_writer_tasks:
            await asyncio.gather(*list(activity._event_writer_tasks), return_exceptions=True)

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "確認します"
    assert [row[0] for row in recorded_events] == [55, 55, 55, 55]
    assert [row[1] for row in recorded_events] == [0, 1, 2, 3]
    assert [row[2] for row in recorded_events] == ["turn", "text", "tool_use", "tool_result"]
    assert recorded_events[2][3] == "Bash"
    assert recorded_events[3][3] == "Bash"
