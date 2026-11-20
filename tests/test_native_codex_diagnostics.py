"""Synthetic protocol と既存 journal 境界で、本文を残さない失敗診断を検証する。"""

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from kotoyose.execution_agent import _ProgressWriter
from kotoyose.native_codex import NativeCodexWorker
from kotoyose.native_codex_diagnostics import NativeCodexProviderError
from kotoyose.codex_egress import CodexEgress
from kotoyose.codex_broker_diagnostics import BrokerFailureDiagnostic
from tests.codex_runtime_fixture import codex_rootfs  # noqa: F401
from tests.test_native_claude import _job


CANARY = "PRIVATE_CANARY_auth_prompt_response_headers_stderr_763a"
KEYS = {"schema_version", "failure_stage", "provider_code", "http_status", "turn_status"}


def event(method, **params):
    return {"method": method, "params": {"threadId": "thread", "turnId": "turn", **params}}


def completed(status="failed", error=None):
    return event("turn/completed", turn={"id": "turn", "status": status, "error": error})


@pytest.fixture
def fixture_worker(tmp_path, codex_rootfs):
    launcher = tmp_path / "fake-codex"
    launcher.write_text('''#!/usr/bin/python3
import json,sys
def emit(message): print(json.dumps(message),flush=True)
for line in sys.stdin:
 message=json.loads(line);method=message.get('method')
 if method=='initialize':emit({'id':message['id'],'result':{}})
 elif method=='thread/start':
  config=json.loads(message['params']['developerInstructions'])
  sys.stderr.write(config.get('stderr',''));sys.stderr.flush()
  emit({'id':message['id'],'result':{'thread':{'id':'thread'},'model':config.get('resolved_model',message['params']['model']),'modelProvider':'kotoyose_native','reasoningEffort':message['params']['config'].get('model_reasoning_effort')}})
 elif method=='turn/start':
  if 'rpc_error' in config:emit({'id':message['id'],'error':config['rpc_error']})
  else:
   emit({'id':message['id'],'result':{'turn':{'id':'turn'}}})
   for notification in config.get('events',[]):emit(notification)
''')
    launcher.chmod(0o700)
    return NativeCodexWorker(launcher), codex_rootfs


class Journal:
    def __init__(self):
        self.records = []

    async def append_event(self, attempt_id, kind, payload):
        self.records.append({"attempt_id": attempt_id, "type": kind, "payload": payload})


async def execute(fixture_worker, tmp_path, config, **overrides):
    worker, rootfs = fixture_worker
    journal, activity = Journal(), []
    writer = _ProgressWriter(journal, "fixture-attempt")
    values = {"attempt_id": "fixture-attempt", "model": "gpt-6-luna", "cli_binary": Path("/usr/bin/sh"),
              "credential_name": "OPENAI_API_KEY", "credential_value": CANARY,
              "instructions": json.dumps(config), "prompt": CANARY, "output_limit_bytes": 65536,
              "codex_rootfs": rootfs[0], "codex_rootfs_digest": rootfs[1],
              "progress_event": writer.emit, "activity_event": activity.append, **overrides}
    tool = AsyncMock(side_effect=AssertionError("no tool dispatch expected"))
    try:
        result = await worker.execute(_job(tmp_path, **values), asyncio.Event(), tool, on_process_started=AsyncMock())
    finally:
        await writer.finish()
    tool.assert_not_awaited()
    return result, journal.records, activity


def assert_failure(result, records, activity, stage, code=None, status=None, http=None):
    assert (result.status, result.reason, result.content, result.stop_confirmation) == ("failed", "provider_error", b"", "confirmed")
    assert len(records) == len(activity) == 1
    record = records[0]
    assert record["attempt_id"] == "fixture-attempt" and record["type"] == "provider_progress"
    assert record["payload"]["kind"] == "error" and record["payload"]["summary"] == "provider_error"
    payload = record["payload"]["payload"]
    assert set(payload) == KEYS
    assert payload == {"schema_version": 1, "failure_stage": stage, "provider_code": code,
                       "http_status": http, "turn_status": status}
    assert asdict(activity[0]) == record["payload"]
    assert CANARY not in json.dumps(records) + repr(activity) + repr(result)


@pytest.mark.parametrize("code", ["contextWindowExceeded", "usageLimitExceeded", "rateLimitExceeded", "serverOverloaded", "unauthorized"])
async def test_failed_turn_journals_only_existing_allowlisted_code(fixture_worker, tmp_path, caplog, capsys, code):
    error = {"message": CANARY, "additionalDetails": CANARY, "headers": {"Authorization": CANARY}, "codexErrorInfo": code}
    result, records, activity = await execute(fixture_worker, tmp_path, {"events": [completed(error=error)], "stderr": CANARY})
    assert_failure(result, records, activity, "turn_completed", code=code, status="failed")
    captured = capsys.readouterr()
    assert CANARY not in caplog.text + captured.out + captured.err


@pytest.mark.parametrize("http", [401, 429, 503])
async def test_terminal_notification_keeps_numeric_status_without_body(fixture_worker, tmp_path, http):
    error = {"message": CANARY, "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": http, "body": CANARY}}}
    result, records, activity = await execute(fixture_worker, tmp_path, {"events": [event("error", error=error, willRetry=False)]})
    assert_failure(result, records, activity, "error_notification", code="httpConnectionFailed", http=http)


@pytest.mark.parametrize("http", [True, "429", 99, 600, None, {"body": CANARY}])
async def test_invalid_http_values_are_null(fixture_worker, tmp_path, http):
    error = {"message": CANARY, "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": http}}}
    result, records, activity = await execute(fixture_worker, tmp_path, {"events": [completed(error=error)]})
    assert_failure(result, records, activity, "turn_completed", code="httpConnectionFailed", status="failed")


