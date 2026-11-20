"""部屋の同時数と workspace の空きで、attempt を作る前に受付を断る。"""

import os
from pathlib import Path

import asyncpg
import pytest

from kotoyose.agent_container import AgentRoomCapacity, filesystem_free_bytes, refuse_agent_room
from kotoyose.codex_coding_policy import HOST_MODE, READ_MODE
from kotoyose.execution_resources import AgentContainerConfig, ExecutionError
from kotoyose.execution_store import ExecutionStore
from tests.test_execution_store import _TABLES, _admit_ready, _database_name


def _limits(**overrides) -> AgentContainerConfig:
    values = dict(
        agent_root="/opt/agent", home_root="/var/lib/kotoyose/home",
        max_containers=2, min_free_disk_bytes=1000,
    )
    values.update(overrides)
    return AgentContainerConfig(**values)


def _capacity(running: int, free: int, **overrides) -> AgentRoomCapacity:
    seen = {}

    async def count():
        return running

    def free_bytes(path: str) -> int:
        seen["path"] = path
        return free

    capacity = AgentRoomCapacity(_limits(**overrides), running=count, free_bytes=free_bytes)
    capacity.seen = seen
    return capacity


def test_agent_room_defaults_are_two_cpus_and_six_gib_and_overridable():
    config = AgentContainerConfig(agent_root="/opt/agent", home_root="/var/lib/kotoyose/home")
    assert config.cpus == 2
    assert config.workspace_memory_bytes == 6 * 1024**3
    overridden = AgentContainerConfig(
        agent_root="/opt/agent", home_root="/var/lib/kotoyose/home", cpus=3.5, workspace_memory_bytes=1024,
    )
    assert overridden.cpus == 3.5
    assert overridden.workspace_memory_bytes == 1024


def test_filesystem_free_bytes_matches_statvfs(tmp_path):
    info = os.statvfs(tmp_path)
    assert filesystem_free_bytes(str(tmp_path)) == info.f_bavail * info.f_frsize


def test_refuse_agent_room_reports_containers_before_disk():
    limits = _limits(max_containers=2, min_free_disk_bytes=1000)
    with pytest.raises(ExecutionError) as caught:
        refuse_agent_room(2, 0, limits)
    assert (caught.value.code, caught.value.status, caught.value.reason_code) == (
        "capacity_exhausted", 429, "containers")


def test_refuse_agent_room_reports_disk_when_containers_have_room():
    limits = _limits(max_containers=2, min_free_disk_bytes=1000)
    with pytest.raises(ExecutionError) as caught:
        refuse_agent_room(1, 999, limits)
    assert (caught.value.code, caught.value.status, caught.value.reason_code) == (
        "capacity_exhausted", 429, "disk")


def test_refuse_agent_room_allows_below_both_limits():
    limits = _limits(max_containers=2, min_free_disk_bytes=1000)
    refuse_agent_room(1, 1000, limits)


async def test_docker_listing_failure_is_containers_unknown():
    """docker の列挙に失敗したら、未分類の例外ではなく満杯と同じ待ちにする。"""
    from kotoyose.agent_container import AgentContainerError

    class _Listing:
        async def running_jobs(self):
            raise AgentContainerError("docker ps failed")

    capacity = AgentRoomCapacity(_limits(), container=_Listing(), free_bytes=lambda path: 1000)
    with pytest.raises(ExecutionError) as caught:
        await capacity("/workspace")
    assert (caught.value.code, caught.value.status, caught.value.reason_code) == (
        "capacity_exhausted", 429, "containers_unknown")


async def test_statvfs_oserror_is_disk_unknown(monkeypatch):
    """statvfs が失敗したら、未分類の例外ではなく disk 不明の待ちにする。"""
    async def running():
        return 0

    def broken(path):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr("kotoyose.agent_container.os.statvfs", broken)
    capacity = AgentRoomCapacity(_limits(), running=running)
    with pytest.raises(ExecutionError) as caught:
        await capacity("/workspace")
    assert (caught.value.code, caught.value.status, caught.value.reason_code) == (
        "capacity_exhausted", 429, "disk_unknown")


