"""永続 session の identity digest 照合を検証するテスト。"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from kotoyose import db


@pytest.mark.asyncio
async def test_upsert_session_persists_identity_digest(monkeypatch):
    """token の生値ではなく digest を sessions metadata へ保存する。"""
    pool = MagicMock()
    pool.execute = AsyncMock()
    monkeypatch.setattr(db, "_pool", pool)

    await db.upsert_session(
        "pm:conversation", "sdk-session", "taskhub-pm",
        session_identity="digest-value",
    )

    sql, *args = pool.execute.await_args.args
    assert "session_identity" in sql
    assert args[-1] == "digest-value"


@pytest.mark.asyncio
async def test_upsert_session_keeps_metadata_when_identity_is_missing(monkeypatch):
    """identity 無しの旧呼び出しでも metadata 全体を SQL NULL にしない。"""
    pool = MagicMock()
    pool.execute = AsyncMock()
    monkeypatch.setattr(db, "_pool", pool)

    await db.upsert_session("legacy:key", "sdk-session", "legacy")

    sql, *args = pool.execute.await_args.args
    assert "WHEN $6::text IS NULL THEN COALESCE(kotoyose.sessions.metadata, '{}'::jsonb)" in sql
    assert args[-1] is None


@pytest.mark.asyncio
async def test_lookup_session_requires_matching_identity_digest(monkeypatch):
    """resume lookup は provider・preset・identity digest の全一致だけを許可する。"""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"claude_session_id": "sdk-session"})
    monkeypatch.setattr(db, "_pool", pool)

    session_id = await db.lookup_session(
        "pm:conversation",
        provider="claude",
        model="taskhub-pm",
        session_identity="digest-value",
    )

    sql, *args = pool.fetchrow.await_args.args
    assert "provider = $2" in sql
    assert "model = $3" in sql
    assert "session_identity" in sql
    assert args == ["pm:conversation", "claude", "taskhub-pm", "digest-value"]
    assert session_id == "sdk-session"


@pytest.mark.asyncio
async def test_lookup_session_can_find_any_provider_for_admin(monkeypatch):
    """admin は provider 未指定で active な subprocess session も照合できる。"""
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"claude_session_id": "thread-123"})
    monkeypatch.setattr(db, "_pool", pool)

    session_id = await db.lookup_session("codex:key", provider=None)

    sql, *args = pool.fetchrow.await_args.args
    assert "($2::text IS NULL OR provider = $2)" in sql
    assert args == ["codex:key", None, None, None]
    assert session_id == "thread-123"
