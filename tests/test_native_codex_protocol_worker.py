"""実 pipe の疑似 app-server で、大累積・有限拒否・終端整合性を検証する。"""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from kotoyose.execution_agent import _ProgressWriter
from kotoyose.native_codex import NativeCodexWorker, _Protocol
from tests.codex_runtime_fixture import codex_rootfs  # noqa: F401
from tests.test_native_claude import _job
from tests.test_native_codex_diagnostics import Journal


CANARY = "PRIVATE_CANARY_protocol_stderr_prompt_headers_7f3a"


@pytest.fixture
def worker(tmp_path, codex_rootfs):
    launcher = tmp_path / "fake-codex"
    launcher.write_text('''#!/usr/bin/python3
import json,sys,time
CANARY="PRIVATE_CANARY_protocol_stderr_prompt_headers_7f3a"
def emit(v):print(json.dumps(v),flush=True)
def event(method,**params):emit({"method":method,"params":{"threadId":"thread","turnId":"turn",**params}})
for line in sys.stdin:
 m=json.loads(line);method=m.get("method")
 if method=="initialize":emit({"id":m["id"],"result":{}})
 elif method=="thread/start":
  mode=m["params"]["developerInstructions"]
  emit({"id":m["id"],"result":{"thread":{"id":"thread"},"model":m["params"]["model"],"modelProvider":"kotoyose_native"}})
 elif method=="turn/start":
  emit({"id":m["id"],"result":{"turn":{"id":"turn"}}})
  if mode in ("frames","mixed","hang","partial_hang"):
   for _ in range(64):event("item/commandExecution/outputDelta",delta=CANARY+"x"*32768)
  if mode in ("stderr","mixed","hang"):
   for _ in range(512):sys.stderr.write((CANARY+"x"*4096)[:4096]);sys.stderr.flush()
  if mode in ("huge","huge_partial"):
   raw=json.dumps({"method":"notice","params":{"text":CANARY+"x"*262144}})
   sys.stdout.write(raw+("\\n" if mode=="huge" else ""));sys.stdout.flush();time.sleep(60)
  if mode=="partial_hang":sys.stdout.write('{"method":"'+CANARY);sys.stdout.flush()
  if mode in ("hang","partial_hang"):time.sleep(60)
  text=json.dumps({"ok":True},separators=(",",":"))
  if mode.startswith("final_"):text=json.dumps({"x":"x"*(1016+(mode=="final_over"))},separators=(",",":"))
  if mode=="final_redacted":text=json.dumps({"x":"key"*120},separators=(",",":"))
  event("item/completed",item={"type":"agentMessage","id":"answer","phase":"final_answer","text":text})
  event("turn/completed",turn={"id":"turn","status":"completed"})
''')
    launcher.chmod(0o700)
    return NativeCodexWorker(launcher), codex_rootfs


async def execute(worker, tmp_path, mode, cancel=None, **overrides):
    runtime, rootfs = worker
    journal, activity = Journal(), []
    writer = _ProgressWriter(journal, "fixture-protocol")
    job = _job(tmp_path, model="gpt-6-luna", cli_binary=Path("/usr/bin/sh"),
               credential_name="OPENAI_API_KEY", credential_value=overrides.pop("credential_value", CANARY), prompt=CANARY,
               instructions=mode, codex_rootfs=rootfs[0], codex_rootfs_digest=rootfs[1],
               output_limit_bytes=1024, output_contract="json", activity_event=activity.append,
               progress_event=overrides.pop("progress_event", writer.emit), **overrides)
    tool = AsyncMock(side_effect=AssertionError("no real tool or provider"))
    try:
        result = await runtime.execute(job, cancel or asyncio.Event(), tool, on_process_started=AsyncMock())
    finally:
        await writer.finish()
    tool.assert_not_awaited()
    return result, journal.records, activity


