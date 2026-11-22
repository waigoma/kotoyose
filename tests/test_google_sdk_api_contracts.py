"""Installed optional SDK contracts. These never enter Agent or call a model.

Run with the exact optional dependency: uv sync --extra dev --extra antigravity.
A missing optional package is a skip, never evidence of provider conformance.
"""

from importlib.metadata import version

import pytest

from kotoyose.runtime.antigravity_sdk import _build_sdk_config_kwargs, _load_sdk
from kotoyose.runtime.translation import TranslatedRuntimeConfig


def _translated():
    return TranslatedRuntimeConfig(
        instructions="contract-test", plugin_paths=(), mcp_servers={}, skills=(),
        effort=None, timeout_seconds=None, unsupported_fields=(),
    )


@pytest.fixture
def installed_sdk():
    pytest.importorskip("google.antigravity")
    assert version("google-antigravity") == "0.1.23"
    return _load_sdk()


@pytest.mark.parametrize("config,expected", [
    ({}, {"view_file", "read_url_content", "finish"}),
    ({"autonomous_tools": True}, {"view_file", "create_file", "edit_file", "run_command", "generate_image", "search_web", "read_url_content", "finish"}),
    ({"allow_all": True}, {"view_file", "create_file", "edit_file", "run_command", "generate_image", "search_web", "read_url_content", "finish"}),
])
def test_actual_local_config_pins_root_tools_without_provider_subagents(installed_sdk, tmp_path, config, expected, monkeypatch):
    sdk = installed_sdk
    # Reproduce default-helper drift; our adapter must never call these helpers.
    monkeypatch.setattr(sdk["types"].BuiltinTools, "read_only", classmethod(lambda cls: [cls.SCHEDULE]))
    kwargs = _build_sdk_config_kwargs(sdk=sdk, model=None, cwd=str(tmp_path), translated=_translated(), config=config)
    local = sdk["LocalAgentConfig"](**kwargs)
    assert {tool.value for tool in local.capabilities.enabled_tools} == expected
    assert local.capabilities.run_command_config.enable_daemons is False
    assert local.subagents == []


async def test_actual_policy_denies_schedule_and_daemons_even_with_allow_all(installed_sdk, tmp_path):
    sdk = installed_sdk
    kwargs = _build_sdk_config_kwargs(sdk=sdk, model=None, cwd=str(tmp_path), translated=_translated(), config={"allow_all": True})
    hook = sdk["policy"].enforce(kwargs["policies"])
    for name, arguments, allowed in [
        ("schedule", {}, False), ("manage_task", {}, False),
        ("run_command", {"IsDaemon": True}, False), ("run_command", {"IsDaemon": False}, True),
        ("view_file", {}, True),
    ]:
        decision = await hook.run(None, sdk["types"].ToolCall(name=name, args=arguments))
        assert decision.allow is allowed

