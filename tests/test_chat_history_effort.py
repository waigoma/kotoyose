"""Fresh SDK sessions carry earlier chat turns, and reasoning_effort overrides alias effort."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from kotoyose.app import app
from kotoyose.config import HarnessConfig, ModelPreset
from kotoyose.model_discovery import (
    _CONNECTION_DISCOVERY_CACHE,
    _CachedDiscovery,
    _connection_cache_fingerprint,
    clear_connection_discovery_cache,
)
from kotoyose.routes import chat
from kotoyose.runtime.base import RuntimeImageInput, RuntimeTextInput
from kotoyose.runtime.claude_options import build_claude_options
from kotoyose.runtime.codex import build_codex_session
from kotoyose.runtime.cursor import build_cursor_session
from kotoyose.runtime.translation import translate_runtime_config
from kotoyose.session import _cache_identity

from tests.fixtures.api_keys import TEST_API_HEADERS

pytestmark = pytest.mark.usefixtures("client_api_key")

IMAGE = "data:image/png;base64,QUJD"
LIMIT = 200_000


def _messages(*contents: str | list) -> list[dict]:
    roles = ("system", "user", "assistant")
    body = [{"role": roles[index % 3] if index < len(contents) - 1 else "user", "content": content}
            for index, content in enumerate(contents)]
    body[-1]["role"] = "user"
    return body


def _client():
    return TestClient(app, headers=TEST_API_HEADERS)


def _capture_prompt(monkeypatch, *, resumed: bool = False, cache_hit: bool = False):
    captured: dict = {}

    async def ask(_client, prompt):
        captured["prompt"] = prompt
        return "ok", None, None

    monkeypatch.setattr(chat, "get_or_resume_client", AsyncMock(return_value=(AsyncMock(), resumed)))
    monkeypatch.setattr(chat, "ask_claude", ask)
    monkeypatch.setattr(chat, "is_session_cache_hit", lambda *_args: cache_hit)
    recorded: dict = {}

    async def record_received(**kwargs):
        recorded.update(kwargs)
        return None

    monkeypatch.setattr(chat, "record_received", record_received)
    return captured, recorded


def test_fresh_sdk_session_prefixes_three_prior_messages(configured_chat_alias, monkeypatch):
    """A new runtime session receives the earlier turns, then the latest user message."""
    captured, recorded = _capture_prompt(monkeypatch)
    messages = _messages("from the system", "first question", "first answer", "latest question")
    response = _client().post("/v1/chat/completions", json={
        "model": "kotoyose-chat", "messages": messages,
    }, headers={"X-Conversation-Key": "history-fresh"})
    assert response.status_code == 200, response.text
    assert captured["prompt"] == (
        "Earlier conversation:\n"
        "system: from the system\n"
        "user: first question\n"
        "assistant: first answer\n"
        "---\n"
        "latest question"
    )
    assert recorded["prompt"] == "latest question"


def test_cache_hit_sends_only_the_latest_message(configured_chat_alias, monkeypatch):
    captured, recorded = _capture_prompt(monkeypatch, cache_hit=True)
    response = _client().post("/v1/chat/completions", json={
        "model": "kotoyose-chat",
        "messages": _messages("old", "reply", "latest question"),
    }, headers={"X-Conversation-Key": "history-hit"})
    assert response.status_code == 200, response.text
    assert response.headers["x-session-event"] == "cache_hit"
    assert captured["prompt"] == "latest question"
    assert recorded["prompt"] == "latest question"


def test_resumed_session_sends_only_the_latest_message(configured_chat_alias, monkeypatch):
    captured, _recorded = _capture_prompt(monkeypatch, resumed=True)
    response = _client().post("/v1/chat/completions", json={
        "model": "kotoyose-chat",
        "messages": _messages("old", "reply", "latest question"),
    }, headers={"X-Conversation-Key": "history-resume"})
    assert response.status_code == 200, response.text
    assert response.headers["x-session-event"] == "resumed"
    assert captured["prompt"] == "latest question"


def test_fresh_session_keeps_images_on_the_latest_message(configured_chat_alias, monkeypatch):
    captured, recorded = _capture_prompt(monkeypatch)
    response = _client().post("/v1/chat/completions", json={
        "model": "kotoyose-chat",
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "see"},
                {"type": "image_url", "image_url": {"url": IMAGE}},
            ]},
            {"role": "assistant", "content": "noted"},
            {"role": "user", "content": [
                {"type": "text", "text": "again"},
                {"type": "image_url", "image_url": {"url": IMAGE}},
            ]},
        ],
    }, headers={"X-Conversation-Key": "history-image"})
    assert response.status_code == 200, response.text
    prompt = captured["prompt"]
    assert isinstance(prompt, list)
    assert isinstance(prompt[0], RuntimeTextInput)
    assert prompt[0].text == (
        "Earlier conversation:\n"
        "user: see\n[image]\n"
        "assistant: noted\n"
        "---"
    )
    assert prompt[1:] == [RuntimeTextInput("again"), RuntimeImageInput(IMAGE)]
    assert recorded["prompt"] == "again\n[image]"


def test_transcript_drops_the_oldest_messages_past_the_cap(configured_chat_alias, monkeypatch):
    captured, _recorded = _capture_prompt(monkeypatch)
    old = "O" * 120_000
    mid = "M" * 120_000
    new = "N" * 40
    response = _client().post("/v1/chat/completions", json={
        "model": "kotoyose-chat",
        "messages": _messages(old, mid, new, "latest"),
    }, headers={"X-Conversation-Key": "history-cap"})
    assert response.status_code == 200, response.text
    prompt = captured["prompt"]
    prefix, latest = prompt.rsplit("\n", 1)
    assert latest == "latest"
    assert len(prefix) <= LIMIT
    assert prefix.startswith("Earlier conversation:\n[earlier messages omitted]\n")
    assert old not in prompt
    assert f"user: {mid}" in prompt
    assert f"assistant: {new}" in prompt
    assert prefix.endswith("---")


def test_cli_acp_history_is_unchanged(tmp_path, monkeypatch):
    login_home = tmp_path / "login"
    login_home.mkdir()
    cfg = HarnessConfig.model_validate({
        "connections": {"local": {
            "provider": "cursor", "kind": "subscription", "profile": "cursor",
            "config_dir": str(login_home), "data_policy": "no_training",
        }},
        "routes": {"acp": {
            "driver": "cli.acp", "profile": "cursor", "connection": "local", "model": "cursor-test",
        }},
        "models": {"acp-chat": {"route": "acp", "place": "host", "allow": {}}},
    })
    monkeypatch.setattr("kotoyose.config._config", cfg)
    monkeypatch.setattr(
        chat, "resolve_preset_secrets_with_versions_async", AsyncMock(return_value=({}, {})),
    )
    captured, _recorded = _capture_prompt(monkeypatch)
    response = _client().post("/v1/chat/completions", json={
        "model": "acp-chat",
        "messages": _messages("from the system", "first question", "first answer", "latest question"),
    }, headers={"X-Conversation-Key": "acp-history"})
    assert response.status_code == 200, response.text
    prompt = captured["prompt"]
    assert type(prompt).__name__ == "AcpPrompt"
    assert prompt.history == (
        "system: from the system\n\n"
        "user: first question\n\n"
        "assistant: first answer\n\n"
        "user: latest question"
    )
    assert str(prompt) == "latest question"
    assert "Earlier conversation:" not in prompt.history


def _routed_config():
    return HarnessConfig.model_validate({
        "connections": {"gateway": {
            "provider": "claude", "kind": "endpoint", "data_policy": "no_training",
            "endpoint": "https://demo.invalid", "secret": "env:DEMO_GATEWAY_KEY",
        }},
        "routes": {"gateway": {"driver": "claude.agent-sdk", "model": "claude-opus-test", "connection": "gateway"}},
        "models": {
            "demo": {"route": "gateway", "effort": "low"},
            "plain": {"effort": "medium"},
            "agy": {"provider": "antigravity", "effort": "high"},
        },
    })


def _seed_claude_efforts(cfg):
    conn = cfg.connections["gateway"]
    _CONNECTION_DISCOVERY_CACHE["gateway"] = _CachedDiscovery(
        expires_at=time.monotonic() + 600,
        fingerprint=_connection_cache_fingerprint("claude", conn),
        result={"model_details": [{
            "id": "claude-opus-test",
            "supportedEffortLevels": ["low", "high"],
        }]},
    )


def test_reasoning_effort_shapes_from_discovery_details():
    from kotoyose.model_discovery import reasoning_efforts_from_detail

    assert reasoning_efforts_from_detail({
        "id": "gpt",
        "default_reasoning_effort": "high",
        "supported_reasoning_efforts": [
            {"reasoning_effort": "low", "description": "Brief"},
            {"reasoning_effort": "high", "description": "Thorough"},
        ],
    }) == {"supported": ["low", "high"], "default": "high"}
    assert reasoning_efforts_from_detail({
        "id": "claude",
        "supportedEffortLevels": ["low", "medium", "high"],
    }) == {"supported": ["low", "medium", "high"], "default": None}
    assert reasoning_efforts_from_detail({"id": "other"}) is None


def test_reasoning_effort_rejected_for_runtime_that_does_not_apply_it(monkeypatch):
    monkeypatch.setattr("kotoyose.config._config", _routed_config())
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    resume = AsyncMock(return_value=(AsyncMock(), False))
    monkeypatch.setattr(chat, "get_or_resume_client", resume)
    response = _client().post("/v1/chat/completions", json={
        "model": "agy", "messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "high",
    })
    assert response.status_code == 422
    assert "not supported" in response.json()["error"]["message"]
    resume.assert_not_awaited()


def test_reasoning_effort_rejected_when_outside_cached_catalog(monkeypatch):
    cfg = _routed_config()
    monkeypatch.setattr("kotoyose.config._config", cfg)
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    _seed_claude_efforts(cfg)
    resume = AsyncMock(return_value=(AsyncMock(), False))
    monkeypatch.setattr(chat, "get_or_resume_client", resume)
    try:
        response = _client().post("/v1/chat/completions", json={
            "model": "demo", "messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "max",
        })
    finally:
        clear_connection_discovery_cache()
    assert response.status_code == 422
    message = response.json()["error"]["message"]
    assert "max" in message and "low" in message and "high" in message
    resume.assert_not_awaited()


def test_reasoning_effort_override_is_passed_to_the_session(monkeypatch):
    monkeypatch.setattr("kotoyose.config._config", _routed_config())
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    resume = AsyncMock(return_value=(AsyncMock(), False))
    monkeypatch.setattr(chat, "get_or_resume_client", resume)
    monkeypatch.setattr(chat, "ask_claude", AsyncMock(return_value=("ok", None, None)))
    response = _client().post("/v1/chat/completions", json={
        "model": "plain", "messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "high",
    })
    assert response.status_code == 200, response.text
    assert resume.await_args.kwargs["reasoning_effort"] == "high"


def test_effort_override_reaches_runtime_config(monkeypatch):
    monkeypatch.setattr("kotoyose.runtime.claude_options.bundled_claude_cli", lambda: "claude")
    cfg = HarnessConfig(models={"alias": {"provider": "codex", "effort": "low"}})
    claude = build_claude_options(
        cfg, ModelPreset(effort="low"), model_name="alias", cwd_override=None,
        request_headers=None, env={}, session_store=None, effort_override="high",
    )
    assert claude.effort == "high"
    translated = translate_runtime_config(cfg, ModelPreset(provider="codex", effort="low"), effort_override="xhigh")
    assert translated.effort == "xhigh"
    codex = build_codex_session(ModelPreset(provider="codex", effort="low", model="gpt-test"), None, None, None, effort_override="high")
    assert codex.effort == "high"
    cursor = build_cursor_session(
        ModelPreset(provider="cursor", effort="low", model="cursor-test"), None, None, None, effort_override="high",
    )
    assert cursor.model_params["effort"] == "high"


def test_cache_identity_differs_by_reasoning_effort():
    args = ("claude", "alias", "claude-opus-test", [], None)
    assert _cache_identity(*args) == _cache_identity(*args, reasoning_effort=None)
    low = _cache_identity(*args, reasoning_effort="low")
    high = _cache_identity(*args, reasoning_effort="high")
    assert low != high
    assert low != _cache_identity(*args)


@pytest.mark.asyncio
async def test_different_reasoning_effort_does_not_reuse_the_runtime_session(monkeypatch):
    import kotoyose.session as session_module

    preset = ModelPreset(effort="low", model="claude-sonnet")
    monkeypatch.setattr(session_module, "sessions", {})
    monkeypatch.setattr(session_module, "session_last_used", {})
    monkeypatch.setattr(session_module, "session_cache_identities", {})
    monkeypatch.setattr(session_module, "lookup_session", AsyncMock(return_value=None))
    built = []

    def build_options(*_args, **kwargs):
        built.append(kwargs.get("effort_override"))
        return MagicMock()

    def sdk_client(options=None):
        return MagicMock(connect=AsyncMock(), options=options)

    monkeypatch.setattr(session_module, "get_config", lambda: MagicMock(
        preset_for=lambda _model: preset, models={"alias": preset},
    ))
    monkeypatch.setattr(session_module, "build_options", build_options)
    monkeypatch.setattr(session_module, "ClaudeSDKClient", sdk_client)
    first, first_resumed = await session_module.get_or_resume_client("conv", "alias", reasoning_effort="low")
    low_identity = session_module.session_cache_identities["conv"]
    second, second_resumed = await session_module.get_or_resume_client("conv", "alias", reasoning_effort="high")
    assert first is not second
    assert first_resumed is False and second_resumed is False
    assert built == ["low", "high"]
    assert low_identity != session_module.session_cache_identities["conv"]
    assert session_module.session_cache_identities["conv"] == _cache_identity(
        "claude", "alias", "claude-sonnet", preset.required_request_headers, None, reasoning_effort="high",
    )
    # 別 effort は別の権限境界なので、保存済み session を引き直さない。
    session_module.lookup_session.assert_awaited_once()


def test_models_list_exposes_cached_reasoning_efforts(monkeypatch):
    cfg = _routed_config()
    monkeypatch.setattr("kotoyose.config._config", cfg)
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    monkeypatch.setattr(app.state, "execution_service", None, raising=False)
    _seed_claude_efforts(cfg)

    def discover_models(*_args, **_kwargs):
        raise AssertionError("model discovery must not run on the models list")

    monkeypatch.setattr("kotoyose.model_discovery.discover_models", discover_models)
    try:
        rows = {item["id"]: item for item in _client().get("/v1/models").json()["data"]}
    finally:
        clear_connection_discovery_cache()
    assert rows["demo"]["reasoning_efforts"] == {"supported": ["low", "high"], "default": None}
    assert rows["demo"]["data_policy"] == "no_training"
    assert "reasoning_efforts" not in rows["plain"]
    assert "reasoning_efforts" not in rows["agy"]


def test_efforts_outlive_the_model_list_cache(monkeypatch):
    from kotoyose import model_discovery

    cfg = _routed_config()
    conn = cfg.connections["gateway"]
    model_discovery._remember_efforts("claude", "gateway", conn, {"model_details": [
        {"id": "claude-opus-test", "supportedEffortLevels": ["low", "high"]},
        {"id": "no-efforts"},
    ]})
    try:
        # The 10-minute model list cache is empty or expired; the effort cache still answers.
        assert model_discovery.cached_reasoning_efforts(cfg, "demo") == {"supported": ["low", "high"], "default": None}
        expires, fingerprint, efforts = model_discovery._EFFORT_CACHE["gateway"]
        assert set(efforts) == {"claude-opus-test"}
        model_discovery._EFFORT_CACHE["gateway"] = (time.monotonic() - 1, fingerprint, efforts)
        assert model_discovery.cached_reasoning_efforts(cfg, "demo") is None
    finally:
        clear_connection_discovery_cache()


async def test_warm_reasoning_efforts_discovers_each_connection_once(monkeypatch):
    from kotoyose import model_discovery

    cfg = _routed_config()
    calls = []

    async def discover_models(provider, *, connection=None, execution_service=None):
        calls.append((provider, connection))
        model_discovery._remember_efforts(provider, connection, cfg.connections[connection], {"model_details": [
            {"id": "claude-opus-test", "supportedEffortLevels": ["high"]},
        ]})
        return {}

    monkeypatch.setattr(model_discovery, "discover_models", discover_models)
    try:
        assert await model_discovery.warm_reasoning_efforts(cfg) == 1
        assert calls == [("claude", "gateway")]
    finally:
        clear_connection_discovery_cache()