@pytest.mark.parametrize("error,status", [
    (None, None), ({}, "failed"), (CANARY, "failed"),
    ({"message": CANARY, "codexErrorInfo": CANARY}, CANARY),
    ({"codexErrorInfo": [CANARY]}, {"message": CANARY}),
    ({"codexErrorInfo": {}}, [CANARY]),
])
async def test_missing_unknown_and_malformed_fields_do_not_escape(fixture_worker, tmp_path, error, status):
    result, records, activity = await execute(fixture_worker, tmp_path, {"events": [completed(status, error)]})
    assert_failure(result, records, activity, "turn_completed", status="failed" if status == "failed" else None)


async def test_rpc_error_has_stage_without_reflecting_rpc_code_or_data(fixture_worker, tmp_path):
    result, records, activity = await execute(fixture_worker, tmp_path, {
        "rpc_error": {"code": -32603, "message": CANARY, "data": {"headers": CANARY}}, "stderr": CANARY})
    assert_failure(result, records, activity, "rpc_response")


async def test_thread_identity_mismatch_does_not_record_the_returned_model(fixture_worker, tmp_path):
    result, records, activity = await execute(fixture_worker, tmp_path, {"resolved_model": CANARY})
    assert_failure(result, records, activity, "thread_start")


@pytest.mark.parametrize("effort", [None, "high"])
async def test_broker_failure_carries_only_numeric_status(fixture_worker, tmp_path, monkeypatch, effort):
    start = CodexEgress.start

    async def fail(broker):
        path = await start(broker)
        broker.failure_reason = "provider_error"
        broker.failure_http_status = 503
        broker.failure_event.set()
        return path

    monkeypatch.setattr(CodexEgress, "start", fail)
    result, records, activity = await execute(fixture_worker, tmp_path, {}, reasoning_effort=effort)
    assert_failure(result, records, activity, "broker", http=503)


async def test_subscription_http_error_does_not_retain_headers_or_body():
    reader = asyncio.StreamReader()
    reader.feed_data(f"HTTP/1.1 503 Unavailable\r\nAuthorization: {CANARY}\r\nContent-Length: {len(CANARY)}\r\n\r\n{CANARY}".encode())
    reader.feed_eof()
    broker = CodexEgress(1, 65536, "gpt-6-luna", "synthetic-credential", None)
    try:
        with pytest.raises(ValueError, match="^provider_error$"):
            await broker.subscription_response(reader, Mock())
        assert broker.failure_http_status == 503
        assert CANARY not in repr(broker.__dict__)
    finally:
        await broker.close(.1)


async def test_broker_category_reaches_journal_without_changing_stop_or_terminal_reason(fixture_worker, tmp_path, monkeypatch):
    start = CodexEgress.start

    async def fail(broker):
        path = await start(broker)
        broker.failure_reason = "provider_error"
        broker.failure_diagnostic = BrokerFailureDiagnostic("upstream_response_body", "connection_reset")
        broker.failure_event.set()
        return path

    monkeypatch.setattr(CodexEgress, "start", fail)
    result, records, activity = await execute(fixture_worker, tmp_path, {})
    assert (result.status, result.reason, result.stop_confirmation) == ("failed", "provider_error", "confirmed")
    assert len(records) == len(activity) == 1
    payload = records[0]["payload"]["payload"]
    assert set(payload) == KEYS | {"broker_phase", "broker_error"}
    assert payload["broker_phase"] == "upstream_response_body" and payload["broker_error"] == "connection_reset"
    assert CANARY not in repr(records) + repr(activity) + repr(result)


@pytest.mark.parametrize("retry_notice", [False, True])
async def test_success_does_not_emit_failure_or_replay_after_retry_advice(fixture_worker, tmp_path, retry_notice):
    events = [event("item/completed", item={"type": "agentMessage", "id": "a", "phase": "final_answer", "text": "ok"}),
              completed("completed", {"message": CANARY})]
    if retry_notice:
        events.insert(0, event("error", error={"message": CANARY, "codexErrorInfo": "rateLimitExceeded"}, willRetry=True))
    result, records, activity = await execute(fixture_worker, tmp_path, {"events": events})
    assert (result.status, result.reason, result.content, result.stop_confirmation) == ("succeeded", "completed", b"ok", "confirmed")
    assert records == activity == []


async def test_untyped_exception_has_unknown_stage_without_message(fixture_worker, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(fixture_worker[0], "_run", AsyncMock(side_effect=RuntimeError(CANARY)))
    result, records, activity = await execute(fixture_worker, tmp_path, {})
    assert_failure(result, records, activity, "unknown")
    assert CANARY not in caplog.text


async def test_broken_diagnostic_callback_cannot_change_terminal_proof(fixture_worker, tmp_path):
    result, records, activity = await execute(fixture_worker, tmp_path, {"events": [completed()]},
                                             progress_event=Mock(side_effect=RuntimeError(CANARY)))
    assert (result.status, result.reason, result.stop_confirmation) == ("failed", "provider_error", "confirmed")
    assert records == [] and len(activity) == 1
    assert CANARY not in repr(result) + repr(activity)


def test_diagnostic_stage_and_unhashable_code_are_normalized():
    value = NativeCodexProviderError(CANARY, {"message": CANARY, "codexErrorInfo": [CANARY]}, turn_status=[CANARY])
    assert value.as_event().payload == {"schema_version": 1, "failure_stage": "unknown", "provider_code": None,
                                        "http_status": None, "turn_status": None}
    assert str(value) == "provider_error" and CANARY not in repr(value.as_event())
