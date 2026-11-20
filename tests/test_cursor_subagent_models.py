"""Cursor subagent model-tier translation tests."""

import pytest

from kotoyose.runtime.cursor import (
    _cursor_agent_models,
    _to_sdk_agents,
    build_cursor_session,
)


def test_cursor_agent_models_accept_common_tiers():
    assert _cursor_agent_models(
        {
            "haiku": "fast-model",
            "sonnet": "balanced-model",
            "opus": "max-model",
        }
    ) == {
        "haiku": "fast-model",
        "sonnet": "balanced-model",
        "opus": "max-model",
    }


def test_cursor_agent_models_reject_unknown_tier():
    with pytest.raises(ValueError, match="未対応の tier"):
        _cursor_agent_models({"mini": "some-model"})


def test_cursor_agent_models_reject_empty_model_id():
    with pytest.raises(ValueError, match="空でない文字列"):
        _cursor_agent_models({"haiku": ""})


def test_to_sdk_agents_maps_common_tier_to_cursor_model():
    from kotoyose.config import AgentPreset

    agents = _to_sdk_agents(
        {
            "researcher": AgentPreset(
                description="research",
                prompt="research carefully",
                model="haiku",
            ),
            "reviewer": AgentPreset(
                description="review",
                prompt="review carefully",
                model="inherit",
            ),
        },
        {"haiku": "fast-model"},
    )

    assert agents["researcher"].model == "fast-model"
    assert agents["reviewer"].model == "inherit"


def test_unmapped_tier_keeps_parent_model(monkeypatch):
    from kotoyose.config import AgentPreset

    warnings = []
    monkeypatch.setattr(
        "kotoyose.runtime.cursor.log.warning",
        lambda event, **kwargs: warnings.append((event, kwargs)),
    )
    agents = _to_sdk_agents(
        {
            "researcher": AgentPreset(
                description="research",
                prompt="research carefully",
                model="haiku",
            )
        },
        {},
    )

    assert agents["researcher"].model is None
    assert any(event == "cursor.agent.model_alias_unsupported" for event, _ in warnings)


def test_builder_reads_cursor_agent_model_mapping(tmp_path, monkeypatch):
    from kotoyose.config import AgentPreset, HarnessConfig, ModelPreset

    monkeypatch.setattr("kotoyose.config._config", HarnessConfig())
    session = build_cursor_session(
        ModelPreset(
            provider="cursor",
            cwd=str(tmp_path),
            agents={
                "researcher": AgentPreset(
                    description="research",
                    prompt="research carefully",
                    model="haiku",
                )
            },
            cursor={
                "agent_models": {
                    "haiku": "fast-model",
                    "sonnet": "balanced-model",
                    "opus": "max-model",
                }
            },
        ),
        None,
        None,
    )

    assert session.agents["researcher"].model == "fast-model"


@pytest.mark.parametrize("tools", [[], ["Read", "Grep"]])
def test_subagent_tools_fail_closed_including_empty_list(tools):
    """Cursor AgentDefinition has no generic built-in tool allowlist field."""
    from kotoyose.config import AgentPreset

    with pytest.raises(ValueError, match="tools is unsupported"):
        _to_sdk_agents({
            "researcher": AgentPreset(
                description="research", prompt="research carefully", tools=tools,
            )
        })


@pytest.mark.parametrize("tools", [[], ["Read"]])
def test_builder_rejects_unenforceable_subagent_tool_policy(tmp_path, monkeypatch, tools):
    from kotoyose.config import AgentPreset, HarnessConfig, ModelPreset

    monkeypatch.setattr("kotoyose.config._config", HarnessConfig())
    preset = ModelPreset(
        provider="cursor", cwd=str(tmp_path),
        agents={"reviewer": AgentPreset(description="review", prompt="review", tools=tools)},
    )
    with pytest.raises(ValueError, match="tools is unsupported"):
        build_cursor_session(preset, None, None)
