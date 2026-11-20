"""Cancellation proof survives real worker -> agent control flow without Docker/model I/O."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kotoyose import execution_agent
from kotoyose.command_worker import CommandCancelledError
from kotoyose.execution_agent import AgentExecutor
from kotoyose.local_worker import DockerLocalWorker
from tests.test_local_worker import IMAGE_ID, _FakeProcess, _inspect_output, _job


from tests.command_stop_fixture import _DockerBoundary, _agent


@pytest.mark.parametrize("phase", ["create", "start", "main"])
async def test_real_worker_agent_preserves_primary_stop_after_removal(tmp_path, monkeypatch, phase):
    docker = _DockerBoundary(phase)
    worker = DockerLocalWorker(IMAGE_ID)
    monkeypatch.setattr(worker, "_spawn", docker.spawn)
    fallback = AsyncMock(wraps=worker.stop)
    monkeypatch.setattr(worker, "stop", fallback)
    executor, attempt, store, states = _agent(tmp_path, monkeypatch, worker)
    task = asyncio.create_task(executor.execute(attempt, asyncio.Event()))
    await asyncio.wait_for(docker.entered.wait(), 1)
    task.cancel()
    with pytest.raises(CommandCancelledError) as raised:
        await task
    assert task.cancelled()
    assert raised.value.job.attempt_id == "command-operation"
    assert raised.value.job.generation == 3
    assert raised.value.stop_confirmation == "confirmed"
    assert not docker.exists
    assert len([args for args in docker.calls if args[0] == "stop"]) == 1
    fallback.assert_not_awaited()
    store.resolve_operation.assert_awaited_once_with("command-operation", "confirmed")
    assert states == {"provider-operation": "sent", "command-operation": "confirmed"}
    store.append_event.assert_not_awaited()


@pytest.mark.parametrize("phase", ["create", "start", "main"])
@pytest.mark.parametrize("evidence", ["missing", "foreign", "wrong_generation", "inspect_error", "running"])
async def test_cancel_unknown_evidence_stays_unconfirmed(tmp_path, monkeypatch, phase, evidence):
    docker = _DockerBoundary(phase, evidence=evidence)
    worker = DockerLocalWorker(IMAGE_ID)
    monkeypatch.setattr(worker, "_spawn", docker.spawn)
    fallback = AsyncMock(wraps=worker.stop)
    monkeypatch.setattr(worker, "stop", fallback)
    executor, attempt, store, states = _agent(tmp_path, monkeypatch, worker)
    task = asyncio.create_task(executor.execute(attempt, asyncio.Event()))
    await asyncio.wait_for(docker.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as raised:
        await task
    assert not isinstance(raised.value, CommandCancelledError)
    fallback.assert_awaited_once_with("command-operation", 3, 0)
    store.resolve_operation.assert_awaited_once_with("command-operation", "unconfirmed")
    assert states["command-operation"] == "unknown"
    assert not any(args[0] == "rm" for args in docker.calls)
    if evidence != "running":
        assert not any(args[0] == "stop" for args in docker.calls)


@pytest.mark.parametrize("kind", ["cancel", "error", "foreign_operation", "foreign_generation", "other_job", "unknown", "duck_typed"])
async def test_agent_rejects_unscoped_or_unconfirmed_proof(tmp_path, monkeypatch, kind):
    async def execute(job, cancel):
        if kind == "cancel":
            raise asyncio.CancelledError
        if kind == "error":
            raise RuntimeError("fixture execution failed")
        if kind == "duck_typed":
            error = asyncio.CancelledError()
            error.job, error.stop_confirmation = job, "confirmed"
            raise error
        proof_job = job
        if kind == "foreign_operation":
            proof_job = replace(job, attempt_id="foreign-operation")
        elif kind == "foreign_generation":
            proof_job = replace(job, generation=4)
        elif kind == "other_job":
            proof_job = replace(job)
        raise CommandCancelledError(proof_job, "unconfirmed" if kind == "unknown" else "confirmed")

    worker = SimpleNamespace(execute=execute, stop=AsyncMock(return_value="unconfirmed"))
    executor, attempt, store, states = _agent(tmp_path, monkeypatch, worker)
    with pytest.raises((asyncio.CancelledError, RuntimeError)):
        await executor.execute(attempt, asyncio.Event())
    worker.stop.assert_awaited_once_with("command-operation", 3, 0)
    store.resolve_operation.assert_awaited_once_with("command-operation", "unconfirmed")
    assert states["command-operation"] == "unknown"


async def test_confirmed_cleanup_database_failure_does_not_resolve_operation(tmp_path, monkeypatch):
    docker = _DockerBoundary("main")
    worker = DockerLocalWorker(IMAGE_ID)
    monkeypatch.setattr(worker, "_spawn", docker.spawn)
    worker.stop = AsyncMock(wraps=worker.stop)
    executor, attempt, store, states = _agent(tmp_path, monkeypatch, worker, database_error=True)
    task = asyncio.create_task(executor.execute(attempt, asyncio.Event()))
    await asyncio.wait_for(docker.entered.wait(), 1)
    task.cancel()
    with pytest.raises(RuntimeError, match="database unavailable"):
        await task
    worker.stop.assert_not_awaited()
    assert states["command-operation"] == "sent"
    store.resolve_operation.assert_awaited_once_with("command-operation", "confirmed")


@pytest.mark.parametrize("phase,during", [
    (phase, during) for phase in ("create", "start", "main") for during in ("stop", "remove")
] + [("main", "reap")])
async def test_repeated_cancellation_waits_for_same_cleanup(tmp_path, monkeypatch, phase, during):
    docker = _DockerBoundary(phase)
    worker = DockerLocalWorker(IMAGE_ID)
    monkeypatch.setattr(worker, "_spawn", docker.spawn)
    entered, release = asyncio.Event(), asyncio.Event()
    method = {"stop": "_stop_name", "remove": "_remove_stopped", "reap": "_reap_process"}[during]
    original = getattr(worker, method)

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(worker, method, delayed)
    job = _job(tmp_path)
    task = asyncio.create_task(worker.execute(job, asyncio.Event()))
    await asyncio.wait_for(docker.entered.wait(), 1)
    task.cancel()
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(CommandCancelledError) as raised:
        await asyncio.wait_for(task, 1)
    assert raised.value.job is job
    assert raised.value.stop_confirmation == "confirmed"
    assert not docker.exists
    assert len([args for args in docker.calls if args[0] == "stop"]) == 1


@pytest.mark.parametrize("method", ["_remove_stopped", "_reap_process"])
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_post_confirmation_cleanup_error_preserves_proof(tmp_path, monkeypatch, method, error):
    docker = _DockerBoundary("main")
    worker = DockerLocalWorker(IMAGE_ID)
    monkeypatch.setattr(worker, "_spawn", docker.spawn)
    monkeypatch.setattr(worker, method, AsyncMock(side_effect=error("fixture cleanup failure")))
    task = asyncio.create_task(worker.execute(_job(tmp_path), asyncio.Event()))
    await asyncio.wait_for(docker.entered.wait(), 1)
    task.cancel()
    with pytest.raises(CommandCancelledError) as raised:
        await task
    assert raised.value.stop_confirmation == "confirmed"


async def test_primary_stop_error_uses_agent_fallback(tmp_path, monkeypatch):
    docker = _DockerBoundary("main")
    worker = DockerLocalWorker(IMAGE_ID)
    monkeypatch.setattr(worker, "_spawn", docker.spawn)
    monkeypatch.setattr(worker, "_stop_name", AsyncMock(side_effect=RuntimeError("fixture stop failure")))
    worker.stop = AsyncMock(return_value="unconfirmed")
    executor, attempt, store, states = _agent(tmp_path, monkeypatch, worker)
    task = asyncio.create_task(executor.execute(attempt, asyncio.Event()))
    await asyncio.wait_for(docker.entered.wait(), 1)
    task.cancel()
    with pytest.raises(RuntimeError, match="stop failure"):
        await task
    worker.stop.assert_awaited_once_with("command-operation", 3, 0)
    assert states["command-operation"] == "unknown"


@pytest.mark.parametrize("status", ["running", "created", "exited", "dead"])
@pytest.mark.parametrize("evidence", ["foreign", "wrong_generation"])
async def test_stop_never_touches_foreign_container(tmp_path, monkeypatch, status, evidence):
    docker = _DockerBoundary("main", evidence=evidence)
    docker.operation, docker.generation = "attempt-1", "3"
    docker.exists, docker.status = True, status
    worker = DockerLocalWorker(IMAGE_ID)
    monkeypatch.setattr(worker, "_spawn", docker.spawn)
    assert await worker.stop("attempt-1", 3, 0) == "unconfirmed"
    assert [args[0] for args in docker.calls] == ["inspect"]
    assert docker.exists


@pytest.mark.parametrize("change", ["operation", "generation", "container_id"])
async def test_stop_revalidates_final_immutable_identity(tmp_path, monkeypatch, change):
    from kotoyose.local_worker import _ContainerState
    labels = {"kotoyose.attempt_id": "attempt-1", "kotoyose.generation": "3"}
    initial = _ContainerState(True, None, "running", "container-id", labels)
    final_labels = dict(labels)
    if change == "operation":
        final_labels["kotoyose.attempt_id"] = "foreign"
    elif change == "generation":
        final_labels["kotoyose.generation"] = "4"
    final = _ContainerState(False, 0, "exited", "other-id" if change == "container_id" else "container-id", final_labels)
    worker = DockerLocalWorker(IMAGE_ID)
    monkeypatch.setattr(worker, "_inspect", AsyncMock(side_effect=[initial, final]))
    spawn = AsyncMock(return_value=_FakeProcess())
    monkeypatch.setattr(worker, "_spawn", spawn)
    assert await worker.stop("attempt-1", 3, 0) == "unconfirmed"
    assert spawn.await_count == 1
    assert spawn.call_args.args == ("stop", "--time", "0", "container-id")
    assert [call.args[0] for call in worker._inspect.call_args_list] == ["kotoyose-attempt-1-3", "container-id"]


async def test_fallback_stop_error_leaves_operation_unresolved(tmp_path, monkeypatch):
    worker = SimpleNamespace(
        execute=AsyncMock(side_effect=RuntimeError("fixture execute failure")),
        stop=AsyncMock(side_effect=RuntimeError("fixture stop failure")),
    )
    executor, attempt, store, states = _agent(tmp_path, monkeypatch, worker)
    with pytest.raises(RuntimeError, match="stop failure"):
        await executor.execute(attempt, asyncio.Event())
    store.resolve_operation.assert_not_awaited()
    assert states["command-operation"] == "sent"


@pytest.mark.parametrize("point", ["remove", "final_gather", "start_error_remove"])
async def test_cancel_after_normal_remove_keeps_already_confirmed_stop(tmp_path, monkeypatch, point):
    docker = _DockerBoundary("complete")
    original_spawn = docker.spawn

    async def complete_spawn(*args, **kwargs):
        if args[0] == "start":
            docker.calls.append(args)
            if point == "start_error_remove":
                docker.status = "running"
                raise OSError("fixture start unavailable")
            docker.status = "exited"
            return _FakeProcess()
        return await original_spawn(*args, **kwargs)

    worker = DockerLocalWorker(IMAGE_ID)
    monkeypatch.setattr(worker, "_spawn", complete_spawn)
    removed = asyncio.Event()
    remove = worker._remove_stopped
    first = True

    async def interrupted_remove(*args, **kwargs):
        nonlocal first
        result = await remove(*args, **kwargs)
        if first:
            first = False
            removed.set()
            if point == "final_gather":
                asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
            else:
                await asyncio.Event().wait()
        return result

    monkeypatch.setattr(worker, "_remove_stopped", interrupted_remove)
    worker.stop = AsyncMock(wraps=worker.stop)
    executor, attempt, store, states = _agent(tmp_path, monkeypatch, worker)
    task = asyncio.create_task(executor.execute(attempt, asyncio.Event()))
    await asyncio.wait_for(removed.wait(), 1)
    assert not docker.exists
    if point != "final_gather":
        task.cancel()
    with pytest.raises(CommandCancelledError):
        await task
    worker.stop.assert_not_awaited()
    assert len([args for args in docker.calls if args[0] == "stop"]) == (1 if point == "start_error_remove" else 0)
    store.resolve_operation.assert_awaited_once_with("command-operation", "confirmed")
    assert states["command-operation"] == "confirmed"


@pytest.mark.parametrize("evidence", ["foreign", "wrong_generation"])
async def test_normal_completion_cannot_promote_foreign_exited_state(tmp_path, monkeypatch, evidence):
    docker = _DockerBoundary("complete", evidence=evidence)
    original_spawn = docker.spawn

    async def complete_spawn(*args, **kwargs):
        if args[0] == "start":
            docker.calls.append(args)
            docker.status = "exited"
            return _FakeProcess()
        return await original_spawn(*args, **kwargs)

    worker = DockerLocalWorker(IMAGE_ID)
    monkeypatch.setattr(worker, "_spawn", complete_spawn)
    result = await worker.execute(_job(tmp_path), asyncio.Event())
    assert result.status == "worker_lost"
    assert result.stop_confirmation == "unconfirmed"
    assert not any(args[0] in {"stop", "rm"} for args in docker.calls)
