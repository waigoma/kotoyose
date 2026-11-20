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
from kotoyose.execution_agent import AgentExecutor
from kotoyose.local_worker import DockerLocalWorker
from tests.test_local_worker import IMAGE_ID, _FakeProcess, _inspect_output, _job


class _DockerBoundary:
    """Only the Docker subprocess boundary is fake; cancellation/inspect/stop are real."""

    def __init__(self, phase, *, evidence="valid"):
        self.phase = phase
        self.evidence = evidence
        self.entered = asyncio.Event()
        self.calls = []
        self.exists = False
        self.status = "created"
        self.operation = None
        self.generation = None
        self.process = _FakeProcess(returncode=None, wait_gate=asyncio.Event())
        wait = self.process.wait

        async def main_wait():
            self.entered.set()
            return await wait()

        self.process.wait = main_wait

    async def spawn(self, *args, **kwargs):
        self.calls.append(args)
        command = args[0]
        if command == "create":
            labels = [args[index + 1] for index, value in enumerate(args) if value == "--label"]
            self.operation = next(value.split("=", 1)[1] for value in labels if value.startswith("kotoyose.attempt_id="))
            self.generation = next(value.split("=", 1)[1] for value in labels if value.startswith("kotoyose.generation="))
            self.exists = True
            if self.evidence == "running":
                self.status = "running"
            if self.phase == "create":
                self.entered.set()
                await asyncio.Event().wait()
            return _FakeProcess(stdout=[b"container-id\n"])
        if command == "start":
            self.status = "running"
            if self.phase == "start":
                self.entered.set()
                await asyncio.Event().wait()
            return self.process
        if command == "inspect":
            if self.evidence == "inspect_error":
                raise OSError("fixture inspect unavailable")
            if not self.exists or self.evidence == "missing":
                return _FakeProcess(returncode=1, stderr=[b"No such container"])
            labels = {"kotoyose.attempt_id": self.operation, "kotoyose.generation": self.generation}
            if self.evidence == "foreign":
                labels["kotoyose.attempt_id"] = "foreign-operation"
            if self.evidence == "wrong_generation":
                labels["kotoyose.generation"] = "99"
            return _FakeProcess(stdout=[_inspect_output(
                self.status == "running", 137 if self.status == "exited" else None, self.status,
                container_id="container-id", labels=labels,
            )])
        if command == "stop":
            assert args[-1] == "container-id", "Only the inspected immutable ID may be stopped"
            if self.status == "running" and self.evidence != "running":
                self.status = "exited"
            return _FakeProcess()
        if command == "rm":
            assert args[-1] == "container-id"
            self.exists = False
            return _FakeProcess()
        raise AssertionError(args)


def _agent(tmp_path, monkeypatch, worker, *, database_error=False):
    binary = tmp_path / "never-executed-provider"
    binary.write_bytes(b"fixture only, not executable")
    snapshot = {
        "backend": "native",
        "alias": "fixture", "provider": "claude", "model": "fixture-model",
        "cli_binary": str(binary), "cli_digest": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "credential_mode": "environment", "credential_env": "FIXTURE_TOKEN",
        "credential_name": "ANTHROPIC_API_KEY", "workspace_path": str(tmp_path),
        "deadline": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "timeout": {"hard_seconds": 30, "grace_seconds": 0},
        "budget": {"output_limit_bytes": 1024, "memory_bytes": 1024 * 1024, "pids_limit": 8, "cpus": 1},
        "definition": {"output_contract": "text", "max_turns": 1}, "tool_refs": ["builtin:sandbox-exec@1"],
    }
    # credential は process_env 経由で読む (env の scrub、ADR-0010 段 1)。実際の秘密には触れない。
    monkeypatch.setattr(execution_agent.process_env, "get", lambda name, default=None: {"FIXTURE_TOKEN": "fake"}.get(name, default))
    states = {"provider-operation": "sent", "command-operation": "sent"}

    async def resolve(operation, confirmation):
        if database_error:
            raise RuntimeError("fixture database unavailable")
        states[operation] = "confirmed" if confirmation == "confirmed" else "unknown"

    store = SimpleNamespace(
        prepare_operation=AsyncMock(side_effect=["provider-operation", "command-operation"]),
        resolve_operation=AsyncMock(side_effect=resolve), append_event=AsyncMock(), client_tools=SimpleNamespace(),
    )

    async def runtime(job, cancel, execute_tool, **kwargs):
        return await execute_tool(("true",), ".")

    executor = object.__new__(AgentExecutor)
    executor.config = SimpleNamespace()
    executor.store = store
    executor.worker = worker
    executor.runtime = SimpleNamespace(execute=runtime)
    executor._render_inputs = AsyncMock(return_value=("fixture", "fixture", []))
    attempt = {"id": "parent-attempt", "principal": "fixture", "generation": 3, "snapshot": snapshot, "request": {}}
    return executor, attempt, store, states
