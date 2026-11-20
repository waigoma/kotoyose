"""Cursor SDK activity -> RuntimeEvent parity tests."""

from types import SimpleNamespace

from kotoyose.runtime.base import RuntimeEvent, RuntimeResult
from kotoyose.runtime.cursor import CursorRuntimeSession, _runtime_event_from_message


class FakeRun:
    def __init__(self, events, usage=None):
        self._events = events
        self.usage = usage
        self.id = "run-test"
        self.status = "running"
        self.cancel_count = 0

    async def events(self):
        for event in self._events:
            if event.result is not None:
                self.status = event.result.get("status", self.status)
            yield event
        self.status = "finished"

    async def wait(self):
        return SimpleNamespace(id=self.id, status=self.status, result="", usage=self.usage)

    async def cancel(self):
        self.cancel_count += 1
        self.status = "cancelled"


class FakeAgent:
    def __init__(self, run):
        self.agent_id = "agent-test"
        self.run = run

    async def send(self, prompt):
        return self.run

    async def close(self):
        return None


def envelope(message=None, result=None):
    return SimpleNamespace(sdk_message=message, result=result)


def assistant(text):
    return SimpleNamespace(
        type="assistant",
        message=SimpleNamespace(content=[SimpleNamespace(text=text)]),
    )


def tool_call(status, *, name="Read", call_id="call-1", args=None, result=None, truncated=False):
    return SimpleNamespace(
        type="tool_call",
        status=status,
        name=name,
        call_id=call_id,
        args=args,
        result=result,
        truncated=truncated,
    )


def test_tool_running_maps_to_tool_use():
    event = _runtime_event_from_message(
        tool_call("running", name="Read", args={"path": "src/app.py"})
    )

    assert event == RuntimeEvent(
        kind="tool_use",
        tool_name="Read",
        summary='{"path": "src/app.py"}',
        payload={
            "tool_use_id": "call-1",
            "input": '{"path": "src/app.py"}',
            "truncated": False,
        },
    )


def test_tool_completed_maps_to_tool_result():
    event = _runtime_event_from_message(
        tool_call("completed", name="Read", result={"content": "ok"}, truncated=True)
    )

    assert event.kind == "tool_result"
    assert event.tool_name == "Read"
    assert event.payload == {
        "tool_use_id": "call-1",
        "is_error": False,
        "output": '{"content": "ok"}',
        "truncated": True,
    }


def test_tool_error_is_a_result_event_not_a_fatal_runtime_error():
    event = _runtime_event_from_message(
        tool_call("error", name="Bash", result="command failed")
    )

    assert event.kind == "tool_result"
    assert event.tool_name == "Bash"
    assert event.payload["is_error"] is True
    assert event.payload["output"] == "command failed"


def test_thinking_and_task_messages_are_observable():
    thinking = _runtime_event_from_message(
        SimpleNamespace(type="thinking", text="inspect the failing test")
    )
    task = _runtime_event_from_message(
        SimpleNamespace(type="task", status="running", text="Run tests")
    )

    assert thinking.kind == "thinking"
    assert thinking.payload["text"] == "inspect the failing test"
    assert task.kind == "task"
    assert task.payload == {"status": "running", "text": "Run tests"}


async def test_stream_emits_turn_tool_activity_and_text_without_changing_result():
    run = FakeRun(
        [
            envelope(tool_call("running", args={"path": "README.md"})),
            envelope(tool_call("completed", result="contents")),
            envelope(assistant("done")),
        ]
    )
    session = CursorRuntimeSession(None, "/tmp")
    session._agent = FakeAgent(run)

    values = [item async for item in session.stream("inspect")]

    assert isinstance(values[0], RuntimeEvent)
    assert values[0].kind == "turn"
    assert values[1].kind == "tool_use"
    assert values[2].kind == "tool_result"
    assert values[3] == "done"
    assert values[-1] == RuntimeResult("done", None, "agent-test", "cursor")


async def test_ask_ignores_runtime_events_when_joining_text():
    run = FakeRun(
        [
            envelope(tool_call("running", name="Read", args={"path": "x"})),
            envelope(tool_call("completed", name="Read", result="y")),
            envelope(assistant("answer")),
        ]
    )
    session = CursorRuntimeSession(None, "/tmp")
    session._agent = FakeAgent(run)

    result = await session.ask("question")

    assert result.text == "answer"
    assert result.runtime_session_id == "agent-test"


async def test_terminal_cursor_error_is_exposed_as_activity_before_raise():
    session = CursorRuntimeSession(None, "/tmp")
    session._agent = FakeAgent(
        FakeRun([envelope(result={"status": "error", "message": "boom"})])
    )

    seen = []
    try:
        async for item in session.stream("x"):
            seen.append(item)
    except RuntimeError as exc:
        assert "status error" in str(exc)
    else:
        raise AssertionError("RuntimeError was not raised")

    assert seen[-1].kind == "error"
    assert seen[-1].summary == "Cursor run ended with status error"
