"""AgentContainer の argv、secret の経路、停止確認、掃除を fake docker で検証する。

実 Docker は使わない。KOTOYOSE_DOCKER_IT=1 のときだけ実 daemon で一周する統合テストを行う。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from kotoyose import agent_container as ac
from kotoyose.agent_container import (
    AgentContainer,
    AgentContainerOutputLimit,
    AgentContainerOwnershipError,
    AgentContainerSpec,
    AgentContainerTimeout,
    AgentContainerError,
)
from kotoyose.agent_container import INSTANCE_LABEL


IMAGE_ID = "sha256:" + "a" * 64
REAL_IMAGE_ID = "sha256:f97cd896d8d896dc21211ad9f6fe62ad21050b3c132b7c3a4bd2a46f02269713"
CONTAINER_ID = "c" * 64
INSTANCE = "t1"


class _Stream:
    def __init__(self, data: bytes = b"", *, block: bool = False) -> None:
        self._data = bytearray(data)
        self._block = block

    async def read(self, size: int) -> bytes:
        if self._block:
            await asyncio.Event().wait()
        if not self._data:
            return b""
        chunk = bytes(self._data[:size])
        del self._data[:size]
        return chunk


class _FakeProcess:
    def __init__(self, *, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"", block: bool = False) -> None:
        self.returncode: int | None = None if block else returncode
        self.stdout = _Stream(stdout, block=block)
        self.stderr = _Stream(stderr, block=block)
        self._stdout = stdout
        self._stderr = stderr
        self._exited = asyncio.Event()
        if not block:
            self._exited.set()
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    async def wait(self) -> int:
        await self._exited.wait()
        return self.returncode if self.returncode is not None else -9

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._exited.set()


class FakeDocker:
    """docker の状態を持つ fake。containers は ID をキーにする。"""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict]] = []
        self.containers: dict[str, dict] = {}
        self.rm_fails = False
        self.inspect_error = False
        self.exec_process: _FakeProcess | None = None
        self.exec_kwargs: dict | None = None
        self.exec_argv: tuple[str, ...] | None = None
        self.ps_stdout: bytes | None = None
        self.image_env_stdout: bytes = b"[]"

    def add(self, name: str, job_id: str | None, *, labels: dict | None = None, running: bool = False) -> str:
        container_id = (uuid.uuid4().hex * 2)[:64]
        all_labels = {INSTANCE_LABEL: INSTANCE}
        all_labels.update(labels or {})
        if job_id is not None:
            all_labels[ac.JOB_LABEL] = job_id
        self.containers[container_id] = {"id": container_id, "name": name, "labels": all_labels, "running": running}
        return container_id

    def _find(self, ref: str) -> dict | None:
        for item in self.containers.values():
            if item["name"] == ref or item["id"] == ref:
                return item
        return None

    async def __call__(self, program: str, *args: str, **kwargs) -> _FakeProcess:
        self.calls.append((args, kwargs))
        command = args[0]
        if command == "image":
            return _FakeProcess(stdout=self.image_env_stdout)
        if command == "run":
            name = args[args.index("--name") + 1]
            labels = {}
            for index, token in enumerate(args):
                if token == "--label":
                    key, _, value = args[index + 1].partition("=")
                    labels[key] = value
            if any(item["name"] == name for item in self.containers.values()):
                return _FakeProcess(returncode=125, stderr=b"name already in use")
            container_id = self.add(name, labels.get(ac.JOB_LABEL), labels={
                k: v for k, v in labels.items() if k != ac.JOB_LABEL
            }, running=True)
            return _FakeProcess(stdout=(container_id + "\n").encode())
        if command == "inspect":
            if self.inspect_error:
                return _FakeProcess(returncode=1, stderr=b"Cannot connect to the Docker daemon")
            item = self._find(args[-1])
            if item is None:
                return _FakeProcess(returncode=1, stderr=f"Error response from daemon: No such container: {args[-1]}".encode())
            state = {"Running": item["running"], "Status": "running" if item["running"] else "exited"}
            return _FakeProcess(stdout=json.dumps({
                "Id": item["id"], "State": state, "Config": {"Labels": item["labels"]},
            }).encode())
        if command == "stop":
            item = self._find(args[-1])
            if item is not None:
                item["running"] = False
            return _FakeProcess(stdout=b"ok\n")
        if command == "rm":
            item = self._find(args[-1])
            if item is None:
                return _FakeProcess(returncode=1, stderr=b"No such container")
            if self.rm_fails:
                return _FakeProcess(returncode=1, stderr=b"device busy")
            self.containers.pop(item["id"])
            return _FakeProcess(stdout=b"removed\n")
        if command == "ps":
            assert "--no-trunc" not in args
            assert f"label={ac.JOB_LABEL}" in args
            instance_filter = f"label={INSTANCE_LABEL}={INSTANCE}"
            assert instance_filter in args, "sweep must filter by instance label"
            if self.ps_stdout is not None:
                return _FakeProcess(stdout=self.ps_stdout)
            lines = []
            for item in self.containers.values():
                if ac.JOB_LABEL in item["labels"] and item["labels"].get(INSTANCE_LABEL) == INSTANCE:
                    lines.append(f"{item['name']} {item['labels'][ac.JOB_LABEL]}")
            return _FakeProcess(stdout=("\n".join(lines) + "\n").encode() if lines else b"")
        if command == "exec":
            self.exec_argv = args
            self.exec_kwargs = kwargs
            return self.exec_process or _FakeProcess(stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected docker command: {args}")


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(ac.asyncio, "create_subprocess_exec", fake)
    return fake


@pytest.fixture
def dirs(tmp_path: Path) -> dict[str, Path]:
    values = {}
    for name in ("workspace", "home", "agent", "egress"):
        path = tmp_path / name
        path.mkdir()
        values[name] = path
    return values


def _spec(dirs: dict[str, Path], **overrides) -> AgentContainerSpec:
    values = dict(
        job_id="job-1",
        image_id=IMAGE_ID,
        workspace=dirs["workspace"],
        home=dirs["home"],
        agent_root=dirs["agent"],
        egress_dir=None,
        wall_seconds=600,
        memory_bytes=512 * 1024 * 1024,
        cpus=1.5,
        pids_limit=128,
        env={"LANG": "C.UTF-8"},
    )
    values.update(overrides)
    return AgentContainerSpec(**values)


def _mounts(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, token in enumerate(argv) if token == "--mount"]


def _create_argv(docker: FakeDocker) -> list[str]:
    args, _ = docker.calls[-1]
    assert args[0] == "run"
    return list(args)


async def test_create_argv_has_isolation_flags_and_entrypoint(docker, dirs):
    container = AgentContainer(instance=INSTANCE)
    container_id = await container.create(_spec(dirs))

    assert container_id == next(iter(docker.containers))
    argv = _create_argv(docker)
    assert "--init" in argv
    assert f"{INSTANCE_LABEL}={INSTANCE}" in argv
    assert argv[argv.index("--entrypoint") : argv.index("--entrypoint") + 3] == ["--entrypoint", "sleep", IMAGE_ID]
    assert argv[-1] == "600"
    assert argv[argv.index("--name") + 1] == "kotoyose-agent-job-1"
    assert "--pull=never" in argv and "--restart=no" in argv
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert argv[argv.index("--memory") + 1] == argv[argv.index("--memory-swap") + 1] == str(512 * 1024 * 1024)
    assert argv[argv.index("--pids-limit") + 1] == "128"
    assert "kotoyose.agent_job=job-1" in argv
    assert argv[argv.index("--label") + 1] == "kotoyose.agent_job=job-1"
    assert "--env" in argv and "HOME=/home/agent" in argv and "LANG=C.UTF-8" in argv


async def test_create_mounts_are_bind_with_readonly_for_agent_root_and_egress(docker, dirs):
    container = AgentContainer(instance=INSTANCE)
    await container.create(_spec(dirs, egress_dir=dirs["egress"]))

    mounts = _mounts(_create_argv(docker))
    ws = str(dirs["workspace"].resolve())
    home = str(dirs["home"].resolve())
    agent = str(dirs["agent"].resolve())
    egress = str(dirs["egress"].resolve())
    assert f"type=bind,src={ws},dst=/workspace" in mounts
    assert f"type=bind,src={ws},dst=/workspace,readonly" not in mounts
    assert f"type=bind,src={home},dst=/home/agent" in mounts
    assert f"type=bind,src={agent},dst=/opt/agent,readonly" in mounts
    assert f"type=bind,src={egress},dst=/run/egress,readonly" in mounts


async def test_read_room_workspace_mount_is_readonly_and_home_stays_writable(docker, dirs):
    """write は /workspace を rw、read は ro。どちらも /home/agent は rw。"""
    container = AgentContainer(instance=INSTANCE)
    await container.create(_spec(dirs))
    mounts = _mounts(_create_argv(docker))
    ws = str(dirs["workspace"].resolve())
    home = str(dirs["home"].resolve())
    assert f"type=bind,src={ws},dst=/workspace" in mounts
    assert f"type=bind,src={home},dst=/home/agent" in mounts
    docker.calls.clear()
    await container.create(_spec(dirs, job_id="job-2", workspace_readonly=True))
    mounts = _mounts(_create_argv(docker))
    assert f"type=bind,src={ws},dst=/workspace,readonly" in mounts
    assert f"type=bind,src={home},dst=/home/agent" in mounts
    assert f"type=bind,src={home},dst=/home/agent,readonly" not in mounts


async def test_create_without_egress_has_no_egress_mount(docker, dirs):
    await AgentContainer(instance=INSTANCE).create(_spec(dirs))
    assert not any("/run/egress" in mount for mount in _mounts(_create_argv(docker)))


async def test_create_rejects_secret_looking_env_without_calling_docker(docker, dirs):
    container = AgentContainer(instance=INSTANCE)
    for name in ("ANTHROPIC_API_KEY", "github_token", "DB_SECRET", "x_password", "DB_PASSWD", "AUTH_HEADER",
                 "SESSION_COOKIE", "BEARER_X", "PG_DSN", "PRIVATE_PEM", "GIT_CRED"):
        with pytest.raises(ValueError, match="secret"):
            await container.create(_spec(dirs, env={name: "value-must-not-leak"}))
    assert docker.calls == []


async def test_create_rejects_bad_mount_paths(docker, dirs, tmp_path):
    container = AgentContainer(instance=INSTANCE)
    comma = tmp_path / "a,b"
    comma.mkdir()
    link = tmp_path / "link"
    link.symlink_to(dirs["workspace"])
    cases = {
        "comma": _spec(dirs, workspace=comma),
        "symlink": _spec(dirs, workspace=link),
        "root": _spec(dirs, workspace=Path("/")),
        "missing": _spec(dirs, agent_root=tmp_path / "does-not-exist"),
    }
    for spec in cases.values():
        with pytest.raises(ValueError):
            await container.create(spec)
    assert docker.calls == []


async def test_create_rejects_tagged_image_and_bad_job_id(docker, dirs):
    container = AgentContainer(instance=INSTANCE)
    with pytest.raises(ValueError):
        await container.create(_spec(dirs, image_id="taskhub/verification:latest"))
    with pytest.raises(ValueError):
        await container.create(_spec(dirs, job_id="bad/id"))
    with pytest.raises(ValueError):
        await container.create(_spec(dirs, job_id="x" * 65))
    assert docker.calls == []


async def test_create_failure_reports_exit_code_only(docker, dirs):
    async def failing(program, *args, **kwargs):
        docker.calls.append((args, kwargs))
        return _FakeProcess(returncode=125, stderr=b"LANG=super-secret-value bad")

    with patch.object(ac.asyncio, "create_subprocess_exec", failing):
        with pytest.raises(AgentContainerError) as info:
            await AgentContainer(instance=INSTANCE).create(_spec(dirs))
    assert "super-secret-value" not in str(info.value)


async def test_exec_puts_secret_names_in_argv_and_values_only_in_child_env(docker):
    docker.exec_process = _FakeProcess(stdout=b"")
    container = AgentContainer(instance=INSTANCE)
    process = await container.exec(
        "job-1",
        ["/opt/agent/bin/claude", "--print"],
        env={"KOTOYOSE_MODE": "fast"},
        secret_env={"ANTHROPIC_API_KEY": "sk-live-value-123"},
        workdir="/workspace",
    )
    assert process is docker.exec_process

    argv = list(docker.exec_argv)
    assert argv[:2] == ["exec", "-i"]
    assert "ANTHROPIC_API_KEY" in argv
    assert argv[argv.index("ANTHROPIC_API_KEY") - 1] == "-e"
    assert "KOTOYOSE_MODE=fast" in argv
    assert argv[argv.index("--workdir") + 1] == "/workspace"
    assert argv[argv.index("kotoyose-agent-job-1") + 1 :] == ["/opt/agent/bin/claude", "--print"]
    assert not any("sk-live-value-123" in item for item in argv)

    child_env = docker.exec_kwargs["env"]
    assert child_env["ANTHROPIC_API_KEY"] == "sk-live-value-123"
    assert docker.exec_kwargs["stdin"] == asyncio.subprocess.PIPE


async def test_exec_without_secret_inherits_environment(docker):
    await AgentContainer(instance=INSTANCE).exec("job-1", ["true"], stdin=False)
    assert docker.exec_kwargs.get("env") is None
    assert docker.exec_kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert "-i" not in docker.exec_argv


async def test_exec_rejects_secret_looking_plain_env_and_overlap(docker):
    container = AgentContainer(instance=INSTANCE)
    with pytest.raises(ValueError):
        await container.exec("job-1", ["true"], env={"MY_TOKEN": "v"})
    with pytest.raises(ValueError):
        await container.exec("job-1", ["true"], env={"A": "1"}, secret_env={"A": "2"})
    with pytest.raises(ValueError):
        await container.exec("bad job", ["true"])
    assert docker.calls == []


async def test_run_returns_outputs_and_exit_code(docker):
    docker.exec_process = _FakeProcess(returncode=0, stdout=b"out-data", stderr=b"err-data")
    rc, out, err = await AgentContainer(instance=INSTANCE).run("job-1", ["pnpm", "-v"], timeout=5)
    assert (rc, out, err) == (0, b"out-data", b"err-data")


async def test_run_timeout_kills_exec_and_raises(docker):
    docker.exec_process = _FakeProcess(block=True)
    with pytest.raises(AgentContainerTimeout):
        await AgentContainer(instance=INSTANCE).run("job-1", ["sleep", "100"], timeout=0.05)
    assert docker.exec_process.killed


async def test_run_output_cap_is_enforced_per_stream(docker):
    docker.exec_process = _FakeProcess(returncode=0, stdout=b"x" * (1024 * 1024 + 1))
    with pytest.raises(AgentContainerOutputLimit):
        await AgentContainer(instance=INSTANCE).run("job-1", ["yes"], timeout=5)


async def test_run_at_exact_cap_is_allowed(docker):
    docker.exec_process = _FakeProcess(returncode=0, stdout=b"x" * (1024 * 1024))
    rc, out, _ = await AgentContainer(instance=INSTANCE).run("job-1", ["true"], timeout=5)
    assert rc == 0 and len(out) == 1024 * 1024


async def test_remove_confirms_when_final_inspect_reports_missing(docker):
    container_id = docker.add("kotoyose-agent-job-1", "job-1", running=True)
    assert await AgentContainer(instance=INSTANCE).remove("job-1", 2) == "confirmed"
    assert container_id not in docker.containers
    stop = [args for args, _ in docker.calls if args[0] == "stop"]
    assert stop == [("stop", "--time", "2", container_id)]


async def test_remove_is_confirmed_when_nothing_exists(docker):
    assert await AgentContainer(instance=INSTANCE).remove("job-1", 0) == "confirmed"


async def test_remove_unconfirmed_when_rm_fails_and_container_survives(docker):
    docker.add("kotoyose-agent-job-1", "job-1", running=True)
    docker.rm_fails = True
    assert await AgentContainer(instance=INSTANCE).remove("job-1", 0) == "unconfirmed"


async def test_remove_unconfirmed_when_final_inspect_errors(docker):
    docker.add("kotoyose-agent-job-1", "job-1", running=True)
    docker.inspect_error = True
    assert await AgentContainer(instance=INSTANCE).remove("job-1", 0) == "unconfirmed"


async def test_remove_unconfirmed_not_confirmed_by_inspect_error_on_absent_name(docker):
    docker.inspect_error = True
    assert await AgentContainer(instance=INSTANCE).remove("job-1", 0) == "unconfirmed"


async def test_remove_refuses_container_with_other_job_label(docker):
    container_id = docker.add("kotoyose-agent-job-1", "someone-else", running=True)
    with pytest.raises(AgentContainerOwnershipError):
        await AgentContainer(instance=INSTANCE).remove("job-1", 0)
    assert container_id in docker.containers
    assert not any(args[0] in {"stop", "rm"} for args, _ in docker.calls)


async def test_remove_refuses_container_without_label(docker):
    container_id = docker.add("kotoyose-agent-job-1", None, running=True)
    with pytest.raises(AgentContainerOwnershipError):
        await AgentContainer(instance=INSTANCE).remove("job-1", 0)
    assert container_id in docker.containers


async def test_exists_reports_presence_and_raises_on_inspect_failure(docker):
    container = AgentContainer(instance=INSTANCE)
    assert await container.exists("job-1") is False
    docker.add("kotoyose-agent-job-1", "job-1")
    assert await container.exists("job-1") is True
    docker.inspect_error = True
    with pytest.raises(AgentContainerError):
        await container.exists("job-1")


async def test_running_jobs_asks_docker_for_running_containers_only(docker):
    docker.ps_stdout = b"kotoyose-agent-live live\n"
    jobs = await AgentContainer(instance=INSTANCE).running_jobs()
    assert jobs == ["live"]
    args = docker.calls[-1][0]
    assert "status=running" in args
    assert "-a" not in args


async def test_sweep_removes_only_unkept_jobs(docker):
    keep_id = docker.add("kotoyose-agent-keep-me", "keep-me", running=True)
    gone_id = docker.add("kotoyose-agent-drop-me", "drop-me", running=True)
    docker.add("kotoyose-agent-unrelated", None)  # label なしは列挙されない
    docker.add("kotoyose-agent-renamed", "other-job", running=True)  # name と label がずれたものは触らない

    removed = await AgentContainer(instance=INSTANCE).sweep(lambda job: job == "keep-me")

    assert removed == ["drop-me"]
    assert keep_id in docker.containers
    assert gone_id not in docker.containers
    touched = {args[-1] for args, _ in docker.calls if args[0] in {"stop", "rm"}}
    assert touched == {gone_id}


def test_instance_must_be_valid():
    for bad in ("", "a" * 33, "bad/instance", "sp ace", None):
        with pytest.raises(ValueError):
            AgentContainer(instance=bad)
    assert AgentContainer(instance="a" * 32).instance == "a" * 32


async def test_exec_rejects_reserved_secret_names(docker):
    container = AgentContainer(instance=INSTANCE)
    for name in ("PATH", "DOCKER_HOST", "DOCKER_CONFIG"):
        with pytest.raises(ValueError):
            await container.exec("job-1", ["true"], secret_env={name: "v"})
    assert docker.calls == []


async def test_remove_refuses_container_from_another_instance(docker):
    container_id = docker.add("kotoyose-agent-job-1", "job-1", labels={INSTANCE_LABEL: "other"}, running=True)
    with pytest.raises(AgentContainerOwnershipError):
        await AgentContainer(instance=INSTANCE).remove("job-1", 0)
    assert container_id in docker.containers
    assert not any(args[0] in {"stop", "rm"} for args, _ in docker.calls)


async def test_remove_uses_force_and_drops_volumes(docker):
    container_id = docker.add("kotoyose-agent-job-1", "job-1", running=True)
    await AgentContainer(instance=INSTANCE).remove("job-1", 0)
    rm = [args for args, _ in docker.calls if args[0] == "rm"]
    assert rm == [("rm", "-f", "-v", container_id)]


async def test_sweep_skips_foreign_ownership_and_continues(docker):
    docker.add("kotoyose-agent-aaa", "aaa", running=True)
    docker.add("kotoyose-agent-bbb", "bbb", running=True)
    real_remove = AgentContainer.remove

    async def remove(self, job_id, grace_seconds):
        if job_id == "aaa":
            raise AgentContainerOwnershipError("foreign")
        return await real_remove(self, job_id, grace_seconds)

    with patch.object(AgentContainer, "remove", remove):
        removed = await AgentContainer(instance=INSTANCE).sweep(lambda job: False)
    assert removed == ["bbb"]


async def test_sweep_output_is_capped_at_8_mib(docker):
    docker.ps_stdout = b"x" * (8 * 1024 * 1024 + 1)
    with pytest.raises(AgentContainerError):
        await AgentContainer(instance=INSTANCE).sweep(lambda job: True)
    assert not any(args[0] in {"stop", "rm"} for args, _ in docker.calls)


async def test_bad_job_id_rejected_before_docker(docker):
    with pytest.raises(ValueError):
        await AgentContainer(instance=INSTANCE).remove("../escape", 0)
    with pytest.raises(ValueError):
        await AgentContainer(instance=INSTANCE).exists("")
    assert docker.calls == []


@pytest.mark.skipif(os.environ.get("KOTOYOSE_DOCKER_IT") != "1", reason="set KOTOYOSE_DOCKER_IT=1 to run against a real Docker daemon")
async def test_real_container_lifecycle(tmp_path):
    image = os.environ.get("KOTOYOSE_DOCKER_IT_IMAGE", REAL_IMAGE_ID)
    dirs = {}
    for name in ("workspace", "home", "agent"):
        dirs[name] = tmp_path / name
        dirs[name].mkdir()
    job = f"it-{uuid.uuid4().hex[:12]}"
    container = AgentContainer(instance=INSTANCE)
    spec = AgentContainerSpec(
        job_id=job,
        image_id=image,
        workspace=dirs["workspace"],
        home=dirs["home"],
        agent_root=dirs["agent"],
        egress_dir=None,
        wall_seconds=120,
        memory_bytes=256 * 1024 * 1024,
        cpus=0.5,
        pids_limit=64,
    )
    try:
        await container.create(spec)
        rc, out, err = await container.run(job, ["bash", "-lc", "pnpm -v && git --version"], timeout=60)
        assert rc == 0, err
        assert b"git version" in out
    finally:
        confirmation = await container.remove(job, 1)
    assert confirmation == "confirmed"
    assert await container.exists(job) is False


async def test_extra_mounts_are_bound_in_their_reviewed_modes(docker, dirs, tmp_path):
    codex = tmp_path / "codex"
    codex.write_text("binary\n")
    relay = tmp_path / "relay.py"
    relay.write_text("relay\n")
    session = tmp_path / "session"
    session.mkdir()
    protected = dirs["workspace"] / ".codex"
    protected.mkdir()
    spec = _spec(
        dirs,
        ro_files={"/opt/agent/bin/codex": codex, "/opt/agent/egress_relay.py": relay},
        rw_dirs={"/home/agent/.codex/sessions": session},
        ro_dirs={"/workspace/.codex": protected},
    )
    await AgentContainer(instance=INSTANCE).create(spec)
    mounts = _mounts(_create_argv(docker))
    assert f"type=bind,src={codex.resolve()},dst=/opt/agent/bin/codex,readonly" in mounts
    assert f"type=bind,src={relay.resolve()},dst=/opt/agent/egress_relay.py,readonly" in mounts
    assert f"type=bind,src={session.resolve()},dst=/home/agent/.codex/sessions" in mounts
    assert f"type=bind,src={protected.resolve()},dst=/workspace/.codex,readonly" in mounts


@pytest.mark.parametrize("bad", [
    {"ro_files": {"/usr/bin/codex": "file"}},
    {"ro_files": {"/opt/agent/../etc/passwd": "file"}},
    {"ro_files": {"/opt/agent/bin/codex,readonly": "file"}},
    {"ro_files": {"/opt/agent/": "file"}},
    {"rw_dirs": {"/workspace/out": "dir"}},
    {"rw_dirs": {"/home/agent": "dir"}},
    {"ro_dirs": {"/workspace/a/b": "dir"}},
    {"ro_dirs": {"/workspace": "dir"}},
    {"ro_dirs": {"/home/agent/x": "dir"}},
])
async def test_extra_mount_targets_outside_their_area_are_rejected(docker, dirs, tmp_path, bad):
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (tmp_path / "file").write_text("x")
    translated = {}
    for key, mapping in bad.items():
        translated[key] = {target: (tmp_path / "file" if value == "file" else source)
                           for target, value in mapping.items()}
    with pytest.raises(ValueError):
        await AgentContainer(instance=INSTANCE).create(_spec(dirs, **translated))
    assert docker.calls == []


async def test_extra_mount_source_must_be_a_regular_file_or_directory(docker, dirs, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(dirs["agent"] / "missing-target")
    with pytest.raises(ValueError):
        await AgentContainer(instance=INSTANCE).create(_spec(dirs, ro_files={"/opt/agent/bin/codex": link}))
    with pytest.raises(ValueError):
        await AgentContainer(instance=INSTANCE).create(_spec(dirs, ro_files={"/opt/agent/bin/codex": dirs["agent"]}))
    assert docker.calls == []


async def test_exec_passes_stream_limit_only_when_requested(docker):
    container = AgentContainer(instance=INSTANCE)
    seen = []
    original = ac.asyncio.create_subprocess_exec

    async def capture(*args, **kwargs):
        seen.append(kwargs)
        return await original(*args, **kwargs)

    ac.asyncio.create_subprocess_exec = capture
    try:
        await container.exec("job-1", ["true"], limit=2 * 1024 * 1024)
        await container.exec("job-1", ["true"])
    finally:
        ac.asyncio.create_subprocess_exec = original
    assert seen[0]["limit"] == 2 * 1024 * 1024
    assert "limit" not in seen[1]


async def test_image_env_parses_env_and_rejects_tags_and_bad_output(docker):
    container = AgentContainer(instance=INSTANCE)
    docker.image_env_stdout = json.dumps(["PATH=/usr/bin:/bin", "LANG=C.UTF-8", "EMPTY="]).encode()
    assert await container.image_env(IMAGE_ID) == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "EMPTY": ""}
    assert docker.calls[-1][0] == ("image", "inspect", "--format", "{{json .Config.Env}}", IMAGE_ID)
    with pytest.raises(ValueError):
        await container.image_env("ubuntu:latest")
    for stdout in (b"not json", b'["NOEQUALS"]', b'[1]', b'{"a":1}'):
        docker.image_env_stdout = stdout
        with pytest.raises(ac.AgentContainerError):
            await container.image_env(IMAGE_ID)
    docker.image_env_stdout = b"null"
    assert await container.image_env(IMAGE_ID) == {}


@pytest.mark.skipif(os.environ.get("KOTOYOSE_DOCKER_IT") != "1", reason="set KOTOYOSE_DOCKER_IT=1 to run against a real Docker daemon")
async def test_real_codex_binary_runs_read_only_inside_container(tmp_path):
    import shutil
    from kotoyose import egress_relay

    image = os.environ.get("KOTOYOSE_DOCKER_IT_IMAGE", REAL_IMAGE_ID)
    binary_name = os.environ.get("KOTOYOSE_DOCKER_IT_CODEX") or shutil.which("codex")
    if not binary_name or not Path(binary_name).resolve().is_file():
        pytest.skip("KOTOYOSE_DOCKER_IT_CODEX must name a codex binary")
    binary = Path(binary_name).resolve()
    for name in ("workspace", "home"):
        (tmp_path / name).mkdir()
    job = f"it-{uuid.uuid4().hex[:12]}"
    container = AgentContainer(instance=INSTANCE)
    spec = AgentContainerSpec(
        job_id=job,
        image_id=image,
        workspace=tmp_path / "workspace",
        home=tmp_path / "home",
        agent_root=None,
        egress_dir=None,
        wall_seconds=120,
        memory_bytes=256 * 1024 * 1024,
        cpus=0.5,
        pids_limit=64,
        ro_files={"/opt/agent/bin/codex": binary, "/opt/agent/egress_relay.py": Path(egress_relay.__file__)},
    )
    try:
        await container.create(spec)
        rc, out, err = await container.run(job, ["/opt/agent/bin/codex", "--version"], timeout=60)
        assert rc == 0, err
        assert b"codex" in out.lower()
        # container coding と同じく app-server を stdio で起動し、initialize の応答が返ることを確かめる。
        process = await container.exec(
            job, ["/opt/agent/bin/codex", "app-server", "--listen", "stdio://"],
            env={"CODEX_HOME": "/home/agent/.codex"}, limit=1 << 20)
        try:
            request = {"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "kotoyose-it", "title": "kotoyose-it", "version": "0"}}}
            process.stdin.write((json.dumps(request) + "\n").encode())
            await process.stdin.drain()
            line = await asyncio.wait_for(process.stdout.readline(), 60)
            reply = json.loads(line)
            assert reply.get("id") == 1 and "result" in reply, reply
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
    finally:
        confirmation = await container.remove(job, 1)
    assert confirmation == "confirmed"
    assert await container.exists(job) is False
