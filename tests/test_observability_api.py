"""/admin/interactions, /admin/sessions, /admin/stats の API テスト。"""

import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest
from fastapi.testclient import TestClient

from kotoyose.app import app

from tests.fixtures.api_keys import TEST_API_HEADERS

client = TestClient(app)


@pytest.fixture(autouse=True)
def _no_auth(monkeypatch):
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)


def test_get_interactions_returns_envelope():
    fake_data = {"items": [{"id": 1, "model": "x", "source": "native-agent"}], "total": 1, "limit": 50, "offset": 0}
    with patch("kotoyose.routes.observability.list_interactions", new=AsyncMock(return_value=fake_data)):
        res = client.get("/admin/interactions")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == 1
    assert body["items"][0]["source"] == "native-agent"


def test_get_interactions_passes_query_params():
    captured = {}

    async def fake_list(**kw):
        captured.update(kw)
        return {"items": [], "total": 0, "limit": kw["limit"], "offset": kw["offset"]}

    with patch("kotoyose.routes.observability.list_interactions", new=fake_list):
        res = client.get(
            "/admin/interactions?limit=10&offset=20&model=grok-test&source=native-agent&status=completed"
        )
    assert res.status_code == 200
    assert captured["limit"] == 10
    assert captured["offset"] == 20
    assert captured["model"] == "grok-test"
    assert captured["source"] == "native-agent"
    assert captured["status"] == "completed"


def test_get_interaction_detail():
    with patch(
        "kotoyose.routes.observability.get_interaction",
        new=AsyncMock(return_value={"id": 7, "prompt": "hi"}),
    ):
        res = client.get("/admin/interactions/7")
    assert res.status_code == 200
    assert res.json()["id"] == 7


def test_get_interaction_detail_404():
    with patch("kotoyose.routes.observability.get_interaction", new=AsyncMock(return_value=None)):
        res = client.get("/admin/interactions/9999")
    assert res.status_code == 404


def test_get_interaction_events_passes_after_seq():
    """admin JSON API は after_seq より後の公開 event fields を返す。"""
    row = {
        "seq": 4,
        "ts": "2026-07-23T10:00:00+00:00",
        "kind": "tool_use",
        "tool_name": "Bash",
        "summary": "pytest",
        "payload": {"secret": "not exposed"},
    }
    with (
        patch(
            "kotoyose.routes.observability.get_interaction",
            new=AsyncMock(return_value={"id": 7, "status": "completed"}),
        ),
        patch(
            "kotoyose.routes.observability.list_interaction_events",
            new=AsyncMock(return_value=[row]),
        ) as list_events,
    ):
        res = client.get("/admin/interactions/7/events?after_seq=3")

    assert res.status_code == 200
    assert res.json()["items"] == [{
        "seq": 4,
        "ts": "2026-07-23T10:00:00+00:00",
        "kind": "tool_use",
        "tool_name": "Bash",
        "summary": "pytest",
    }]
    list_events.assert_awaited_once_with(7, after_seq=3)


def test_admin_interaction_events_stream_is_sse():
    """admin live tail は activity event の SSE として返す。"""
    rows = [{
        "seq": 0,
        "ts": "2026-07-23T10:00:00+00:00",
        "kind": "turn",
        "tool_name": None,
        "summary": "Turn 1",
    }]
    with (
        patch(
            "kotoyose.routes.observability.get_interaction",
            new=AsyncMock(side_effect=[{"id": 7}, {"id": 7, "status": "completed"}, {"id": 7, "status": "completed"}]),
        ),
        patch(
            "kotoyose.activity.list_interaction_events",
            new=AsyncMock(side_effect=[rows, [], []]),
        ),
    ):
        res = client.get("/admin/interactions/7/events/stream")

    assert res.status_code == 200
    assert "text/event-stream" in res.headers["content-type"]
    assert "event: activity" in res.text
    assert '"kind": "turn"' in res.text


@pytest.mark.usefixtures("client_api_key")
def test_conversation_events_stream_uses_latest_interaction_and_after_seq():
    """bearer 面は conversation key の対象 interaction を after_seq 付きで流す。"""
    rows = [{
        "seq": 6,
        "ts": "2026-07-23T10:00:00+00:00",
        "kind": "tool_result",
        "tool_name": "Bash",
        "summary": "ok: passed",
    }]
    with (
        patch(
            "kotoyose.routes.chat.get_interaction_for_conversation",
            new=AsyncMock(return_value={"id": 12, "status": "completed"}),
        ) as target,
        patch(
            "kotoyose.activity.list_interaction_events",
            new=AsyncMock(side_effect=[rows, [], []]),
        ) as list_events,
        patch(
            "kotoyose.activity.get_interaction",
            new=AsyncMock(return_value={"id": 12, "status": "completed"}),
        ),
    ):
        res = client.get("/v1/conversations/taskhub:task:1:run:2/events/stream?after_seq=5", headers=TEST_API_HEADERS)

    assert res.status_code == 200
    assert "event: activity" in res.text
    assert '"seq": 6' in res.text
    target.assert_awaited_once_with("taskhub:task:1:run:2", api_key_name="test-client")
    assert list_events.await_args_list[0].kwargs == {"after_seq": 5}


