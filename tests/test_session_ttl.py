"""TTL eviction tests — uses frozen time via manual timestamp injection."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kotoyose.session as session_module
from kotoyose.session import evict_expired_sessions


@pytest.fixture(autouse=True)
def reset_session_state():
    """Reset module-level dicts before each test."""
    session_module.sessions.clear()
    session_module.session_last_used.clear()
    yield
    session_module.sessions.clear()
    session_module.session_last_used.clear()


def _make_client() -> MagicMock:
    client = MagicMock()
    client.disconnect = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_evict_expired_sessions_disconnects_idle_client():
    client = _make_client()
    session_module.sessions["k1"] = client
    session_module.session_last_used["k1"] = time.time() - 3600  # 1 hour ago

    await evict_expired_sessions(ttl_seconds=1800)

    client.disconnect.assert_awaited_once()
    assert "k1" not in session_module.sessions
    assert "k1" not in session_module.session_last_used


@pytest.mark.asyncio
async def test_evict_expired_sessions_keeps_recent_client():
    client = _make_client()
    session_module.sessions["k2"] = client
    session_module.session_last_used["k2"] = time.time() - 60  # 1 minute ago

    await evict_expired_sessions(ttl_seconds=1800)

    client.disconnect.assert_not_called()
    assert "k2" in session_module.sessions


@pytest.mark.asyncio
async def test_evict_handles_disconnect_error_gracefully():
    client = _make_client()
    client.disconnect = AsyncMock(side_effect=RuntimeError("gone"))
    session_module.sessions["k3"] = client
    session_module.session_last_used["k3"] = time.time() - 9999

    # Should not raise
    await evict_expired_sessions(ttl_seconds=1800)

    assert "k3" not in session_module.sessions


@pytest.mark.asyncio
async def test_evict_ignores_key_without_last_used():
    client = _make_client()
    session_module.sessions["k4"] = client
    # session_last_used["k4"] intentionally missing

    await evict_expired_sessions(ttl_seconds=1800)

    # No crash, client not disconnected (no last_used = treat as recent)
    client.disconnect.assert_not_called()