@pytest.fixture
async def store():
    url = os.environ.get("KOTOYOSE_EXECUTION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("KOTOYOSE_EXECUTION_TEST_DATABASE_URL is not set")
    database = _database_name(url)
    if not database.startswith("ahs_execution_test"):
        pytest.fail("refusing to connect: database name must start with ahs_execution_test")
    pool = await asyncpg.create_pool(url, min_size=1, max_size=4)
    try:
        await pool.execute("CREATE SCHEMA IF NOT EXISTS kotoyose")
        result = ExecutionStore(pool)
        await result.initialize()
        await pool.execute("TRUNCATE " + ", ".join(f"kotoyose.{name}" for name in _TABLES) + " CASCADE")
        yield result
    finally:
        await pool.execute("DROP TABLE IF EXISTS " + ", ".join(f"kotoyose.{name}" for name in _TABLES) + " CASCADE")
        await pool.close()


def _host_snapshot(snapshot: dict) -> dict:
    return {**snapshot, "codex_execution_mode": HOST_MODE}


async def _attempt_count(store: ExecutionStore) -> int:
    return await store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts")


async def test_full_containers_refuse_before_an_attempt_row(store, tmp_path):
    registered, _, request, snapshot = await _admit_ready(store, tmp_path)
    store.agent_room_capacity = _capacity(2, 1000, max_containers=2)
    with pytest.raises(ExecutionError) as caught:
        await store.admit("caller-a", request, _host_snapshot(snapshot), 1)
    assert (caught.value.code, caught.value.status, caught.value.reason_code) == (
        "capacity_exhausted", 429, "containers")
    assert await _attempt_count(store) == 0
    assert await store.pool.fetchval(
        "SELECT writer_attempt FROM kotoyose.execution_workspaces WHERE id=$1", registered["id"],
    ) is None
    assert store.agent_room_capacity.seen["path"] == registered["path"]


async def test_low_disk_refuses_before_an_attempt_row(store, tmp_path):
    registered, _, request, snapshot = await _admit_ready(store, tmp_path)
    store.agent_room_capacity = _capacity(0, 999, min_free_disk_bytes=1000)
    with pytest.raises(ExecutionError) as caught:
        await store.admit("caller-a", request, _host_snapshot(snapshot), 1)
    assert (caught.value.code, caught.value.status, caught.value.reason_code) == (
        "capacity_exhausted", 429, "disk")
    assert await _attempt_count(store) == 0
    assert store.agent_room_capacity.seen["path"] == registered["path"]


async def test_below_limits_admission_creates_an_attempt(store, tmp_path):
    _, _, request, snapshot = await _admit_ready(store, tmp_path)
    store.agent_room_capacity = _capacity(1, 1000, max_containers=2, min_free_disk_bytes=1000)
    attempt, created = await store.admit("caller-a", request, _host_snapshot(snapshot), 1)
    assert created
    assert await _attempt_count(store) == 1
    assert attempt["snapshot"]["codex_execution_mode"] == HOST_MODE


async def test_unfinished_room_counts_even_when_docker_reports_none(store, tmp_path):
    """container が立つ前の attempt も枠を使う。docker の 0 だけで次の受付を通さない。"""
    _, _, request, snapshot = await _admit_ready(store, tmp_path)
    store.agent_room_capacity = _capacity(0, 1000, max_containers=1, min_free_disk_bytes=1000)
    first, created = await store.admit("caller-a", request, _host_snapshot(snapshot), 4)
    assert created
    other = tmp_path / "other"
    other.mkdir()
    registered = await store.register_workspace("caller-a", "other-room", other)
    second = {
        **request,
        "idempotency_key": "key-2",
        "workspace_ref": f"workspace:{registered['id']}:{registered['generation']}",
    }
    other_snapshot = {
        "workspace_id": registered["id"], "generation": registered["generation"],
        "workspace_path": str(other), "codex_execution_mode": HOST_MODE,
    }
    with pytest.raises(ExecutionError) as caught:
        await store.admit("caller-a", second, other_snapshot, 4)
    assert (caught.value.code, caught.value.status, caught.value.reason_code) == (
        "capacity_exhausted", 429, "containers")
    assert await _attempt_count(store) == 1
    assert first["id"]


async def test_read_room_uses_the_same_container_capacity(store, tmp_path):
    """read は coding と同じ枠。満杯なら同じ 429 containers で、attempt 行は作らない。"""
    _, _, request, snapshot = await _admit_ready(store, tmp_path)
    store.agent_room_capacity = _capacity(0, 1000, max_containers=1, min_free_disk_bytes=1000)
    first, created = await store.admit("caller-a", request, {**snapshot, "codex_execution_mode": READ_MODE}, 4)
    assert created and first["snapshot"]["codex_execution_mode"] == READ_MODE
    other = tmp_path / "other"
    other.mkdir()
    registered = await store.register_workspace("caller-a", "other-room", other)
    second = {
        **request, "idempotency_key": "key-2",
        "workspace_ref": f"workspace:{registered['id']}:{registered['generation']}",
    }
    other_snapshot = {
        "workspace_id": registered["id"], "generation": registered["generation"],
        "workspace_path": str(other), "codex_execution_mode": HOST_MODE,
    }
    with pytest.raises(ExecutionError) as caught:
        await store.admit("caller-a", second, other_snapshot, 4)
    assert (caught.value.code, caught.value.status, caught.value.reason_code) == (
        "capacity_exhausted", 429, "containers")
    assert await _attempt_count(store) == 1


async def test_command_admission_does_not_consult_agent_room_capacity(store, tmp_path):
    _, _, request, snapshot = await _admit_ready(store, tmp_path)

    async def refuse(path, *, reserved=0):
        raise AssertionError("command admission must not consult agent room capacity")

    store.agent_room_capacity = refuse
    attempt, created = await store.admit("caller-a", request, snapshot, 1)
    assert created and attempt["id"]


async def test_replay_of_an_admitted_room_does_not_recheck_capacity(store, tmp_path):
    _, _, request, snapshot = await _admit_ready(store, tmp_path)
    host = _host_snapshot(snapshot)
    store.agent_room_capacity = _capacity(0, 1000, max_containers=1)
    attempt, created = await store.admit("caller-a", request, host, 1)
    assert created

    async def refuse(path, *, reserved=0):
        raise AssertionError("replay must not consult agent room capacity")

    store.agent_room_capacity = refuse
    replay, again = await store.admit("caller-a", request, host, 1)
    assert replay["id"] == attempt["id"] and not again
    assert await _attempt_count(store) == 1
