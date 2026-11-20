"""This regression runs unchanged against both the exact base and patched modules."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from kotoyose.local_worker import DockerLocalWorker
from tests.command_stop_fixture import _DockerBoundary, _agent
from tests.test_local_worker import IMAGE_ID


@pytest.mark.parametrize("phase", ["create", "start", "main"])
async def test_primary_stop_is_not_replaced_by_post_removal_absence(tmp_path, monkeypatch, phase):
    worker = DockerLocalWorker(IMAGE_ID)
    docker = _DockerBoundary(phase)
    monkeypatch.setattr(worker, "_spawn", docker.spawn)
    removed = False

    async def stop_name(*args):
        return "unconfirmed" if removed else "confirmed"

    async def remove(*args):
        nonlocal removed
        removed = True
        return True

    # Deterministic primary proof and subsequent absence isolate the regression
    # independently of the patch's stricter Docker identity validation.
    primary = AsyncMock(side_effect=stop_name)
    monkeypatch.setattr(worker, "_stop_name", primary)
    monkeypatch.setattr(worker, "_remove_stopped", remove)
    executor, attempt, store, states = _agent(tmp_path, monkeypatch, worker)
    task = asyncio.create_task(executor.execute(attempt, asyncio.Event()))
    await asyncio.wait_for(docker.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert removed
    assert states["command-operation"] == "confirmed"
    assert primary.await_count == 1
    store.resolve_operation.assert_awaited_once_with("command-operation", "confirmed")
