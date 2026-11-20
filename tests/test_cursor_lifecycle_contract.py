"""Credential-free Cursor lifecycle regression against installed SDK objects.

The controlled client supplies wire envelopes, not a real bridge/provider. These
checks exercise AsyncAgent.send and AsyncRun state/iterator/wait/cancel behavior,
including the distinction between compact terminal hints and full results.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from cursor_sdk import AsyncAgent, AsyncRun, RunResult, TokenUsage
from cursor_sdk.errors import CursorSDKError
import pytest

from kotoyose.runtime.base import RuntimeEvent, RuntimeImageInput, RuntimeResult, RuntimeTurnError
from kotoyose.runtime.cursor import CursorRunError, CursorRuntimeSession, _normalize_usage


RUN_ID = "run-controlled"
AGENT_ID = "agent-controlled"


def system():
    return {"sdkMessage": {"type": "system", "agentId": AGENT_ID, "runId": RUN_ID}}


def assistant(text):
    return {"sdkMessage": {
        "type": "assistant", "agentId": AGENT_ID, "runId": RUN_ID,
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }}


def result(status="finished", text="", *, full=True):
    value = {"runId": RUN_ID, "agentId": AGENT_ID, "status": status, "result": text}
    return {"result": {"result": value} if full else value}


class ControlledClient:
    _allow_api_key_env_fallback = False

    def __init__(self, wire, *, terminal="finished", terminal_text="", wait_error=None):
        self.wire = wire
        self.terminal = terminal
        self.terminal_text = terminal_text
        self.wait_error = wait_error
        self.sends = 0
        self.cancels = 0
        self.waits = 0
        self.agent_closes = 0
        self.closes = 0
        self.reading = False
        self.read_entered = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def _agent_stream(self, method, request, **kwargs):
        assert method == "Send"
        self.sends += 1
        return self.events()

    async def events(self):
        for item in self.wire:
            if item == "block":
                self.reading = True
                self.read_entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.reading = False
            else:
                yield item

    async def cancel_run(self, run_id, *, agent_id=None):
        assert run_id == RUN_ID
        assert agent_id == AGENT_ID
        assert not self.reading, "cancel/reconcile must stop the current iterator owner"
        self.cancels += 1
        self.cancelled.set()
        if self.terminal == "running":
            self.terminal = "cancelled"

    async def wait_live_run(self, run_id):
        assert not self.reading, "wait must not re-enter a running stream"
        self.waits += 1
        if self.wait_error:
            raise self.wait_error
        return RunResult(id=run_id, agent_id=AGENT_ID, status=self.terminal, result=self.terminal_text)

    def _unregister_custom_tools(self, agent_id):
        assert agent_id == AGENT_ID

    async def _agent_unary(self, method, request):
        assert method == "CloseAgent"
        self.agent_closes += 1
        return {}

    async def aclose(self):
        self.closes += 1


def session_for(wire, **kwargs):
    client = ControlledClient(wire, **kwargs)
    session = CursorRuntimeSession(None, "/tmp", config={"timeout": 0.1})
    session._client = client
    session._agent = AsyncAgent(client=client, agent_id=AGENT_ID, model=None, api_key="synthetic")
    return session, client


@pytest.mark.parametrize("full", [False, True])
async def test_real_sdk_finished_result_requires_verified_wait(full):
    session, client = session_for([system(), assistant("answer"), result(full=full)])
    values = [item async for item in session.stream("question")]
    assert values == ["answer", RuntimeResult("answer", None, AGENT_ID, "cursor")]
    assert session.last_outcome.run_id == RUN_ID
    assert session.last_outcome.status == "finished"
    assert session.last_outcome.stop_confirmed
    assert client.waits == (0 if full else 1)
    assert client.sends == 1
    await session.disconnect()
    assert client.cancels == 0
    assert client.closes == 1


async def test_terminal_only_result_text_is_returned_once():
    session, client = session_for([result(text="terminal answer")])
    output = await session.ask("question")
    assert output.text == "terminal answer"
    assert client.waits == 0
    await session.disconnect()


@pytest.mark.parametrize("status", ["error", "cancelled", "expired"])
@pytest.mark.parametrize("full", [False, True])
async def test_unsuccessful_terminal_retains_partial_and_never_returns_success(status, full):
    session, client = session_for([system(), assistant("partial"), result(status, full=full)], terminal=status)
    values = []
    with pytest.raises(CursorRunError) as raised:
        async for item in session.stream("question"):
            values.append(item)
    assert isinstance(raised.value, RuntimeTurnError)
    assert raised.value.partial_result.text == "partial"
    assert raised.value.run_id == RUN_ID
    assert raised.value.status == status
    assert values[0] == "partial"
    assert not any(isinstance(item, RuntimeResult) for item in values)
    assert values[-1].kind == "error"
    assert values[-1].payload["stop_confirmed"] is True
    assert session.last_outcome.status == status
    assert client.sends == 1
    assert client.cancels == 0
    assert client.agent_closes == client.closes == 1


async def test_abrupt_eof_is_reconciled_without_resending():
    session, client = session_for([system(), assistant("partial")], terminal="cancelled")
    with pytest.raises(CursorRunError, match="cancelled"):
        await session.ask("question")
    assert client.waits >= 1
    assert client.sends == 1
    assert session.last_outcome.status == "cancelled"


async def test_unverifiable_eof_reports_unknown_stop_and_closes(monkeypatch):
    session, client = session_for(
        [system(), assistant("partial")], terminal="running",
        wait_error=CursorSDKError("private provider detail"),
    )
    with pytest.raises(CursorRunError) as raised:
        await session.ask("question")
    assert str(raised.value) == "Cursor SDK request failed"
    assert raised.value.partial_result.text == "partial"
    assert not session.last_outcome.stop_confirmed
    assert client.cancels == 1
    assert client.agent_closes == client.closes == 1


async def test_timeout_during_tool_cancels_then_reconciles_and_never_resends():
    tool = {"sdkMessage": {
        "type": "tool_call", "agentId": AGENT_ID, "runId": RUN_ID,
        "callId": "tool-1", "name": "shell", "status": "running", "args": {},
    }}
    session, client = session_for([system(), tool, "block"], terminal="running")
    seen = []
    with pytest.raises(CursorRunError, match="idle timeout"):
        async for item in session.stream("question"):
            seen.append(item)
    assert any(isinstance(item, RuntimeEvent) and item.kind == "tool_use" for item in seen)
    assert client.sends == client.cancels == client.waits == 1
    assert session.last_outcome.status == "cancelled"
    assert client.agent_closes == client.closes == 1


async def test_stream_aclose_while_suspended_after_text_owns_cleanup():
    session, client = session_for([system(), assistant("partial"), "block"], terminal="running")
    stream = session.stream("question")
    assert await anext(stream) == "partial"
    await stream.aclose()
    # The SDK wait drains the remaining Send. It is bounded if the fixture does
    # not produce a result after cancellation; no competing reader is created.
    assert client.cancels == 1
    assert client.agent_closes == client.closes == 1
    assert not session.last_outcome.stop_confirmed
    assert session._active_run is None


async def test_disconnect_during_active_read_and_concurrent_disconnect_are_single_owner(monkeypatch):
    session, client = session_for([system(), "block"], terminal="running")
    request = asyncio.create_task(session.ask("question"))
    await client.read_entered.wait()
    await asyncio.gather(session.disconnect(), session.disconnect())
    with pytest.raises(asyncio.CancelledError):
        await request
    assert client.sends == client.cancels == client.waits == 1
    assert client.agent_closes == client.closes == 1
    assert session.last_outcome.stop_confirmed
    assert session.last_outcome.status == "cancelled"
    assert not session._streaming


async def test_concurrent_turn_rejected_without_second_send():
    session, client = session_for([system(), "block"], terminal="running")
    request = asyncio.create_task(session.ask("first"))
    await client.read_entered.wait()
    with pytest.raises(RuntimeError, match="active turn"):
        await session.ask("second")
    assert client.sends == 1
    await session.disconnect()
    with pytest.raises(asyncio.CancelledError):
        await request


async def test_cancel_finish_race_confirms_finished_without_retry():
    session, client = session_for([system(), "block"], terminal="finished")
    request = asyncio.create_task(session.ask("question"))
    await client.read_entered.wait()
    await session.disconnect()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert session.last_outcome.status == "finished"
    assert session.last_outcome.stop_confirmed
    assert client.sends == 1


async def test_cancel_failure_does_not_prevent_reconciliation_or_bridge_close():
    session, client = session_for([system(), "block"], terminal="cancelled")

    async def broken_cancel(*args, **kwargs):
        client.cancels += 1
        raise CursorSDKError("private cancel failure")

    client.cancel_run = broken_cancel
    request = asyncio.create_task(session.ask("question"))
    await client.read_entered.wait()
    await session.disconnect()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert session.last_outcome.stop_confirmed
    assert client.cancels == client.waits == client.agent_closes == client.closes == 1


@pytest.fixture(autouse=True)
def short_cleanup_deadline(monkeypatch):
    monkeypatch.setattr("kotoyose.runtime.cursor._CLEANUP_TIMEOUT_SECONDS", 0.03)


@pytest.mark.parametrize("url", [
    "https://example.invalid/image.png", "http://example.invalid/image.png",
    "data:image/png;base64,***", "data:image/png;base64,A",
])
async def test_invalid_image_rejected_before_bridge_launch(url, monkeypatch):
    launched = False

    async def launch(**kwargs):
        nonlocal launched
        launched = True
        raise AssertionError("Must validate input before launching a bridge")

    monkeypatch.setattr("kotoyose.runtime.cursor.AsyncClient.launch_bridge", launch)
    with pytest.raises(ValueError):
        await CursorRuntimeSession(None, "/tmp").ask([RuntimeImageInput(url)])
    assert not launched


async def test_oversized_image_rejected_before_bridge_launch(monkeypatch):
    monkeypatch.setattr("kotoyose.models._MAX_IMAGE_DATA_BYTES", 1)
    with pytest.raises(ValueError, match="image limit"):
        await CursorRuntimeSession(None, "/tmp").ask([RuntimeImageInput("data:image/png;base64,QUJD")])


def test_reasoning_usage_is_not_added_to_total():
    usage = TokenUsage(2, 10, 0, 0, 12, reasoning_tokens=8)
    normalized = _normalize_usage(usage)
    assert normalized["reasoning_tokens"] == 8
    assert normalized["total_tokens"] == 12
    assert normalized["output_tokens"] == 10


async def test_safe_error_diagnostics_keep_full_request_id_but_drop_private_payloads():
    request_id = "request-" + "r" * 600
    session, client = session_for([])

    async def failed_send(*args, **kwargs):
        client.sends += 1
        raise CursorSDKError(
            "secret-credential private prompt", code="unavailable", request_id=request_id,
            proto_error_code="UPSTREAM_UNAVAILABLE", retry_after="2", is_retryable=True,
            details=[{"authorization": "secret"}], headers={"authorization": "secret"},
        )

    client._agent_stream = failed_send
    seen = []
    with pytest.raises(CursorRunError) as raised:
        async for item in session.stream("question"):
            seen.append(item)
    assert "secret" not in str(raised.value)
    assert raised.value.diagnostics["request_id"] == request_id
    assert seen[-1].payload["code"] == "unavailable"
    assert "secret" not in str(seen[-1].payload)
    assert "headers" not in seen[-1].payload and "details" not in seen[-1].payload
    assert client.sends == 1


async def test_real_sdk_multi_turn_usage_is_cumulative_without_reasoning_double_count():
    def usage(input_tokens, output_tokens, reasoning):
        return {"sdkMessage": {
            "type": "usage", "agentId": AGENT_ID, "runId": RUN_ID,
            "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens,
                      "cacheReadTokens": 0, "cacheWriteTokens": 0,
                      "totalTokens": input_tokens + output_tokens, "reasoningTokens": reasoning},
        }}

    def thinking(text):
        return {"sdkMessage": {
            "type": "thinking", "agentId": AGENT_ID, "runId": RUN_ID, "text": text,
        }}

    session, client = session_for([
        system(), thinking("first"), assistant("answer"), usage(2, 3, 1),
        thinking("background follow-up"), assistant(" follow-up"), usage(4, 5, 2), result(),
    ])
    seen = [item async for item in session.stream("question")]
    turns = [item for item in seen if isinstance(item, RuntimeEvent) and item.kind == "turn"]
    assert [item.payload["turn"] for item in turns] == [1, 2]
    assert all(item.payload["run_id"] == RUN_ID for item in turns)
    assert seen[-1].text == "answer follow-up"
    assert seen[-1].usage == {
        "input_tokens": 6, "output_tokens": 8, "cache_read_tokens": 0,
        "cache_write_tokens": 0, "total_tokens": 14, "reasoning_tokens": 3,
    }
    await session.disconnect()


async def test_cancellation_while_send_primes_first_envelope_is_unknown_and_closes():
    session, client = session_for(["block"], terminal="running")
    request = asyncio.create_task(session.ask("question"))
    await client.read_entered.wait()
    await session.disconnect()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert client.sends == 1
    assert client.cancels == client.waits == 0  # No returned run ID to target.
    assert client.agent_closes == client.closes == 1
    assert session.last_outcome.run_id is None
    assert not session.last_outcome.stop_confirmed


async def test_repeated_caller_cancellation_does_not_cancel_owned_cleanup():
    session, client = session_for([system(), "block"], terminal="running")
    request = asyncio.create_task(session.ask("question"))
    await client.read_entered.wait()
    closing = asyncio.Event()
    release = asyncio.Event()

    async def delayed_close():
        client.closes += 1
        closing.set()
        await release.wait()

    client.aclose = delayed_close
    request.cancel()
    await closing.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert session._closing
    with pytest.raises(RuntimeError, match="cleanup"):
        await session.ask("must not race cleanup")
    release.set()
    await session.disconnect()
    assert not session._closing
    assert client.sends == client.cancels == client.agent_closes == client.closes == 1


@pytest.mark.parametrize("resume", [False, True])
async def test_real_sdk_create_resume_serializes_provider_native_tool_policy(tmp_path, monkeypatch, resume):
    """Real SDK serializers with controlled RPC; no bridge/provider validation claim."""
    from cursor_sdk import AsyncClient
    from kotoyose.config import HarnessConfig

    from unittest.mock import AsyncMock

    client = AsyncClient(transport=SimpleNamespace(aclose=AsyncMock()), allow_api_key_env_fallback=False)
    captured = []

    async def unary(method, request, **kwargs):
        captured.append((method, request))
        return {"agentId": AGENT_ID}

    async def launch(**kwargs):
        return client

    monkeypatch.setattr(client, "_agent_unary", unary)
    monkeypatch.setattr("kotoyose.runtime.cursor.AsyncClient.launch_bridge", launch)
    monkeypatch.setenv("FIXTURE_CURSOR_KEY", "synthetic")
    from kotoyose.config import Connection
    monkeypatch.setattr(
        "kotoyose.runtime.cursor.get_config",
        lambda: HarnessConfig(connections={"cursor": Connection(provider="cursor", kind="api_key", data_policy="no_training", secret="env:FIXTURE_CURSOR_KEY")}),
    )
    session = CursorRuntimeSession(
        "fixture-model", str(tmp_path), session_id=AGENT_ID if resume else None,
        tools=[], disallowed_tools=["shell"],
        mcp_servers={"fixture": {"command": "not-executed"}},
    )
    await session._ensure_agent()
    method, request = captured[0]
    assert method == ("ResumeAgent" if resume else "CreateAgent")
    assert request["options"]["tools"] == {"names": []}
    assert request["options"]["disallowedTools"] == ["shell"]
    assert request["options"]["mcpServers"]["fixture"]["stdio"]["command"] == "not-executed"
    await session.disconnect()


async def test_cancellation_resistant_reader_is_bounded_and_never_reentered():
    session, client = session_for([], terminal="running")
    session.config["timeout"] = 10
    release = asyncio.Event()

    async def stubborn_events():
        yield system()
        client.reading = True
        client.read_entered.set()
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            yield assistant("too late")
        finally:
            client.reading = False

    async def cancel_while_reading(*args, **kwargs):
        # Sending cancellation is legal; only iterator consumption is exclusive.
        client.cancels += 1

    client.events = stubborn_events
    client.cancel_run = cancel_while_reading
    request = asyncio.create_task(session.ask("question"))
    try:
        await client.read_entered.wait()
        await asyncio.wait_for(session.disconnect(), 0.3)
        assert not session.last_outcome.stop_confirmed
        assert client.waits == 0
        assert client.closes == 1
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert client.sends == 1
    assert client.agent_closes == client.closes == 1


async def test_hanging_cancel_is_bounded_and_still_reconciles():
    session, client = session_for([system(), "block"], terminal="cancelled")

    async def hanging_cancel(*args, **kwargs):
        client.cancels += 1
        await asyncio.Event().wait()

    client.cancel_run = hanging_cancel
    request = asyncio.create_task(session.ask("question"))
    await client.read_entered.wait()
    await asyncio.wait_for(session.disconnect(), 0.3)
    with pytest.raises(asyncio.CancelledError):
        await request
    assert session.last_outcome.stop_confirmed
    assert client.cancels == client.waits == client.agent_closes == client.closes == 1


async def test_nonterminal_wait_result_never_becomes_runtime_success():
    session, client = session_for([system(), assistant("partial")], terminal="running")
    with pytest.raises(CursorRunError, match="did not finish successfully") as raised:
        await session.ask("question")
    assert raised.value.status == "running"
    assert raised.value.partial_result.text == "partial"
    assert client.sends == client.cancels == 1
    # The real SDK caches this invalid nonterminal wait result; it cannot be
    # promoted to a confirmed cancellation merely because cancel() returned.
    assert session.last_outcome.status == "running"
    assert not session.last_outcome.stop_confirmed


async def test_auth_error_terminal_before_any_visible_text_is_not_retried():
    envelope = result("error")
    envelope["result"]["result"]["message"] = "ERROR_NOT_LOGGED_IN secret"
    session, client = session_for([system(), envelope], terminal="error")
    with pytest.raises(CursorRunError) as raised:
        await session.ask("question")
    assert client.sends == 1
    assert "secret" not in str(raised.value)


async def test_unknown_stop_blocks_following_turn_without_resending():
    session, client = session_for([system()], wait_error=CursorSDKError("unavailable"))
    with pytest.raises(CursorRunError):
        await session.ask("first")
    with pytest.raises(RuntimeError, match="stop is unconfirmed"):
        await session.ask("second")
    assert client.sends == 1


async def test_idle_timeout_is_bounded_even_when_read_ignores_cancellation():
    session, client = session_for([], terminal="running")
    session.config["timeout"] = 0.01
    release = asyncio.Event()

    async def stubborn_events():
        yield system()
        try:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            yield assistant("too late")
        finally:
            client.reading = False

    client.events = stubborn_events
    try:
        with pytest.raises(CursorRunError, match="idle timeout"):
            await asyncio.wait_for(session.ask("question"), 0.3)
        assert not session.last_outcome.stop_confirmed
        assert client.waits == 0
        assert client.sends == client.cancels == client.agent_closes == client.closes == 1
    finally:
        release.set()
        await asyncio.sleep(0)


async def test_agent_close_failure_does_not_skip_client_close():
    session, client = session_for([system(), result()])
    await session.ask("question")

    async def failed_close(method, request):
        client.agent_closes += 1
        raise CursorSDKError("private close failure")

    client._agent_unary = failed_close
    await session.disconnect()
    assert client.agent_closes == client.closes == 1
    assert session._agent is session._client is None
