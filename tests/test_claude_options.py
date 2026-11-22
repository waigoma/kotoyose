"""Claude SDK option factory の単体テスト。"""

from pathlib import Path

import pytest

from kotoyose.config import HarnessConfig, ModelPreset
from kotoyose.runtime.claude_options import build_claude_options, claude_disallowed_tools


def test_build_claude_options_translates_preset_without_global_config():
    """明示された config と preset だけから SDK option を構築する。"""
    preset = ModelPreset(
        model="claude-sonnet-5",
        thinking="enabled",
        thinking_budget_tokens=2048,
    )
    config = HarnessConfig(models={"coding": preset})

    options = build_claude_options(
        config,
        preset,
        model_name="coding",
        cwd_override=None,
        request_headers=None,
        env={"ANTHROPIC_API_KEY": "token"},
        session_store=None,
    )

    assert options.model == "claude-sonnet-5"
    assert options.thinking == {"type": "enabled", "budget_tokens": 2048}
    assert options.agents is None
    assert options.disallowed_tools == ["Task", "Agent"]
    assert options.env == {"ANTHROPIC_API_KEY": "token"}


def test_build_claude_options_keeps_cwd_gate_fail_closed(tmp_path):
    """Facade を迂回しても未許可の cwd override を拒否する。"""
    preset = ModelPreset(allow_cwd_override=False)
    config = HarnessConfig(models={"chat": preset})

    with pytest.raises(ValueError, match="allow_cwd_override=False"):
        build_claude_options(
            config,
            preset,
            model_name="chat",
            cwd_override=str(tmp_path),
            request_headers=None,
            env={},
            session_store=None,
        )



@pytest.mark.parametrize("bare", [True, False])
def test_claude_bare_and_prompt_defaults_remain_explicitly_unchanged(bare):
    """Argv contract only; real bundled CLI resource discovery needs live acceptance."""
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    preset = ModelPreset(bare=bare)
    options = build_claude_options(
        HarnessConfig(models={"chat": preset}), preset, model_name="chat",
        cwd_override=None, request_headers=None, env={}, session_store=None,
    )
    options.cli_path = "/synthetic/unused-cli"
    command = SubprocessCLITransport(prompt="fixture", options=options)._build_command()
    assert ("--bare" in command) is bare
    assert options.system_prompt == {"type": "preset", "preset": "claude_code"}
    assert options.verbatim_prompts is False
    assert options.include_partial_messages is False
    assert options.output_format is None


def test_claude_output_schema_is_passed_to_installed_sdk():
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
    preset = ModelPreset(output_schema=schema)
    options = build_claude_options(
        HarnessConfig(models={"chat": preset}), preset, model_name="chat",
        cwd_override=None, request_headers=None, env={}, session_store=None,
    )
    assert options.output_format == {"type": "json_schema", "schema": schema}


def test_host_and_jail_builders_always_disallow_task_and_agent():
    """host の option builder と jail / native の builder は Task と Agent を拒否する。"""
    from kotoyose.container_claude import _DISALLOWED_TOOLS, _options as container_options
    from kotoyose.native_claude import NativeAgentJob, _build_options

    preset = ModelPreset(deny=["Bash(*)", "Task"])
    options = build_claude_options(
        HarnessConfig(models={"chat": preset}), preset, model_name="chat",
        cwd_override=None, request_headers=None, env={}, session_store=None,
    )
    assert options.disallowed_tools == ["Bash(*)", "Task", "Agent"]
    assert claude_disallowed_tools(["Agent", "Read"]) == ["Agent", "Read", "Task"]
    assert set(_DISALLOWED_TOOLS) >= {"WebFetch", "WebSearch", "Task", "Agent"}

    job = NativeAgentJob(
        attempt_id="attempt",
        model="claude-test",
        instructions="stay",
        prompt="hi",
        credential_name="ANTHROPIC_API_KEY",
        credential_value="secret",
        cli_binary=Path("/usr/bin/claude"),
        max_turns=1,
        timeout_seconds=1,
        grace_seconds=1,
        output_limit_bytes=1024,
        output_contract="text",
        tool_refs=(),
        image_inputs=(),
        client_tool_specs=(),
        approval_bindings=(),
    )
    native = _build_options(job, Path("/tmp/launcher"), Path("/tmp/empty"), None)
    assert "Task" in native.disallowed_tools and "Agent" in native.disallowed_tools
    jail = container_options(job, Path("/tmp/launcher"), Path("/tmp/empty"), {})
    assert "Task" in jail.disallowed_tools and "Agent" in jail.disallowed_tools


@pytest.mark.parametrize("snapshot", [None, False, True])
def test_explicit_oauth_migration_keeps_mcp_allowlist_and_prompt_policy(snapshot):
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    preset = ModelPreset(
        bare=False, setting_sources=[], require_isolation=True,
        strict_mcp_config=True, system_prompt_snapshot=snapshot,
        skills=[], tools=[], deny=["Bash(*)"],
        mcp_servers={"approved": {"type": "http", "url": "http://127.0.0.1:9/mcp"}},
    )
    config = HarnessConfig(models={"isolated": preset})
    options = build_claude_options(
        config, preset, model_name="isolated", cwd_override=None,
        request_headers=None, env={"CLAUDE_CODE_OAUTH_TOKEN": "fixture"}, session_store=None,
    )
    options.cli_path = "/synthetic/unused-cli"
    command = SubprocessCLITransport(prompt="fixture", options=options)._build_command()
    assert "--strict-mcp-config" in command
    assert "--setting-sources=" in command
    assert "--bare" not in command
    assert options.mcp_servers == preset.mcp_servers
    assert options.skills == options.tools == []
    assert options.disallowed_tools == ["Bash(*)", "Task", "Agent"]
    assert options.plugins == []
    if snapshot is None:
        assert "snapshot" not in options.system_prompt
    else:
        assert options.system_prompt["snapshot"] is snapshot
    serialized = config.model_dump()
    restored = HarnessConfig.model_validate(serialized).models["isolated"]
    assert restored.strict_mcp_config
    assert restored.system_prompt_snapshot is snapshot
