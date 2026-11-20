"""runtime provider ごとの session routing を固定するテスト。"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kotoyose.config import HarnessConfig, ModelPreset
from kotoyose.runtime.antigravity import AntigravityRuntimeSession
from kotoyose.runtime.base import RuntimeResult, RuntimeSession
from kotoyose.runtime.claude import ClaudeRuntimeSession
from kotoyose.runtime.codex import CodexRuntimeSession
from kotoyose.runtime.cursor import CursorRuntimeSession
from kotoyose.session import (
    RUNTIME_PROVIDER_REGISTRY,
    RuntimeProviderSpec,
    _cache_identity,
    get_or_resume_client,
    session_cache_identities,
    session_last_used,
    sessions,
)


class _RegistryProbeSession:
    """registry builder 経由で作られたことを観測するための最小 RuntimeSession。"""

    provider = "codex"

    def __init__(self, session_id: str | None):
        self.session_id = session_id

    async def disconnect(self) -> None:
        return None

    async def ask(self, prompt: str) -> RuntimeResult:
        return RuntimeResult(text=prompt, runtime_session_id=self.session_id, provider=self.provider)

    async def stream(self, prompt: str):
        yield RuntimeResult(text=prompt, runtime_session_id=self.session_id, provider=self.provider)


@pytest.fixture(autouse=True)
def clear_runtime_session_cache(monkeypatch):
    """グローバル session cache が provider routing テストへ漏れないようにする。"""
    monkeypatch.setattr("kotoyose.config._get_oauth_token", lambda: None)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    sessions.clear()
    session_last_used.clear()
    session_cache_identities.clear()
    yield
    sessions.clear()
    session_last_used.clear()
    session_cache_identities.clear()


@pytest.mark.asyncio
async def test_get_or_resume_client_builds_cursor_runtime_session(tmp_path):
    """provider=cursor は runtime_session_id を CursorRuntimeSession に resume する。"""
    config = HarnessConfig(
        models={
            "cursor-coding": ModelPreset(
                provider="cursor",
                model="gpt-5.1",
                cwd=str(tmp_path),
                cursor={"output_format": "stream-json"},
            )
        }
    )

    lookup = AsyncMock(return_value="chat_abc")
    with (
        patch("kotoyose.session.get_config", return_value=config),
        patch("kotoyose.session.lookup_runtime_session", new=lookup),
    ):
        client, was_resumed = await get_or_resume_client("conv:cursor", "cursor-coding")

    assert isinstance(client, CursorRuntimeSession)
    assert client.provider == "cursor"
    assert client.session_id == "chat_abc"
    assert was_resumed is True
    lookup.assert_awaited_once_with(
        "conv:cursor",
        provider="cursor",
        model="cursor-coding",
        session_identity=_cache_identity("cursor", "cursor-coding", "gpt-5.1", [], None)[2],
    )
    assert sessions["conv:cursor"] is client


@pytest.mark.asyncio
async def test_get_or_resume_client_uses_provider_registry_builder(tmp_path):
    """subprocess provider は registry の builder から生成される。"""
    config = HarnessConfig(
        models={
            "codex-coding": ModelPreset(
                provider="codex",
                model="gpt-5-codex",
                cwd=str(tmp_path),
                required_request_headers=["X-Token"],
            )
        }
    )
    built: list[tuple[ModelPreset, str | None, str | None, dict[str, str] | None]] = []

    def probe_builder(preset, cwd_override, session_id, request_headers):
        built.append((preset, cwd_override, session_id, request_headers))
        return _RegistryProbeSession(session_id)

    original = RUNTIME_PROVIDER_REGISTRY["codex"]
    RUNTIME_PROVIDER_REGISTRY["codex"] = RuntimeProviderSpec(probe_builder, "codex")
    try:
        with (
            patch("kotoyose.session.get_config", return_value=config),
            patch("kotoyose.session.lookup_runtime_session", new=AsyncMock(return_value="thread_123")),
        ):
            client, was_resumed = await get_or_resume_client(
                "conv:registry",
                "codex-coding",
                cwd_override=str(tmp_path),
                request_headers={"X-Token": "secret"},
            )
    finally:
        RUNTIME_PROVIDER_REGISTRY["codex"] = original

    assert isinstance(client, RuntimeSession)
    assert isinstance(client, _RegistryProbeSession)
    assert client.session_id == "thread_123"
    assert was_resumed is True
    assert built == [
        (
            config.models["codex-coding"],
            str(tmp_path),
            "thread_123",
            {"X-Token": "secret"},
        )
    ]
    assert sessions["conv:registry"] is client


@pytest.mark.asyncio
async def test_get_or_resume_client_builds_antigravity_runtime_session(tmp_path):
    """provider=antigravity は runtime_session_id を AntigravityRuntimeSession に resume する。"""
    config = HarnessConfig(
        models={
            "antigravity-coding": ModelPreset(
                provider="antigravity",
                model="Gemini 3.5 Flash (High)",
                cwd=str(tmp_path),
                antigravity={"print_timeout": "30s"},
            )
        }
    )

    lookup = AsyncMock(return_value="conv_abc")
    with (
        patch("kotoyose.session.get_config", return_value=config),
        patch("kotoyose.session.lookup_runtime_session", new=lookup),
    ):
        client, was_resumed = await get_or_resume_client("conv:antigravity", "antigravity-coding")

    assert isinstance(client, AntigravityRuntimeSession)
    assert client.provider == "antigravity"
    assert client.session_id == "conv_abc"
    assert was_resumed is True
    lookup.assert_awaited_once_with(
        "conv:antigravity",
        provider="antigravity",
        model="antigravity-coding",
        session_identity=_cache_identity(
            "antigravity", "antigravity-coding", "Gemini 3.5 Flash (High)", [], None
        )[2],
    )
    assert sessions["conv:antigravity"] is client


# ── provider mismatch 時の後始末 (リーク修正 + 誤セッション返却修正) ──


@pytest.mark.asyncio
async def test_mismatch_disconnects_old_session_and_replaces_with_new_provider(tmp_path):
    """claude セッションがキャッシュ済みの状態で codex preset に切り替えると、
    旧 client を disconnect した上で新しい CodexRuntimeSession に置き換わる
    (disconnect せず上書きしていたリークの回帰テスト)。"""
    config = HarnessConfig(
        models={
            "codex-coding": ModelPreset(
                provider="codex",
                model="gpt-5-codex",
                cwd=str(tmp_path),
            )
        }
    )

    old_client = MagicMock()
    old_client.provider = "claude"
    old_client.disconnect = AsyncMock()
    sessions["conv:switch"] = old_client
    session_last_used["conv:switch"] = time.time()

    lookup = AsyncMock(return_value=None)
    with (
        patch("kotoyose.session.get_config", return_value=config),
        patch("kotoyose.session.lookup_runtime_session", new=lookup),
    ):
        client, was_resumed = await get_or_resume_client("conv:switch", "codex-coding")

    old_client.disconnect.assert_awaited_once()
    assert isinstance(client, CodexRuntimeSession)
    assert client.provider == "codex"
    assert was_resumed is False
    assert sessions["conv:switch"] is client
    assert sessions["conv:switch"] is not old_client
    assert "conv:switch" not in session_last_used or session_last_used["conv:switch"] is not None


@pytest.mark.asyncio
async def test_same_provider_preset_mismatch_does_not_resume_old_runtime_session(tmp_path):
    """同じ provider でも preset が異なれば古い runtime session を resume しない。"""
    config = HarnessConfig(models={
        "codex-a": ModelPreset(provider="codex", model="gpt-5.1", cwd=str(tmp_path)),
        "codex-b": ModelPreset(provider="codex", model="gpt-5.2", cwd=str(tmp_path)),
    })
    old_client = MagicMock(provider="codex", disconnect=AsyncMock())
    sessions["conv:codex"] = old_client
    session_last_used["conv:codex"] = time.time()
    session_cache_identities["conv:codex"] = _cache_identity(
        "codex", "codex-a", "gpt-5.1", [], None
    )
    lookup = AsyncMock(return_value="old-thread")

    with (
        patch("kotoyose.session.get_config", return_value=config),
        patch("kotoyose.session.lookup_runtime_session", new=lookup),
    ):
        client, was_resumed = await get_or_resume_client("conv:codex", "codex-b")

    old_client.disconnect.assert_awaited_once()
    lookup.assert_not_awaited()
    assert client.session_id is None
    assert was_resumed is False


@pytest.mark.asyncio
async def test_mismatch_does_not_return_stale_session_as_new_provider(tmp_path, monkeypatch):
    """codex セッションがキャッシュ済みの状態で claude preset に切り替えると、
    誤って古い codex セッションを返さず新規の ClaudeRuntimeSession を返す
    (session.py の重複 cache-hit チェックによる誤セッション返却の回帰テスト)。"""
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(models={"cc/claude-sonnet-5": {}}))
    stale_codex = CodexRuntimeSession(model="o1", cwd=str(tmp_path))
    sessions["conv:back-to-claude"] = stale_codex
    session_last_used["conv:back-to-claude"] = time.time()

    fake_sdk_client = MagicMock()
    fake_sdk_client.connect = AsyncMock()

    with (
        patch("kotoyose.session.lookup_session", new=AsyncMock(return_value=None)),
        patch("kotoyose.session.ClaudeSDKClient", return_value=fake_sdk_client),
    ):
        client, was_resumed = await get_or_resume_client("conv:back-to-claude", "cc/claude-sonnet-5")

    assert isinstance(client, ClaudeRuntimeSession)
    assert client is not stale_codex
    assert was_resumed is False
    assert sessions["conv:back-to-claude"] is client


@pytest.mark.asyncio
async def test_mismatch_disconnect_failure_does_not_block_new_session(tmp_path):
    """mismatch 時に旧セッションの disconnect が例外を投げても、
    新セッション生成は継続してリクエストが成功する (warning ログのみに留める)。"""
    config = HarnessConfig(
        models={
            "codex-coding": ModelPreset(
                provider="codex",
                model="gpt-5-codex",
                cwd=str(tmp_path),
            )
        }
    )

    old_client = MagicMock()
    old_client.provider = "claude"
    old_client.disconnect = AsyncMock(side_effect=RuntimeError("boom"))
    sessions["conv:disconnect-fail"] = old_client
    session_last_used["conv:disconnect-fail"] = time.time()

    lookup = AsyncMock(return_value=None)
    with (
        patch("kotoyose.session.get_config", return_value=config),
        patch("kotoyose.session.lookup_runtime_session", new=lookup),
    ):
        client, was_resumed = await get_or_resume_client("conv:disconnect-fail", "codex-coding")

    old_client.disconnect.assert_awaited_once()
    assert isinstance(client, CodexRuntimeSession)
    assert sessions["conv:disconnect-fail"] is client
