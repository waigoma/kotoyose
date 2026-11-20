"""Claude terminal contracts using installed SDK objects and synthetic JSONL.

These tests never authenticate or call Claude. Synthetic bytes verify the SDK
adapter boundary, not the behavior of a real Claude Code deployment.
"""

import asyncio
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ConversationResetMessage,
    ResultError, ResultMessage, TextBlock, ToolUseBlock, UserMessage,
    MirrorErrorMessage, RateLimitEvent, RateLimitInfo, SystemMessage, TaskUpdatedMessage,
    TaskProgressMessage,
)

from kotoyose.runtime.base import RuntimeEvent, RuntimeResult, RuntimeTurnError
from kotoyose.runtime.claude import ClaudeRuntimeSession


def result(**overrides) -> ResultMessage:
    fields = dict(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="result-session", terminal_reason="completed",
        usage={"input_tokens": 8, "output_tokens": 2}, total_cost_usd=0.01,
    )
    fields.update(overrides)
    return ResultMessage(**fields)


def runtime(*batches, error=None) -> ClaudeRuntimeSession:
    """Each batch matches one SDK receive_response call, ending at one result."""
    responses = iter(batches)

    async def receive():
        for message in next(responses, []):
            yield message
        if error:
            raise error

    client = MagicMock()
    client.query = AsyncMock()
    client.disconnect = AsyncMock()
    client.receive_response = receive
    return ClaudeRuntimeSession(client)


@pytest.mark.asyncio
async def test_observations_never_change_foreground_response_usage_or_resume_identity():
    session = runtime([
        SystemMessage("init", {"session_id": "session-1"}),
        RateLimitEvent(RateLimitInfo("allowed_warning"), "uuid-1", "session-1"),
        RateLimitEvent(RateLimitInfo("rejected"), "uuid-2", "foreign-session"),
        MirrorErrorMessage("mirror_error", {}, error="SECRET backend error"),
        TaskUpdatedMessage("task_updated", {"SECRET": "raw"}, "task-1", {"status": "killed"}),
        AssistantMessage([TextBlock("foreground")], "fixture", session_id="session-1"),
        result(session_id="session-1"),
    ])
    items = [item async for item in session.stream("fixture")]
    observations = [i for i in items if isinstance(i, RuntimeEvent) and i.kind in {
        "rate_limit", "session_store_warning", "task_updated",
    }]
    assert [i.kind for i in observations] == ["rate_limit", "session_store_warning", "task_updated"]
    assert "SECRET" not in repr(observations)
    answer = next(i for i in items if isinstance(i, RuntimeResult))
    assert answer.text == "foreground"
    assert answer.runtime_session_id == "session-1"
    assert answer.usage == {"input_tokens": 8, "output_tokens": 2, "cost_usd": 0.01}
    assert not session.invalidated


@pytest.mark.asyncio
async def test_rate_limit_advisory_alone_is_not_success_or_foreground_progress():
    client = MagicMock(query=AsyncMock(), disconnect=AsyncMock())

    async def receive():
        while True:
            await asyncio.sleep(0.002)
            yield RateLimitEvent(RateLimitInfo("allowed"), "uuid", "session-1")

    client.receive_response = receive
    session = ClaudeRuntimeSession(client, timeout_seconds=0.02)
    with pytest.raises(RuntimeTurnError) as error:
        await session.ask("fixture")
    assert error.value.terminal_reason == "idle_timeout"
    assert session.invalidated


