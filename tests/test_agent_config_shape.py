"""route / agent / drivers だけが agent 実行の形。古い key は読込時に拒む。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from kotoyose.agent_route import resolve_declared_agent
from kotoyose.agent_sanction import sanction_reasons
from kotoyose.agent_spec import accepted_runs
from kotoyose.config import AgentRoute, AliasAgent, Connection, HarnessConfig, ModelPreset
from kotoyose.execution_resources import (
    CallerPolicy, CodexEgressLimits, DriverCell, DriverInstall, NativeExecutionConfig, resolve_agent_model,
)


def _caller(tmp_path, **extra):
    return CallerPolicy(
        workspace_roots=[tmp_path], agent_models=["*"],
        places=["isolated", "host"], max_surface="write", max_seconds=60,
        max_memory_bytes=16 * 1024 * 1024, max_pids=16, max_cpus=1, max_concurrent=1, **extra,
    )


def test_replaced_execution_keys_fail_at_load(tmp_path):
    """消した key は名前と置き換え先を出して拒む。"""
    caller = {
        "workspace_roots": [str(tmp_path)],
        "max_seconds": 60, "max_memory_bytes": 16 * 1024 * 1024,
        "max_pids": 16, "max_cpus": 1, "max_concurrent": 1,
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        NativeExecutionConfig.model_validate({
            "schema_version": "1", "image_id": "sha256:" + "a" * 64, "runtime_profiles": {},
        })
    with pytest.raises(ValidationError, match="agent_sandbox_profiles was replaced by alias agent"):
        NativeExecutionConfig.model_validate({
            "schema_version": "1", "image_id": "sha256:" + "a" * 64, "agent_sandbox_profiles": {},
        })
    with pytest.raises(ValidationError, match="agent_cli_binary was replaced by drivers"):
        NativeExecutionConfig.model_validate({
            "schema_version": "1", "image_id": "sha256:" + "a" * 64, "agent_cli_binary": "/bin/sh",
        })
    with pytest.raises(ValidationError, match="agent_credential_mode was replaced by connections"):
        NativeExecutionConfig.model_validate({
            "schema_version": "1", "image_id": "sha256:" + "a" * 64, "agent_credential_mode": "runtime_managed",
        })
    with pytest.raises(ValidationError, match="callers moved to config.yml clients"):
        NativeExecutionConfig.model_validate({
            "schema_version": "1", "image_id": "sha256:" + "a" * 64, "callers": {"taskhub": caller},
        })
    with pytest.raises(ValidationError, match="isolation_modes was replaced by places"):
        CallerPolicy.model_validate({**caller, "isolation_modes": ["sandbox"]})
    with pytest.raises(ValidationError, match="runtime_profiles was replaced by agent_models"):
        CallerPolicy.model_validate({**caller, "runtime_profiles": ["coding"]})


def test_flat_rootfs_keys_were_replaced_by_cells():
    """rootfs と egress は driver 直下ではなく cells に置く。"""
    limits = {
        "version": 1, "total_bytes": "disabled", "total_connections": "disabled",
        "request_bytes": 16777216, "response_bytes": 33554432,
    }
    with pytest.raises(ValidationError, match="rootfs was replaced by cells"):
        DriverInstall.model_validate({"rootfs": "/opt/fixture/rootfs", "rootfs_digest": "0" * 64})
    with pytest.raises(ValidationError, match="rootfs_digest was replaced by cells"):
        DriverInstall.model_validate({"rootfs_digest": "0" * 64})
    with pytest.raises(ValidationError, match="codex_egress_limits was replaced by cells"):
        DriverInstall.model_validate({"codex_egress_limits": limits})
    with pytest.raises(ValidationError, match="coding cell"):
        DriverInstall(cells={"standard": DriverCell(
            rootfs=Path("/opt/fixture/std"), rootfs_digest="0" * 64,
            codex_egress_limits=CodexEgressLimits.model_validate(limits),
        )})


def test_a_declared_cell_does_not_stand_in_for_the_other(tmp_path):
    """coding だけを置いた機械で jail を解くと、その cell が無いと分かる。"""
    config = HarnessConfig(
        connections={"codex-env": Connection(provider="codex", kind="api_key", data_policy="no_training", secret="env:CODEX_TOKEN")},
        routes={"terra": AgentRoute(driver="codex.app-server", model="gpt-6.1-terra", connection="codex-env")},
        models={"jail": ModelPreset(
            cwd="/tmp", route="terra",
            agent=AliasAgent(place="isolated", surface="exec"),
        )},
    )
    settings = type("Settings", (), {
        "drivers": {"codex.app-server": DriverInstall(
            cli_binary=Path("/tmp/codex"),
            cells={"coding": DriverCell(rootfs=Path("/opt/fixture/coding"), rootfs_digest="ab" * 32)},
        )},
        "agent_compression_mode": "runtime_managed",
    })()
    with pytest.raises(ValueError, match="no standard cell"):
        resolve_agent_model("jail", settings, config=config, resolve_prompts=False)


def test_agent_requires_route_and_drivers(tmp_path):
    """agent には route が要る。実行には drivers が要る。agent が無い alias は動かさない。"""
    with pytest.raises(ValidationError, match="agent には route が必要"):
        HarnessConfig(models={"coding": ModelPreset(
            cwd="/tmp", agent=AliasAgent(place="isolated", surface="write"),
        )})
    config = HarnessConfig(
        connections={"codex-chatgpt": Connection(
            provider="codex", kind="subscription", data_policy="no_training", auth_file=Path("/tmp/auth.json"),
        )},
        routes={"terra": AgentRoute(driver="codex.app-server", model="gpt-6.1-terra", connection="codex-chatgpt")},
        models={
            "coding": ModelPreset(
                cwd="/tmp", route="terra",
                agent=AliasAgent(place="isolated", surface="write"),
            ),
            "chat": ModelPreset(provider="claude", model="claude-sonnet-5", cwd="/tmp"),
        },
    )
    bare = type("Settings", (), {"drivers": None, "agent_compression_mode": "runtime_managed"})()
    with pytest.raises(ValueError, match="agent execution requires drivers"):
        resolve_agent_model("coding", bare, config=config, resolve_prompts=False)
    with pytest.raises(ValueError, match="no agent binding"):
        resolve_agent_model("chat", bare, config=config, resolve_prompts=False)
    settings = type("Settings", (), {
        "drivers": {"codex.app-server": DriverInstall(cli_binary=Path("/tmp/codex"))},
        "agent_compression_mode": "runtime_managed",
    })()
    resolved = resolve_agent_model(
        "coding", settings, config=config, resolve_preferences=False, resolve_prompts=False,
    )
    assert resolved.model == "gpt-6.1-terra"
    assert resolved.codex_execution_mode == "host_native_coding_v1"
    assert resolved.backend == "native"


def test_declared_surface_is_a_ceiling(monkeypatch):
    """Claude の read は読み取り専用の部屋に固定し、write は従来の道具を使える。"""
    monkeypatch.setattr("kotoyose.provider_jail._trusted_binary", lambda value: Path(value))
    config = HarnessConfig(
        connections={"claude-oauth": Connection(provider="claude", kind="subscription", data_policy="no_training", secret="claude-oauth")},
        routes={"sonnet": AgentRoute(driver="claude.agent-sdk", model="claude-sonnet-5", connection="claude-oauth")},
        models={
            "wide": ModelPreset(cwd="/tmp", route="sonnet", agent=AliasAgent(place="isolated", surface="write")),
            "narrow": ModelPreset(cwd="/tmp", route="sonnet", agent=AliasAgent(place="isolated", surface="none")),
            "reading": ModelPreset(cwd="/tmp", route="sonnet", agent=AliasAgent(place="isolated", surface="read")),
        },
    )
    settings = type("Settings", (), {
        "drivers": {"claude.agent-sdk": DriverInstall(cli_binary=Path("/bin/sh"))},
        "agent_compression_mode": "runtime_managed",
    })()
    wide = resolve_declared_agent(
        "wide", config.preset_for("wide"), settings, config, resolve_preferences=False, resolve_prompts=False,
    )
    narrow = resolve_declared_agent(
        "narrow", config.preset_for("narrow"), settings, config, resolve_preferences=False, resolve_prompts=False,
    )
    assert [item["accepted_tools"] for item in accepted_runs(wide, agent_container=True)] == [
        "none", "exactly host-coding",
    ]
    assert accepted_runs(narrow, agent_container=True) == [{"accepted_tools": "none", "place": "jail"}]
    reading = resolve_declared_agent(
        "reading", config.preset_for("reading"), settings, config, resolve_preferences=False, resolve_prompts=False,
    )
    assert accepted_runs(reading, agent_container=True) == [{"accepted_tools": "none", "place": "room"}]
    assert accepted_runs(reading, agent_container=False) == []


def test_removed_host_agent_is_not_published(tmp_path):
    """Caller の places と max_surface が /v1/models の agent を決める。"""
    from kotoyose.agent_route import published_agent

    config = HarnessConfig(
        connections={"cursor-api-key": Connection(provider="cursor", kind="api_key", data_policy="no_training", secret="env:CURSOR_API_KEY")},
        routes={"grok": AgentRoute(driver="cursor.sdk", model="grok-4.7", connection="cursor-api-key")},
        models={"coding": ModelPreset(
            cwd="/tmp", route="grok",
            agent=AliasAgent(place="host", surface="write"),
        )},
    )
    settings = type("Settings", (), {
        "drivers": {"cursor.sdk": DriverInstall()},
        "agent_compression_mode": "runtime_managed",
    })()
    host = _caller(tmp_path)
    assert published_agent("coding", host, settings, config=config) is None


def test_sanction_reasons_flag_unofficial_and_cross_provider_models():
    assert sanction_reasons("claude.agent-sdk", "claude-sonnet-5", "https://9router.example/v1") == [
        "unofficial_endpoint",
    ]
    assert sanction_reasons("claude.agent-sdk", "cc/claude-sonnet-5-5", None) == []
    assert sanction_reasons("cursor.sdk", "grok-4.7", None) == []
    assert sanction_reasons("claude.agent-sdk", "gcli/grok-4.7", None) == ["cross_provider_model"]
