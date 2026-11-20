"""累積通信と保存量を分離し、frame・queue・終端の有限契約を保つ。"""
import asyncio
from collections import deque
import json
from types import SimpleNamespace
import tracemalloc
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from kotoyose.execution_jobs import WorkerResult
from kotoyose.native_codex import NativeCodexWorker, _CONFIG, _Protocol
from kotoyose.native_codex_diagnostics import NativeCodexLimitError
from tests.test_native_claude import _job
from kotoyose.codex_coding_policy import HOST_MODE, TOOL_POLICY_DIGEST


CANARY = "PRIVATE_CANARY_protocol_stderr_prompt_headers_7f3a"


def protocol(limit=8192):
    reader = asyncio.StreamReader(limit=limit)
    stdin = Mock()
    stdin.drain = AsyncMock()
    return _Protocol(SimpleNamespace(stdout=reader, stdin=stdin), limit), reader


def frame(size):
    prefix, suffix = b'{"text":"', b'"}\n'
    return prefix + b"x" * (size - len(prefix) - len(suffix)) + suffix


async def test_many_complete_frames_can_exceed_the_retained_output_budget():
    reader = asyncio.StreamReader(limit=32768)
    rpc = _Protocol(SimpleNamespace(stdout=reader), 32768)
    frame = (json.dumps({"method": "delta", "params": {"text": "x" * 16384}}) + "\n").encode()
    for _ in range(128):
        reader.feed_data(frame)
        assert (await rpc.read())["method"] == "delta"
    assert rpc.bytes > 1024 * 1024


async def test_stderr_is_drained_without_a_cumulative_output_cutoff():
    class Stderr:
        def __init__(self):
            self.remaining = 512

        async def read(self, size):
            assert size == 4096
            if self.remaining:
                self.remaining -= 1
                return b"x" * size
            return b""

    stderr = Stderr()
    rpc = _Protocol(SimpleNamespace(stderr=stderr), 32768)
    await rpc.drain_stderr()
    assert stderr.remaining == 0 and rpc.bytes > 1024 * 1024


@pytest.mark.parametrize("offset", [-1, 0, 1, 2])
async def test_complete_frame_size_boundary(offset):
    rpc, reader = protocol()
    reader.feed_data(frame(rpc.limit + offset))
    if offset <= 0:
        assert "text" in await rpc.read()
    else:
        with pytest.raises(NativeCodexLimitError) as caught:
            await rpc.read()
        assert caught.value.stage == ("protocol_frame_bytes" if offset == 1 else "protocol_line_bytes")
        assert caught.value.observed > caught.value.limit == rpc.limit


async def test_unterminated_frame_is_rejected_at_the_stream_bound():
    rpc, reader = protocol()
    raw = b'{"text":"' + CANARY.encode() + b"x" * (rpc.limit * 2)
    reader.feed_data(raw)
    with pytest.raises(NativeCodexLimitError) as caught:
        await asyncio.wait_for(rpc.read(), .2)
    assert caught.value.stage == "protocol_line_bytes"
    assert caught.value.observed > caught.value.limit
    assert CANARY not in repr(caught.value.as_event())


@pytest.mark.parametrize("partial,reason", [(b"", "missing_completion"), (b'{"text":"ok"}', "invalid_protocol"), (b'{"text":', "invalid_protocol")])
async def test_eof_cannot_turn_a_partial_frame_into_success(partial, reason):
    rpc, reader = protocol()
    reader.feed_data(partial)
    reader.feed_eof()
    with pytest.raises(ValueError, match="^" + reason + "$"):
        await rpc.read()


@pytest.mark.parametrize("count", [511, 512, 513])
async def test_rpc_pending_queue_is_still_bounded(count):
    rpc, reader = protocol(131072)
    notice = (json.dumps({"method": "notice", "params": {"text": CANARY}}) + "\n").encode()
    reader.feed_data(notice * count + b'{"id":"kotoyose-1","result":{}}\n')
    if count <= 512:
        assert await rpc.request("initialize", {}) == {}
        assert len(rpc.pending) == count
    else:
        with pytest.raises(NativeCodexLimitError) as caught:
            await rpc.request("initialize", {})
        assert (caught.value.stage, caught.value.observed, caught.value.limit) == ("rpc_pending_count", 513, 512)
        assert len(rpc.pending) == 512
        assert CANARY not in repr(caught.value.as_event())


