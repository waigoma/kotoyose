"""Chat completion use case の単体テスト。"""

from unittest.mock import AsyncMock

import pytest

from kotoyose.chat_completion import (
    CompletionPorts,
    TurnContext,
    finalize_chat_turn,
)
from kotoyose.models import ChatMessage


def _context(conversation_key: str | None, lookup_key: str | None) -> TurnContext:
    return TurnContext(
        client=AsyncMock(),
        latest_text="question",
        messages=[ChatMessage(role="user", content="question")],
        model="kotoyose-chat",
        provider="claude",
        interaction_id=9,
        session_event="created",
        conversation_key=conversation_key,
        lookup_key=lookup_key,
        session_identity=("claude", "kotoyose-chat", "digest"),
    )


def _ports() -> CompletionPorts:
    return CompletionPorts(
        record_completed=AsyncMock(),
        backfill_conversation_key=AsyncMock(),
        rotate_hash_key=AsyncMock(return_value="storage-key"),
        upsert_session=AsyncMock(),
        extract_tokens=lambda usage, prompt, answer: (10, 4, "sdk"),
    )


@pytest.mark.asyncio
async def test_finalize_fixed_conversation_upserts_runtime_session():
    """固定 conversation key は provider session ID を永続化する。"""
    ports = _ports()

    result = await finalize_chat_turn(
        _context("conversation-1", "conversation-1"),
        ports,
        answer="answer",
        usage={"input_tokens": 10, "output_tokens": 4},
        runtime_session_id="session-1",
    )

    assert result.tokens_in == 10
    assert result.tokens_out == 4
    assert result.session_registered is True
    ports.record_completed.assert_awaited_once()
    ports.upsert_session.assert_awaited_once()
    ports.rotate_hash_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalize_first_hash_turn_rotates_and_backfills_key():
    """履歴のない初回 hash path は key 回転後に interaction を backfill する。"""
    ports = _ports()

    result = await finalize_chat_turn(
        _context(None, None),
        ports,
        answer="answer",
        usage=None,
        runtime_session_id="session-1",
    )

    assert result.session_registered is True
    ports.rotate_hash_key.assert_awaited_once()
    ports.backfill_conversation_key.assert_awaited_once_with(9, "storage-key")
    ports.upsert_session.assert_not_awaited()

