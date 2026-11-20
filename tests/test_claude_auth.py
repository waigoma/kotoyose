"""Claude 認証 adapter の単体テスト。"""

import asyncio
from pathlib import Path
import re
from unittest.mock import MagicMock

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from kotoyose.config import (
    AgentRoute,
    ClaudeSubscriptionAuthError,
    Connection,
    HarnessConfig,
    ModelPreset,
    resolve_claude_subscription_token_sync,
)
from kotoyose.runtime.claude_auth import build_auth_env, validate_auth_options
from kotoyose.secret_store import SecretStore, set_secret_store


def test_claude_subscription_token_resolves_from_secret_store(monkeypatch):
    """Claude subscription token comes from Kotoyose secret store via the route's connection."""
    for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    store = SecretStore(master_key=b"\x01" * 32)
    asyncio.run(store.set("claude-oauth-prod", "sk\x2dant-oat-fake-secret"))
    set_secret_store(store)
    try:
        cfg = HarnessConfig(
            connections={"claude-oauth": Connection(provider="claude", kind="subscription", data_policy="no_training", secret="claude-oauth-prod")},
            routes={"r": AgentRoute(driver="claude.agent-sdk", model="claude-sonnet-5", connection="claude-oauth")},
            models={"chat": ModelPreset(route="r", bare=False)},
        )
        preset = cfg.preset_for("chat")
        env = build_auth_env(
            preset,
            cfg,
            lambda: resolve_claude_subscription_token_sync(cfg, preset),
        )
        assert env == {"CLAUDE_CODE_OAUTH_TOKEN": "sk\x2dant-oat-fake-secret"}
    finally:
        set_secret_store(None)


def test_claude_subscription_missing_secret_fails_with_setup_token_hint(monkeypatch):
    """Missing secret fails explicitly with `claude setup-token` and the secret name; no file fallback."""
    for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    store = SecretStore(master_key=b"\x01" * 32)
    set_secret_store(store)
    try:
        cfg = HarnessConfig(
            connections={"claude-oauth": Connection(provider="claude", kind="subscription", data_policy="no_training", secret="claude-oauth-missing")},
            routes={"r": AgentRoute(driver="claude.agent-sdk", model="claude-sonnet-5", connection="claude-oauth")},
            models={"chat": ModelPreset(route="r", bare=False)},
        )
        preset = cfg.preset_for("chat")
        with pytest.raises(ClaudeSubscriptionAuthError) as exc:
            build_auth_env(
                preset,
                cfg,
                lambda: resolve_claude_subscription_token_sync(cfg, preset),
            )
        assert "claude setup-token" in str(exc.value)
        assert "claude-oauth-missing" in str(exc.value)
    finally:
        set_secret_store(None)


def test_no_claude_credentials_file_or_refresh_endpoint_in_src():
    """Verify no source file under src/ references `.credentials.json` or Claude Code OAuth refresh endpoint/client id."""
    src_root = Path(__file__).resolve().parent.parent / "src"
    forbidden_patterns = (
        re.compile(re.escape(".credentials.json")),
        re.compile(re.escape("https://platform.claude.com/v1/oauth/token")),
        re.compile(re.escape("9d1c250a-e61b-44d9-88ed-5944d1962f5e")),
        re.compile(r"\bget_oauth_token\b"),
    )
    hits: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pat in forbidden_patterns:
            if pat.search(text):
                hits.append(f"{path}: {pat.pattern}")
    assert hits == []


@pytest.mark.parametrize("bare", [True, False])
def test_oauth_uses_its_own_authentication_channel(monkeypatch, bare):
    for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    preset = ModelPreset(bare=bare)
    assert build_auth_env(preset, HarnessConfig(), lambda: "oauth-fixture") == {
        "CLAUDE_CODE_OAUTH_TOKEN": "oauth-fixture",
    }
    # bare の auth 制限を API key への偽装や bare=false への変更で迂回しない。
    assert preset.bare is bare


def test_explicit_api_key_overrides_inherited_oauth(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-fixture")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "host-oauth-fixture")
    fallback = MagicMock()
    assert build_auth_env(ModelPreset(), HarnessConfig(), fallback) == {
        "ANTHROPIC_API_KEY": "api-fixture", "CLAUDE_CODE_OAUTH_TOKEN": "",
    }
    fallback.assert_not_called()


def test_existing_oauth_environment_precedes_file_fallback(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "host-oauth-fixture")
    fallback = MagicMock()
    assert build_auth_env(ModelPreset(bare=False), HarnessConfig(), fallback) == {
        "CLAUDE_CODE_OAUTH_TOKEN": "host-oauth-fixture",
    }
    fallback.assert_not_called()


@pytest.mark.parametrize("bare, env, rejected", [
    (True, {"CLAUDE_CODE_OAUTH_TOKEN": "private-oauth-fixture"}, True),
    (False, {"CLAUDE_CODE_OAUTH_TOKEN": "private-oauth-fixture"}, False),
    (True, {"ANTHROPIC_API_KEY": "api-fixture"}, False),
    (True, {"ANTHROPIC_API_KEY": "api-fixture", "CLAUDE_CODE_OAUTH_TOKEN": ""}, False),
    (True, {}, False),
])
def test_bare_auth_preflight_is_explicit_and_does_not_mutate_options(bare, env, rejected):
    options = ClaudeAgentOptions(env=env.copy(), extra_args={"bare": None} if bare else {})
    if rejected:
        with pytest.raises(ValueError, match="bare=true does not support subscription OAuth") as exc:
            validate_auth_options(options)
        assert "private-oauth-fixture" not in str(exc.value)
        assert "bare=false" in str(exc.value)
    else:
        validate_auth_options(options)
    assert options.env == env
    assert ("bare" in options.extra_args) is bare