def test_get_sessions_envelope():
    with patch(
        "kotoyose.routes.observability.list_sessions",
        new=AsyncMock(return_value={"items": [], "total": 0, "limit": 50, "offset": 0}),
    ):
        res = client.get("/admin/sessions")
    assert res.status_code == 200
    assert res.json()["total"] == 0


def test_get_sessions_forwards_search_query():
    mock_list = AsyncMock(return_value={"items": [], "total": 0, "limit": 100, "offset": 0})
    with patch("kotoyose.routes.observability.list_sessions", new=mock_list):
        res = client.get("/admin/sessions?limit=100&offset=0&q=lut")
    assert res.status_code == 200
    mock_list.assert_awaited_once_with(limit=100, offset=0, q="lut")


def test_get_session_detail():
    with patch(
        "kotoyose.routes.observability.get_session_turns",
        new=AsyncMock(return_value=[{"id": 1, "prompt": "a"}, {"id": 2, "prompt": "b"}]),
    ):
        res = client.get("/admin/sessions/channel:42")
    assert res.status_code == 200
    body = res.json()
    assert body["conversation_key"] == "channel:42"
    assert body["claude_session_id"] is None
    assert len(body["turns"]) == 2


def test_get_session_detail_preserves_legacy_response_field():
    """旧 query parameter を使う client は response からも同じ field を読める。"""
    with patch("kotoyose.routes.observability.get_session_turns", new=AsyncMock(return_value=[])):
        res = client.get("/admin/sessions/channel:42?claude_session_id=sess_legacy")

    assert res.status_code == 200
    assert res.json()["claude_session_id"] == "sess_legacy"


def test_get_session_detail_accepts_runtime_session_and_provider():
    """Sessions UI は非 Claude の provider 固有 runtime ID で詳細を取得できる。"""
    get_turns = AsyncMock(return_value=[])
    with patch("kotoyose.routes.observability.get_session_turns", new=get_turns):
        res = client.get("/admin/sessions/task:42?runtime_session_id=thread_codex&provider=codex")

    assert res.status_code == 200
    get_turns.assert_awaited_once_with(
        "task:42", runtime_session_id="thread_codex", provider="codex"
    )


def test_get_stats():
    fake_stats = {
        "past_24h": {"total_requests": 5, "successful": 5, "failed": 0,
                     "total_tokens_in": 100, "total_tokens_out": 30,
                     "avg_duration_ms": 1000, "by_model": {}, "by_source": {}},
        "past_7d": {"total_requests": 10, "successful": 10, "failed": 0,
                    "total_tokens_in": 200, "total_tokens_out": 60,
                    "avg_duration_ms": 1100, "by_model": {}, "by_source": {}},
    }
    with patch(
        "kotoyose.routes.observability.compute_stats",
        new=AsyncMock(return_value=fake_stats),
    ):
        res = client.get("/admin/stats")
    assert res.status_code == 200
    body = res.json()
    assert body["past_24h"]["total_requests"] == 5
    assert body["past_7d"]["total_requests"] == 10


@pytest.mark.parametrize(('custom', 'native', 'expected'), [
    ('Custom', 'Native', 'Custom'), (None, 'Native', 'Native'), (None, None, 'First prompt'),
])
def test_session_detail_title_priority(custom, native, expected):
    turns = [{'prompt': '\nFirst  prompt\nnext', 'metadata': {'session_title': 'Old'}},
             {'metadata': '{broken'}, {'metadata': json.dumps({'session_title': native})}]
    if native is None:
        turns[0]['metadata'] = {}
    with (
        patch('kotoyose.routes.observability.get_session_turns', new=AsyncMock(return_value=turns)),
        patch('kotoyose.routes.observability.get_session_title', new=AsyncMock(return_value=custom)),
    ):
        response = client.get('/admin/sessions/conversation')
    assert response.status_code == 200
    assert response.json()['title'] == expected
    assert response.json()['custom_title'] == custom


@pytest.mark.parametrize('title', [' New\n title ', None, '', 'a' * 160])
def test_put_session_title(title):
    expected = ' '.join((title or '').split()) or None
    with patch('kotoyose.routes.observability.set_session_title', new=AsyncMock(return_value=expected)) as save:
        response = client.put('/admin/sessions/title', json={'conversation_key': 'conversation', 'title': title})
    assert response.status_code == 200
    assert response.json() == {'conversation_key': 'conversation', 'title': expected}
    save.assert_awaited_once_with('conversation', expected)


@pytest.mark.parametrize('body', [
    {'conversation_key': '', 'title': 'Title'}, {'conversation_key': '  ', 'title': None},
    {'conversation_key': 'conversation', 'title': 'a' * 161},
])
def test_put_session_title_validation(body):
    assert client.put('/admin/sessions/title', json=body).status_code == 422