@pytest.mark.parametrize("notices", [511, 512])
async def test_settings_confirmation_wait_remains_finite(notices):
    messages = [{"method": "notice", "params": {"text": CANARY}}] * notices
    messages.append({"method": "thread/settings/updated", "params": {"threadId": "thread",
                     "threadSettings": {"cwd": "/workspace", "approvalPolicy": "never",
                                        "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "restricted"}}}})
    rpc = SimpleNamespace(request=AsyncMock(return_value={}), next=AsyncMock(side_effect=messages), pending=deque(),
                          pending_bytes=0, frame_bytes=128, limit=131072)
    if notices < 512:
        await NativeCodexWorker()._apply_external_policy(rpc, "thread")
        assert len(rpc.pending) == notices
    else:
        with pytest.raises(NativeCodexLimitError) as caught:
            await NativeCodexWorker()._apply_external_policy(rpc, "thread")
        assert (caught.value.stage, caught.value.observed, caught.value.limit) == ("settings_pending_count", 512, 512)
        assert CANARY not in repr(caught.value.as_event())


async def test_pending_bytes_are_released_when_frames_are_processed():
    rpc, reader = protocol(1024)
    reader.feed_data(frame(512) * 2 + b'{"id":"kotoyose-1","result":{}}\n')
    assert await rpc.request("initialize", {}) == {} and rpc.pending_bytes == 1024
    await rpc.next()
    assert rpc.pending_bytes == 512
    await rpc.next()
    assert rpc.pending_bytes == 0 and not rpc.pending
    reader.feed_data(frame(512) * 2 + b'{"id":"kotoyose-2","result":{}}\n')
    assert await rpc.request("next", {}) == {} and rpc.pending_bytes == 1024


async def test_pending_bytes_reject_large_queues_before_the_count_cap():
    rpc, reader = protocol(1024)
    reader.feed_data(frame(512) * 3)
    with pytest.raises(NativeCodexLimitError) as caught:
        await rpc.request("initialize", {})
    assert (caught.value.stage, caught.value.observed, caught.value.limit) == ("rpc_pending_bytes", 1536, 1024)
    assert len(rpc.pending) == 2 and rpc.pending_bytes == 1024


@pytest.mark.parametrize("notices", [2, 3])
async def test_settings_wait_also_bounds_retained_bytes(notices):
    messages = [{"method": "notice", "params": {}}] * notices
    messages.append({"method": "thread/settings/updated", "params": {"threadId": "thread",
                     "threadSettings": {"cwd": "/workspace", "approvalPolicy": "never",
                                        "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "restricted"}}}})
    rpc = SimpleNamespace(request=AsyncMock(return_value={}), next=AsyncMock(side_effect=messages), pending=deque(),
                          pending_bytes=0, frame_bytes=512, limit=1024)
    if notices == 2:
        await NativeCodexWorker()._apply_external_policy(rpc, "thread")
        assert rpc.pending_bytes == 1024 and len(rpc.pending) == 2
    else:
        with pytest.raises(NativeCodexLimitError) as caught:
            await NativeCodexWorker()._apply_external_policy(rpc, "thread")
        assert (caught.value.stage, caught.value.observed, caught.value.limit) == ("settings_pending_bytes", 1536, 1024)


@pytest.mark.parametrize("order", ["applied_after_response", "applied_before_response"])
async def test_real_rpc_settings_wait_restores_notifications_in_order(order):
    rpc, reader = protocol(16384)
    before = {"method": "notice", "params": {"index": 1}}
    after = {"method": "notice", "params": {"index": 2}}
    applied = {"method": "thread/settings/updated", "params": {"threadId": "thread", "threadSettings": {
        "cwd": "/workspace", "approvalPolicy": "never",
        "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "restricted"},
    }}}
    response = {"id": "kotoyose-1", "result": {}}
    sequence = [before, response, applied, after] if order == "applied_after_response" else [before, applied, after, response]
    reader.feed_data(b"".join((json.dumps(value) + "\n").encode() for value in sequence))
    await NativeCodexWorker()._apply_external_policy(rpc, "thread")
    assert rpc.pending_bytes > 0
    assert await rpc.next() == before
    assert await rpc.next() == after
    assert not rpc.pending and rpc.pending_bytes == 0


async def test_processed_frames_do_not_accumulate_in_python_memory():
    rpc, reader = protocol(32768)
    raw = frame(16384)
    tracemalloc.start()
    try:
        for _ in range(8192):
            reader.feed_data(raw)
            await rpc.read()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert rpc.bytes == 128 * 1024 * 1024
    assert not rpc.pending and peak < 1024 * 1024


@pytest.mark.parametrize("observed,limit", [(True, False), (-1, 0), (CANARY, [CANARY])])
def test_limit_diagnostics_never_reflect_unknown_values(observed, limit):
    error = NativeCodexLimitError(CANARY, observed, limit)
    assert error.as_event().payload == {"schema_version": 1, "failure_kind": "output_limit_exceeded",
                                       "failure_stage": "unknown", "observed": None, "limit": None}
    assert str(error) == "output_limit_exceeded" and CANARY not in repr(error.as_event())


def run_rpc(events):
    return SimpleNamespace(image_uris=(), turn_id=None, request=AsyncMock(side_effect=[
        {}, {"thread": {"id": "thread"}, "model": "gpt-6-luna", "modelProvider": "kotoyose_native"},
        {"turn": {"id": "turn"}},
    ]), next=AsyncMock(side_effect=events), send=AsyncMock())


def item(method, **params):
    return {"method": method, "params": {"threadId": "thread", "turnId": "turn", **params}}


def final_events(text):
    return [item("item/completed", item={"type": "agentMessage", "id": "answer", "phase": "final_answer", "text": text}),
            item("turn/completed", turn={"id": "turn", "status": "completed"})]


async def test_soft_deadline_never_interrupts_a_healthy_coding_turn(tmp_path):
    rpc = run_rpc(final_events('complete after soft notification'))
    rpc.pending, rpc.sequence = deque(), 0
    job = replace(_job(tmp_path), model='gpt-6-luna', codex_execution_mode=HOST_MODE,
                  codex_tool_policy_digest=TOOL_POLICY_DIGEST,
                  soft_deadline_monotonic=asyncio.get_running_loop().time()-1)
    result = await NativeCodexWorker()._run(rpc, job, asyncio.Event(), AsyncMock(), {}, {}, _CONFIG)
    assert result == b'complete after soft notification'
    assert all(call.args[0].get('method') != 'turn/interrupt' for call in rpc.send.await_args_list)


async def test_tool_response_json_and_cumulative_limit_are_preserved(tmp_path):
    stdout = (CANARY + "x" * 200).encode()
    calls = [{"id": f"rpc-{i}", **item("item/tool/call", callId=f"call-{i}", tool="sandbox_exec",
                                     arguments={"argv": ["true"], "cwd": "."})} for i in range(2)]
    rpc = run_rpc([*calls, *final_events("ok")])
    tool = AsyncMock(return_value=WorkerResult("succeeded", 0, stdout, b"", "confirmed", "done"))
    job = _job(tmp_path, model="gpt-6-luna", tool_refs=("builtin:sandbox-exec@1",), output_limit_bytes=512)
    with pytest.raises(NativeCodexLimitError) as caught:
        await NativeCodexWorker()._run(rpc, job, asyncio.Event(), tool, {}, {}, dict(_CONFIG))
    assert caught.value.stage == "tool_result_bytes"
    assert len(stdout) * 2 < 512 < caught.value.observed and caught.value.limit == 512
    responses = [call.args[0]["result"] for call in rpc.send.await_args_list if "result" in call.args[0]]
    assert tool.await_count == 2 and len(responses) == 1
    response = responses[0]
    assert response["success"] and json.loads(response["contentItems"][0]["text"])["stdout"] == stdout.decode()
    assert CANARY not in repr(caught.value.as_event())


async def test_tool_and_final_response_share_the_existing_budget(tmp_path):
    overhead = len(json.dumps({"status": "succeeded", "exit_code": 0, "stdout": "", "stderr": ""}).encode())
    stdout = b"x" * (512 - overhead - 2)
    call = {"id": "rpc", **item("item/tool/call", callId="call", tool="sandbox_exec", arguments={"argv": ["true"], "cwd": "."})}
    rpc = run_rpc([call, *final_events("ok")])
    tool = AsyncMock(return_value=WorkerResult("succeeded", 0, stdout, b"", "confirmed", "done"))
    job = _job(tmp_path, model="gpt-6-luna", tool_refs=("builtin:sandbox-exec@1",), output_limit_bytes=512)
    assert await NativeCodexWorker()._run(rpc, job, asyncio.Event(), tool, {}, {}, dict(_CONFIG)) == b"ok"
    assert sum("result" in call.args[0] for call in rpc.send.await_args_list) == 1


async def test_credential_replacement_cannot_make_an_oversized_final_success(tmp_path):
    rpc = run_rpc(final_events("key" * 70))
    job = _job(tmp_path, model="gpt-6-luna", credential_value="key", output_limit_bytes=256)
    with pytest.raises(NativeCodexLimitError) as caught:
        await NativeCodexWorker()._run(rpc, job, asyncio.Event(), AsyncMock(), {}, {}, dict(_CONFIG))
    assert (caught.value.stage, caught.value.observed, caught.value.limit) == ("final_response_bytes", 700, 256)
    assert "key" not in repr(caught.value.as_event())
