"""保存済みの private namespace 所有証拠から、終了枠を安全に回復する。"""

from copy import deepcopy
import asyncio
from dataclasses import asdict
import os
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kotoyose import codex_stop_recovery
from kotoyose.codex_stop_evidence import STAGES, StopComponents, make_stop_evidence, stop_evidence_context
from kotoyose.execution_resources import ExecutionError
from kotoyose.execution_service import ExecutionService
from kotoyose.codex_process_stop import _children, _private_init
from kotoyose.process_identity import capture_process_identity, stop_identified_process
from tests.test_codex_stop_evidence import saved as saved


def partial(saved):
    attempt, ownership, _ = saved
    attempt.update(finished_at="finished", status="timeout", reason="soft_timeout",
                   stop_confirmation="unconfirmed", side_effect_resolution="unresolved")
    proof = make_stop_evidence(stop_evidence_context(attempt), ownership,
        StopComponents(**{**dict.fromkeys(STAGES, "confirmed"), "scope": "unconfirmed"}), 0)
    return attempt, {**ownership, "codex_stop_evidence": proof.model_dump()}


async def test_gone_scope_recovers_only_after_namespace_exit_is_durably_recorded(saved, monkeypatch):
    attempt, result = partial(saved)
    original = deepcopy(result)
    stop = AsyncMock(return_value="confirmed")
    monkeypatch.setattr(codex_stop_recovery, "stop_codex_process", stop)
    monkeypatch.setattr(codex_stop_recovery, "stop_recovered_provider_scope", AsyncMock(return_value="unconfirmed"))
    record = AsyncMock()
    assert await codex_stop_recovery.recover_codex_stop(attempt, result, record_namespace_stop=record) == "confirmed"
    record.assert_awaited_once()
    assert record.await_args.args[0]["source_stop_digest"] == result["codex_stop_evidence"]["digest"]
    assert stop.await_args.kwargs["namespace_init"].pid == result["codex_provider_scope"]["namespace_init"]["pid"]
    assert result == original


@pytest.mark.parametrize("failure", ["no_writer", "write_failed", "namespace_alive"])
async def test_missing_observation_or_persistence_never_releases_a_slot(saved, monkeypatch, failure):
    attempt, result = partial(saved)
    monkeypatch.setattr(codex_stop_recovery, "stop_codex_process",
                        AsyncMock(return_value="unconfirmed" if failure == "namespace_alive" else "confirmed"))
    monkeypatch.setattr(codex_stop_recovery, "stop_recovered_provider_scope", AsyncMock(return_value="unconfirmed"))
    writer = None if failure == "no_writer" else AsyncMock(side_effect=RuntimeError("private detail") if failure == "write_failed" else None)
    assert await codex_stop_recovery.recover_codex_stop(attempt, result, record_namespace_stop=writer) == "unconfirmed"
    if failure == "namespace_alive":
        writer.assert_not_awaited()


@pytest.mark.parametrize("field", ["provider", "tasks", "pipes", "egress", "pending_tasks", "missing", "corrupt", "init_missing"])
async def test_partial_or_unowned_history_cannot_be_repaired_from_namespace_absence(saved, monkeypatch, field):
    attempt, result = partial(saved)
    ownership = {k: v for k, v in result.items() if k != "codex_stop_evidence"}
    states = {**dict.fromkeys(STAGES, "confirmed"), "scope": "unconfirmed"}
    if field in states:
        states[field] = "unconfirmed"
    proof = make_stop_evidence(stop_evidence_context(attempt), ownership, StopComponents(**states), int(field == "pending_tasks"))
    result["codex_stop_evidence"] = proof.model_dump()
    if field == "missing":
        result.pop("codex_stop_evidence")
    elif field == "corrupt":
        result["codex_stop_evidence"]["digest"] = "0" * 64
    elif field == "init_missing":
        result["codex_provider_scope"].pop("namespace_init")
    monkeypatch.setattr(codex_stop_recovery, "stop_codex_process", AsyncMock(return_value="confirmed"))
    monkeypatch.setattr(codex_stop_recovery, "stop_recovered_provider_scope", AsyncMock(return_value="unconfirmed"))
    record = AsyncMock()
    assert await codex_stop_recovery.recover_codex_stop(attempt, result, record_namespace_stop=record) == "unconfirmed"
    record.assert_not_awaited()


async def test_saved_recovery_survives_another_restart_without_touching_processes(saved, monkeypatch):
    attempt, result = partial(saved)
    monkeypatch.setattr(codex_stop_recovery, "stop_codex_process", AsyncMock(return_value="confirmed"))
    monkeypatch.setattr(codex_stop_recovery, "stop_recovered_provider_scope", AsyncMock(return_value="unconfirmed"))
    record = AsyncMock()
    assert await codex_stop_recovery.recover_codex_stop(attempt, result, record_namespace_stop=record) == "confirmed"
    result["codex_namespace_stop_evidence"] = record.await_args.args[0]
    forbidden = AsyncMock(side_effect=AssertionError("Saved stop must not reopen a process"))
    monkeypatch.setattr(codex_stop_recovery, "stop_codex_process", forbidden)
    monkeypatch.setattr(codex_stop_recovery, "stop_recovered_provider_scope", forbidden)
    assert await codex_stop_recovery.recover_codex_stop(attempt, result) == "confirmed"
    forbidden.assert_not_awaited()


