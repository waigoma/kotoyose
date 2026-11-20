"""Kotoyose 再起動後の container 復旧と起動時の掃除。

provider_process の停止証拠が agent_container のとき、jail 側の復旧には回らず container の不在だけで確定すること、
起動時の掃除が生きている attempt の container と処理中の home を消さないことを検証する。
KOTOYOSE_DOCKER_IT=1 のときだけ実 Docker daemon で復旧を一周させる。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kotoyose import agent_container as ac
from kotoyose import codex_stop_recovery
from kotoyose.agent_container import (
    AgentContainer,
    AgentContainerError,
    AgentContainerSpec,
    DEFAULT_INSTANCE,
    container_identity_job,
    recover_recorded_container,
    remove_leftover_homes,
)
from kotoyose.container_codex import container_job_id_for
from kotoyose.execution_service import ExecutionService

IDENTITY_ID = "c" * 64
REAL_IMAGE_ID = "sha256:f97cd896d8d896dc21211ad9f6fe62ad21050b3c132b7c3a4bd2a46f02269713"


def _identity(job_id: str = "att-1-g0", instance: str = DEFAULT_INSTANCE, container_id: str = IDENTITY_ID) -> dict:
    return {"name": ac.CONTAINER_PREFIX + job_id, "id": container_id, "instance": instance}


def _service(tmp_path: Path | None = None, *, tasks: dict | None = None, task_jobs: dict | None = None,
             home_root: str | None = None, operations=None) -> ExecutionService:
    service = ExecutionService.__new__(ExecutionService)
    service.store = SimpleNamespace(operations=AsyncMock(return_value=operations or []), resolve_operation=AsyncMock())
    service._tasks = tasks or {}
    service._task_jobs = task_jobs or {}
    service.config = SimpleNamespace(agent_container=None if home_root is None else SimpleNamespace(home_root=home_root))
    service._worker_for = lambda snapshot: None
    return service


def _codex_container_attempt() -> dict:
    return {
        "id": "att-1", "generation": 0,
        "snapshot": {"kind": "agent", "backend": "native", "provider": "codex",
                     "codex_execution_mode": "host_native_coding_v1"},
    }


def _claude_container_attempt() -> dict:
    return {"id": "att-1", "generation": 0, "snapshot": {"kind": "agent", "backend": "native", "provider": "claude"}}


def _operation(identity: object) -> dict:
    return {"id": "provider-op", "kind": "provider_process", "state": "sent", "result": {"pid": 1234, "agent_container": identity}}


@pytest.fixture
def forbid_docker(monkeypatch):
    async def fail(*args, **kwargs):
        raise AssertionError(f"docker must not be called: {args}")

    monkeypatch.setattr(ac.asyncio, "create_subprocess_exec", fail)


@pytest.mark.parametrize("provider", ["codex", "claude"])
async def test_recorded_container_is_removed_and_confirmed(monkeypatch, provider):
    remove = AsyncMock(return_value="confirmed")
    monkeypatch.setattr(AgentContainer, "remove", remove)
    jail = AsyncMock(return_value="unconfirmed")
    monkeypatch.setattr(codex_stop_recovery, "recover_codex_stop", jail)
    attempt = _codex_container_attempt() if provider == "codex" else _claude_container_attempt()
    service = _service(operations=[_operation(_identity())])
    assert await service._stop_attempt(attempt) == "confirmed"
    remove.assert_awaited_once_with("att-1-g0", 0)
    service.store.resolve_operation.assert_awaited_once_with("provider-op", "confirmed")


async def test_codex_container_attempt_never_reaches_jail_recovery(monkeypatch):
    monkeypatch.setattr(AgentContainer, "remove", AsyncMock(return_value="unconfirmed"))
    jail = AsyncMock(side_effect=AssertionError("jail recovery must not run for a container attempt"))
    monkeypatch.setattr(codex_stop_recovery, "recover_codex_stop", jail)
    service = _service(operations=[_operation(_identity())])
    assert await service._stop_attempt(_codex_container_attempt()) == "unconfirmed"
    jail.assert_not_awaited()
    service.store.resolve_operation.assert_awaited_once_with("provider-op", "unconfirmed")


async def test_unconfirmed_remove_leaves_operation_unconfirmed(monkeypatch):
    monkeypatch.setattr(AgentContainer, "remove", AsyncMock(return_value="unconfirmed"))
    service = _service(operations=[_operation(_identity())])
    assert await service._stop_attempt(_claude_container_attempt()) == "unconfirmed"
    service.store.resolve_operation.assert_awaited_once_with("provider-op", "unconfirmed")


async def test_ownership_refusal_is_unconfirmed(monkeypatch):
    monkeypatch.setattr(AgentContainer, "remove", AsyncMock(side_effect=ac.AgentContainerOwnershipError("foreign")))
    service = _service(operations=[_operation(_identity())])
    assert await service._stop_attempt(_codex_container_attempt()) == "unconfirmed"


@pytest.mark.parametrize("identity", [
    {"name": "other-att-1-g0", "id": IDENTITY_ID, "instance": DEFAULT_INSTANCE},
    {"name": "kotoyose-agent-a/b", "id": IDENTITY_ID, "instance": DEFAULT_INSTANCE},
    {"name": "kotoyose-agent-", "id": IDENTITY_ID, "instance": DEFAULT_INSTANCE},
    {"name": "kotoyose-agent-att-1-g0", "id": "not-hex", "instance": DEFAULT_INSTANCE},
    {"name": "kotoyose-agent-att-1-g0", "id": IDENTITY_ID, "instance": "bad instance"},
    {"name": "kotoyose-agent-att-1-g0", "id": IDENTITY_ID},
    "kotoyose-agent-att-1-g0",
    None,
])
async def test_malformed_identity_is_unconfirmed_without_docker(forbid_docker, identity):
    service = _service(operations=[_operation(identity)])
    assert await service._stop_attempt(_claude_container_attempt()) == "unconfirmed"
    service.store.resolve_operation.assert_awaited_once_with("provider-op", "unconfirmed")


def test_identity_parser_accepts_only_the_recorded_shape():
    assert container_identity_job(_identity("att-9-g2")) == ("att-9-g2", DEFAULT_INSTANCE)
    with pytest.raises(ValueError):
        container_identity_job("kotoyose-agent-x")


async def test_recover_recorded_container_uses_identity_instance_and_grace_zero(monkeypatch):
    seen = {}

    class Recording:
        def __init__(self, *args, instance, **kwargs):
            seen["instance"] = instance

        async def remove(self, job_id, grace):
            seen["call"] = (job_id, grace)
            return "confirmed"

    monkeypatch.setattr(ac, "AgentContainer", Recording)
    assert await recover_recorded_container(_identity("att-3-g1", instance=ac.DEFAULT_INSTANCE)) == "confirmed"
    assert seen == {"instance": ac.DEFAULT_INSTANCE, "call": ("att-3-g1", 0)}
    # 別の instance の記録には docker を触らずに unconfirmed を返す。
    seen.clear()
    assert await recover_recorded_container(_identity("att-3-g1", instance="other")) == "unconfirmed"
    assert seen == {}


def test_job_id_helper_matches_codex_job_name():
    assert container_job_id_for("att-1", 0) == "att-1-g0"
    assert ac.container_name(container_job_id_for("a/b c", 1)).startswith(ac.CONTAINER_PREFIX)


async def test_startup_sweep_keeps_live_jobs_and_passes_instance(monkeypatch, tmp_path):
    seen = {}

    async def fake_sweep(self, keep, *, grace_seconds=10):
        seen["instance"] = self.instance
        seen["live"] = keep(container_job_id_for("live", 3))
        seen["dead"] = keep("stale-g0")
        return ["stale-g0"]

    monkeypatch.setattr(AgentContainer, "sweep", fake_sweep)
    service = _service(task_jobs={"live": container_job_id_for("live", 3)}, home_root=str(tmp_path))
    await service._sweep_agent_containers()
    assert seen == {"instance": DEFAULT_INSTANCE, "live": True, "dead": False}


async def test_startup_sweep_failure_keeps_homes(monkeypatch, tmp_path):
    home_root = tmp_path / "homes"
    home_root.mkdir()
    (home_root / "kotoyose-agent-home-x").mkdir()
    monkeypatch.setattr(AgentContainer, "sweep", AsyncMock(side_effect=AgentContainerError("docker down")))
    await _service(home_root=str(home_root))._sweep_agent_containers()
    assert (home_root / "kotoyose-agent-home-x").is_dir()


async def test_startup_sweep_does_not_touch_homes_while_attempts_are_live(monkeypatch, tmp_path):
    home_root = tmp_path / "homes"
    home_root.mkdir()
    (home_root / "kotoyose-agent-home-x").mkdir()
    monkeypatch.setattr(AgentContainer, "sweep", AsyncMock(return_value=[]))
    await _service(tasks={"att": object()}, home_root=str(home_root))._sweep_agent_containers()
    assert (home_root / "kotoyose-agent-home-x").is_dir()


async def test_unconfirmed_container_keeps_homes_for_the_next_start(monkeypatch, tmp_path):
    home_root = tmp_path / "homes"
    home_root.mkdir()
    (home_root / "kotoyose-agent-home-x").mkdir()
    monkeypatch.setattr(AgentContainer, "sweep", AsyncMock(return_value=[]))
    monkeypatch.setattr(AgentContainer, "owned_jobs", AsyncMock(return_value=["stuck-g0"]))
    await _service(home_root=str(home_root))._sweep_agent_containers()
    assert (home_root / "kotoyose-agent-home-x").is_dir()


async def test_startup_sweep_removes_homes_when_nothing_unkept_remains(monkeypatch, tmp_path):
    home_root = tmp_path / "homes"
    home_root.mkdir()
    (home_root / "kotoyose-agent-home-x").mkdir()
    monkeypatch.setattr(AgentContainer, "sweep", AsyncMock(return_value=["stale-g0"]))
    monkeypatch.setattr(AgentContainer, "owned_jobs", AsyncMock(return_value=[]))  # 削除後は残りが無い
    await _service(home_root=str(home_root))._sweep_agent_containers()
    assert not (home_root / "kotoyose-agent-home-x").exists()


async def test_startup_sweep_without_container_config_is_a_no_op(monkeypatch):
    sweep = AsyncMock(side_effect=AssertionError("sweep must not run"))
    monkeypatch.setattr(AgentContainer, "sweep", sweep)
    await _service()._sweep_agent_containers()
    sweep.assert_not_awaited()


def _make_homes(root: Path, outside: Path) -> None:
    (root / "kotoyose-agent-home-a" / ".codex").mkdir(parents=True)
    (root / "kotoyose-agent-home-a" / ".codex" / "config.toml").write_text("x")
    (root / "kotoyose-agent-home-b").symlink_to(outside, target_is_directory=True)
    (root / "kotoyose-agent-home-file").write_text("not a dir")
    (root / "unrelated").mkdir()
    (outside / "keep.txt").write_text("outside")


def test_home_cleanup_removes_only_real_prefixed_dirs(tmp_path):
    root = tmp_path / "homes"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_homes(root, outside)
    removed = remove_leftover_homes(root)
    assert removed == ["kotoyose-agent-home-a"]
    assert not (root / "kotoyose-agent-home-a").exists()
    assert (root / "kotoyose-agent-home-b").is_symlink()
    assert (outside / "keep.txt").read_text() == "outside"
    assert (root / "kotoyose-agent-home-file").is_file()
    assert (root / "unrelated").is_dir()


def test_home_cleanup_refuses_symlink_root_and_dangerous_roots(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError):
        remove_leftover_homes(link)
    with pytest.raises(ValueError):
        remove_leftover_homes(Path("/"))
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    with pytest.raises(ValueError):
        remove_leftover_homes(fake_home)


@pytest.mark.skipif(os.environ.get("KOTOYOSE_DOCKER_IT") != "1", reason="set KOTOYOSE_DOCKER_IT=1 to run against a real Docker daemon")
async def test_real_container_is_removed_by_recovery(tmp_path, monkeypatch):
    image = os.environ.get("KOTOYOSE_DOCKER_IT_IMAGE", REAL_IMAGE_ID)
    base = Path(os.environ.get("KOTOYOSE_DOCKER_IT_DIR", str(tmp_path)))
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="c5it-", dir=base))
    workspace, home = root / "workspace", root / "home"
    workspace.mkdir()
    home.mkdir()
    job = f"it-rec-{uuid.uuid4().hex[:12]}"
    instance = "itrec"  # 本番 Kotoyose の container と混ざらないよう DEFAULT_INSTANCE は使わない。
    monkeypatch.setattr(ac, "DEFAULT_INSTANCE", instance)
    container = AgentContainer(instance=instance)
    spec = AgentContainerSpec(
        job_id=job, image_id=image, workspace=workspace, home=home, agent_root=None, egress_dir=None,
        wall_seconds=120, memory_bytes=256 * 1024 * 1024, cpus=0.5, pids_limit=64,
    )
    try:
        container_id = await container.create(spec)
        assert await container.exists(job) is True
        identity = {"name": ac.CONTAINER_PREFIX + job, "id": container_id, "instance": instance}
        service = _service(operations=[_operation(identity)])
        attempt = {"id": job, "generation": 0, "snapshot": _codex_container_attempt()["snapshot"]}
        assert await service._stop_attempt(attempt) == "confirmed"
        assert await container.exists(job) is False
    finally:
        await container.remove(job, 0)
        shutil.rmtree(root, ignore_errors=True)
