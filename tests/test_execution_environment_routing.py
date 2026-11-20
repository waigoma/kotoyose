"""Explicit execution environments: pure routing, with no provider/auth/OS calls."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from kotoyose.config import AgentRoute, AliasAgent, Connection, HarnessConfig, ModelPreset
from kotoyose.execution_resources import (
    DriverInstall, resolve_agent_model,
)


def preset(**changes):
    if changes.get("route"):
        return ModelPreset(cwd="/fixture", **changes)
    return ModelPreset(provider="codex", model="gpt-6-luna", cwd="/fixture", **changes)


def agent_settings():
    """機械の driver。alias の sandbox はここには置かない。"""
    return SimpleNamespace(
        drivers={"codex.app-server": DriverInstall(cli_binary=Path("/fixture/codex"))},
        agent_compression_mode="runtime_managed",
    )


def agent_config(**changes):
    values = {"route": "coding", "agent": AliasAgent(place="isolated", surface="write")}
    values.update(changes)
    body = preset(**values)
    return HarnessConfig(
        connections={"codex-chatgpt": Connection(
            provider="codex", kind="subscription", data_policy="no_training", auth_file=Path("/fixture/auth-not-read.json"),
        )},
        routes={"coding": AgentRoute(driver="codex.app-server", model="gpt-6-luna", connection="codex-chatgpt")},
        models={"coding": body},
    )


@pytest.mark.parametrize("choice", [None, "host", "isolated"])
def test_choice_round_trip_and_historical_omission(choice):
    saved = preset(execution_environment=choice)
    encoded = saved.model_dump(mode="json")
    assert ("execution_environment" in encoded) is (choice is not None)
    assert ModelPreset.model_validate(encoded).execution_environment == choice
    assert saved.execution == "direct"


@pytest.mark.parametrize("provider", ["codex", "cursor", "antigravity", "claude"])
@pytest.mark.parametrize("choice", ["host", "isolated"])
def test_new_provider_matrix(provider, choice):
    allowed = provider == "codex" or (choice == "host" and provider != "claude")
    if allowed:
        assert ModelPreset(provider=provider, cwd="/fixture", execution_environment=choice).execution_environment == choice
    else:
        with pytest.raises(ValidationError, match="not supported"):
            ModelPreset(provider=provider, cwd="/fixture", execution_environment=choice)


def test_route_provider_owns_matrix_and_preserves_inheritance():
    saved = ModelPreset(route="codex-route", cwd="/fixture", execution_environment="host")
    definition = AgentRoute(driver="codex.app-server", model="gpt-6-luna", connection="codex-host", model_provider={"id": "fixture"})
    config = HarnessConfig(
        connections={"codex-host": Connection(provider="codex", kind="subscription", data_policy="no_training")},
        models={"coding": saved},
        routes={"codex-route": definition},
    )
    effective = config.preset_for("coding")
    assert effective.provider == "codex" and effective.model == "gpt-6-luna"
    assert effective.codex == {"model_provider": {"id": "fixture"}, "sandbox": "workspace-write"}
    assert config.models["coding"].provider == "claude" and config.models["coding"].codex == {}
    bad = preset(route="claude-route", execution_environment="host")
    with pytest.raises(ValidationError, match="not supported for claude"):
        HarnessConfig(
            connections={"claude-oauth": Connection(provider="claude", kind="subscription", data_policy="no_training", secret="claude-oauth")},
            models={"coding": bad},
            routes={"claude-route": AgentRoute(driver="claude.agent-sdk", model="claude-fixture", connection="claude-oauth")},
        )


@pytest.mark.parametrize("saved", [{}, {"sandbox": None}, {"sandbox": "read-only"},
                                   {"sandbox": "workspace-write"}, {"sandbox": "danger-full-access"}])
def test_only_explicit_host_defaults_missing_sandbox_without_mutating_saved_config(saved):
    original = deepcopy(saved)
    for choice in (None, "isolated", "host"):
        config = HarnessConfig(models={"coding": preset(codex=deepcopy(saved), execution_environment=choice)})
        effective = config.preset_for("coding")
        expected = (saved.get("sandbox") or "workspace-write") if choice == "host" else saved.get("sandbox")
        assert effective.codex.get("sandbox") == expected
        assert config.models["coding"].codex == original
    assert saved == original


@pytest.mark.parametrize("choice", ["isolated", "host"])
@pytest.mark.parametrize("sandbox", ["externalSandbox", {}, 1])
def test_new_choice_rejects_unknown_sdk_sandbox(choice, sandbox):
    with pytest.raises(ValidationError, match="codex.sandbox requires"):
        preset(execution_environment=choice, codex={"sandbox": sandbox})


@pytest.mark.parametrize("choice", ["isolated", "host"])
def test_new_choice_cannot_bypass_saved_sandbox(choice):
    with pytest.raises(ValidationError, match="dangerously_bypass"):
        preset(execution_environment=choice, codex={"dangerously_bypass_approvals_and_sandbox": True})
    # The new choice is not an implicit migration of historical settings.
    assert preset(codex={"sandbox": "full-access", "dangerously_bypass_approvals_and_sandbox": True})


def test_only_inactive_isolated_preference_accepts_historical_full_access_alias():
    with pytest.raises(ValidationError, match="codex.sandbox requires"):
        preset(execution_environment="host", codex={"sandbox": "full-access"})
    config = agent_config(execution_environment="isolated", codex={"sandbox": "full-access"})
    resolved = resolve_agent_model("coding", agent_settings(), config=config)
    assert resolved.codex_sandbox == "danger-full-access"
    assert resolved.execution_environment == "isolated"
    assert config.models["coding"].codex["sandbox"] == "full-access"


def test_explicit_host_disallows_sdk_auto_review_escalation():
    with pytest.raises(ValidationError, match="approval_mode=deny_all"):
        preset(execution_environment="host", codex={"approval_mode": "auto_review"})
    assert preset(execution_environment="host", codex={"approval_mode": "deny_all"})
    assert preset(codex={"approval_mode": "auto_review"})
    assert preset(execution_environment="isolated", codex={"approval_mode": "auto_review"})


@pytest.mark.parametrize("sandbox", [None, "read-only", "workspace-write", "danger-full-access"])
def test_isolated_retains_only_saved_host_preference_and_leaves_it_untouched(sandbox):
    saved = {} if sandbox is None else {"sandbox": sandbox}
    config = agent_config(execution_environment="isolated", codex=saved)
    resolved = resolve_agent_model("coding", agent_settings(), config=config)
    assert resolved.execution_environment == "isolated"
    assert resolved.codex_sandbox == sandbox
    assert resolved.codex_preferences.effort == "low"
    assert config.models["coding"].codex == saved


def test_alias_without_agent_cannot_run_as_agent():
    config = HarnessConfig(models={"coding": preset(execution_environment="isolated")})
    with pytest.raises(ValueError, match="no agent binding"):
        resolve_agent_model("coding", agent_settings(), config=config)


def test_identity_revalidation_does_not_read_current_preferences_or_prompts(monkeypatch):
    import kotoyose.codex_preferences as preferences
    import kotoyose.preset_prompts as prompts
    config = agent_config(execution_environment="isolated")
    config.models["coding"].codex = {"sandbox": "future-setting", "timeout": "invalid"}
    config.models["coding"].effort = "invalid"
    config.models["coding"].prompt_file_refs = ["unavailable"]
    monkeypatch.setattr(preferences, "resolve_codex_preferences", Mock(side_effect=AssertionError("preferences read")))
    monkeypatch.setattr(prompts, "resolve_preset_prompts", Mock(side_effect=AssertionError("prompts read")))
    resolved = resolve_agent_model("coding", agent_settings(), config=config, resolve_preferences=False)
    assert resolved.model == "gpt-6-luna"
    assert resolved.execution_environment == "isolated"
    assert "codex_preferences" not in resolved.model_dump()
    assert "codex_sandbox" not in resolved.model_dump()


@pytest.mark.parametrize("sandbox", ["workspace-write", "danger-full-access"])
def test_agent_jail_rejects_non_read_only_sandbox(sandbox):
    config = agent_config(agent=AliasAgent(place="isolated", surface="exec"), codex={"sandbox": sandbox})
    config.connections["openai-env"] = Connection(provider="codex", kind="api_key", data_policy="no_training", secret="env:OPENAI_API_KEY")
    config.routes["coding"] = AgentRoute(driver="codex.app-server", model="gpt-6-luna", connection="openai-env")
    with pytest.raises(ValueError, match="requires read-only"):
        resolve_agent_model("coding", agent_settings(), config=config)


def test_host_environment_stays_on_the_direct_chat_path():
    config = HarnessConfig(models={"coding": preset(execution_environment="host")})
    with pytest.raises(ValueError, match="no agent binding"):
        resolve_agent_model("coding", agent_settings(), config=config)
    assert config.preset_for("coding").codex["sandbox"] == "workspace-write"


@pytest.mark.parametrize("provider", ["cursor", "antigravity"])
def test_other_explicit_host_providers_use_existing_legacy_adapter(provider):
    config = HarnessConfig(models={"coding": ModelPreset(provider=provider, execution_environment="host", cwd="/fixture")})
    assert config.preset_for("coding").provider == provider


async def test_session_isolated_rejects_before_cache_database_auth_or_provider(monkeypatch):
    import kotoyose.session as session
    config = HarnessConfig(models={"coding": preset(execution_environment="isolated")})
    monkeypatch.setattr(session, "get_config", lambda: config)
    monkeypatch.setattr(session, "sessions", {"existing": SimpleNamespace(provider="codex")})
    monkeypatch.setattr(session, "_cache_identity", Mock(side_effect=AssertionError("cache lookup")))
    monkeypatch.setattr(session, "lookup_runtime_session", AsyncMock(side_effect=AssertionError("database resume")))
    monkeypatch.setattr(session, "build_options", Mock(side_effect=AssertionError("auth read")))
    monkeypatch.setattr(session, "_build_registered_runtime_session", AsyncMock(side_effect=AssertionError("provider launch")))
    with pytest.raises(session.SDKConnectionError, match="Isolated execution"):
        await session.get_or_resume_client("existing", "coding")


def test_explicit_host_cache_identity_pins_environment_and_sdk_sandbox():
    from kotoyose.session import _cache_identity
    args = ("codex", "coding", "gpt-6-luna", [], None)
    historical = _cache_identity(*args)
    assert _cache_identity(*args, codex_sandbox="danger-full-access") == historical
    identities = {_cache_identity(*args, execution_environment="host", codex_sandbox=value)
                  for value in ("read-only", "workspace-write", "danger-full-access")}
    assert len(identities) == 3 and historical not in identities


async def test_session_explicit_host_passes_effective_sandbox_to_existing_adapter(monkeypatch):
    import kotoyose.session as session
    config = HarnessConfig(models={"coding": preset(execution_environment="host")})
    monkeypatch.setattr(session, "get_config", lambda: config)
    builder = Mock(return_value=SimpleNamespace(provider="codex"))
    monkeypatch.setattr(session, "RUNTIME_PROVIDER_REGISTRY", {
        "codex": session.RuntimeProviderSpec(builder, "codex")})
    await session.get_or_resume_client(None, "coding")
    assert builder.call_args.args[0].codex["sandbox"] == "workspace-write"
    assert config.models["coding"].codex == {}


async def test_switching_to_explicit_host_discards_old_cached_and_persisted_session(monkeypatch):
    import kotoyose.session as session
    config = HarnessConfig(models={"coding": preset(execution_environment="host")})
    monkeypatch.setattr(session, "get_config", lambda: config)
    cached = SimpleNamespace(provider="codex", disconnect=AsyncMock())
    monkeypatch.setattr(session, "sessions", {"existing": cached})
    monkeypatch.setattr(session, "session_last_used", {})
    monkeypatch.setattr(session, "session_cache_identities", {
        "existing": session._cache_identity("codex", "coding", "gpt-6-luna", [], None)})
    resume = AsyncMock(side_effect=AssertionError("old persisted session cannot resume"))
    monkeypatch.setattr(session, "lookup_runtime_session", resume)
    replacement = SimpleNamespace(provider="codex")
    builder = Mock(return_value=replacement)
    monkeypatch.setattr(session, "RUNTIME_PROVIDER_REGISTRY", {"codex": session.RuntimeProviderSpec(builder, "codex")})
    result, was_resumed = await session.get_or_resume_client("existing", "coding")
    assert result is replacement and was_resumed is False
    cached.disconnect.assert_awaited_once()
    assert builder.call_args.args[2] is None
    resume.assert_not_awaited()
