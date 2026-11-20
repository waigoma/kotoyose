"""DockerLocalWorker の隔離、停止確認、失敗分類を検証する。"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kotoyose.local_worker import CommandJob, DockerLocalWorker
from kotoyose.execution_service import ExecutionService


IMAGE_ID = "sha256:" + "a" * 64


class _FakeStream:
    def __init__(self, chunks: list[bytes] | None = None, gate: asyncio.Event | None = None):
        self._chunks = list(chunks or [])
        self._gate = gate

    async def read(self, _size: int) -> bytes:
        if self._gate is not None:
            await self._gate.wait()
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _FakeProcess:
    def __init__(
        self,
        *,
        returncode: int | None = 0,
        stdout: list[bytes] | None = None,
        stderr: list[bytes] | None = None,
        wait_gate: asyncio.Event | None = None,
    ):
        self.returncode = returncode
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self._done = asyncio.Event()
        self._wait_gate = wait_gate
        self.terminated = False
        if wait_gate is None and returncode is not None:
            self._done.set()

    async def wait(self) -> int:
        if self._wait_gate is not None and not self._done.is_set():
            await self._wait_gate.wait()
        await self._done.wait()
        return self.returncode or 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._done.set()

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9
        self._done.set()


def _job(workspace: Path, **overrides: object) -> CommandJob:
    values: dict[str, object] = {
        "attempt_id": "attempt-1",
        "generation": 3,
        "workspace": workspace,
        "argv": ("sh", "-c", "printf ok"),
        "timeout_seconds": 5.0,
        "grace_seconds": 0.1,
        "output_limit_bytes": 1024,
        "memory_bytes": 128 * 1024 * 1024,
        "pids_limit": 32,
        "cpus": 0.5,
    }
    values.update(overrides)
    return CommandJob(**values)


def _inspect_output(
    running: bool,
    exit_code: int | None = 0,
    status: str | None = None,
    *,
    container_id: str | None = None,
    labels: dict[str, str] | None = None,
    finished_at: datetime | None = None,
) -> bytes:
    if status is None:
        status = "running" if running else ("exited" if exit_code is not None else "created")
    state = {"Running": running, "Status": status, "ExitCode": exit_code}
    if finished_at is not None:
        state["FinishedAt"] = finished_at.isoformat()
    if container_id is None:
        return json.dumps(state).encode()
    return json.dumps(
        {
            "Id": container_id,
            "State": state,
            "Config": {"Labels": labels or {}},
        }
    ).encode()


async def _cleanup_real_container(worker: DockerLocalWorker, job: CommandJob) -> None:
    """停止確認済みの opt-in container だけを自身の名前で削除する。"""
    if await worker.stop(job.attempt_id, job.generation, job.grace_seconds) != "confirmed":
        return
    process = await asyncio.create_subprocess_exec(
        worker.docker_binary,
        "rm",
        worker._container_name(job),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await process.communicate()


def _fake_docker(
    calls: list[tuple[str, ...]],
    *,
    state: dict[str, object],
    start: _FakeProcess | None = None,
    create_output: bytes = b"container-id\n",
    inspect_labels: dict[str, str] | None = None,
    rm_returncode: int = 0,
):
    """Docker の各サブコマンドを記録し、決めた状態だけを返す。"""
    async def fake_create(*args: str, **_kwargs: object) -> _FakeProcess:
        calls.append(args)
        command = args[1]
        if command == "create":
            return _FakeProcess(stdout=[create_output], stderr=[])
        if command == "start":
            return start or _FakeProcess(stdout=[b"out"], stderr=[b"err"])
        if command == "inspect":
            return _FakeProcess(stdout=[_inspect_output(
                bool(state["running"]), state["exit_code"], state["status"],
                container_id="container-id",
                labels=inspect_labels if inspect_labels is not None else {
                    "kotoyose.attempt_id": "attempt-1", "kotoyose.generation": "3",
                },
            )])
        if command == "stop":
            state["running"] = False
            state["status"] = "exited"
            state["exit_code"] = state["exit_code"] if state["exit_code"] is not None else 137
            return _FakeProcess(stdout=[b"kotoyose-container\n"], stderr=[])
        if command == "image":
            return _FakeProcess(stdout=[b"[]"], stderr=[])
        if command == "rm":
            assert args[2:] == ("container-id",)
            return _FakeProcess(returncode=rm_returncode, stdout=[b"container-id\n"])
        raise AssertionError(f"unexpected docker command: {args}")

    return fake_create


def _reaper_case(entries, owners, *, batch_size=100, active=()):
    """DB と Docker CLI の双方を fake にした 1 周回用 service。"""
    calls: list[tuple[str, ...]] = []
    worker = DockerLocalWorker(IMAGE_ID)

    class Store:
        async def container_attempt_owners(self, ids):
            return {key: value for key, value in owners.items() if key in ids}

    service = ExecutionService.__new__(ExecutionService)
    service.worker = worker
    service.store = Store()
    service._tasks = {key: object() for key in active}
    service._container_cursor = 0
    service.config = SimpleNamespace(container_cleanup=SimpleNamespace(
        batch_size=batch_size, unknown_grace_seconds=3600,
    ))

    async def fake_docker(*args: str, **_kwargs: object) -> _FakeProcess:
        calls.append(args)
        if args[1] == "ps":
            assert "label=kotoyose.attempt_id" in args
            assert "status=exited" in args and "status=dead" in args
            rows = [f"{item['id']} {item['name']}" for item in entries]
            return _FakeProcess(stdout=[("\n".join(rows) + "\n").encode()])
        if args[1] == "inspect":
            item = next(item for item in entries if item["name"] == args[3])
            return _FakeProcess(stdout=[_inspect_output(
                item.get("running", False), 0,
                "running" if item.get("running", False) else "exited",
                container_id=item["id"], labels=item["labels"],
                finished_at=item.get("finished_at"),
            )])
        if args[1] == "rm":
            return _FakeProcess(stdout=[b"removed"])
        raise AssertionError(args)

    return service, calls, fake_docker


def _reaper_entry(attempt_id, *, finished_at=None, running=False, label_id=None):
    return {
        "id": f"id-{attempt_id}", "name": f"kotoyose-{attempt_id}-0",
        "labels": {"kotoyose.attempt_id": label_id or attempt_id, "kotoyose.generation": "0"},
        "finished_at": finished_at, "running": running,
    }


@pytest.mark.asyncio
async def test_reaper_removes_only_terminal_owned_stopped_containers():
    """label、running、未終端、in-flight を除外し、終端実体だけを ID で削除する。"""
    now = datetime.now(timezone.utc) - timedelta(hours=2)
    entries = [
        _reaper_entry("terminal", finished_at=now),
        _reaper_entry("running", finished_at=now, running=True),
        _reaper_entry("other", finished_at=now, label_id="other-project"),
        _reaper_entry("inflight", finished_at=now),
        _reaper_entry("unfinished", finished_at=now),
        {"id": "other-id", "name": "foreign-container", "labels": {}},
    ]
    owners = {
        "terminal": ("terminal", True, 0), "running": ("running", True, 0),
        "inflight": ("inflight", True, 0), "unfinished": ("unfinished", False, 0),
    }
    service, calls, fake = _reaper_case(entries, owners, active=("inflight",))
    with patch("asyncio.create_subprocess_exec", new=fake):
        assert await service._reap_containers_once() == 1
    assert [call[2] for call in calls if call[1] == "rm"] == ["id-terminal"]
    assert [call[1] for call in calls].index("inspect") < [call[1] for call in calls].index("rm")


@pytest.mark.asyncio
async def test_reaper_unknown_attempt_retains_stop_evidence_regardless_of_age():
    """台帳にない停止済み実体は経過時間だけで停止確認に変えず保持する。"""
    now = datetime.now(timezone.utc)
    entries = [
        _reaper_entry("old", finished_at=now - timedelta(hours=2)),
        _reaper_entry("fresh", finished_at=now - timedelta(minutes=30)),
        _reaper_entry("no-date"),
    ]
    service, calls, fake = _reaper_case(entries, {})
    with patch("asyncio.create_subprocess_exec", new=fake):
        assert await service._reap_containers_once() == 0
    assert not [call for call in calls if call[1] == "rm"]


@pytest.mark.asyncio
async def test_reaper_batch_rotates_across_candidates():
    """1 周回の inspect/rm を batch 上限に抑え、次の候補へ進む。"""
    entries = [_reaper_entry(f"terminal-{index}") for index in range(3)]
    owners = {f"terminal-{index}": (f"terminal-{index}", True, 0) for index in range(3)}
    service, calls, fake = _reaper_case(entries, owners, batch_size=1)
    with patch("asyncio.create_subprocess_exec", new=fake):
        assert await service._reap_containers_once() == 1
        assert len([call for call in calls if call[1] == "rm"]) == 1
        assert await service._reap_containers_once() == 1
        assert await service._reap_containers_once() == 1
    assert [call[2] for call in calls if call[1] == "rm"] == [
        "id-terminal-0", "id-terminal-1", "id-terminal-2",
    ]


def test_constructor_rejects_tags_and_root_execution():
    """image tag と root uid は実行境界へ入れない。"""
    with pytest.raises(ValueError, match="sha256"):
        DockerLocalWorker("alpine:latest")
    with patch("kotoyose.local_worker.os.getuid", return_value=0):
        with pytest.raises(ValueError, match="root"):
            DockerLocalWorker(IMAGE_ID)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"cwd": "../outside"},
        {"cwd": "/absolute"},
        {"env": {"HOME": "/host"}},
        {"env": {"PATH": "/host"}},
        {"env": {"1INVALID": "x"}},
        {"env": {"OK": "nul\x00value"}},
        {"argv": ("sh\x00",)},
    ],
)
def test_command_job_rejects_escape_or_process_injection(tmp_path, kwargs):
    """cwd traversal、reserved env、invalid key、NUL を拒否する。"""
    with pytest.raises(ValueError):
        _job(tmp_path, **kwargs)


def test_empty_arguments_and_environment_values_are_allowed(tmp_path):
    """実行可能ファイル以外の argv と env value は空文字を許可する。"""
    job = _job(tmp_path, argv=("sh", "", ""), env={"EMPTY": ""})

    assert job.argv[1:] == ("", "")
    assert job.env["EMPTY"] == ""


def test_workspace_root_and_symlink_are_rejected(tmp_path):
    """workspace は実在する非 root の canonical directory に限定する。"""
    worker = DockerLocalWorker(IMAGE_ID)
    with pytest.raises(ValueError):
        worker._validate_workspace(Path("/"))
    link = tmp_path / "workspace-link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        worker._validate_workspace(link)


@pytest.mark.parametrize("suffix", [",comma", "newline\npath", "carriage\rpath"])
def test_workspace_mount_path_rejects_docker_mount_delimiters(tmp_path, suffix):
    """bind mount の CSV 解釈を壊す path は開始前に拒否する。"""
    workspace = tmp_path / f"workspace{suffix}"
    workspace.mkdir()
    with pytest.raises(ValueError, match="comma or newline"):
        DockerLocalWorker(IMAGE_ID)._validate_workspace(workspace)


def test_create_command_has_required_isolation_and_only_workspace_mount(tmp_path):
    """create argv に hardening と workspace だけが deterministic に含まれる。"""
    worker = DockerLocalWorker(IMAGE_ID)
    job = _job(tmp_path, cwd="src", env={"LANG": "C", "TASK": "one"})
    argv = worker._create_argv(job, tmp_path.resolve(), "kotoyose-attempt-1-3")
    assert argv[0] == "create"
    assert "--pull=never" in argv
    assert "--restart=no" in argv
    assert "--cap-drop" in argv and "ALL" in argv
    assert "--security-opt" in argv and "no-new-privileges" in argv
    assert "--network" in argv and "none" in argv
    assert "--read-only" in argv
    assert _job(tmp_path).argv[0] in argv
    mounts = [value for flag, value in zip(argv, argv[1:]) if flag == "--mount"]
    workspace_mount = f"type=bind,src={tmp_path.resolve()},dst=/workspace"
    assert mounts == [workspace_mount]
    assert "HOME=/tmp" in argv
    assert "/tmp:rw,noexec,nosuid,nodev,size=64m" in argv
    assert "LANG=C" in argv and "TASK=one" in argv
    assert "docker.sock" not in " ".join(argv)
    # The host workspace can live under /home. Reject home paths elsewhere in Docker's arguments.
    assert "/home" not in " ".join(arg for arg in argv if arg != workspace_mount)


@pytest.mark.asyncio
async def test_execute_success_collects_output_and_confirms_stopped(tmp_path):
    """正常終了は bounded output と inspect 停止確認を伴って成功する。"""
    calls: list[tuple[str, ...]] = []
    state = {"running": False, "status": "exited", "exit_code": 0}
    with patch(
        "asyncio.create_subprocess_exec",
        new=_fake_docker(calls, state=state, start=_FakeProcess(stdout=[b"out"], stderr=[b"err"])),
    ):
        result = await DockerLocalWorker(IMAGE_ID).execute(_job(tmp_path), asyncio.Event())

    assert result.status == "succeeded"
    assert result.exit_code == 0
    assert result.stdout == b"out"
    assert result.stderr == b"err"
    assert result.stop_confirmation == "confirmed"
    assert [call[1] for call in calls] == ["create", "start", "inspect", "inspect", "rm"]


@pytest.mark.asyncio
async def test_execute_nonzero_command_is_failed(tmp_path):
    """command の非 0 exit は failed として報告する。"""
    state = {"running": False, "status": "exited", "exit_code": 7}
    with patch(
        "asyncio.create_subprocess_exec",
        new=_fake_docker(calls=[], state=state, start=_FakeProcess(returncode=7)),
    ):
        result = await DockerLocalWorker(IMAGE_ID).execute(_job(tmp_path), asyncio.Event())

    assert result.status == "failed"
    assert result.exit_code == 7
    assert result.stop_confirmation == "confirmed"


@pytest.mark.asyncio
async def test_remove_failure_does_not_change_result(tmp_path):
    """停止証拠を取得した後の rm 障害は command の verdict を変えない。"""
    calls: list[tuple[str, ...]] = []
    state = {"running": False, "status": "exited", "exit_code": 0}
    with patch("asyncio.create_subprocess_exec", new=_fake_docker(
        calls, state=state, rm_returncode=1,
    )):
        result = await DockerLocalWorker(IMAGE_ID).execute(_job(tmp_path), asyncio.Event())
    assert result.status == "succeeded"
    assert result.stop_confirmation == "confirmed"
    assert [call[1] for call in calls] == ["create", "start", "inspect", "inspect", "rm"]


@pytest.mark.asyncio
async def test_finished_container_with_different_generation_is_not_removed(tmp_path):
    """同じ名前でも inspect label が別世代なら停止確認も削除もしない。"""
    calls: list[tuple[str, ...]] = []
    state = {"running": False, "status": "exited", "exit_code": 0}
    with patch("asyncio.create_subprocess_exec", new=_fake_docker(
        calls, state=state,
        inspect_labels={"kotoyose.attempt_id": "attempt-1", "kotoyose.generation": "4"},
    )):
        result = await DockerLocalWorker(IMAGE_ID).execute(_job(tmp_path), asyncio.Event())
    assert result.status == "worker_lost"
    assert result.stop_confirmation == "unconfirmed"
    assert "rm" not in [call[1] for call in calls]


@pytest.mark.asyncio
async def test_cancel_stops_container_and_requires_inspect_confirmation(tmp_path):
    """cancel は attach CLI の取消だけで完了とせず、stop と inspect を行う。"""
    calls: list[tuple[str, ...]] = []
    state = {"running": True, "status": "running", "exit_code": None}
    start = _FakeProcess(returncode=None, wait_gate=asyncio.Event())
    cancel = asyncio.Event()
    with patch(
        "asyncio.create_subprocess_exec",
        new=_fake_docker(calls, state=state, start=start),
    ):
        task = asyncio.create_task(DockerLocalWorker(IMAGE_ID).execute(_job(tmp_path), cancel))
        await asyncio.sleep(0)
        cancel.set()
        result = await task

    assert result.status == "cancelled"
    assert result.stop_confirmation == "confirmed"
    assert start.terminated is True
    assert [call[1] for call in calls].count("stop") == 1
    assert [call[1] for call in calls].count("inspect") >= 2
    assert [call[1] for call in calls][-1] == "rm"


@pytest.mark.asyncio
async def test_timeout_stops_container(tmp_path):
    """timeout も container stop と stopped inspect を要求する。"""
    calls: list[tuple[str, ...]] = []
    state = {"running": True, "status": "running", "exit_code": None}
    start = _FakeProcess(returncode=None, wait_gate=asyncio.Event())
    with patch(
        "asyncio.create_subprocess_exec",
        new=_fake_docker(calls, state=state, start=start),
    ):
        result = await DockerLocalWorker(IMAGE_ID).execute(
            _job(tmp_path, timeout_seconds=0.01), asyncio.Event()
        )

    assert result.status == "timeout"
    assert result.stop_confirmation == "confirmed"
    assert "stop" in [call[1] for call in calls]
    assert [call[1] for call in calls][-1] == "rm"


@pytest.mark.asyncio
async def test_output_overflow_is_failed_and_container_is_stopped(tmp_path):
    """stdout + stderr の合計超過は failed とし、container を停止する。"""
    calls: list[tuple[str, ...]] = []
    state = {"running": True, "status": "running", "exit_code": None}
    start = _FakeProcess(returncode=None, wait_gate=asyncio.Event(), stdout=[b"123456"])
    with patch(
        "asyncio.create_subprocess_exec",
        new=_fake_docker(calls, state=state, start=start),
    ):
        result = await DockerLocalWorker(IMAGE_ID).execute(
            _job(tmp_path, output_limit_bytes=5), asyncio.Event()
        )

    assert result.status == "failed"
    assert "output_limit" in result.reason
    assert result.stop_confirmation == "confirmed"
    assert "stop" in [call[1] for call in calls]


@pytest.mark.asyncio
async def test_unknown_create_response_is_worker_lost_and_never_starts(tmp_path):
    """create の応答不明時は inspect で回復しても start を推測実行しない。"""
    calls: list[tuple[str, ...]] = []
    state = {"running": False, "status": "created", "exit_code": None}
    with patch(
        "asyncio.create_subprocess_exec",
        new=_fake_docker(calls, state=state, create_output=b""),
    ):
        result = await DockerLocalWorker(IMAGE_ID).execute(_job(tmp_path), asyncio.Event())

    assert result.status == "worker_lost"
    assert [call[1] for call in calls] == ["create", "inspect"]


@pytest.mark.asyncio
async def test_cancelled_before_create_has_confirmed_no_side_effect(tmp_path):
    """create 前 cancel は Docker side effect がないため確認済みとして返す。"""
    calls: list[tuple[str, ...]] = []
    cancel = asyncio.Event()
    cancel.set()

    async def fail_if_called(*args: str, **kwargs: object) -> _FakeProcess:
        calls.append(args)
        raise AssertionError("cancelled operation must not call Docker")

    with patch("asyncio.create_subprocess_exec", new=fail_if_called):
        result = await DockerLocalWorker(IMAGE_ID).execute(_job(tmp_path), cancel)

    assert result.status == "cancelled"
    assert result.stop_confirmation == "confirmed"
    assert calls == []


@pytest.mark.asyncio
async def test_start_response_without_exit_code_is_worker_lost(tmp_path):
    """start の非 0 応答を command failure と断定せず inspect を正本にする。"""
    state = {"running": False, "status": "created", "exit_code": None}
    start = _FakeProcess(returncode=125, stdout=[b""], stderr=[b"daemon error"])
    with patch(
        "asyncio.create_subprocess_exec",
        new=_fake_docker(calls=[], state=state, start=start),
    ):
        result = await DockerLocalWorker(IMAGE_ID).execute(_job(tmp_path), asyncio.Event())

    assert result.status == "worker_lost"
    assert "authoritative exit code" in result.reason


@pytest.mark.asyncio
async def test_stop_unconfirmed_when_inspect_still_running(tmp_path):
    """stop 成功応答だけでは停止済みとせず、inspect の running を優先する。"""
    calls: list[tuple[str, ...]] = []
    state = {"running": True, "status": "running", "exit_code": None}
    with patch(
        "asyncio.create_subprocess_exec",
        new=_fake_docker(calls, state=state),
    ):
        # stop fake は running=false にするため、inspect を常に running に戻す。
        async def fake_still_running(*args: str, **kwargs: object) -> _FakeProcess:
            calls.append(args)
            if args[1] == "stop":
                return _FakeProcess(stdout=[b"ok"], stderr=[])
            if args[1] == "inspect":
                return _FakeProcess(stdout=[_inspect_output(True, None)], stderr=[])
            raise AssertionError(args)

        with patch("asyncio.create_subprocess_exec", new=fake_still_running):
            confirmation = await DockerLocalWorker(IMAGE_ID).stop("attempt-1", 3, 0)

    assert confirmation == "unconfirmed"


@pytest.mark.asyncio
async def test_stop_created_container_is_unconfirmed(tmp_path):
    """Running=false でも created は terminal stopped confirmation にならない。"""
    async def fake_created(*args: str, **kwargs: object) -> _FakeProcess:
        if args[1] == "inspect":
            return _FakeProcess(stdout=[_inspect_output(False, None, "created")], stderr=[])
        return _FakeProcess(stdout=[b"ok"], stderr=[])

    with patch("asyncio.create_subprocess_exec", new=fake_created):
        confirmation = await DockerLocalWorker(IMAGE_ID).stop("attempt-1", 3, 0)

    assert confirmation == "unconfirmed"


@pytest.mark.asyncio
async def test_stop_fences_created_container_after_identity_check(tmp_path):
    """created container は immutable ID と Kotoyose labels を確認してから削除する。"""
    calls: list[tuple[str, ...]] = []
    identity = "container-created-1"
    labels = {"kotoyose.attempt_id": "attempt-1", "kotoyose.generation": "3"}

    async def fake_fence(*args: str, **kwargs: object) -> _FakeProcess:
        calls.append(args)
        if args[1] == "inspect":
            return _FakeProcess(
                stdout=[
                    _inspect_output(
                        False, None, "created", container_id=identity, labels=labels
                    )
                ],
                stderr=[],
            )
        if args[1] == "rm":
            assert args[2:] == ("--force", identity)
            return _FakeProcess(stdout=[b"removed"], stderr=[])
        if args[1] == "stop":
            return _FakeProcess(stdout=[b""], stderr=[])
        raise AssertionError(args)

    with patch("asyncio.create_subprocess_exec", new=fake_fence):
        confirmation = await DockerLocalWorker(IMAGE_ID).stop("attempt-1", 3, 0)

    assert confirmation == "confirmed"
    assert [call[1] for call in calls] == ["inspect", "stop", "inspect", "rm", "inspect"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "labels,container_id",
    [
        ({"kotoyose.attempt_id": "other", "kotoyose.generation": "3"}, "other-id"),
        ({"kotoyose.attempt_id": "attempt-1", "kotoyose.generation": "4"}, "other-id"),
        ({"kotoyose.attempt_id": "attempt-1", "kotoyose.generation": "3"}, None),
    ],
)
async def test_stop_created_does_not_remove_wrong_or_unknown_identity(
    tmp_path, labels, container_id
):
    """identity 不一致または欠落時は別 container を削除せず未確認にする。"""
    calls: list[tuple[str, ...]] = []

    async def fake_wrong(*args: str, **kwargs: object) -> _FakeProcess:
        calls.append(args)
        if args[1] == "inspect":
            return _FakeProcess(
                stdout=[
                    _inspect_output(False, None, "created", container_id=container_id, labels=labels)
                ],
                stderr=[],
            )
        if args[1] == "stop":
            return _FakeProcess(stdout=[b""], stderr=[])
        raise AssertionError(args)

    with patch("asyncio.create_subprocess_exec", new=fake_wrong):
        confirmation = await DockerLocalWorker(IMAGE_ID).stop("attempt-1", 3, 0)

    assert confirmation == "unconfirmed"
    assert "rm" not in [call[1] for call in calls]


@pytest.mark.asyncio
async def test_stop_created_unknown_rm_response_is_unconfirmed(tmp_path):
    """fencing の rm 応答が不明なら停止確認を返さない。"""
    identity = "container-created-1"
    labels = {"kotoyose.attempt_id": "attempt-1", "kotoyose.generation": "3"}
    rm = _FakeProcess(returncode=None, stdout=[], stderr=[], wait_gate=asyncio.Event())

    async def fake_unknown_rm(*args: str, **kwargs: object) -> _FakeProcess:
        if args[1] == "inspect":
            return _FakeProcess(
                stdout=[
                    _inspect_output(
                        False, None, "created", container_id=identity, labels=labels
                    )
                ],
                stderr=[],
            )
        if args[1] == "rm":
            return rm
        if args[1] == "stop":
            return _FakeProcess(stdout=[b""], stderr=[])
        raise AssertionError(args)

    with patch("asyncio.create_subprocess_exec", new=fake_unknown_rm):
        with patch("kotoyose.local_worker._CONTROL_DEADLINE_SECONDS", 0.01):
            confirmation = await DockerLocalWorker(IMAGE_ID).stop("attempt-1", 3, 0)

    assert confirmation == "unconfirmed"
    assert rm.terminated is True


@pytest.mark.asyncio
async def test_stop_missing_container_is_unconfirmed(tmp_path):
    """inspect が missing を返した場合は fencing 完了とみなさない。"""
    calls: list[tuple[str, ...]] = []

    async def fake_missing(*args: str, **kwargs: object) -> _FakeProcess:
        calls.append(args)
        if args[1] == "inspect":
            return _FakeProcess(returncode=1, stdout=[], stderr=[b"No such container"])
        if args[1] == "stop":
            return _FakeProcess(returncode=1, stdout=[], stderr=[b"not found"])
        raise AssertionError(args)

    with patch("asyncio.create_subprocess_exec", new=fake_missing):
        confirmation = await DockerLocalWorker(IMAGE_ID).stop("attempt-1", 3, 0)

    assert confirmation == "unconfirmed"
    assert "rm" not in [call[1] for call in calls]


@pytest.mark.asyncio
async def test_control_cli_deadline_terminates_hanging_process(tmp_path):
    """daemon 応答停止でも control CLI と pipe reader を bounded に回収する。"""
    worker = DockerLocalWorker(IMAGE_ID)
    process = _FakeProcess(returncode=None, wait_gate=asyncio.Event())
    process.stdout = _FakeStream(gate=asyncio.Event())
    process.stderr = _FakeStream(gate=asyncio.Event())
    with patch("kotoyose.local_worker._CONTROL_DEADLINE_SECONDS", 0.01):
        with pytest.raises(asyncio.TimeoutError):
            await worker._communicate_control(process)

    assert process.terminated is True


@pytest.mark.asyncio
async def test_cancelled_execute_stops_container_before_propagating_cancel(tmp_path):
    """caller cancellation でも attach CLI を回収し、container 停止確認を試みる。"""
    calls: list[tuple[str, ...]] = []
    state = {"running": True, "status": "running", "exit_code": None}
    start = _FakeProcess(returncode=None, wait_gate=asyncio.Event())
    cancel = asyncio.Event()
    with patch(
        "asyncio.create_subprocess_exec",
        new=_fake_docker(calls, state=state, start=start),
    ):
        task = asyncio.create_task(DockerLocalWorker(IMAGE_ID).execute(_job(tmp_path), cancel))
        for _ in range(20):
            await asyncio.sleep(0)
            if "start" in [call[1] for call in calls]:
                break
        assert "start" in [call[1] for call in calls]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert start.terminated is True
    assert "stop" in [call[1] for call in calls]
    assert "inspect" in [call[1] for call in calls]


@pytest.mark.asyncio
async def test_probe_inspects_image_without_pull(tmp_path):
    """probe は image inspect だけを実行し、pull/create は行わない。"""
    calls: list[tuple[str, ...]] = []
    worker = DockerLocalWorker(IMAGE_ID)
    with patch(
        "asyncio.create_subprocess_exec",
        new=_fake_docker(calls, state={"running": False, "status": "exited", "exit_code": 0}),
    ):
        assert await worker.probe() is True

    assert calls == [("docker", "image", "inspect", IMAGE_ID)]


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("KOTOYOSE_DOCKER_TEST_IMAGE"),
    reason="set KOTOYOSE_DOCKER_TEST_IMAGE=sha256:... to run the Docker integration test",
)
async def test_real_docker_image_is_isolated_and_can_write_workspace(tmp_path):
    """opt-in 実 Docker で workspace 書込み、資格情報遮断、network none を確認する。"""
    image_id = os.environ["KOTOYOSE_DOCKER_TEST_IMAGE"]
    outside = tmp_path.parent / "host-outside-sentinel"
    outside.write_text("host-only")
    script = (
        "set -eu; "
        "printf ok > /workspace/result; "
        "test \"$HOME\" = /tmp; "
        "test -z \"${AWS_ACCESS_KEY_ID:-}\"; "
        "test ! -e /root/.docker/config.json; "
        "test ! -e /workspace/../host-outside-sentinel; "
        "test $(wc -l < /proc/net/route) -le 1"
    )
    job = _job(
        tmp_path,
        attempt_id=f"real-{tmp_path.name}",
        argv=("sh", "-c", script),
        timeout_seconds=20,
    )
    worker = DockerLocalWorker(image_id)
    try:
        result = await worker.execute(job, asyncio.Event())
    finally:
        await _cleanup_real_container(worker, job)

    assert result.status == "succeeded", result.reason
    assert (tmp_path / "result").read_text() == "ok"
    assert outside.read_text() == "host-only"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("KOTOYOSE_DOCKER_TEST_IMAGE"),
    reason="set KOTOYOSE_DOCKER_TEST_IMAGE=sha256:... to run the Docker integration test",
)
async def test_real_docker_cancel_stops_child_processes(tmp_path):
    """opt-in 実 Docker で cancel 後に child を含む container 停止を確認する。"""
    cancel = asyncio.Event()
    job = _job(
        tmp_path,
        attempt_id=f"real-{tmp_path.name}",
        argv=("sh", "-c", "sleep 300 & child=$!; wait $child"),
        timeout_seconds=20,
    )
    worker = DockerLocalWorker(os.environ["KOTOYOSE_DOCKER_TEST_IMAGE"])
    task = asyncio.create_task(worker.execute(job, cancel))
    try:
        await asyncio.sleep(0.2)
        cancel.set()
        result = await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await _cleanup_real_container(worker, job)

    assert result.status == "cancelled"
    assert result.stop_confirmation == "confirmed"
