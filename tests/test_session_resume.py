"""SDK resume の正しい注入を検証するテスト。

2026-05-04 の silent failure バグ再発防止 + session_store 導入後の挙動検証:
- ClaudeAgentOptions は dataclass のため `model_copy` は使えない (dataclasses.replace 必須)
- get_or_resume_client は v1 ヘッダパス・v2 ハッシュパスの統合エントリ
- replay_messages は廃止され、SDK が session_store から履歴を復元する
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import ClaudeAgentOptions

import kotoyose.session as session_module
from kotoyose.config import HarnessConfig, ModelPreset
from kotoyose.session import SDKConnectionError, _cache_identity, get_or_resume_client


@pytest.fixture(autouse=True)
def reset_session_state(monkeypatch):
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(models={"cc/claude-sonnet-5": {}, "taskhub-pm": {}, "invalid-preset": {}}))
    # Synthetic clients must not inherit the developer's provider credentials.
    monkeypatch.setattr("kotoyose.config._get_oauth_token", lambda: None)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    session_module.sessions.clear()
    session_module.session_last_used.clear()
    session_module.session_cache_identities.clear()
    yield
    session_module.sessions.clear()
    session_module.session_last_used.clear()
    session_module.session_cache_identities.clear()


@pytest.mark.asyncio
async def test_oauth_bare_failure_precedes_subprocess_and_session_registration():
    options = ClaudeAgentOptions(
        env={"CLAUDE_CODE_OAUTH_TOKEN": "private-oauth-fixture"},
        extra_args={"bare": None},
    )
    with (
        patch("kotoyose.session.lookup_session", new=AsyncMock(return_value="existing-session")),
        patch("kotoyose.session.build_options", return_value=options),
        patch("kotoyose.session.ClaudeSDKClient") as constructor,
    ):
        with pytest.raises(SDKConnectionError, match="subscription OAuth") as exc:
            await get_or_resume_client("oauth:bare", "cc/claude-sonnet-5")
    assert "private-oauth-fixture" not in str(exc.value)
    constructor.assert_not_called()
    assert "oauth:bare" not in session_module.sessions
    assert "oauth:bare" not in session_module.session_cache_identities


@pytest.mark.asyncio
async def test_resume_option_injected_when_session_exists():
    """DB に既存 session_id があれば resume が SDK に注入される。"""
    captured_options = []

    def fake_sdk_client_ctor(options=None):
        captured_options.append(options)
        c = MagicMock()
        c.connect = AsyncMock()
        return c

    with (
        patch("kotoyose.session.lookup_session", new=AsyncMock(return_value="sess_xyz")),
        patch("kotoyose.session.ClaudeSDKClient", side_effect=fake_sdk_client_ctor),
    ):
        client, was_resumed = await get_or_resume_client("example-client:default", "cc/claude-sonnet-5")

    assert len(captured_options) == 1
    assert getattr(captured_options[0], "resume", None) == "sess_xyz"
    assert was_resumed is True


@pytest.mark.asyncio
async def test_no_resume_when_no_existing_session():
    """DB に session_id が無ければ resume は None。"""
    captured_options = []

    def fake_sdk_client_ctor(options=None):
        captured_options.append(options)
        c = MagicMock()
        c.connect = AsyncMock()
        return c

    with (
        patch("kotoyose.session.lookup_session", new=AsyncMock(return_value=None)),
        patch("kotoyose.session.ClaudeSDKClient", side_effect=fake_sdk_client_ctor),
    ):
        client, was_resumed = await get_or_resume_client("brand-new:key", "cc/claude-sonnet-5")

    assert len(captured_options) == 1
    assert getattr(captured_options[0], "resume", None) is None
    assert was_resumed is False


@pytest.mark.asyncio
async def test_no_lookup_when_lookup_key_is_none():
    """lookup_key=None (初回ハッシュパス) は DB 照会せず即新規。"""
    lookup_mock = AsyncMock()
    with (
        patch("kotoyose.session.lookup_session", new=lookup_mock),
        patch(
            "kotoyose.session.ClaudeSDKClient",
            side_effect=lambda options=None: MagicMock(connect=AsyncMock()),
        ),
    ):
        await get_or_resume_client(None, "cc/claude-sonnet-5")
    lookup_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_in_memory_cache_hit_skips_lookup():
    """インメモリキャッシュにあれば lookup_session は呼ばれない。"""
    cached_client = MagicMock()
    # provider を明示しないと getattr の既定値 "claude" ではなく MagicMock の
    # auto-attribute が返り、provider mismatch と誤判定されてしまう。
    cached_client.provider = "claude"
    session_module.sessions["example-client:default"] = cached_client
    preset = session_module.get_config().preset_for("cc/claude-sonnet-5")
    session_module.session_cache_identities["example-client:default"] = _cache_identity(
        "claude", "cc/claude-sonnet-5", preset.model, preset.required_request_headers, None
    )

    lookup_mock = AsyncMock(return_value="ignored")
    with patch("kotoyose.session.lookup_session", new=lookup_mock):
        client, was_resumed = await get_or_resume_client("example-client:default", "cc/claude-sonnet-5")

    assert client is cached_client
    assert was_resumed is False  # cache hit は resumed ではない
    lookup_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_rebuilds_when_required_request_header_changes():
    """同一 conversation key でも PM token が異なれば MCP client を再生成する。"""
    old_client = MagicMock(provider="claude", disconnect=AsyncMock())
    session_module.sessions["pm:conversation"] = old_client
    old_headers = {"x-pm-mcp-token": "old-token"}
    session_module.session_cache_identities["pm:conversation"] = _cache_identity(
        "claude", "taskhub-pm", None, ["X-Pm-Mcp-Token"], old_headers
    )
    preset = ModelPreset(model="claude-sonnet", required_request_headers=["X-Pm-Mcp-Token"])
    new_sdk_client = MagicMock(connect=AsyncMock())
    lookup_mock = AsyncMock(return_value="old-session-id")

    with (
        patch("kotoyose.session.get_config", return_value=MagicMock(preset_for=lambda _: preset)),
        patch("kotoyose.session.lookup_session", new=lookup_mock),
        patch("kotoyose.session.build_options", return_value=MagicMock()),
        patch("kotoyose.session.ClaudeSDKClient", return_value=new_sdk_client),
    ):
        client, _ = await get_or_resume_client(
            "pm:conversation", "taskhub-pm", request_headers={"x-pm-mcp-token": "new-token"}
        )

    old_client.disconnect.assert_awaited_once()
    assert client.client is new_sdk_client
    lookup_mock.assert_not_awaited()
    assert session_module.session_cache_identities["pm:conversation"] != _cache_identity(
        "claude", "taskhub-pm", None, ["X-Pm-Mcp-Token"], old_headers
    )


@pytest.mark.asyncio
async def test_required_header_preset_resumes_when_persisted_identity_matches():
    """同じ PM token digest を保存済みなら再起動後も安全に Claude session を resume する。"""
    preset = ModelPreset(model="claude-sonnet", required_request_headers=["X-Pm-Mcp-Token"])
    headers = {"x-pm-mcp-token": "stable-token"}
    expected_identity = _cache_identity(
        "claude", "taskhub-pm", "claude-sonnet", ["X-Pm-Mcp-Token"], headers
    )[2]
    lookup_mock = AsyncMock(return_value="pm-session-id")
    captured_options: list = []

    def fake_sdk_client_ctor(options=None):
        captured_options.append(options)
        return MagicMock(connect=AsyncMock())

    with (
        patch("kotoyose.session.get_config", return_value=MagicMock(preset_for=lambda _: preset)),
        patch("kotoyose.session.lookup_session", new=lookup_mock),
        patch("kotoyose.session.ClaudeSDKClient", side_effect=fake_sdk_client_ctor),
    ):
        _, was_resumed = await get_or_resume_client(
            "pm:conversation", "taskhub-pm", request_headers=headers
        )

    lookup_mock.assert_awaited_once_with(
        "pm:conversation",
        provider="claude",
        model="taskhub-pm",
        session_identity=expected_identity,
    )
    assert captured_options[0].resume == "pm-session-id"
    assert was_resumed is True


def test_cache_identity_changes_when_preset_runtime_model_changes():
    """同じ preset 名でも実 model の変更後は永続 session を再利用しない。"""
    sonnet_identity = _cache_identity("claude", "taskhub-pm", "claude-sonnet", [], None)
    opus_identity = _cache_identity("claude", "taskhub-pm", "claude-opus", [], None)

    assert sonnet_identity[:2] == opus_identity[:2]
    assert sonnet_identity[2] != opus_identity[2]


@pytest.mark.asyncio
async def test_does_not_upsert_session_at_connect():
    """connect 時点では DB upsert は行われない (chat.py が真の session_id で行う)。"""
    assert not hasattr(session_module, "upsert_session")
    with (
        patch("kotoyose.session.lookup_session", new=AsyncMock(return_value=None)),
        patch(
            "kotoyose.session.ClaudeSDKClient",
            side_effect=lambda options=None: MagicMock(connect=AsyncMock()),
        ),
    ):
        await get_or_resume_client("example-client:default", "cc/claude-sonnet-5")


@pytest.mark.asyncio
async def test_empty_string_session_id_treated_as_no_resume():
    """lookup_session が空文字列を返すバグった DB 状態でも、was_resumed=False になる。"""
    captured_options = []

    def fake_sdk_client_ctor(options=None):
        captured_options.append(options)
        c = MagicMock()
        c.connect = AsyncMock()
        return c

    with (
        patch("kotoyose.session.lookup_session", new=AsyncMock(return_value="")),
        patch("kotoyose.session.ClaudeSDKClient", side_effect=fake_sdk_client_ctor),
    ):
        client, was_resumed = await get_or_resume_client("buggy:key", "cc/claude-sonnet-5")

    assert getattr(captured_options[0], "resume", None) is None
    assert was_resumed is False


@pytest.mark.asyncio
async def test_build_options_exception_propagates():
    """build_options が例外を投げた場合、run_in_executor を通して伝播する。"""
    with (
        patch("kotoyose.session.lookup_session", new=AsyncMock(return_value=None)),
        patch("kotoyose.session.build_options", side_effect=ValueError("Invalid preset")),
    ):
        with pytest.raises(ValueError, match="Invalid preset"):
            await get_or_resume_client("test:key", "invalid-preset")
