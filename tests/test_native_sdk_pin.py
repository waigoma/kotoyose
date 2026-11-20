"""Native Claude adapter の SDK 固定と、依存宣言・lock の食い違いを CI で止める。"""

from pathlib import Path
import tomllib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kotoyose.execution_service import ExecutionService, NATIVE_CLAUDE_SDK_VERSION

ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_and_lock_pin_the_native_claude_sdk():
    """2026-09-27 に SDK だけ上げた lock が配備され、Native の起動検査で Kotoyose が落ちた。"""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert f"claude-agent-sdk=={NATIVE_CLAUDE_SDK_VERSION}" in project["project"]["dependencies"]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {p["name"]: p["version"] for p in lock["package"]}
    assert locked["claude-agent-sdk"] == NATIVE_CLAUDE_SDK_VERSION


@pytest.mark.parametrize("sdk_version", ["0.2.163", "0.2.186", NATIVE_CLAUDE_SDK_VERSION])
async def test_native_start_checks_sdk_before_worker_or_database(sdk_version, monkeypatch):
    """実 worker / DB がなくても旧版・未検証版の拒否を退行させない。"""
    from kotoyose import config, execution_service, provider_jail
    from kotoyose.config import AgentRoute, AliasAgent, Client, Connection, HarnessConfig, ModelPreset
    from kotoyose.execution_resources import DriverInstall

    kotoyose = HarnessConfig(
        connections={"claude-oauth": Connection(provider="claude", kind="subscription", data_policy="no_training", secret="claude-oauth")},
        routes={"claude": AgentRoute(driver="claude.agent-sdk", model="claude-sonnet-4-5", connection="claude-oauth")},
        models={"claude": ModelPreset(
            route="claude",
            agent=AliasAgent(place="isolated", surface="none"),
        )},
        clients={"caller": Client(
            workspace_roots=[Path("/tmp")], models=["claude"],
            max_run_seconds=30, max_memory_bytes=64 * 1024 * 1024, max_processes=8, max_cpus=1, max_concurrent_runs=1,
        )},
    )
    service = ExecutionService.__new__(ExecutionService)
    service.config = SimpleNamespace(
        validate_client_resources=Mock(),
        drivers={"claude.agent-sdk": DriverInstall(
            cli_binary=Path("/test/claude"),
        )},
        worker_capabilities={},
    )
    monkeypatch.setattr(provider_jail, "build_jail_command", lambda *args: [])
    monkeypatch.setattr(config, "get_config", lambda: kotoyose)
    monkeypatch.setattr(execution_service, "version", lambda package: sdk_version)
    monkeypatch.setattr(execution_service.shutil, "which", lambda executable: None)

    # The supported pin reaches the next startup prerequisite; mismatches fail before it.
    expected = "requires bubblewrap" if sdk_version == NATIVE_CLAUDE_SDK_VERSION else "requires the pinned SDK"
    with pytest.raises(RuntimeError, match=expected):
        await service.start()


def test_native_resume_rejects_a_snapshot_from_the_previous_sdk():
    """SDK 更新で旧 Attempt の provider session を暗黙に再利用しない。"""
    from kotoyose.execution_resources import ExecutionError, content_digest
    from kotoyose.execution_resume import build_resume_manifest
    from kotoyose.runtime.resume import ResumeSupport

    old = {"kind": "agent", "provider": "claude", "sdk_version": "0.2.163"}
    previous = {
        "snapshot": old, "request": {"caller_run_ref": "run"},
        "workspace_id": "workspace", "generation": 0,
    }
    request = SimpleNamespace(
        execution=SimpleNamespace(kind="agent", caller_run_ref="run"),
        predecessor_snapshot_digest=content_digest(old),
    )
    snapshot = {
        **old, "sdk_version": NATIVE_CLAUDE_SDK_VERSION,
        "workspace_id": "workspace", "generation": 1,
    }
    with pytest.raises(ExecutionError) as caught:
        build_resume_manifest("caller", previous, [], request, snapshot, ResumeSupport(modes=("native",), reason="SDK fixture"))
    assert caught.value.code == "resume_unsafe"
    assert caught.value.status == 409
    assert caught.value.reason_code == "runtime_mismatch"