@pytest.mark.parametrize("mode", ["frames", "stderr", "mixed"])
async def test_large_cumulative_io_preserves_complete_final_json(worker, tmp_path, mode, caplog, capsys):
    result, records, activity = await execute(worker, tmp_path, mode)
    assert (result.status, result.reason, result.stop_confirmation) == ("succeeded", "completed", "confirmed")
    assert json.loads(result.content) == {"ok": True}
    assert records == activity == []
    captured = capsys.readouterr()
    assert CANARY not in repr(result) + caplog.text + captured.out + captured.err


@pytest.mark.parametrize("mode", ["huge", "huge_partial"])
async def test_large_single_frame_fails_with_only_numeric_diagnostics(worker, tmp_path, mode, caplog):
    result, records, activity = await execute(worker, tmp_path, mode)
    assert (result.status, result.reason, result.stop_confirmation) == ("failed", "output_limit_exceeded", "confirmed")
    assert result.content == b"" and len(records) == len(activity) == 1
    assert records[0]["attempt_id"] == "fixture-protocol" and records[0]["type"] == "provider_progress"
    assert asdict(activity[0]) == records[0]["payload"]
    payload = activity[0].payload
    assert set(payload) == {"schema_version", "failure_kind", "failure_stage", "observed", "limit"}
    assert payload["failure_kind"] == "output_limit_exceeded" and payload["failure_stage"] == "protocol_line_bytes"
    assert payload["observed"] > payload["limit"] == 1024 + 65536
    assert CANARY not in json.dumps(records) + repr(result) + repr(activity) + caplog.text


@pytest.mark.parametrize("mode", ["final_exact", "final_over"])
async def test_final_json_limit_is_not_silently_relaxed_or_truncated_to_success(worker, tmp_path, mode):
    result, records, activity = await execute(worker, tmp_path, mode)
    if mode == "final_exact":
        assert result.status == "succeeded" and len(result.content) == 1024
        assert json.loads(result.content) == {"x": "x" * 1016}
        assert records == activity == []
    else:
        assert (result.status, result.reason, result.stop_confirmation) == ("failed", "output_limit_exceeded", "confirmed")
        assert activity[0].payload == {"schema_version": 1, "failure_kind": "output_limit_exceeded",
                                       "failure_stage": "agent_message_bytes", "observed": 1025, "limit": 1024}


@pytest.mark.parametrize("stop", ["cancel", "deadline", "partial_frame_deadline"])
async def test_stop_after_large_cumulative_io_keeps_cleanup_proof(worker, tmp_path, monkeypatch, stop):
    large = asyncio.Event()
    original = _Protocol.read

    async def read(rpc):
        result = await original(rpc)
        if rpc.bytes > 1024 * 1024:
            large.set()
        return result

    monkeypatch.setattr(_Protocol, "read", read)
    cancel = asyncio.Event()
    task = asyncio.create_task(execute(worker, tmp_path, "partial_hang" if stop == "partial_frame_deadline" else "hang",
                                       cancel, timeout_seconds=2))
    await asyncio.wait_for(large.wait(), 1.5)
    if stop == "cancel":
        cancel.set()
    result, records, activity = await task
    assert (result.status, result.reason, result.stop_confirmation) == (("cancelled", "cancelled", "confirmed")
        if stop == "cancel" else ("timeout", "timeout", "confirmed"))
    assert records == activity == []


async def test_broken_limit_diagnostic_sink_cannot_change_stop_proof(worker, tmp_path):
    result, records, activity = await execute(worker, tmp_path, "huge", progress_event=Mock(side_effect=RuntimeError(CANARY)))
    assert (result.status, result.reason, result.stop_confirmation) == ("failed", "output_limit_exceeded", "confirmed")
    assert records == [] and len(activity) == 1 and CANARY not in repr(activity)


async def test_post_redaction_limit_is_not_a_truncated_success(worker, tmp_path):
    result, records, activity = await execute(worker, tmp_path, "final_redacted", credential_value="key")
    assert (result.status, result.reason, result.stop_confirmation) == ("failed", "output_limit_exceeded", "confirmed")
    assert len(result.content) == 1024 and b"key" not in result.content
    assert activity[0].payload == {"schema_version": 1, "failure_kind": "output_limit_exceeded",
                                   "failure_stage": "redacted_output_bytes", "observed": 1208, "limit": 1024}
