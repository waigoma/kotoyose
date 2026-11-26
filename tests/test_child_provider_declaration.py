"""Launch settings at the SDK boundary and a product added through a CLI profile."""

from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest
from openai_codex import ApprovalMode, Sandbox

from kotoyose import child_codex, child_components, child_turn, claude_cli, session
from kotoyose.child_protocol import Frame
from kotoyose.config import HarnessConfig
from kotoyose.runtime import claude_options, codex
from kotoyose.runtime.child import build_turn_payload
from tests.test_child_k3 import _host_config, _policy
from tests.test_child_k8 import _config as _codex_config, _sdk_with_config


def _payload(config, root):
    return build_turn_payload(
        alias="dev", config=config, preset=config.models["dev"], conversation_key=None,
        cwd=str(root), prompt="test",
    )


@pytest.mark.parametrize("choice", ["bundled", "system"])
async def test_claude_child_launch_golden(tmp_path, monkeypatch, choice):
    home = tmp_path / "child"
    os_home = tmp_path / "os-user"
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("USERPROFILE", str(os_home))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "local-env-api-key")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    if choice == "system":
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(os_home / "normal-custom-login"))
    else:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    _policy(home, tmp_path)
    (home / "app.json").write_text(json.dumps({"claude_cli": choice}))
    wheel = tmp_path / "wheel" / "claude"
    system = tmp_path / "bin" / "claude"
    monkeypatch.setattr(claude_cli, "bundled_claude_cli", lambda: wheel)
    monkeypatch.setattr(claude_options, "bundled_claude_cli", lambda: wheel)
    monkeypatch.setattr(child_components, "system_cli", lambda row: system)
    observed = []

    class SDK:
        def __init__(self, *, options):
            observed.append(options)

        async def connect(self):
            pass

        async def disconnect(self):
            pass

    monkeypatch.setattr(session, "ClaudeSDKClient", SDK)
    config = HarnessConfig.model_validate(_host_config(tmp_path))
    payload = _payload(config, tmp_path)
    assert "config_dir" not in payload["connection"]
    payload["connection"]["config_dir"] = str(tmp_path / "spoofed")
    payload["preset"]["anthropic_base_url"] = "https://untrusted.invalid"
    prepared = child_turn._prepare(home, payload)
    try:
        runtime = await child_turn._build_runtime(prepared)
        try:
            [options] = observed
            plugin = Path(options.plugins[0]["path"])
            login_dir = os_home / ("normal-custom-login" if choice == "system" else ".claude")
            assert prepared["config"].connections["dev-conn"].config_dir == login_dir
            assert {
                "cli": options.cli_path,
                "model": options.model,
                "cwd": options.cwd,
                "env": options.env,
                "prompt": options.system_prompt,
                "mcp": options.mcp_servers,
                "plugin_types": [item["type"] for item in options.plugins],
                "skills": options.skills,
            } == {
                "cli": system if choice == "system" else wheel,
                "model": "claude-sonnet-4-5",
                "cwd": str(tmp_path),
                "env": {
                    "ANTHROPIC_API_KEY": "",
                    **({"CLAUDE_CONFIG_DIR": str(login_dir)} if choice == "system" else {}),
                },
                "prompt": {"type": "preset", "preset": "claude_code", "append": "PROMPT_BODY_UNIQUE\n"},
                "mcp": {"files": {"command": "child-local-mcp", "args": ["--from-policy"], "env": {"CHILD_TOKEN": "POLICY_SECRET"}}},
                "plugin_types": ["local", "local"],
                "skills": ["demo"],
            }
            assert (plugin / "skills" / "demo" / "SKILL.md").read_text() == "SKILL_BODY_UNIQUE"
        finally:
            await runtime.disconnect()
    finally:
        prepared["cleanup"]()


