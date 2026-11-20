"""v2 ハッシュパスで Claude API 呼び出しが N 回発生しないことを保証する。

session_store 導入前は replay_messages が過去 user メッセージを N 回 query して
応答を破棄していた。session_store 導入後は SDK が store.load() で履歴を復元し、
最新 user 発言だけを Claude API に送るため、呼び出し回数は 1 になる。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kotoyose.session as session_module
from kotoyose.config import HarnessConfig

from kotoyose.session import get_or_resume_client


@pytest.fixture(autouse=True)
def reset_session_state(monkeypatch):
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(models={"cc/claude-sonnet-5": {}}))
    monkeypatch.setattr("kotoyose.config._get_oauth_token", lambda: None)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    session_module.sessions.clear()
    session_module.session_last_used.clear()
    yield
    session_module.sessions.clear()
    session_module.session_last_used.clear()


@pytest.mark.asyncio
async def test_no_replay_query_calls_during_session_resume():
    """get_or_resume_client は client.query を一切呼ばない (replay 不在の証明)。"""
    fake_client = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.query = AsyncMock()  # ← これが await されたら failure

    with (
        patch("kotoyose.session.lookup_session", new=AsyncMock(return_value="sess_existing")),
        patch("kotoyose.session.ClaudeSDKClient", return_value=fake_client),
    ):
        client, was_resumed = await get_or_resume_client("example-client:default", "cc/claude-sonnet-5")

    assert was_resumed is True
    fake_client.query.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_uses_dataclasses_replace_not_query_replay():
    """resume は ClaudeAgentOptions.resume= 経由で行われ、query() で履歴を流し込まない。"""
    captured_options = []
    fake_client = MagicMock()
    fake_client.connect = AsyncMock()
    fake_client.query = AsyncMock()

    def ctor(options=None):
        captured_options.append(options)
        return fake_client

    with (
        patch("kotoyose.session.lookup_session", new=AsyncMock(return_value="sess_zzz")),
        patch("kotoyose.session.ClaudeSDKClient", side_effect=ctor),
    ):
        client, was_resumed = await get_or_resume_client("v2:abc123", "cc/claude-sonnet-5")

    assert was_resumed is True
    assert getattr(captured_options[0], "resume", None) == "sess_zzz"
    fake_client.query.assert_not_awaited()