@pytest.mark.asyncio
async def test_task_progress_keeps_existing_idle_liveness_without_adding_usage():
    client = MagicMock(query=AsyncMock(), disconnect=AsyncMock())

    async def receive():
        for _ in range(3):
            await asyncio.sleep(0.02)
            yield TaskProgressMessage(
                "task_progress", {}, "task-1", "SECRET", {"total_tokens": 10000},
                "uuid", "session-1",
            )
        yield result(session_id="session-1")

    client.receive_response = receive
    answer = await ClaudeRuntimeSession(client, timeout_seconds=0.04).ask("fixture")
    assert answer.usage == {"input_tokens": 8, "output_tokens": 2, "cost_usd": 0.01}


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [
    {"is_error": True},
    {"subtype": "error_max_turns", "terminal_reason": "max_turns"},
    {"subtype": "error_max_budget_usd", "terminal_reason": "max_budget_usd"},
    {"is_error": True, "terminal_reason": "api_error", "api_error_status": 429},
    {"is_error": True, "terminal_reason": "api_error", "api_error_status": 529},
    {"terminal_reason": "aborted_streaming"},
    {"terminal_reason": "aborted_tools"},
])
@pytest.mark.parametrize("partial", [False, True])
async def test_terminal_failure_never_becomes_success(fields, partial):
    messages = []
    if partial:
        messages.append(AssistantMessage([TextBlock("partial answer")], "fixture-model"))
    messages.append(result(**fields, result="SECRET raw API prose", errors=["SECRET"]))
    session = runtime(messages)
    items = []
    with pytest.raises(RuntimeTurnError) as caught:
        async for item in session.stream("fixture"):
            items.append(item)
    error = caught.value
    assert error.partial_result.text == ("partial answer" if partial else "")
    assert error.partial_result.runtime_session_id == "result-session"
    assert error.partial_result.usage == {"input_tokens": 8, "output_tokens": 2, "cost_usd": 0.01}
    assert not any(isinstance(item, RuntimeResult) for item in items)
    errors = [item for item in items if isinstance(item, RuntimeEvent) and item.kind == "error"]
    assert len(errors) == 1
    assert "SECRET" not in str(error)
    assert "SECRET" not in repr(errors)
    assert session.invalidated
    if "api_error_status" in fields:
        assert error.api_error_status == fields["api_error_status"]


@pytest.mark.asyncio
async def test_ask_propagates_failure_instead_of_discarding_error_event():
    session = runtime([result(is_error=True)])
    with pytest.raises(RuntimeTurnError):
        await session.ask("fixture")


@pytest.mark.asyncio
async def test_result_error_exception_preserves_typed_details_without_raw_prose():
    sdk_error = ResultError("SECRET exception", data={
        "subtype": "error_during_execution", "terminal_reason": "api_error",
        "api_error_status": 529, "session_id": "error-session", "result": "SECRET result",
        "errors": ["SECRET errors"], "usage": {"input_tokens": 3}, "total_cost_usd": 0.03,
    }, exit_code=1)
    session = runtime([AssistantMessage([TextBlock("partial")], "fixture")], error=sdk_error)
    with pytest.raises(RuntimeTurnError) as caught:
        await session.ask("fixture")
    assert str(caught.value) == "Claude turn failed"
    assert caught.value.terminal_reason == "api_error"
    assert caught.value.api_error_status == 529
    assert caught.value.partial_result.text == "partial"
    assert caught.value.partial_result.runtime_session_id == "error-session"
    assert caught.value.partial_result.usage == {"input_tokens": 3, "cost_usd": 0.03}


@pytest.mark.asyncio
async def test_unknown_classifications_and_generic_exceptions_are_sanitized():
    for session in (
        runtime([result(is_error=True, subtype="SECRET subtype", terminal_reason="SECRET reason")]),
        runtime([], error=RuntimeError("SECRET transport")),
    ):
        with pytest.raises(RuntimeTurnError) as caught:
            await session.ask("fixture")
        assert "SECRET" not in str(caught.value)
        assert "SECRET" not in repr(caught.value.diagnostics)


@pytest.mark.asyncio
async def test_result_only_turn_retains_authoritative_id_and_structured_payload():
    session = runtime([result(structured_output={"answer": 42})])
    answer = await session.ask("fixture")
    assert answer.runtime_session_id == "result-session"
    assert answer.structured_output == {"answer": 42}


@pytest.mark.asyncio
async def test_reset_discards_outgoing_resume_identity_but_never_uses_conversation_id():
    session = runtime([
        AssistantMessage([TextBlock("before")], "fixture", session_id="outgoing-session"),
        ConversationResetMessage("ui-conversation-id", "reset-uuid", "outgoing-session"),
        result(session_id="incoming-session"),
    ])
    answer = await session.ask("fixture")
    assert answer.runtime_session_id == "incoming-session"
    session = runtime([
        AssistantMessage([], "fixture", session_id="outgoing-session"),
        ConversationResetMessage("ui-conversation-id", "reset-uuid", "outgoing-session"),
    ])
    with pytest.raises(RuntimeTurnError) as caught:
        await session.ask("fixture")
    assert caught.value.partial_result.runtime_session_id is None