@pytest.mark.parametrize("drift", ["receipt", "attempt", "generation", "workspace", "source", "owner"])
async def test_saved_namespace_receipt_cannot_be_corrupted_or_replayed(saved, monkeypatch, drift):
    from kotoyose.codex_stop_evidence import make_namespace_stop_evidence
    attempt, result = partial(saved)
    ownership = {k: v for k, v in result.items() if k != "codex_stop_evidence"}
    result["codex_namespace_stop_evidence"] = make_namespace_stop_evidence(
        stop_evidence_context(attempt), ownership, result["codex_stop_evidence"]).model_dump()
    if drift == "receipt":
        result["codex_namespace_stop_evidence"]["digest"] = "0" * 64
    elif drift == "attempt":
        attempt["id"] = "different-attempt"
    elif drift == "generation":
        attempt["generation"] += 1
    elif drift == "workspace":
        attempt["snapshot"]["workspace_inode"] += 1
    elif drift == "source":
        result["codex_stop_evidence"]["pending_tasks"] = 1
    else:
        result["pid"] += 1
    forbidden = AsyncMock(side_effect=AssertionError("Invalid evidence must not reach live stop"))
    monkeypatch.setattr(codex_stop_recovery, "stop_codex_process", forbidden)
    monkeypatch.setattr(codex_stop_recovery, "stop_recovered_provider_scope", forbidden)
    assert await codex_stop_recovery.recover_codex_stop(attempt, result) == "unconfirmed"
    forbidden.assert_not_awaited()


async def test_saved_stop_proof_does_not_depend_on_current_execution_configuration(saved):
    attempt, ownership, proof = saved
    operation = {"id": "op", "kind": "provider_process", "state": "unknown",
                 "result": {**ownership, "codex_stop_evidence": proof.model_dump()}}
    service = ExecutionService.__new__(ExecutionService)
    service.store = SimpleNamespace(operations=AsyncMock(return_value=[operation]), resolve_operation=AsyncMock())
    def changed_worker(_snapshot):
        raise ExecutionError("request_conflict", "Pinned executor changed", 409)
    service._worker_for = changed_worker
    assert await service._stop_attempt(attempt) == "confirmed"
    service.store.resolve_operation.assert_awaited_once_with("op", "confirmed")


async def test_live_scope_recovery_without_a_receipt_keeps_legacy_snapshot_compatibility(saved, monkeypatch):
    attempt, ownership, _ = saved
    attempt["snapshot"] = {}
    monkeypatch.setattr(codex_stop_recovery, "stop_codex_process", AsyncMock(return_value="confirmed"))
    monkeypatch.setattr(codex_stop_recovery, "stop_recovered_provider_scope", AsyncMock(return_value="confirmed"))
    assert await codex_stop_recovery.recover_codex_stop(attempt, ownership) == "confirmed"


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="PID namespace verification requires bubblewrap")
async def test_real_namespace_exit_reaps_children_and_recovers_without_a_live_cgroup(tmp_path, monkeypatch):
    command = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL"]
    for path in ("/usr", "/bin", "/lib", "/lib64"):
        if os.path.lexists(path):
            command.extend(("--ro-bind", path, path))
    command.extend(("--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--clearenv",
        "--", "/usr/bin/python3", "-u", "-c",
        "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);print('ready',flush=True);time.sleep(60)"))
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    unrelated = await asyncio.create_subprocess_exec("/usr/bin/sleep", "60")
    try:
        ready = await asyncio.wait_for(process.stdout.readline(), 5)
        if ready != b"ready\n":
            diagnostic = await process.stderr.read()
            if b"No permissions" in diagnostic or b"Operation not permitted" in diagnostic:
                pytest.skip("Host policy does not permit a private PID namespace")
            pytest.fail("Private PID namespace fixture did not start")
        root = capture_process_identity(process.pid)
        children = _children(root)
        init = _private_init(children)
        assert init is not None and len(children) >= 2
        info = tmp_path.stat()
        attempt = {"id": "real-namespace", "generation": 0, "finished_at": "finished", "snapshot": {
            "workspace_device": info.st_dev, "workspace_inode": info.st_ino}}
        ownership = {**asdict(root), "codex_provider_scope": {
            "unit": "kotoyose-native-real-namespace-provider-0.scope", "control_group": "/expired-owned-scope",
            "device": 1, "inode": 1, "namespace_init": asdict(init)}}
        assert await stop_identified_process(init, .2) == "confirmed"
        await asyncio.wait_for(process.wait(), 3)
        for child in children:
            try:
                assert capture_process_identity(child.pid) != child
            except RuntimeError:
                pass
        source = make_stop_evidence(stop_evidence_context(attempt), ownership,
            StopComponents(**{**dict.fromkeys(STAGES, "confirmed"), "scope": "unconfirmed"}), 0)
        monkeypatch.setattr(codex_stop_recovery, "stop_recovered_provider_scope", AsyncMock(return_value="unconfirmed"))
        record = AsyncMock()
        assert await codex_stop_recovery.recover_codex_stop(attempt,
            {**ownership, "codex_stop_evidence": source.model_dump()}, record_namespace_stop=record) == "confirmed"
        record.assert_awaited_once()
        assert unrelated.returncode is None
    finally:
        for child in (process, unrelated):
            if child.returncode is None:
                child.kill()
            await child.wait()
