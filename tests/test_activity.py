"""Activity event application service の単体テスト。"""

from unittest.mock import AsyncMock

import pytest

from kotoyose import activity


def test_public_activity_data_omits_internal_payload():
    """外部公開値には内部 payload を含めない。"""
    row = {
        "seq": 3,
        "ts": "2026-09-04T12:00:00+00:00",
        "kind": "tool_use",
        "tool_name": "Bash",
        "summary": "pytest",
        "payload": {"secret": "hidden"},
    }

    assert activity.public_activity_data(row) == {
        "seq": 3,
        "ts": "2026-09-04T12:00:00+00:00",
        "kind": "tool_use",
        "tool_name": "Bash",
        "summary": "pytest",
    }


@pytest.mark.asyncio
async def test_stream_activity_stops_after_completed_interaction(monkeypatch):
    """完了済み interaction は蓄積 event を流した後に有限回で終了する。"""
    list_events = AsyncMock(
        side_effect=[
            [{"seq": 0, "ts": "now", "kind": "turn", "tool_name": None, "summary": "start"}],
            [],
            [],
        ]
    )
    monkeypatch.setattr(activity, "list_interaction_events", list_events)
    monkeypatch.setattr(
        activity,
        "get_interaction",
        AsyncMock(return_value={"id": 7, "status": "completed"}),
    )
    monkeypatch.setattr(activity.asyncio, "sleep", AsyncMock())

    chunks = [chunk async for chunk in activity.stream_activity(7, -1)]

    assert len(chunks) == 1
    assert '"kind": "turn"' in chunks[0]
    assert list_events.await_count == 3

