"""Tests for `HostPlace` and XDG configuration path generalization."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions
import pytest

from kotoyose import process_env
from kotoyose.config import HarnessConfig, ModelPreset
from kotoyose.config_paths import (
    default_config_dir,
    default_data_dir,
    default_execution_config_path,
    resolve_config_file_path,
)
from kotoyose.host_place import HostPlace, shutdown_process_group, watch_work_timeout
from kotoyose.runtime.claude_options import build_claude_options


def test_host_place_validate_driver_allows_only_conformant_without_host_tools() -> None:
    HostPlace.validate_driver("claude.agent-sdk")

    for driver in ("codex.app-server", "cli.print:agy", "cursor.sdk"):
        with pytest.raises(ValueError, match="place='jail'"):
            HostPlace.validate_driver(driver)
        HostPlace.validate_driver(driver, allow_host_tools=True)


def test_host_place_constrain_claude_options_forces_empty_setting_sources(tmp_path: Path) -> None:
    preset_default_sources = ModelPreset(
        provider="claude",
        model="claude-sonnet-4-5",
        cwd=str(tmp_path),
        tools=[],
        setting_sources=None,
    )
    preset_project_sources = ModelPreset(
        provider="claude",
        model="claude-sonnet-4-5",
        cwd=str(tmp_path),
        tools=[],
        setting_sources=["user", "project"],
    )
    cfg = HarnessConfig(
        models={
            "none-default": preset_default_sources,
            "none-project": preset_project_sources,
        }
    )

    for alias_name in ("none-default", "none-project"):
        options = build_claude_options(
            cfg,
            cfg.preset_for(alias_name),
            model_name=alias_name,
            cwd_override=None,
            request_headers=None,
            env={"CLAUDE_CODE_OAUTH_TOKEN": "fixture-token"},
            session_store=None,
        )
        assert options.tools == []
        assert options.setting_sources == []
        assert options.env == {"CLAUDE_CODE_OAUTH_TOKEN": "fixture-token"}


def test_host_place_constrain_claude_options_masks_leaked_scrubbed_env(monkeypatch) -> None:
    snapshot = dict(os.environ)
    process_env._reset_for_tests(restore_environ=False)
    monkeypatch.setenv("SENTINEL_SECRET", "top-secret-value")
    process_env.scrub(["SENTINEL_SECRET"])
    # Simulate a leaked env var remaining in os.environ after scrubbing.
    monkeypatch.setenv("SENTINEL_SECRET", "reinjected")
    try:
        options = ClaudeAgentOptions(tools=["Read"], setting_sources=["project"], env={"EXPLICIT": "ok"})
        constrained = HostPlace.constrain_claude_options(options, allow_host_tools=True)
        assert constrained.tools == ["Read"]
        assert constrained.setting_sources == ["project"]
        assert constrained.env == {"EXPLICIT": "ok", "SENTINEL_SECRET": ""}

        minimal = HostPlace.build_subprocess_env(overrides={"FOO": "bar"})
        assert minimal["FOO"] == "bar"
    finally:
        process_env._reset_for_tests(restore_environ=False)
        for key in list(os.environ.keys()):
            if key not in snapshot:
                os.environ.pop(key, None)
        for key, value in snapshot.items():
            os.environ[key] = value


def test_config_paths_use_xdg_or_home_without_taskhub_or_personal_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_home = {"HOME": "/tmp/example-home"}
    cfg_dir = default_config_dir(env_home)
    data_dir = default_data_dir(env_home)
    assert cfg_dir == Path("/tmp/example-home/.config/kotoyose")
    assert data_dir == Path("/tmp/example-home/.local/share/kotoyose")
    assert ".task-hub" not in str(cfg_dir) and "sample-user" not in str(cfg_dir)
    assert ".task-hub" not in str(data_dir) and "sample-user" not in str(data_dir)

    env_xdg = {
        "HOME": "/tmp/example-home",
        "XDG_CONFIG_HOME": "/tmp/custom-xdg-cfg",
        "XDG_DATA_HOME": "/tmp/custom-xdg-data",
    }
    assert default_config_dir(env_xdg) == Path("/tmp/custom-xdg-cfg/kotoyose")
    assert default_data_dir(env_xdg) == Path("/tmp/custom-xdg-data/kotoyose")

    monkeypatch.chdir(tmp_path)
    xdg_cfg_file = tmp_path / "xdg-cfg" / "kotoyose" / "config.yml"
    xdg_cfg_file.parent.mkdir(parents=True)
    xdg_cfg_file.write_text("models: {}\n", encoding="utf-8")

    # 1. Explicit KOTOYOSE_CONFIG wins.
    assert resolve_config_file_path({"KOTOYOSE_CONFIG": "/explicit/config.yml"}) == "/explicit/config.yml"
    # 2. XDG config.yml is selected when local ./config.yml does not exist.
    assert resolve_config_file_path({"XDG_CONFIG_HOME": str(tmp_path / "xdg-cfg")}) == str(xdg_cfg_file)
    # 3. Local ./config.yml wins over XDG config.yml when KOTOYOSE_CONFIG is unset.
    local_cfg = tmp_path / "config.yml"
    local_cfg.write_text("models: {}\n", encoding="utf-8")
    assert resolve_config_file_path({"XDG_CONFIG_HOME": str(tmp_path / "xdg-cfg")}) == "config.yml"

    # 4. Execution config defaults to XDG config directory without .task-hub or personal paths.
    exec_from_home = default_execution_config_path(env_home)
    exec_from_xdg = default_execution_config_path(env_xdg)
    assert exec_from_home == Path("/tmp/example-home/.config/kotoyose/execution.json")
    assert exec_from_xdg == Path("/tmp/custom-xdg-cfg/kotoyose/execution.json")
    assert ".task-hub" not in str(exec_from_home) and "sample-user" not in str(exec_from_home)


@pytest.mark.asyncio
async def test_watch_work_timeout_and_shutdown_process_group() -> None:
    calls = 0

    def exhausted() -> float:
        nonlocal calls
        calls += 1
        return 0.0

    await asyncio.wait_for(watch_work_timeout(exhausted), timeout=0.5)
    assert calls == 1

    proc = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        "sleep 30",
        start_new_session=True,
    )
    assert await shutdown_process_group(proc, 0.05) == "confirmed"
    assert proc.returncode is not None


def test_execution_config_path_prefers_env_then_existing_xdg_default(tmp_path):
    from kotoyose.config_paths import resolve_execution_config_path

    xdg = tmp_path / "xdg"
    env = {"XDG_CONFIG_HOME": str(xdg), "HOME": str(tmp_path / "home")}
    # 既定ファイルが無ければ実行は未設定。
    assert resolve_execution_config_path(env) is None
    default = xdg / "kotoyose" / "execution.json"
    default.parent.mkdir(parents=True)
    default.write_text("{}")
    assert resolve_execution_config_path(env) == str(default)
    # env の明示が既定より勝つ。空白だけの値は未設定と同じ。
    assert resolve_execution_config_path({**env, "KOTOYOSE_EXECUTION_CONFIG": "/etc/explicit.json"}) == "/etc/explicit.json"
    assert resolve_execution_config_path({**env, "KOTOYOSE_EXECUTION_CONFIG": "  "}) == str(default)


@pytest.mark.asyncio
async def test_execution_bootstrap_reads_xdg_default_when_env_is_unset(tmp_path, monkeypatch):
    from kotoyose import execution_bootstrap

    monkeypatch.delenv("KOTOYOSE_EXECUTION_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert await execution_bootstrap.start_execution_service() is None

    default = tmp_path / "kotoyose" / "execution.json"
    default.parent.mkdir(parents=True)
    default.write_text("not json")
    # 既定ファイルがあれば読みに行く (壊れていれば起動時に失敗させる)。
    with pytest.raises(ValueError):
        await execution_bootstrap.start_execution_service()
