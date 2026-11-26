"""Real parser, route and session boundaries with synthetic SDK/DB endpoints."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from kotoyose import config as config_module, session
from kotoyose.app import app
from kotoyose.chat_request import RequestPreparationPorts, prepare_chat_request
from kotoyose.chat_selection import pin_pm_saved_preset
from kotoyose.config import HarnessConfig
from kotoyose.execution_billing import owner_claude_subscription_billing, require_runtime_selection
from kotoyose.execution_resources import ExecutionError
from kotoyose.models import ChatCompletionRequest
from kotoyose.routes import chat

from tests.fixtures.api_keys import TEST_API_HEADERS

pytestmark = pytest.mark.usefixtures("client_api_key")


REQUIREMENTS = {"provider": "claude", "billing_mode": "subscription", "metered_fallback": "forbidden"}
HEADERS = {"X-PM-MCP-Token": "synthetic-project-token"}


def wire(**updates):
    return {
        "model": "saved-pm", "messages": [{"role": "user", "content": "synthetic PM message"}],
        "metadata": {"kotoyose": {"schema_version": "1", "pm_saved_preset": dict(REQUIREMENTS)}},
        **updates,
    }


def request(**updates):
    return ChatCompletionRequest.model_validate(wire(**updates))


@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "API_KEY", None)
    monkeypatch.setenv("SYNTHETIC_PM_TOKEN", "synthetic-route-token")
    monkeypatch.setenv("ALTERNATE_PM_TOKEN", "synthetic-alternate-token")
    prompt = tmp_path / "taskhub-pm.md"
    prompt.write_text("Synthetic TaskHub PM instructions.")
    memory = tmp_path / "memory-gateway-discipline.md"
    memory.write_text("Synthetic Memory Gateway discipline.")
    config = HarnessConfig.model_validate({
        "connections": {"pm-conn": {
            "provider": "claude", "kind": "endpoint", "data_policy": "no_training",
            "endpoint": "https://router.invalid/v1", "secret": "env:SYNTHETIC_PM_TOKEN",
        }},
        "routes": {"existing-route": {
            "driver": "claude.agent-sdk", "model": "synthetic/pm-model",
            "connection": "pm-conn", "subscription_only": True,
        }},
        "models": {"saved-pm": {
            "route": "existing-route", "cwd": str(tmp_path),
            "tools": ["Read", "Grep", "Agent"], "deny": ["Bash"],
            "mcp_refs": ["memory-taskhub"], "required_request_headers": ["X-PM-MCP-Token"],
            "prompt_file_refs": ["taskhub-pm", "memory-gateway-discipline"],
            "setting_sources": ["project"], "strict_mcp_config": True,
            "system_prompt_snapshot": False, "skills": ["synthetic-skill"],
            "thinking": "adaptive", "effort": "high", "timeout_seconds": 30,
        }},
        "mcps": {"memory-taskhub": {"server": {
            "type": "http", "url": "https://memory.invalid/mcp",
            "headers": {"Authorization": "Bearer {{header:X-PM-MCP-Token}}"},
        }}},
        "prompt_files": {"taskhub-pm": {"path": str(prompt)}, "memory-gateway-discipline": {"path": str(memory)}},
    })
    monkeypatch.setattr(config_module, "_config", config)
    monkeypatch.setattr("kotoyose.session_store.get_default_store", lambda: None)
    session.sessions.clear()
    session.session_cache_identities.clear()
    session.session_last_used.clear()
    yield config
    session.sessions.clear()
    session.session_cache_identities.clear()
    session.session_last_used.clear()


@pytest.fixture
def sdk(monkeypatch):
    clients = []

    def build(*, options):
        client = AsyncMock()
        client.options = options
        clients.append(client)
        return client

    monkeypatch.setattr(session, "ClaudeSDKClient", build)
    monkeypatch.setattr(session, "lookup_session", AsyncMock(return_value=None))
    monkeypatch.setattr(session, "build_options", Mock(side_effect=AssertionError("strict chat resolved twice")))
    return clients


@pytest.fixture
def route_boundary(owner, sdk, monkeypatch):
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    monkeypatch.setattr(chat, "record_received", AsyncMock(return_value=1))
    monkeypatch.setattr(chat, "record_completed", AsyncMock())
    monkeypatch.setattr(chat, "record_failed", AsyncMock())
    monkeypatch.setattr(chat, "_safe_upsert_session", AsyncMock())
    monkeypatch.setattr(chat, "_rotate_hash_key", AsyncMock(return_value="synthetic-rotated"))
    monkeypatch.setattr(chat, "ask_claude", AsyncMock(return_value=("synthetic answer", None, "synthetic-session")))

    async def stream(client, prompt):
        yield "synthetic answer"
        yield (None, None, "synthetic-session")

    monkeypatch.setattr(chat, "stream_claude", stream)
    monkeypatch.setattr(chat, "start_event_writer", lambda *args, **kwargs: asyncio.Queue())
    return sdk


async def post(body=None, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://synthetic.test", headers=TEST_API_HEADERS) as client:
        return await client.post("/v1/chat/completions", json=body or wire(),
                                 headers={**HEADERS, "X-Conversation-Key": "synthetic-conversation"}, **kwargs)


@pytest.mark.parametrize("requirements", [None, {}, {"provider": "claude"},
    {**REQUIREMENTS, "provider": "codex"}, {**REQUIREMENTS, "billing_mode": "metered"},
    {**REQUIREMENTS, "metered_fallback": "allowed"}, {**REQUIREMENTS, "subscription_only": True},
    {**REQUIREMENTS, "base_url": "https://caller.invalid"}, {**REQUIREMENTS, "credential_name": "ANYTHING"},
])
def test_real_parser_rejects_unknown_or_relaxed_requirement(requirements):
    body = wire()
    body["metadata"]["kotoyose"]["pm_saved_preset"] = requirements
    with pytest.raises(ValidationError):
        ChatCompletionRequest.model_validate(body)


def test_omitted_flag_keeps_existing_metadata_wire():
    parsed = request(metadata={"caller_note": "retained", "kotoyose": {"schema_version": "1"}})
    assert parsed.metadata.kotoyose.pm_saved_preset is None
    assert "pm_saved_preset" not in parsed.metadata.model_dump()["kotoyose"]
    assert parsed.metadata.model_dump()["caller_note"] == "retained"


def test_pin_retains_full_pm_recipe_and_actual_auth_convention(owner, monkeypatch):
    for name in ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"]:
        monkeypatch.setenv(name, "synthetic-unapproved-token")
    for name in ["CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"]:
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://unapproved.invalid")
    pinned = pin_pm_saved_preset(owner, request(), HEADERS)
    options = pinned.options
    # skills があるので Skill の道具が足される (無いと skill がモデルに提示されない)。
    assert options.tools == ["Read", "Grep", "Agent", "Skill"] and options.disallowed_tools == ["Bash", "Task", "Agent"]
    assert options.mcp_servers["memory-taskhub"]["headers"]["Authorization"] == "Bearer synthetic-project-token"
    assert options.system_prompt["append"] == "Synthetic TaskHub PM instructions.\nSynthetic Memory Gateway discipline."
    assert options.system_prompt["snapshot"] is False
    assert options.skills == ["synthetic-skill"] and options.agents is None
    assert options.setting_sources == ["project"] and options.strict_mcp_config is True
    assert options.model == "synthetic/pm-model" and options.fallback_model is None
    assert options.env == {
        "ANTHROPIC_API_KEY": "synthetic-route-token", "ANTHROPIC_BASE_URL": "https://router.invalid/v1",
        "ANTHROPIC_AUTH_TOKEN": "", "CLAUDE_CODE_OAUTH_TOKEN": "", "ANTHROPIC_CUSTOM_HEADERS": "",
        "CLAUDE_CODE_USE_BEDROCK": "0", "CLAUDE_CODE_USE_VERTEX": "0", "CLAUDE_CODE_USE_FOUNDRY": "0",
    }
    assert pinned.billing["mode"] == "subscription" and pinned.billing["metered_fallback"] == "forbidden"
    assert "synthetic-route-token" not in repr(pinned) and "synthetic-project-token" not in repr(pinned)
    assert "synthetic-route-token" not in json.dumps(pinned.billing)


@pytest.mark.parametrize("change", ["missing", "policy", "endpoint_override", "source_override", "provider"])
def test_unapproved_or_missing_saved_alias_never_defaults(owner, change):
    if change == "missing":
        owner.models.clear()
    elif change == "policy":
        owner.routes["existing-route"].subscription_only = False
    elif change == "endpoint_override":
        owner.models["saved-pm"].anthropic_base_url = "https://other.invalid/v1"
    elif change == "source_override":
        owner.models["saved-pm"].anthropic_auth_token_env = "ALTERNATE_PM_TOKEN"
    else:
        owner.routes["existing-route"].driver = "codex.app-server"
    with pytest.raises(ExecutionError):
        pin_pm_saved_preset(owner, request(), HEADERS)


@pytest.mark.parametrize("token", [None, ""])
def test_missing_or_empty_source_does_not_fallback(owner, monkeypatch, token):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-other-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-oauth")
    if token is None:
        monkeypatch.delenv("SYNTHETIC_PM_TOKEN")
    else:
        monkeypatch.setenv("SYNTHETIC_PM_TOKEN", token)
    with pytest.raises(ExecutionError) as caught:
        pin_pm_saved_preset(owner, request(), HEADERS)
    assert caught.value.reason_code == "pm_credential_unavailable"


@pytest.mark.parametrize("change", ["endpoint", "source", "model", "mcp", "prompt", "token", "headers", "tools"])
def test_actual_route_resources_and_authority_bind_session_identity(owner, monkeypatch, change):
    first = pin_pm_saved_preset(owner, request(), HEADERS)
    headers = dict(HEADERS)
    definition = owner.routes["existing-route"]
    conn = owner.connections[definition.connection]
    if change == "endpoint":
        conn.endpoint = "https://updated.invalid/v1"
    elif change == "source":
        conn.secret = "env:ALTERNATE_PM_TOKEN"
    elif change == "model":
        definition.model = "synthetic/new-model"
    elif change == "mcp":
        owner.mcps["memory-taskhub"].server["url"] = "https://updated-memory.invalid/mcp"
    elif change == "prompt":
        from pathlib import Path
        Path(owner.prompt_files["taskhub-pm"].path).write_text("Updated synthetic PM instructions.")
    elif change == "token":
        monkeypatch.setenv("SYNTHETIC_PM_TOKEN", "synthetic-rotated-key")
    elif change == "headers":
        headers["X-PM-MCP-Token"] = "synthetic-new-project"
    else:
        owner.models["saved-pm"].tools = ["Read"]
    second = pin_pm_saved_preset(owner, request(), headers)
    assert first.identity != second.identity
    assert first.options.model == "synthetic/pm-model"


def test_owner_policy_is_shared_but_adapter_bindings_cannot_widen():
    route = {"backend": "direct", "provider": "claude", "model": "synthetic-model",
             "base_url": "https://router.invalid/v1", "credential_mode": "environment",
             "credential_name": "ANTHROPIC_API_KEY", "credential_env": "SYNTHETIC_PM_TOKEN",
             "subscription_only": True}
    assert owner_claude_subscription_billing(route, adapter="direct_claude_sdk")
    assert owner_claude_subscription_billing(route, adapter="native_claude") is None
    with pytest.raises(ExecutionError):
        require_runtime_selection(route, provider="claude", billing_mode="subscription")
    with pytest.raises(ValueError):
        owner_claude_subscription_billing(route, adapter="caller-chosen")
    route.update(backend="native", credential_name="ANTHROPIC_AUTH_TOKEN")
    assert require_runtime_selection(route, provider="claude", billing_mode="subscription")


async def test_session_consumes_same_snapshot_without_second_resolution(owner, sdk, monkeypatch):
    original = HarnessConfig.preset_for
    resolver = Mock(side_effect=lambda self, alias: original(self, alias))
    monkeypatch.setattr(HarnessConfig, "preset_for", lambda self, alias: resolver(self, alias))
    pinned = pin_pm_saved_preset(owner, request(), HEADERS)
    owner.models.clear()
    owner.routes.clear()
    monkeypatch.setattr(session, "get_config", Mock(side_effect=AssertionError("second config lookup")))
    identity_resolver = Mock(side_effect=AssertionError("second identity resolution"))
    prepared = prepare_chat_request(request(), HEADERS, pinned.preset,
                                   RequestPreparationPorts(identity_resolver, lambda *args: False),
                                   conversation_key="synthetic-key", session_identity=pinned.identity)
    client, resumed = await session.get_or_resume_client("synthetic-key", "saved-pm", selection=pinned)
    assert len(sdk) == 1 and sdk[0].options is pinned.options
    assert prepared.session_identity == session.session_cache_identities["synthetic-key"] == pinned.identity
    assert resolver.call_count == 1 and not resumed
    session.lookup_session.assert_awaited_once_with("synthetic-key", provider="claude", model="saved-pm",
                                                   session_identity=pinned.identity[2])
    again, _ = await session.get_or_resume_client("synthetic-key", "saved-pm", selection=pinned)
    assert again is client and len(sdk) == 1


async def test_changed_alias_disconnects_old_client_and_does_not_resume(owner, sdk):
    first = pin_pm_saved_preset(owner, request(), HEADERS)
    await session.get_or_resume_client("synthetic-key", "saved-pm", selection=first)
    session.lookup_session.reset_mock()
    owner.connections[owner.routes["existing-route"].connection].endpoint = "https://new-route.invalid/v1"
    second = pin_pm_saved_preset(owner, request(), HEADERS)
    await session.get_or_resume_client("synthetic-key", "saved-pm", selection=second)
    sdk[0].disconnect.assert_awaited_once()
    session.lookup_session.assert_not_awaited()
    assert len(sdk) == 2 and sdk[1].options.env["ANTHROPIC_BASE_URL"] == "https://new-route.invalid/v1"


async def test_strict_request_cannot_reuse_preexisting_ordinary_client(owner, sdk):
    old = AsyncMock()
    old.provider = "claude"
    old.invalidated = False
    session.sessions["synthetic-key"] = old
    preset = owner.preset_for("saved-pm")
    session.session_cache_identities["synthetic-key"] = session._cache_identity(
        "claude", "saved-pm", preset.model, preset.required_request_headers, HEADERS)
    pinned = pin_pm_saved_preset(owner, request(), HEADERS)
    await session.get_or_resume_client("synthetic-key", "saved-pm", selection=pinned)
    old.disconnect.assert_awaited_once()
    session.lookup_session.assert_not_awaited()
    assert len(sdk) == 1


@pytest.mark.parametrize("stream", [False, True])
async def test_real_route_keeps_stream_and_session_event_protocol(route_boundary, stream):
    first = await post(wire(stream=stream))
    assert first.status_code == 200, first.text
    assert first.headers["X-Session-Event"] == "created"
    assert "synthetic answer" in first.text
    if stream:
        assert "[DONE]" in first.text
    second = await post(wire(stream=stream))
    assert second.status_code == 200 and second.headers["X-Session-Event"] == "cache_hit"
    assert len(route_boundary) == 1


async def test_real_route_changed_snapshot_reports_created(route_boundary, owner):
    assert (await post()).status_code == 200
    owner.routes["existing-route"].model = "synthetic/new-model"
    response = await post()
    assert response.status_code == 200 and response.headers["X-Session-Event"] == "created"
    assert len(route_boundary) == 2


async def test_real_route_rejects_unknown_alias_before_interaction_or_client(route_boundary):
    response = await post(wire(model="missing-pm"))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"
    chat.record_received.assert_not_awaited()
    assert route_boundary == []


async def test_real_route_policy_removal_rejects_even_with_cached_client(route_boundary, owner):
    assert (await post()).status_code == 200
    owner.routes["existing-route"].subscription_only = False
    response = await post()
    assert response.status_code == 501
    assert response.json()["error"]["reason_code"] == "billing_policy_unavailable"
    assert len(route_boundary) == 1


async def test_real_route_parser_rejects_relaxed_metadata_before_client(route_boundary):
    body = wire()
    body["metadata"]["kotoyose"]["pm_saved_preset"]["metered_fallback"] = "allowed"
    response = await post(body)
    assert response.status_code == 422
    assert route_boundary == []


async def test_omitted_flag_rejects_unknown_alias_before_runtime(route_boundary, monkeypatch):
    monkeypatch.setattr(chat, "get_or_resume_client", AsyncMock(return_value=(AsyncMock(), False)))
    response = await post(wire(model="ordinary-unknown", metadata=None))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"
    chat.get_or_resume_client.assert_not_awaited()


async def test_native_only_alias_fails_as_transport_before_native_dispatch(route_boundary, owner, monkeypatch):
    from kotoyose.compatibility_config import CompatibilityConfig
    native = CompatibilityConfig.model_validate({"schema_version": "1", "callers": {"synthetic": {"native-only": {
        "workspace_path": "/synthetic", "caller_workspace_ref": "synthetic-workspace", "policy_ref": "synthetic-policy",
        "instructions": "synthetic", "expires_after_seconds": 300,
    }}}})
    monkeypatch.setattr(owner, "_compatibility_config", native)
    native_dispatch = AsyncMock(side_effect=AssertionError("ordinary PM must not create Native Attempt"))
    monkeypatch.setattr(chat, "native_chat_response", native_dispatch)
    response = await post(wire(model="native-only"))
    assert response.status_code == 501, response.text
    assert response.json()["error"]["reason_code"] == "pm_chat_transport_unavailable"
    native_dispatch.assert_not_awaited()
    assert route_boundary == []


async def test_provider_failure_does_not_select_default_or_retry(route_boundary, monkeypatch):
    monkeypatch.setattr(chat, "ask_claude", AsyncMock(side_effect=RuntimeError("synthetic provider failure")))
    response = await post()
    assert response.status_code == 500
    assert len(route_boundary) == 1
    chat.ask_claude.assert_awaited_once()
    assert route_boundary[0].options.env["ANTHROPIC_BASE_URL"] == "https://router.invalid/v1"
    assert route_boundary[0].options.fallback_model is None


async def test_connection_failure_does_not_select_default_or_retry(route_boundary, monkeypatch):
    created = []

    def failed_sdk(*, options):
        client = AsyncMock()
        client.connect.side_effect = RuntimeError("synthetic connection failure")
        created.append(options)
        return client

    monkeypatch.setattr(session, "ClaudeSDKClient", failed_sdk)
    response = await post()
    assert response.status_code == 503
    assert len(created) == 1 and created[0].fallback_model is None
    chat.ask_claude.assert_not_awaited()


async def test_matching_persisted_identity_uses_pinned_options_for_resume(owner, sdk, monkeypatch):
    pinned = pin_pm_saved_preset(owner, request(), HEADERS)
    monkeypatch.setattr(session, "lookup_session", AsyncMock(return_value="synthetic-resume"))
    _, resumed = await session.get_or_resume_client("synthetic-key", "saved-pm", selection=pinned)
    assert resumed is True
    assert sdk[0].options.resume == "synthetic-resume"
    assert sdk[0].options.env == pinned.options.env
    assert pinned.options.resume is None
    session.lookup_session.assert_awaited_once_with("synthetic-key", provider="claude", model="saved-pm",
                                                   session_identity=pinned.identity[2])
