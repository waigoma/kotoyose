"""Codex の停止不明を recovery が外側 process gone だけで確定しない。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kotoyose import codex_process_stop, execution_service
from kotoyose.codex_process_stop import stop_codex_process
from kotoyose.execution_service import ExecutionService
from kotoyose.process_identity import capture_process_identity


async def test_disappeared_root_never_proves_private_namespace_stop():
    process = await asyncio.create_subprocess_exec("/usr/bin/python3", "-c", "import time;time.sleep(.1)")
    identity = capture_process_identity(process.pid)
    await process.wait()
    assert await stop_codex_process(identity, .1) == "unconfirmed"


@pytest.mark.parametrize("provider", ["codex", "claude"])
async def test_recovered_codex_operation_reuses_namespace_stop_proof(monkeypatch, provider):
    process = await asyncio.create_subprocess_exec("/usr/bin/python3", "-c", "import time;time.sleep(.1)")
    from dataclasses import asdict
    identity = asdict(capture_process_identity(process.pid))
    await process.wait()
    root_only = AsyncMock(return_value="confirmed")
    monkeypatch.setattr(execution_service, "stop_identified_process", root_only)
    service = ExecutionService.__new__(ExecutionService)
    service.store = SimpleNamespace(operations=AsyncMock(return_value=[{
        "id": "provider-op", "kind": "provider_process", "state": "unknown", "result": identity,
    }]), resolve_operation=AsyncMock())
    service._worker_for = lambda snapshot: None
    attempt = {"id": "attempt", "generation": 0, "snapshot": {"kind": "agent", "backend": "native", "provider": provider}}
    stop = await service._stop_attempt(attempt)
    expected = "unconfirmed" if provider == "codex" else "confirmed"
    assert stop == expected
    service.store.resolve_operation.assert_awaited_once_with("provider-op", expected)
    assert root_only.await_count == (0 if provider == "codex" else 1)


async def test_unobserved_or_host_namespace_init_cannot_confirm(monkeypatch):
    identity = capture_process_identity(__import__("os").getpid())
    monkeypatch.setattr(codex_process_stop, "_children", lambda root: [])
    stop = AsyncMock(return_value="confirmed")
    monkeypatch.setattr(codex_process_stop, "stop_identified_process", stop)
    assert await stop_codex_process(identity, .1) == "unconfirmed"
    assert codex_process_stop._private_init([identity]) is None