@pytest.mark.asyncio
async def test_background_result_does_not_finish_foreground_or_attribute_usage():
    session = runtime([
        UserMessage("injected", origin={"kind": "task-notification"}),
        AssistantMessage([TextBlock("background answer")], "fixture", session_id="background-session"),
        result(origin={"kind": "task-notification"}, session_id="background-session", usage={"input_tokens": 999}),
    ], [
        AssistantMessage([TextBlock("foreground answer")], "fixture", session_id="foreground-session"),
        result(session_id="foreground-session"),
    ])
    answer = await session.ask("fixture")
    assert answer.text == "foreground answer"
    assert answer.runtime_session_id == "foreground-session"
    assert answer.usage["input_tokens"] == 8


@pytest.mark.asyncio
async def test_background_result_with_unattributed_text_fails_closed():
    session = runtime([
        AssistantMessage([TextBlock("ambiguous")], "fixture"),
        result(origin={"kind": "task-notification"}),
    ])
    with pytest.raises(RuntimeTurnError) as caught:
        await session.ask("fixture")
    assert caught.value.terminal_reason == "ambiguous_origin"


@pytest.mark.asyncio
async def test_subagent_text_is_activity_with_linkage_not_main_answer():
    session = runtime([
        AssistantMessage(
            [TextBlock("child text"), ToolUseBlock("tool-child", "Read", {"path": "fixture"})],
            "fixture", parent_tool_use_id="parent-agent", session_id="child-session",
        ),
        AssistantMessage([TextBlock("main answer")], "fixture", session_id="main-session"),
        result(session_id="main-session"),
    ])
    items = [item async for item in session.stream("fixture")]
    assert [item for item in items if isinstance(item, str)] == ["main answer"]
    child_events = [item for item in items if isinstance(item, RuntimeEvent) and item.payload.get("parent_tool_use_id")]
    assert [item.kind for item in child_events] == ["turn", "text", "tool_use"]
    assert all(item.payload["parent_tool_use_id"] == "parent-agent" for item in child_events)


@pytest.fixture
def synthetic_cli(tmp_path: Path):
    def make(messages):
        cli = tmp_path / "synthetic-claude"
        cli.write_text(
            f"#!{sys.executable}\nimport json, sys\nMESSAGES = json.loads({json.dumps(json.dumps(messages))})\n" + '''
if "--version" in sys.argv:
    print("99.0.0 (synthetic fixture)")
    sys.exit(0)
for line in sys.stdin:
    request = json.loads(line)
    if request["type"] == "control_request":
        print(json.dumps({"type": "control_response", "response": {
            "subtype": "success", "request_id": request["request_id"], "response": {}}}), flush=True)
    elif request["type"] == "user":
        for message in MESSAGES:
            print(json.dumps(message), flush=True)
''', encoding="utf-8",
        )
        cli.chmod(0o700)
        return cli
    return make


def wire_result(**overrides):
    message = dict(
        type="result", subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=False, num_turns=1, session_id="wire-session", terminal_reason="completed",
        usage={"input_tokens": 1, "output_tokens": 2},
    )
    message.update(overrides)
    return message