@pytest.mark.parametrize("choice", ["bundled", "system"])
async def test_codex_child_launch_and_mcp_retry_golden(tmp_path, monkeypatch, choice):
    home = tmp_path / "child"
    login_dir = tmp_path / "normal-login"
    monkeypatch.setenv("CODEX_HOME", str(login_dir))
    _policy(home, tmp_path, adapters=["codex.app-server"], mcp={"files": {"command": "local-files", "args": ["--read"]}})
    (home / "app.json").write_text(json.dumps({"codex_cli": choice}))
    wheel = str(tmp_path / "wheel" / "codex")
    system = str(tmp_path / "bin" / "codex")
    monkeypatch.setattr(child_codex, "_bundled_binary", lambda: wheel)
    monkeypatch.setattr(child_codex.shutil, "which", {"codex": system}.get)
    config = _codex_config(tmp_path)
    config.routes["r"].model_provider = "host-provider"
    config.models["dev"].codex = {"sandbox": "full-access", "timeout": 12, "approval_mode": "never", "codex_bin": "/host-only"}
    config.models["dev"].codex_home = "/host-only"
    payload = _payload(config, tmp_path)
    assert payload["route"] == {"driver": "codex.app-server", "model": "gpt-5", "connection": "child-codex"}
    assert payload["preset"]["codex"] == {"sandbox": "full-access", "timeout": 12}
    assert "codex_home" not in payload["preset"]

    allowed = {"command": "local-files", "args": ["--read"], "enabled": True, "default_tools_approval_mode": "approve"}
    first, second = [
        _sdk_with_config({"mcp_servers": {"files": allowed, "local-extra": {"command": "extra", "enabled": enabled}}})
        for enabled in (True, False)
    ]
    configs = []
    sdks = iter((first, second))

    def sdk(sdk_config):
        configs.append(sdk_config)
        return next(sdks)

    monkeypatch.setattr(codex, "AsyncCodex", sdk)
    prepared = child_turn._prepare(home, payload)
    try:
        assert prepared["config"].connections["child-codex"].config_dir is None
        assert prepared["builder_options"]["child_launch"].argv == ((wheel,) if choice == "bundled" else (system,))
        runtime = await child_turn._build_runtime(prepared)
        try:
            await runtime._ensure_thread()
            assert runtime.config == {
                "sandbox": "workspace-write", "timeout": 12, "approval_mode": "deny_all",
                "config": {"project_doc_max_bytes": 0},
            }
            assert runtime.codex_home is None
            assert first.close.await_count == 1
            first.thread_start.assert_not_awaited()
            second.thread_start.assert_awaited_once_with(
                model="gpt-5", cwd=str(tmp_path), developer_instructions=None,
                sandbox=Sandbox.workspace_write, approval_mode=ApprovalMode.deny_all,
            )
            table = 'mcp_servers={"files"={"command"="local-files", "args"=["--read"], "env"={}, "env_vars"=[], "disabled_tools"=[], "enabled"=true, "default_tools_approval_mode"="approve"}}'
            for sdk_config, disabled in zip(configs, ((), ("mcp_servers.local-extra.enabled=false",)), strict=True):
                overrides = (
                    "project_doc_max_bytes=0", "agents.enabled=false", table, *disabled,
                    "features.plugins=false", "features.apps=false", 'model_provider="openai"',
                )
                assert sdk_config.cwd == str(tmp_path)
                assert sdk_config.env is None  # The SDK inherits the OS user's CODEX_HOME.
                if choice == "bundled":
                    assert sdk_config.config_overrides == overrides
                    assert sdk_config.launch_args_override is None
                else:
                    flags = tuple(item for override in overrides for item in ("--config", override))
                    assert sdk_config.launch_args_override == (system, *flags, "app-server", "--listen", "stdio://")
                    assert not sdk_config.config_overrides
        finally:
            await runtime.disconnect()
    finally:
        prepared["cleanup"]()


async def test_third_provider_runs_child_turn_from_profile_data(tmp_path, monkeypatch):
    from kotoyose.print_cli_profiles import _REGISTRY, get_print_cli_profile
    from kotoyose.provider_spec import ChildLoginHome, ChildTurnSpec

    home = tmp_path / "child"
    os_home = tmp_path / "os-user"
    login_dir = os_home / ".fixture"
    login_dir.mkdir(parents=True)
    cli = tmp_path / "fixture-cli"
    cli.write_text(f"#!{sys.executable}\n" + """
import json
import os
print(json.dumps({"type": "result", "subtype": "success", "session_id": "fixture-session",
                  "result": os.environ["FIXTURE_HOME"] + " fixture-key"}))
""")
    cli.chmod(0o700)
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("USERPROFILE", str(os_home))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("FIXTURE_KEY", "fixture-key")
    monkeypatch.delenv("FIXTURE_HOME", raising=False)
    _policy(home, tmp_path, adapters=["cli.print"], mcp={})

    profile = replace(
        get_print_cli_profile("cursor"), id="fixture", provider="fixture", display_name="Fixture",
        binary=cli.name, install=None, login_home_env="FIXTURE_HOME", login_home_env_paths={},
        child_turn=ChildTurnSpec(
            login_home=ChildLoginHome("FIXTURE_HOME", ".fixture", inject_config_dir=True),
            skill_format="none", output_guard_env=("FIXTURE_KEY",),
        ),
    )
    monkeypatch.setitem(_REGISTRY, profile.id, profile)
    config = HarnessConfig.model_validate({
        "connections": {"c": {
            "provider": "fixture", "kind": "subscription", "profile": "fixture",
            "config_dir": str(tmp_path / "host-only"), "data_policy": "no_training",
        }},
        "routes": {"r": {"driver": "cli.print", "connection": "c", "profile": "fixture", "model": "fixture-model"}},
        "models": {"dev": {"route": "r", "allow_cwd_override": True, "cwd_allowed_roots": [str(tmp_path)]}},
    })
    payload = _payload(config, tmp_path)
    payload["connection"]["config_dir"] = str(tmp_path / "ignored-host-login")
    payload["conversation_key"] = "fixture-conversation"
    frames = []

    async def send(stream, kind, body):
        frames.append(Frame(stream, kind, body))

    turns = child_turn.ChildTurns(home, send, None)
    try:
        await turns.handle(Frame(1, "turn", payload))
        assert [(frame.kind, frame.payload["text"]) for frame in frames] == [
            ("delta", f"{login_dir} [REDACTED]"),
            ("result", f"{login_dir} [REDACTED]"),
        ]
        assert frames[-1].payload["runtime_session_id"] == "fixture-session"
        other_login = os_home / "custom-fixture-login"
        other_login.mkdir()
        monkeypatch.setenv("FIXTURE_HOME", str(other_login))
        await turns.handle(Frame(2, "turn", payload))
        assert frames[-1].kind == "result"
        assert frames[-1].payload["text"] == f"{other_login} [REDACTED]"
    finally:
        await turns.finish()
