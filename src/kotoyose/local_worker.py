"""固定済み Docker image で command を実行する LocalWorker。"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from .command_worker import CommandCancelledError
from .execution_jobs import (
    CommandJob, WorkerResult, ExecutionStatus,
    _validate_identifier, _validate_text, _validate_nonnegative_finite,
)


_IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_CONTAINER_PREFIX = "kotoyose-"
_MINIMAL_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_TMPFS = "/tmp:rw,noexec,nosuid,nodev,size=64m"
_CONTROL_OUTPUT_LIMIT = 64 * 1024
_CONTROL_DEADLINE_SECONDS = 15.0
_READ_CHUNK_SIZE = 64 * 1024
_REAP_TIMEOUT_SECONDS = 5.0
_CONTAINER_LIST_OUTPUT_LIMIT = 8 * 1024 * 1024

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class _ContainerState:
    running: bool
    exit_code: int | None
    status: str | None
    container_id: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    finished_at: datetime | None = None


class _OutputLimitExceeded(Exception):
    """stdout と stderr の合計上限を超えた。"""


class _OutputCollector:
    """2 stream の合計バイト数を制限しながら保持する。"""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self.stdout = bytearray()
        self.stderr = bytearray()

    def append(self, stream: str, chunk: bytes) -> None:
        if self.total + len(chunk) > self.limit:
            raise _OutputLimitExceeded
        self.total += len(chunk)
        if stream == "stdout":
            self.stdout.extend(chunk)
        else:
            self.stderr.extend(chunk)


def _validate_mount_path(path: Path) -> None:
    """Docker の mount CSV を壊す delimiter と改行を拒否する。"""
    if any(character in str(path) for character in (",", "\r", "\n")):
        raise ValueError("workspace path must not contain comma or newline")


def _is_stopped_state(state: _ContainerState | None) -> bool:
    """Docker の terminal state だけを停止確認として認める。"""
    return state is not None and not state.running and state.status in {"exited", "dead"}


def _expected_container_identity(name: str) -> tuple[str, int] | None:
    """deterministic name から fencing 対象の attempt / generation を取り出す。"""
    if not name.startswith(_CONTAINER_PREFIX):
        return None
    parts = name.removeprefix(_CONTAINER_PREFIX).rsplit("-", 1)
    if len(parts) != 2:
        return None
    attempt, generation = parts
    if not attempt or _ID_RE.fullmatch(attempt) is None:
        return None
    if not generation.isdigit():
        return None
    return attempt, int(generation)


def _matches_container_identity(
    state: _ContainerState, attempt_id: str, generation: int
) -> bool:
    """inspect の immutable ID と Kotoyose labels が要求された名前に一致するか確認する。"""
    return (
        bool(state.container_id)
        and state.labels.get("kotoyose.attempt_id") == attempt_id
        and state.labels.get("kotoyose.generation") == str(generation)
    )


class DockerLocalWorker:
    """管理者指定の immutable Docker image 内で command を実行する。"""

    def __init__(self, image_id: str, docker_binary: str = "docker") -> None:
        if not isinstance(image_id, str):
            raise ValueError("image_id must be a full sha256 image ID; tags are not allowed")
        if _IMAGE_ID_RE.fullmatch(image_id) is None:
            raise ValueError("image_id must be a full sha256 image ID; tags are not allowed")
        _validate_text(docker_binary, "docker_binary")
        if os.getuid() == 0:
            raise ValueError("root uid is not allowed for DockerLocalWorker")
        self.image_id = image_id
        self.docker_binary = docker_binary

    async def execute(self, job: CommandJob, cancel: asyncio.Event) -> WorkerResult:
        """container を作成して command を実行し、停止状態を inspect で確認する。

        既に cancel 済みで create 前なら Docker 操作がないため停止確認を返す。
        controller は一つの operation ID に対して execute を一度だけ呼び、同じ ID の
        execute 再実行を行わない契約を持つ。
        """
        if not isinstance(job, CommandJob):
            raise TypeError("job must be a CommandJob")
        if not hasattr(cancel, "is_set"):
            raise TypeError("cancel must provide is_set()")
        workspace = self._validate_workspace(job.workspace)
        name = self._container_name(job)

        if cancel.is_set():
            return WorkerResult(
                status="cancelled",
                exit_code=None,
                stdout=b"",
                stderr=b"",
                stop_confirmation="confirmed",
                reason="cancel requested before container creation",
            )

        try:
            create = await self._create_container(job, workspace, name)
        except asyncio.CancelledError:
            confirmation = await self._cleanup_cancelled_execute(job)
            if confirmation == "confirmed":
                raise CommandCancelledError(job, confirmation) from None
            raise
        if not create:
            state = await self._inspect(name)
            if state is not None and state.running:
                await self._stop_name(name, job.grace_seconds)
            return WorkerResult(
                status="worker_lost",
                exit_code=state.exit_code if state is not None else None,
                stdout=b"",
                stderr=b"",
                # create の応答が不明なため、既存 container を止めても create の
                # 対応関係を証明できず、確認済みとは扱わない。
                stop_confirmation="unconfirmed",
                reason="docker create response was unknown or failed",
            )

        try:
            process = await self._spawn(
                "start",
                "-a",
                name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except asyncio.CancelledError:
            confirmation = await self._cleanup_cancelled_execute(job)
            if confirmation == "confirmed":
                raise CommandCancelledError(job, confirmation) from None
            raise
        except (OSError, RuntimeError) as exc:
            state = await self._inspect(name)
            confirmation = (
                await self._stop_name(name, job.grace_seconds)
                if state and state.running
                else "unconfirmed"
            )
            if confirmation == "confirmed":
                try:
                    await self._remove_stopped(name)
                except asyncio.CancelledError:
                    raise CommandCancelledError(job, confirmation) from None
            return WorkerResult(
                status="worker_lost",
                exit_code=state.exit_code if state is not None else None,
                stdout=b"",
                stderr=b"",
                stop_confirmation=confirmation,
                reason=f"docker start could not be started: {exc}",
            )

        collector = _OutputCollector(job.output_limit_bytes)
        process_wait = asyncio.create_task(process.wait())
        output_task = asyncio.create_task(self._collect_output(process, collector))
        cancel_task = asyncio.create_task(cancel.wait())
        timeout_task = asyncio.create_task(asyncio.sleep(job.timeout_seconds))
        status: ExecutionStatus | None = None
        reason = ""
        confirmation = "unconfirmed"
        try:
            pending = {process_wait, output_task, cancel_task, timeout_task}
            while status is None:
                done, _ = await asyncio.wait(
                    pending,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if output_task in done:
                    pending.discard(output_task)
                    output_error = output_task.exception()
                    if output_error is not None:
                        if isinstance(output_error, _OutputLimitExceeded):
                            status = "failed"
                            reason = "container output exceeded output_limit_bytes"
                        else:
                            status = "worker_lost"
                            reason = f"container output collection failed: {output_error}"
                if status is None and cancel_task in done:
                    pending.discard(cancel_task)
                    if cancel.is_set():
                        status = "cancelled"
                        reason = "cancel requested"
                if status is None and timeout_task in done:
                    pending.discard(timeout_task)
                    status = "timeout"
                    reason = f"execution exceeded {job.timeout_seconds:g} seconds"
                if status is None and process_wait in done:
                    pending.discard(process_wait)
                    try:
                        await process_wait
                    except (OSError, RuntimeError) as exc:
                        status = "worker_lost"
                        reason = f"docker start wait failed: {exc}"
                    if status is None and output_task in pending:
                        try:
                            await output_task
                        except _OutputLimitExceeded:
                            status = "failed"
                            reason = "container output exceeded output_limit_bytes"
                        except (OSError, RuntimeError) as exc:
                            status = "worker_lost"
                            reason = f"container output collection failed: {exc}"
                        else:
                            pending.discard(output_task)
                    if status is None:
                        status = "succeeded" if process.returncode == 0 else "failed"
                        reason = "command_completed"

            should_stop = status in {"cancelled", "timeout", "worker_lost"} or reason.startswith(
                "container output"
            )
            if should_stop:
                confirmation = await self._stop_name(name, job.grace_seconds)
                await self._reap_process(process)
            else:
                confirmation = "unconfirmed"

            state = await self._inspect(name)
            if state is not None and not _matches_container_identity(state, job.attempt_id, job.generation):
                state = None
            if state is None:
                if confirmation == "confirmed":
                    await self._remove_stopped(name)
                return WorkerResult(
                    status="worker_lost",
                    exit_code=process.returncode,
                    stdout=bytes(collector.stdout),
                    stderr=bytes(collector.stderr),
                    stop_confirmation=confirmation,
                    reason="container state could not be inspected",
                )
            if state.running:
                confirmation = await self._stop_name(name, job.grace_seconds)
                status = "worker_lost"
                reason = "docker start ended while container was still running"
                if confirmation == "confirmed":
                    state = await self._inspect(name)
                    if state is not None and not _matches_container_identity(state, job.attempt_id, job.generation):
                        state = None
                    if state is None:
                        await self._remove_stopped(name)
                        return WorkerResult(
                            status=status, exit_code=process.returncode,
                            stdout=bytes(collector.stdout), stderr=bytes(collector.stderr),
                            stop_confirmation="confirmed", reason=reason,
                        )
            if (
                status == "failed"
                and reason == "command_completed"
                and process.returncode != 0
                and state.exit_code is None
            ):
                status = "worker_lost"
                reason = "docker start response had no authoritative exit code"
            if reason == "command_completed":
                if not _is_stopped_state(state) or state.exit_code is None:
                    status = "worker_lost"
                    reason = "container did not reach an authoritative exited state"
                elif state.exit_code != 0:
                    status = "failed"
                    reason = "command_completed"
                else:
                    status = "succeeded"
            result = WorkerResult(
                status=status,
                exit_code=state.exit_code if state.exit_code is not None else process.returncode,
                stdout=bytes(collector.stdout),
                stderr=bytes(collector.stderr),
                stop_confirmation=("confirmed" if _is_stopped_state(state) else "unconfirmed"),
                reason=reason,
            )
            confirmation = result.stop_confirmation
            if result.stop_confirmation == "confirmed":
                await self._remove_stopped(name)
            return result
        except asyncio.CancelledError:
            confirmation = await self._cleanup_cancelled_execute(
                job, process=process,
                tasks=(process_wait, output_task, cancel_task, timeout_task),
                stop_confirmation=confirmation,
            )
            if confirmation == "confirmed":
                raise CommandCancelledError(job, confirmation) from None
            raise
        finally:
            tasks = (process_wait, output_task, cancel_task, timeout_task)
            for task in tasks:
                if not task.done():
                    task.cancel()
            if not all(task.done() for task in tasks):
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                except asyncio.CancelledError:
                    confirmation = await self._cleanup_cancelled_execute(
                        job, process=process, tasks=tasks, stop_confirmation=confirmation,
                    )
                    if confirmation == "confirmed":
                        raise CommandCancelledError(job, confirmation) from None
                    raise

    async def _cleanup_cancelled_execute(self, job: CommandJob, *, process=None, tasks=(), stop_confirmation="unconfirmed") -> str:
        """再取消でも cleanup を切り離さず、その呼出しの一次停止確認を返す。"""
        async def cleanup() -> str:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            confirmation = stop_confirmation
            try:
                try:
                    name = self._container_name(job)
                    if confirmation != "confirmed":
                        confirmation = await self._stop_name(name, job.grace_seconds)
                    if confirmation == "confirmed":
                        await self._remove_stopped(name)
                finally:
                    if process is not None:
                        await self._reap_process(process)
            except (Exception, asyncio.CancelledError) as exc:
                if confirmation != "confirmed":
                    raise
                log.warning("docker.cancel_cleanup_failed", error_type=type(exc).__name__)
            return confirmation

        cleanup_task = asyncio.create_task(cleanup())
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                # shield は cleanup を守るが caller の再取消は待機を中断する。
                # 結果を取り切ってから元の取消を伝え、証拠を取り落とさない。
                continue
        return cleanup_task.result()

    async def stop(self, attempt_id: str, generation: int, grace_seconds: float) -> str:
        """deterministic name の container を停止し、inspect で停止を確認する。"""
        _validate_identifier(attempt_id, "attempt_id")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("generation must be a non-negative integer")
        _validate_nonnegative_finite(grace_seconds, "grace_seconds")
        name = f"{_CONTAINER_PREFIX}{attempt_id}-{generation}"
        confirmation = await self._stop_name(name, grace_seconds)
        if confirmation == "confirmed":
            await self._remove_stopped(name)
        return confirmation

    async def list_stopped_containers(self) -> list[tuple[str, str]]:
        """Kotoyose label の terminal container だけを列挙する。削除前には再 inspect する。"""
        try:
            process = await self._spawn(
                "ps", "-a", "--no-trunc", "--filter", "label=kotoyose.attempt_id",
                "--filter", "status=exited", "--filter", "status=dead",
                "--format={{.ID}} {{.Names}}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await self._communicate_control(
                process, output_limit=_CONTAINER_LIST_OUTPUT_LIMIT,
            )
            if process.returncode != 0:
                raise RuntimeError("docker ps failed")
            rows = []
            for line in stdout.decode("utf-8").splitlines():
                parts = line.split()
                if len(parts) == 2 and _expected_container_identity(parts[1]) is not None:
                    rows.append((parts[0], parts[1]))
            return rows
        except (OSError, RuntimeError, UnicodeDecodeError, asyncio.TimeoutError, _OutputLimitExceeded):
            log.warning("docker.container_list_failed")
            return []

    async def inspect_stopped_container(self, name: str, container_id: str) -> _ContainerState | None:
        """再利用された名前・異なる世代・running state を削除候補から外す。"""
        identity = _expected_container_identity(name)
        if identity is None:
            return None
        state = await self._inspect(name)
        if (not _is_stopped_state(state) or state.container_id != container_id
                or not _matches_container_identity(state, *identity)):
            return None
        return state

    async def _remove_stopped(self, name: str, container_id: str | None = None) -> bool:
        """停止済み実体を ID で削除する。失敗は実行結果に影響させない。"""
        try:
            if container_id is None:
                state = await self._inspect(name)
                if state is None or state.status == "created":
                    return False
                identity = _expected_container_identity(name)
                if not _is_stopped_state(state) or identity is None or not _matches_container_identity(state, *identity):
                    log.warning("docker.container_remove_unconfirmed", container_name=name)
                    return False
                container_id = state.container_id
            else:
                if await self.inspect_stopped_container(name, container_id) is None:
                    log.warning("docker.container_remove_unconfirmed", container_name=name)
                    return False
            process = await self._spawn(
                "rm", container_id,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await self._communicate_control(process)
            if process.returncode == 0:
                return True
        except Exception as exc:
            log.warning(
                "docker.container_remove_failed", container_name=name,
                container_id=container_id, error_type=type(exc).__name__,
            )
            return False
        log.warning("docker.container_remove_failed", container_name=name, container_id=container_id)
        return False

    async def probe(self) -> bool:
        """image inspect のみを実行し、image を pull せず存在を確認する。"""
        try:
            process = await self._spawn(
                "image",
                "inspect",
                self.image_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await self._communicate_control(process)
            return process.returncode == 0
        except (OSError, RuntimeError, asyncio.TimeoutError):
            return False

    def _validate_workspace(self, workspace: Path) -> Path:
        if workspace == Path("/") or workspace.is_symlink():
            raise ValueError("workspace must be a non-root canonical directory")
        try:
            canonical = workspace.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("workspace must resolve to an existing directory") from exc
        if canonical == Path("/") or not canonical.is_dir():
            raise ValueError("workspace must be a non-root directory")
        _validate_mount_path(canonical)
        return canonical

    def _container_name(self, job: CommandJob) -> str:
        return f"{_CONTAINER_PREFIX}{job.attempt_id}-{job.generation}"

    def _create_argv(self, job: CommandJob, workspace: Path, name: str) -> list[str]:
        _validate_mount_path(workspace)
        cwd = Path(job.cwd)
        resolved_cwd = (workspace / cwd).resolve(strict=False)
        if not resolved_cwd.is_relative_to(workspace):
            raise ValueError("cwd resolves outside workspace")
        workdir = "/workspace" if job.cwd == "." else f"/workspace/{cwd.as_posix()}"
        command = [
            "create",
            "--name",
            name,
            "--pull=never",
            "--restart=no",
            "--label",
            f"kotoyose.attempt_id={job.attempt_id}",
            "--label",
            f"kotoyose.generation={job.generation}",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(job.pids_limit),
            "--memory",
            str(job.memory_bytes),
            "--memory-swap",
            str(job.memory_bytes),
            "--cpus",
            str(job.cpus),
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            _TMPFS,
            "--mount",
            f"type=bind,src={workspace},dst=/workspace",
            "--workdir",
            workdir,
            "--env",
            "HOME=/tmp",
            "--env",
            f"PATH={_MINIMAL_PATH}",
        ]
        for key in sorted(job.env):
            command.extend(("--env", f"{key}={job.env[key]}"))
        command.extend(("--entrypoint", job.argv[0], self.image_id, *job.argv[1:]))
        return command

    async def _create_container(self, job: CommandJob, workspace: Path, name: str) -> bool:
        try:
            process = await self._spawn(
                *self._create_argv(job, workspace, name),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await self._communicate_control(process)
        except (OSError, RuntimeError, asyncio.TimeoutError, _OutputLimitExceeded):
            return False
        return process.returncode == 0 and bool(stdout.strip())

    async def _stop_name(self, name: str, grace_seconds: float) -> str:
        """名前の所有を検証してから immutable ID を停止し、同じ実体を確認する。"""
        grace = str(max(0, math.ceil(grace_seconds)))
        deadline = asyncio.get_running_loop().time() + grace_seconds + _CONTROL_DEADLINE_SECONDS
        identity = _expected_container_identity(name)
        state = await self._inspect(name, timeout_seconds=max(0.0, deadline - asyncio.get_running_loop().time()))
        if state is None or identity is None or not _matches_container_identity(state, *identity):
            return "unconfirmed"
        container_id = state.container_id
        if _is_stopped_state(state):
            return "confirmed"
        try:
            process = await self._spawn(
                "stop",
                "--time",
                grace,
                container_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await self._communicate_control(
                process,
                timeout_seconds=max(0.0, deadline - asyncio.get_running_loop().time()),
            )
        except (OSError, RuntimeError, asyncio.TimeoutError, _OutputLimitExceeded):
            pass
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        state = await self._inspect(container_id, timeout_seconds=remaining)
        if state is None or state.container_id != container_id or not _matches_container_identity(state, *identity):
            return "unconfirmed"
        if _is_stopped_state(state):
            return "confirmed"
        if state.running or state.status != "created":
            return "unconfirmed"
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        try:
            process = await self._spawn(
                "rm",
                "--force",
                state.container_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await self._communicate_control(process, timeout_seconds=remaining)
        except (OSError, RuntimeError, asyncio.TimeoutError, _OutputLimitExceeded):
            return "unconfirmed"
        return "confirmed" if process.returncode == 0 else "unconfirmed"

    async def _inspect(
        self,
        name: str,
        *,
        timeout_seconds: float | None = None,
    ) -> _ContainerState | None:
        try:
            process = await self._spawn(
                "inspect",
                "--format={{json .}}",
                name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await self._communicate_control(process, timeout_seconds)
        except (OSError, RuntimeError, asyncio.TimeoutError, _OutputLimitExceeded):
            return None
        if process.returncode != 0:
            return None
        try:
            decoded = json.loads(stdout.decode("utf-8").strip())
            if isinstance(decoded, list):
                decoded = decoded[0]
            if not isinstance(decoded, dict):
                return None
            state_decoded = decoded.get("State", decoded)
            if not isinstance(state_decoded, dict) or not isinstance(
                state_decoded.get("Running"), bool
            ):
                return None
            exit_code = state_decoded.get("ExitCode")
            if not isinstance(exit_code, int) or isinstance(exit_code, bool):
                exit_code = None
            status = state_decoded.get("Status")
            if not isinstance(status, str):
                status = None
            finished_at = state_decoded.get("FinishedAt")
            try:
                finished_at = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
                if finished_at.year <= 1 or finished_at.tzinfo is None:
                    finished_at = None
                else:
                    finished_at = finished_at.astimezone(timezone.utc)
            except (AttributeError, TypeError, ValueError):
                finished_at = None
            container_id = decoded.get("Id")
            if not isinstance(container_id, str) or not container_id:
                container_id = None
            config = decoded.get("Config")
            labels = config.get("Labels") if isinstance(config, dict) else {}
            if not isinstance(labels, dict):
                labels = {}
            labels = {
                key: value for key, value in labels.items() if isinstance(key, str) and isinstance(value, str)
            }
            return _ContainerState(
                running=state_decoded["Running"],
                exit_code=exit_code,
                status=status,
                container_id=container_id,
                labels=labels,
                finished_at=finished_at,
            )
        except (UnicodeDecodeError, IndexError, json.JSONDecodeError):
            return None

    async def _spawn(self, *args: str, **kwargs: object):
        return await asyncio.create_subprocess_exec(self.docker_binary, *args, **kwargs)

    async def _communicate_control(
        self,
        process,
        timeout_seconds: float | None = None,
        *,
        output_limit: int = _CONTROL_OUTPUT_LIMIT,
    ) -> tuple[bytes, bytes]:
        """制御コマンドを bounded I/O と hard wall deadline 付きで回収する。"""
        timeout_seconds = (
            _CONTROL_DEADLINE_SECONDS if timeout_seconds is None else timeout_seconds
        )
        collector = _OutputCollector(output_limit)
        wait_task = asyncio.create_task(process.wait())
        output_task = asyncio.create_task(self._collect_output(process, collector))
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        try:
            done, _ = await asyncio.wait(
                {wait_task, output_task},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise asyncio.TimeoutError
            if output_task in done:
                output_task.result()
            if wait_task in done:
                wait_task.result()
            if output_task not in done:
                remaining = deadline - asyncio.get_running_loop().time()
                await asyncio.wait_for(asyncio.shield(output_task), max(0, remaining))
                output_task.result()
            if wait_task not in done:
                remaining = deadline - asyncio.get_running_loop().time()
                await asyncio.wait_for(asyncio.shield(wait_task), max(0, remaining))
                wait_task.result()
            return bytes(collector.stdout), bytes(collector.stderr)
        except asyncio.CancelledError:
            await self._cancel_tasks(wait_task, output_task)
            await asyncio.shield(self._reap_process(process))
            raise
        except Exception:
            await self._cancel_tasks(wait_task, output_task)
            await self._reap_process(process)
            raise
        finally:
            await self._cancel_tasks(wait_task, output_task)

    async def _cancel_tasks(self, *tasks: asyncio.Task) -> None:
        """内部の待機 task を cancel して例外を回収する。"""
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _collect_output(self, process, collector: _OutputCollector) -> None:
        async def read_stream(stream_name: str, stream) -> None:
            if stream is None:
                return
            while True:
                chunk = await stream.read(min(_READ_CHUNK_SIZE, collector.limit - collector.total + 1))
                if not chunk:
                    return
                collector.append(stream_name, chunk)

        readers = (
            asyncio.create_task(read_stream("stdout", getattr(process, "stdout", None))),
            asyncio.create_task(read_stream("stderr", getattr(process, "stderr", None))),
        )
        try:
            await asyncio.gather(*readers)
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

    async def _reap_process(self, process) -> None:
        try:
            if getattr(process, "returncode", None) is None:
                terminate = getattr(process, "terminate", None)
                if callable(terminate):
                    terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=_REAP_TIMEOUT_SECONDS)
            except (asyncio.TimeoutError, OSError, RuntimeError):
                kill = getattr(process, "kill", None)
                if callable(kill):
                    kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=_REAP_TIMEOUT_SECONDS)
                except (asyncio.TimeoutError, OSError, RuntimeError):
                    pass
        finally:
            # 取消された Docker command は wait() 後も pipe transport を保持し得るため、
            # 再起動した remote gateway に FileIO を残さないよう明示的に閉じる。
            transport = getattr(process, "_transport", None)
            get_pipe = getattr(transport, "get_pipe_transport", None)
            if callable(get_pipe):
                for fd in (1, 2):
                    pipe = get_pipe(fd)
                    if pipe is not None:
                        pipe.close()