def wire_assistant(text):
    return {"type": "assistant", "message": {
        "role": "assistant", "model": "fixture", "content": [{"type": "text", "text": text}],
    }}


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["result-only", "error-only", "partial-error", "background"])
async def test_actual_sdk_synthetic_jsonl_terminal_boundary(synthetic_cli, tmp_path, scenario):
    messages = [wire_result()]
    if scenario in ("error-only", "partial-error"):
        messages = [wire_result(is_error=True, terminal_reason="api_error", api_error_status=429, result="SECRET")]
        if scenario == "partial-error":
            messages.insert(0, wire_assistant("partial"))
    elif scenario == "background":
        messages = [
            {"type": "user", "message": {"role": "user", "content": "background"}, "origin": {"kind": "task-notification"}},
            wire_assistant("background"),
            wire_result(origin={"kind": "task-notification"}, session_id="background-session"),
            wire_assistant("answer"), wire_result(),
        ]
    client = ClaudeSDKClient(options=ClaudeAgentOptions(
        cli_path=str(synthetic_cli(messages)), cwd=str(tmp_path), tools=[],
        setting_sources=[], permission_mode="dontAsk",
    ))
    try:
        async with asyncio.timeout(10):
            await client.connect()
            session = ClaudeRuntimeSession(client)
            if "error" in scenario:
                with pytest.raises(RuntimeTurnError) as caught:
                    await session.ask("fixture prompt")
                assert caught.value.api_error_status == 429
                assert caught.value.partial_result.text == ("partial" if scenario == "partial-error" else "")
                assert "SECRET" not in str(caught.value)
            else:
                answer = await session.ask("fixture prompt")
                assert answer.runtime_session_id == "wire-session"
                assert answer.text == ("answer" if scenario == "background" else "")
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_assistant_api_error_prose_is_not_streamed_or_silently_successful():
    session = runtime([
        AssistantMessage([TextBlock("SECRET raw API error")], "fixture", error="rate_limit"),
        result(),
    ])
    items = []
    with pytest.raises(RuntimeTurnError) as caught:
        async for item in session.stream("fixture"):
            items.append(item)
    assert caught.value.partial_result.text == ""
    assert caught.value.diagnostics["assistant_error"] == "rate_limit"
    assert "SECRET" not in repr(items)
    session.client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_closing_stream_after_text_invalidates_and_closes_sdk_iterator():
    closed = asyncio.Event()

    async def receive():
        try:
            yield AssistantMessage([TextBlock("partial")], "fixture")
            await asyncio.Event().wait()
        finally:
            closed.set()

    session = runtime([])
    session.client.receive_response = receive
    stream = session.stream("fixture")
    while not isinstance(await anext(stream), str):
        pass
    await stream.aclose()
    assert session.invalidated
    assert closed.is_set()
    session.client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_successful_terminal_stream_close_keeps_client_reusable():
    session = runtime([AssistantMessage([TextBlock("answer")], "fixture"), result()])
    stream = session.stream("fixture")
    while not isinstance(await anext(stream), RuntimeResult):
        pass
    await stream.aclose()
    assert not session.invalidated
    session.client.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_ask_invalidates_cached_session_and_prevents_resume(monkeypatch, tmp_path):
    from kotoyose.config import HarnessConfig, ModelPreset
    from kotoyose import session as session_module

    waiting = asyncio.Event()
    closed = asyncio.Event()

    async def receive():
        try:
            yield AssistantMessage([TextBlock("partial")], "fixture")
            waiting.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    cancelled_session = runtime([])
    cancelled_session.client.receive_response = receive
    config = HarnessConfig(models={"fixture": ModelPreset(model="fixture", cwd=str(tmp_path))})
    key = "conv:cancelled-claude-contract"
    identity = session_module._cache_identity("claude", "fixture", "fixture", [], None)
    session_module.sessions[key] = cancelled_session
    session_module.session_cache_identities[key] = identity
    lookup = AsyncMock(return_value="must-not-resume-interrupted-session")
    new_sdk = MagicMock()
    new_sdk.connect = AsyncMock()
    monkeypatch.setattr(session_module, "get_config", lambda: config)
    monkeypatch.setattr(session_module, "lookup_session", lookup)
    monkeypatch.setattr(session_module, "build_options", lambda *args, **kwargs: ClaudeAgentOptions())
    monkeypatch.setattr(session_module, "ClaudeSDKClient", lambda **kwargs: new_sdk)
    try:
        task = asyncio.create_task(cancelled_session.ask("fixture"))
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled_session.invalidated
        assert closed.is_set()
        cancelled_session.client.disconnect.assert_awaited_once()
        replacement, resumed = await session_module.get_or_resume_client(key, "fixture")
        assert replacement is not cancelled_session
        assert replacement.client is new_sdk
        assert not resumed
        lookup.assert_not_awaited()
    finally:
        session_module.sessions.pop(key, None)
        session_module.session_cache_identities.pop(key, None)
        session_module.session_last_used.pop(key, None)


@pytest.mark.asyncio
async def test_incomplete_cleanup_is_bounded_when_sdk_disconnect_ignores_cancellation(monkeypatch):
    import kotoyose.runtime.claude as claude_module

    release = asyncio.Event()
    cleanup_stopped = asyncio.Event()

    async def disconnect():
        try:
            await release.wait()
        except asyncio.CancelledError:
            # A third-party transport can suppress cancellation. The caller
            # still must finish, and the poisoned session must not be reused.
            await release.wait()
        finally:
            cleanup_stopped.set()

    session = runtime([AssistantMessage([TextBlock("partial")], "fixture")])
    session.client.disconnect = AsyncMock(side_effect=disconnect)
    monkeypatch.setattr(claude_module, "_CLEANUP_TIMEOUT_SECONDS", 0.01)
    stream = session.stream("fixture")
    while not isinstance(await anext(stream), str):
        pass
    try:
        async with asyncio.timeout(0.2):
            await stream.aclose()
        assert session.invalidated
        session.client.disconnect.assert_awaited_once()
        assert not cleanup_stopped.is_set()
    finally:
        release.set()
        await asyncio.wait_for(cleanup_stopped.wait(), timeout=1)
        await asyncio.sleep(0)
