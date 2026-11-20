"""Tests for the shared `JailPlace` container lifecycle and helper layer."""

from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from kotoyose.agent_container import JOB_ID_RE
from kotoyose.execution_resources import AgentContainerConfig
from kotoyose.jail_place import (
    CONTAINER_HOME,
    CONTAINER_RELAY,
    CONTAINER_SOCKET,
    CONTAINER_WORKSPACE,
    PROXY_URL,
    JailPlace,
    bounded_limit,
    build_connect_proxy_env,
    build_relay_exec_argv,
    compute_wall_seconds,
    container_identity,
    container_job_id_for,
    create_egress_socket_dir,
    protected_ro_dirs,
    reap_late_process,
    remove_container_confirmed,
)
from kotoyose.print_cli_egress import MAX_SOCKET_PATH_BYTES


def test_container_job_id_for_normal_and_hash_fallback() -> None:
    normal = container_job_id_for("attempt-123", 4)
    assert normal == "attempt-123-g4"
    assert JOB_ID_RE.fullmatch(normal)

    oversized = container_job_id_for("x" * 70, 1)
    assert oversized.startswith("job-")
    assert len(oversized) == 44
    assert JOB_ID_RE.fullmatch(oversized)
    assert oversized != container_job_id_for("x" * 70, 2)


def test_create_egress_socket_dir_falls_back_to_tmp_when_root_path_too_long(tmp_path: Path) -> None:
    long_root = tmp_path / ("a" * 95)
    long_root.mkdir()
    sock_dir = create_egress_socket_dir(long_root, prefix="kotoyose-jp-")
    try:
        assert sock_dir.parent == Path("/tmp")
        assert len(str(sock_dir / "egress.sock").encode("utf-8")) <= MAX_SOCKET_PATH_BYTES
        assert not any(long_root.iterdir())
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)

    missing_root = tmp_path / "does-not-exist"
    fallback_dir = create_egress_socket_dir(missing_root, prefix="kotoyose-jp-")
    try:
        assert fallback_dir.parent == Path("/tmp")
    finally:
        shutil.rmtree(fallback_dir, ignore_errors=True)


def test_build_connect_proxy_env_and_relay_exec_argv() -> None:
    env = build_connect_proxy_env({"HOME": CONTAINER_HOME, "LANG": "C.UTF-8"})
    assert env == {
        "HOME": CONTAINER_HOME,
        "LANG": "C.UTF-8",
        "HTTPS_PROXY": PROXY_URL,
        "HTTP_PROXY": PROXY_URL,
        "NO_PROXY": "",
    }
    argv = build_relay_exec_argv("/opt/agent/bin/claude", ["-p", "--output-format", "stream-json"])
    assert argv == [
        "python3",
        CONTAINER_RELAY,
        CONTAINER_SOCKET,
        "--",
        "/opt/agent/bin/claude",
        "-p",
        "--output-format",
        "stream-json",
    ]


def test_jail_place_build_spec_applies_bounded_limits_and_wall_seconds(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    egress_dir = tmp_path / "eg"
    cli = tmp_path / "cli"
    workspace.mkdir()
    home.mkdir()
    egress_dir.mkdir()
    cli.write_text("#!/bin/sh\n")
    settings = AgentContainerConfig(
        agent_root="/opt/agent-root",
        home_root=str(tmp_path),
        workspace_memory_bytes=4 * 1024**3,
        cpus=2.0,
        pids_limit=256,
    )
    place = JailPlace(container=None, settings=settings, image_id="sha256:" + ("a" * 64))

    assert bounded_limit(0, 512) == 512
    assert bounded_limit(1024, 512) == 512
    assert bounded_limit(128, 512) == 128
    assert compute_wall_seconds(10.2, 0.5) == 71

    ro_dirs = protected_ro_dirs([{"name": ".git"}, {"name": ".harness"}], workspace)
    assert ro_dirs == {
        f"{CONTAINER_WORKSPACE}/.git": workspace / ".git",
        f"{CONTAINER_WORKSPACE}/.harness": workspace / ".harness",
    }

    spec = place.build_spec(
        job_id="att-1-g0",
        workspace=workspace,
        home=home,
        egress_dir=egress_dir,
        timeout_seconds=10.2,
        grace_seconds=0.5,
        env={"LANG": "C.UTF-8"},
        ro_files={"/opt/agent/bin/cli": cli},
        ro_dirs=ro_dirs,
        workspace_readonly=True,
        memory_bytes=8 * 1024**3,
        cpus=1.5,
        pids_limit=0,
    )
    assert spec.wall_seconds == 71
    assert spec.memory_bytes == 4 * 1024**3
    assert spec.cpus == 1.5
    assert spec.pids_limit == 256
    assert spec.workspace_readonly is True
    assert spec.ro_dirs == ro_dirs
    assert spec.rw_dirs == {}


def test_cleanup_run_dirs_respects_stop_confirmation_and_egress_closed(tmp_path: Path) -> None:
    def make_dirs(tag: str) -> tuple[Path, Path, Path]:
        home = tmp_path / f"{tag}-home"
        sock = tmp_path / f"{tag}-sock"
        extra = tmp_path / f"{tag}-extra"
        for d in (home, sock, extra):
            d.mkdir()
            (d / "marker").write_text("keep")
        return home, sock, extra

    h1, s1, e1 = make_dirs("unconfirmed")
    JailPlace.cleanup_run_dirs(
        container_confirmation="unconfirmed",
        egress_closed=True,
        temp_home=h1,
        socket_dir=s1,
        extra_dirs=(e1,),
    )
    assert h1.exists() and s1.exists() and not e1.exists()

    h2, s2, e2 = make_dirs("egress-open")
    JailPlace.cleanup_run_dirs(
        container_confirmation="confirmed",
        egress_closed=False,
        temp_home=h2,
        socket_dir=s2,
        extra_dirs=(e2,),
    )
    assert not h2.exists() and s2.exists() and not e2.exists()

    h3, s3, e3 = make_dirs("confirmed")
    JailPlace.cleanup_run_dirs(
        container_confirmation="confirmed",
        egress_closed=True,
        temp_home=h3,
        socket_dir=s3,
        extra_dirs=(e3,),
    )
    assert not h3.exists() and not s3.exists() and not e3.exists()


async def test_container_helpers_identity_remove_and_late_reap() -> None:
    class RaisingContainer:
        instance = "kotoyose"

        async def remove(self, job_id: str, grace_seconds: int) -> str:
            raise RuntimeError("docker unavailable")

    c = RaisingContainer()
    assert container_identity(c, "job-1", "cid-1") == {"name": "kotoyose-agent-job-1", "id": "cid-1", "instance": "kotoyose"}
    assert await remove_container_confirmed(c, "job-1", 0) == "unconfirmed"

    killed = []
    proc = SimpleNamespace(returncode=None, kill=lambda: killed.append(True))

    async def late_proc():
        return proc

    task = asyncio.create_task(late_proc())
    await task
    reap_late_process(task)
    assert killed == [True]
    with pytest.raises(ValueError):
        JailPlace(container=None, settings=None, image_id=None).build_spec(
            job_id="x",
            workspace=Path("/tmp"),
            home=Path("/tmp"),
            egress_dir=None,
            timeout_seconds=1.0,
            grace_seconds=0.0,
            env={},
            ro_files={},
        )
