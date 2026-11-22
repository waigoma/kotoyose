"""live provider を呼ばない Native Claude worker 契約テスト。"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import anyio
import pytest
from claude_agent_sdk import (
    AssistantMessage, ResultMessage, TextBlock, ThinkingBlock, ToolUseBlock,
    MirrorErrorMessage, RateLimitEvent, RateLimitInfo, SystemMessage, TaskUpdatedMessage,
)

from kotoyose.native_claude import (
    NativeAgentJob, NativeClaudeWorker, NativeImageInput, _extract_json_object, _is_json_object,
    _build_options,
)
from kotoyose.execution_resources import NativeRuntimeProfile
from kotoyose.provider_jail import provider_environment
from kotoyose.client_tool_bridge import ClientToolCallOutcome
from kotoyose.execution_agent import _ProgressWriter
from kotoyose.runtime.base import RuntimeEvent


def _job(tmp_path: Path, **overrides: object) -> NativeAgentJob:
    values: dict[str, object] = {
        "attempt_id": "attempt-1",
        "model": "claude-test",
        "cli_binary": Path("/usr/local/bin/claude"),
        "credential_name": "ANTHROPIC_API_KEY",
        "credential_value": "dummy-secret",
        "instructions": "Answer briefly.",
        "prompt": "hello",
        "output_contract": "text",
        "max_turns": 2,
        "timeout_seconds": 2.0,
        "grace_seconds": 0.1,
        "output_limit_bytes": 1024,
        "tool_refs": (),
    }
    values.update(overrides)
    return NativeAgentJob(**values)


class _Process:
    def __init__(self, *, returncode: int | None = 0, pid: int = 1234):
        self.returncode = returncode
        self.pid = pid
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.terminated = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


class _Client:
    instances: list["_Client"] = []
    messages: list[object] = []
    wait_for_messages = False

    def __init__(self, *, options):
        self.options = options
        self._transport = SimpleNamespace(_process=_Process())
        self.queried: list[str] = []
        self.disconnected = False
        type(self).instances.append(self)

    async def connect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self.queried.append(prompt)

    async def receive_response(self):
        if type(self).wait_for_messages:
            await asyncio.Future()
        for message in type(self).messages:
            yield message

    async def disconnect(self) -> None:
        self.disconnected = True
        self._transport._process.returncode = 0


class _ScopedClient(_Client):
    def __init__(self, *, options):
        super().__init__(options=options)
        self._scope = anyio.CancelScope()
        self._owner = None

    async def connect(self) -> None:
        self._owner = asyncio.current_task()
        self._scope.__enter__()

    async def query(self, prompt: str) -> None:
        assert asyncio.current_task() is self._owner
        await super().query(prompt)

    async def receive_response(self):
        assert asyncio.current_task() is self._owner
        async for message in super().receive_response():
            yield message

    async def disconnect(self) -> None:
        assert asyncio.current_task() is self._owner
        self._scope.__exit__(None, None, None)
        await super().disconnect()


async def _successful_tool(_argv: tuple[str, ...], _cwd: str):
    return SimpleNamespace(status="succeeded", stdout=b"", stderr=b"")


async def _failed_tool(_argv: tuple[str, ...], _cwd: str):
    return SimpleNamespace(status="failed", exit_code=17, stdout=b"out", stderr=b"err")


def _result_message(*, error: bool = False, usage: dict | None = None) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=error,
        num_turns=1,
        session_id="session",
        total_cost_usd=0.25 if usage else None,
        usage=usage,
    )


@pytest.mark.asyncio
async def test_sdk_observations_are_passive_in_native_worker(tmp_path):
    _Client.messages = [
        SystemMessage("init", {"session_id": "session"}),
        RateLimitEvent(RateLimitInfo("allowed_warning"), "uuid", "session"),
        RateLimitEvent(RateLimitInfo("rejected"), "uuid", "foreign-session"),
        MirrorErrorMessage("mirror_error", {}, error="SECRET storage error"),
        TaskUpdatedMessage("task_updated", {}, "task-1", {"status": "killed", "error": "SECRET"}),
        AssistantMessage([TextBlock("hello")], "fixture", session_id="session"),
        _result_message(usage={"input_tokens": 3, "output_tokens": 4}),
    ]
    progress, activity = [], []
    with patch("kotoyose.native_claude.ClaudeSDKClient", _Client):
        result = await NativeClaudeWorker(Path("/repo/scripts/native-claude")).execute(
            _job(tmp_path, progress_event=progress.append, activity_event=activity.append),
            asyncio.Event(), _successful_tool, on_process_started=lambda _: asyncio.sleep(0),
        )
    assert result.status == "succeeded"
    assert result.content == b"hello"
    assert result.usage["input_tokens"] == 3
    assert result.stop_confirmation == "confirmed"
    assert [event.kind for event in activity] == [
        "rate_limit", "session_store_warning", "task_updated", "text",
    ]
    assert "SECRET" not in repr(progress + activity)
    assert [event.payload["status"] for event in progress if event.kind == "rate_limit"] == ["allowed_warning"]


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_name", ["progress_event", "activity_event"])
async def test_observation_callback_failure_does_not_fail_native_turn(tmp_path, callback_name):
    _Client.messages = [
        RateLimitEvent(RateLimitInfo("allowed_warning"), "uuid", "session"),
        _result_message(),
    ]

    def unavailable_callback(_event):
        raise RuntimeError("SECRET callback failure")

    with patch("kotoyose.native_claude.ClaudeSDKClient", _Client):
        result = await NativeClaudeWorker(Path("/repo/scripts/native-claude")).execute(
            _job(tmp_path, **{callback_name: unavailable_callback}), asyncio.Event(),
            _successful_tool, on_process_started=lambda _: asyncio.sleep(0),
        )
    assert result.status == "succeeded"
    assert result.stop_confirmation == "confirmed"


@pytest.mark.asyncio
async def test_minimal_session_builds_restricted_options_and_reports_usage(tmp_path: Path):
    _Client.instances.clear()
    _Client.messages = [
        AssistantMessage(content=[TextBlock(text="hello")], model="claude-test"),
        _result_message(usage={"input_tokens": 3, "output_tokens": 4}),
    ]
    started: list[int] = []
    activity = []
    stopped = []

    async def on_started(pid: int) -> None:
        started.append(pid)

    with patch("kotoyose.native_claude.ClaudeSDKClient", _Client):
        result = await NativeClaudeWorker(Path("/repo/scripts/native-claude")).execute(
            _job(tmp_path, activity_event=activity.append, on_runtime_stopped=lambda: stopped.append(True)), asyncio.Event(), _successful_tool,
            on_process_started=on_started,
        )

    assert result.status == "succeeded"
    assert result.content == b"hello"
    assert result.usage == {
        "source": "reported",
        "cost": 0.25,
        "input_tokens": 3,
        "output_tokens": 4,
    }
    assert result.stop_confirmation == "confirmed"
    assert started == [1234]
    assert stopped == [True]
    assert [(event.kind, event.summary) for event in activity] == [("text", "hello")]
    options = _Client.instances[0].options
    assert options.model == "claude-test"
    assert options.tools == []
    assert options.allowed_tools == []
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    assert options.permission_mode == "dontAsk"
    assert options.fallback_model is None
    assert options.plugins == []
    assert options.skills == []
    assert options.agents is None
    assert options.disallowed_tools == ["Task", "Agent"]
    assert options.max_buffer_size > 1024
    assert options.cwd != tmp_path
    assert options.env == {
        "CLAUDE_CODE_OAUTH_TOKEN": "",
        "ANTHROPIC_API_KEY": "dummy-secret",
        "ANTHROPIC_AUTH_TOKEN": "",
        "ANTHROPIC_BASE_URL": "",
        "KOTOYOSE_NATIVE_CLAUDE_BINARY": "/usr/local/bin/claude",
    }


def test_explicit_native_profile_keeps_default_endpoint_unset(tmp_path: Path):
    profile = NativeRuntimeProfile(
        provider="claude", model="claude-sonnet-4-5", cli_binary=Path("/usr/bin/sh"),
        credential_mode="runtime_managed",
    )
    options = _build_options(_job(tmp_path), Path("/usr/bin/env"), tmp_path, None)
    assert profile.base_url is None
    assert options.env["ANTHROPIC_BASE_URL"] == ""
    assert "ANTHROPIC_BASE_URL" not in provider_environment(options.env)


@pytest.mark.asyncio
async def test_native_sdk_progress_matches_legacy_event_shape_and_order(tmp_path: Path):
    events: list[dict] = []
    captured_tools = {}

    def server(name, *, version, tools):
        captured_tools[name] = tools[0]
        return {"type": "sdk", "name": name}

    class ProgressClient(_Client):
        async def receive_response(self):
            yield AssistantMessage(content=[
                TextBlock(text="reading"), ThinkingBlock(thinking="considering", signature="hidden"),
                ToolUseBlock(id="call-1", name="mcp__sandbox__exec",
                             input={"argv": ["echo", "hello"], "cwd": "."}),
            ], model="claude-test")
            await captured_tools["sandbox"].handler({"argv": ["echo", "hello"], "cwd": "."})
            yield AssistantMessage(content=[TextBlock(text="done")], model="claude-test")
            yield _result_message()

    async def execute_tool(_argv, _cwd):
        return SimpleNamespace(status="succeeded", exit_code=0, stdout=b"hello\n", stderr=b"")

    with patch("kotoyose.native_claude.create_sdk_mcp_server", side_effect=server), \
            patch("kotoyose.native_claude.ClaudeSDKClient", ProgressClient):
        result = await NativeClaudeWorker().execute(
            _job(tmp_path, tool_refs=("builtin:sandbox-exec@1",),
                 progress_event=lambda event: events.append(asdict(event))),
            asyncio.Event(), execute_tool, on_process_started=lambda _pid: asyncio.sleep(0),
        )
    assert result.status == "succeeded"
    assert [event["kind"] for event in events] == [
        "turn", "text", "thinking", "tool_use", "tool_result", "turn", "text",
    ]
    assert events[0] == asdict(RuntimeEvent(kind="turn", summary="Assistant turn 1", payload={"turn": 1}))
    assert events[1] == asdict(RuntimeEvent(kind="text", summary="reading", payload={"text": "reading"}))
    assert events[2]["payload"] == {"text": "considering"}
    assert events[3]["tool_name"] == "mcp__sandbox__exec"
    assert events[3]["summary"] == "echo hello"
    assert events[4]["tool_name"] == "mcp__sandbox__exec"
    assert events[4]["summary"] == "exit code 0: hello"
    assert events[4]["payload"] == {"exit_code": 0, "status": "succeeded", "output": "hello\n"}
    assert events[5]["payload"] == {"turn": 2}


@pytest.mark.asyncio
async def test_native_progress_truncation_and_attempt_cap(tmp_path: Path):
    class Store:
        def __init__(self):
            self.events = []

        async def append_event(self, attempt_id, kind, payload):
            self.events.append((attempt_id, kind, payload))

    store = Store()
    writer = _ProgressWriter(store, "attempt-1")
    long_text = "x" * 900
    _Client.messages = [AssistantMessage(content=[TextBlock(text=long_text)], model="claude-test"),
                        _result_message()]
    with patch("kotoyose.native_claude.ClaudeSDKClient", _Client):
        result = await NativeClaudeWorker().execute(
            _job(tmp_path, output_limit_bytes=1000, progress_event=writer.emit),
            asyncio.Event(), _successful_tool, on_process_started=lambda _pid: asyncio.sleep(0),
        )
    assert result.status == "succeeded"
    for _ in range(5_005):
        writer.emit(RuntimeEvent(kind="turn", summary="Assistant turn 3", payload={"turn": 3}))
    await writer.finish()
    assert len(store.events) == 5_000
    assert all(kind == "provider_progress" for _, kind, _ in store.events)
    assert store.events[1][2]["summary"] == long_text[:500]
    assert store.events[1][2]["payload"]["text"] == long_text[:500]


@pytest.mark.asyncio
async def test_native_progress_append_failure_does_not_fail_attempt(tmp_path: Path):
    class FailingStore:
        calls = 0

        async def append_event(self, _attempt_id, _kind, _payload):
            self.calls += 1
            raise OSError("journal unavailable")

    store = FailingStore()
    writer = _ProgressWriter(store, "attempt-1")
    _Client.messages = [AssistantMessage(content=[TextBlock(text="ok")], model="claude-test"),
                        _result_message()]
    with patch("kotoyose.native_claude.ClaudeSDKClient", _Client):
        result = await NativeClaudeWorker().execute(
            _job(tmp_path, progress_event=writer.emit), asyncio.Event(), _successful_tool,
            on_process_started=lambda _pid: asyncio.sleep(0),
        )
    await writer.finish()
    assert result.status == "succeeded"
    assert result.content == b"ok"
    assert store.calls == 1


@pytest.mark.asyncio
async def test_image_input_uses_sdk_streaming_base64_content_block(tmp_path: Path):
    _Client.instances.clear()
    _Client.messages = [_result_message()]
    content = b"\x89PNG\r\n\x1a\nimage"
    digest = hashlib.sha256(content).hexdigest()
    image = NativeImageInput("artifact:attempt:image:" + digest, digest, "image/png", content)
    with patch("kotoyose.native_claude.ClaudeSDKClient", _Client):
        result = await NativeClaudeWorker().execute(
            _job(tmp_path, image_inputs=(image,)), asyncio.Event(), _successful_tool,
            on_process_started=lambda _pid: asyncio.sleep(0),
        )
    assert result.status == "succeeded"
    stream = _Client.instances[0].queried[0]
    messages = [message async for message in stream]
    assert messages[0]["type"] == "user"
    blocks = messages[0]["message"]["content"]
    assert blocks[0] == {"type": "text", "text": "hello"}
    assert blocks[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(content).decode("ascii")},
    }


@pytest.mark.asyncio
async def test_image_input_enforces_count_and_total_size_bound_before_sdk(tmp_path: Path):
    _Client.instances.clear()
    content = b"\x89PNG\r\n\x1a\nimage"
    digest = hashlib.sha256(content).hexdigest()
    image = NativeImageInput("artifact:attempt:image:" + digest, digest, "image/png", content)
    with patch("kotoyose.native_claude.ClaudeSDKClient", _Client):
        result = await NativeClaudeWorker().execute(
            _job(tmp_path, image_inputs=(image,) * 11), asyncio.Event(), _successful_tool,
            on_process_started=lambda _pid: asyncio.sleep(0),
        )
    assert result.status == "failed"
    assert result.reason == "invalid_job"
    assert _Client.instances == []


@pytest.mark.asyncio
async def test_sdk_anyio_scope_is_owned_by_one_task(tmp_path: Path):
    _ScopedClient.instances.clear()
    _ScopedClient.messages = [_result_message()]
    with patch("kotoyose.native_claude.ClaudeSDKClient", _ScopedClient):
        result = await NativeClaudeWorker().execute(
            _job(tmp_path), asyncio.Event(), _successful_tool,
            on_process_started=lambda _pid: asyncio.sleep(0),
        )
    assert result.status == "succeeded"
    assert _ScopedClient.instances[0].disconnected is True


@pytest.mark.asyncio
async def test_builtin_tool_ref_exposes_only_exact_mcp_tool(tmp_path: Path):
    _Client.instances.clear()
    _Client.messages = [_result_message()]
    with patch("kotoyose.native_claude.ClaudeSDKClient", _Client):
        result = await NativeClaudeWorker().execute(
            _job(tmp_path, tool_refs=("builtin:sandbox-exec@1",)),
            asyncio.Event(),
            _successful_tool,
            on_process_started=lambda _pid: asyncio.sleep(0),
        )
    assert result.status == "succeeded"
    options = _Client.instances[0].options
    assert options.allowed_tools == ["mcp__sandbox__exec"]
    assert list(options.mcp_servers) == ["sandbox"]


@pytest.mark.asyncio
async def test_unknown_tool_ref_is_rejected_before_sdk(tmp_path: Path):
    _Client.instances.clear()
    with patch("kotoyose.native_claude.ClaudeSDKClient", _Client):
        result = await NativeClaudeWorker().execute(
            _job(tmp_path, tool_refs=("mcp:unknown",)),
            asyncio.Event(),
            _successful_tool,
            on_process_started=lambda _pid: asyncio.sleep(0),
        )
    assert result.status == "failed"
    assert result.reason == "unsupported_tool_ref"
    assert _Client.instances == []


@pytest.mark.asyncio
async def test_process_identity_callback_failure_skips_query(tmp_path: Path):
    _Client.instances.clear()
    _Client.messages = [_result_message()]

    async def reject(_pid: int) -> None:
        raise RuntimeError("private detail")

    with patch("kotoyose.native_claude.ClaudeSDKClient", _Client):
        result = await NativeClaudeWorker().execute(
            _job(tmp_path), asyncio.Event(), _successful_tool, on_process_started=reject
        )
    assert result.status == "failed"
    assert result.reason == "sdk_error"
    assert result.content == b""
    assert _Client.instances[0].queried == []
    assert "private detail" not in result.reason


@pytest.mark.asyncio
async def test_cancellation_disconnects_and_is_never_confirmation(tmp_path: Path):
    _Client.instances.clear()
    _Client.messages = []
    _Client.wait_for_messages = True
    cancel = asyncio.Event()
    try:
        with patch("kotoyose.native_claude.ClaudeSDKClient", _Client):
            task = asyncio.create_task(
                NativeClaudeWorker().execute(
                    _job(tmp_path), cancel, _successful_tool,
                    on_process_started=lambda _pid: asyncio.sleep(0),
                )
            )
            while not _Client.instances or not _Client.instances[0].queried:
                await asyncio.sleep(0)
            cancel.set()
            result = await task
    finally:
        _Client.wait_for_messages = False
    assert result.status == "cancelled"
    assert result.stop_confirmation == "confirmed"
    assert _Client.instances[0].disconnected is True


@pytest.mark.asyncio
async def test_caller_task_cancellation_still_disconnects(tmp_path: Path):
    _Client.instances.clear()
    _Client.messages = []
    _Client.wait_for_messages = True
    try:
        with patch("kotoyose.native_claude.ClaudeSDKClient", _Client):
            task = asyncio.create_task(
                NativeClaudeWorker().execute(
                    _job(tmp_path), asyncio.Event(), _successful_tool,
                    on_process_started=lambda _pid: asyncio.sleep(0),
                )
            )
            while not _Client.instances or not _Client.instances[0].queried:
                await asyncio.sleep(0)
            task.cancel()
            result = await task
    finally:
        _Client.wait_for_messages = False
    assert result.status == "cancelled"
    assert _Client.instances[0].disconnected is True


@pytest.mark.asyncio
async def test_json_contract_and_output_limit(tmp_path: Path):
    _Client.messages = [
        AssistantMessage(content=[TextBlock(text="free prose")], model="claude-test"),
        _result_message(),
    ]
    with patch("kotoyose.native_claude.ClaudeSDKClient", _Client):
        invalid = await NativeClaudeWorker().execute(
            _job(tmp_path, output_contract="json"), asyncio.Event(), _successful_tool,
            on_process_started=lambda _pid: asyncio.sleep(0),
        )
    assert invalid.status == "failed"
    assert invalid.reason == "invalid_json_output"

    _Client.messages = [
        AssistantMessage(content=[TextBlock(text=json.dumps({"ok": True}))], model="claude-test"),
        _result_message(),
    ]
    with patch("kotoyose.native_claude.ClaudeSDKClient", _Client):
        limited = await NativeClaudeWorker().execute(
            _job(tmp_path, output_limit_bytes=3), asyncio.Event(), _successful_tool,
            on_process_started=lambda _pid: asyncio.sleep(0),
        )
    assert limited.status == "failed"
    assert limited.reason == "output_limit_exceeded"
    assert len(limited.content) == 3


def test_tool_argument_validation_is_strict(tmp_path: Path):
    worker = NativeClaudeWorker()
    invocation = SimpleNamespace(
        terminal=False,
        cancel=asyncio.Event(),
        deadline=float("inf"),
        lock=asyncio.Lock(),
        callbacks=0,
        idle=asyncio.Event(),
        output_limit=100,
        tool_overflow=False,
    )
    invocation.idle.set()

    async def callback(_argv, _cwd):
        raise AssertionError("invalid arguments must not dispatch")

    async def run() -> dict:
        return await worker._dispatch_tool(
            {"argv": ["echo", "\x00"], "cwd": "workspace"},
            _job(tmp_path), invocation, callback
        )

    result = asyncio.run(run())
    assert result["is_error"] is True
    assert result["content"][0]["text"] == "tool_denied"


def test_failed_tool_keeps_bounded_diagnostic_fields(tmp_path: Path):
    worker = NativeClaudeWorker()
    invocation = SimpleNamespace(
        terminal=False,
        cancel=asyncio.Event(),
        deadline=float("inf"),
        lock=asyncio.Lock(),
        callbacks=0,
        idle=asyncio.Event(),
        output_limit=256,
        tool_overflow=False,
    )
    invocation.idle.set()

    async def run() -> dict:
        return await worker._dispatch_tool(
            {"argv": ["false"], "cwd": "."}, _job(tmp_path), invocation, _failed_tool
        )

    result = asyncio.run(run())
    payload = json.loads(result["content"][0]["text"])
    assert result["is_error"] is True
    assert result["isError"] is True
    assert payload == {"status": "failed", "exit_code": 17, "stdout": "out", "stderr": "err"}


def test_json_contract_rejects_nonfinite_numbers():
    assert _is_json_object(b'{"value": NaN}') is False
    assert _is_json_object(b'{"value": Infinity}') is False
    assert _is_json_object(b'{"value": -Infinity}') is False
    assert _extract_json_object(b'{"value": NaN}') is None


def test_json_contract_extracts_single_object_from_mixed_output():
    # tool 呼び出しの前後の自由文や fence は判定に使わず、最後の object だけを正規化して残す。
    assert _extract_json_object(b'{"verdict": "PASS"}') == b'{"verdict":"PASS"}'
    fenced = 'I will inspect the evidence.\n```json\n{"verdict": "FAIL", "findings": []}\n```\n'
    assert _extract_json_object(fenced.encode()) == b'{"verdict":"FAIL","findings":[]}'
    trailing = b'Checking files first. {"verdict":"PASS","findings":[]}'
    assert _extract_json_object(trailing) == b'{"verdict":"PASS","findings":[]}'
    assert _extract_json_object(b'free prose') is None
    assert _extract_json_object(b'[1, 2]') is None
    assert _extract_json_object(b'{"a": 1} and more prose') is None


@pytest.mark.asyncio
async def test_client_approval_mechanically_stops_provider_without_sandbox_exposure(tmp_path):
    worker = NativeClaudeWorker()
    approval_id = "11111111-1111-4111-8111-111111111111"
    action = {"kind": "approval", "approval_id": approval_id, "subject_digest": "a" * 64, "call_id": "call:1"}
    captured = {}
    build = worker._build_server

    def capture(job, invocation, execute_tool):
        captured.update(job=job, invocation=invocation)
        return build(job, invocation, execute_tool)

    class ApprovalClient(_Client):
        async def receive_response(self):
            await worker._dispatch_client_tool("builtin:request-approval@1",
                {"approval_id": approval_id, "subject_digest": "a" * 64},
                captured["job"], captured["invocation"])
            await asyncio.sleep(0)
            raise AssertionError("Provider must be interrupted before continuing")
            yield _result_message()

    async def call(_ref, _args):
        return ClientToolCallOutcome("failed", {"outcome": "approval_required"}, action, "call:1")

    _Client.instances.clear()
    with patch.object(worker, "_build_server", capture), patch("kotoyose.native_claude.ClaudeSDKClient", ApprovalClient):
        result = await worker.execute(_job(tmp_path, tool_refs=("builtin:request-approval@1",),
            approval_bindings=({"approval_id": approval_id, "subject_digest": "a" * 64, "wait_seconds": 300, "mode": "blocking"},),
            client_tool_call=call), asyncio.Event(), _successful_tool,
            on_process_started=lambda _pid: asyncio.sleep(0))
    assert result.status == "approval_required", (result.reason, list(captured), len(_Client.instances))
    assert result.stop_confirmation == "confirmed"
    assert result.required_action == action
    assert _Client.instances[0].disconnected
    assert _Client.instances[0].options.allowed_tools == ["mcp__client__request_approval"]
    assert "sandbox" not in _Client.instances[0].options.mcp_servers


@pytest.mark.asyncio
async def test_unavailable_client_is_a_tool_error_without_stopping_attempt(tmp_path):
    worker = NativeClaudeWorker()
    invocation = SimpleNamespace(terminal=False, cancel=asyncio.Event(), deadline=float("inf"),
        lock=asyncio.Lock(), callbacks=0, idle=asyncio.Event(), owner=None, required_action=None)
    async def unavailable(_ref, _args):
        return ClientToolCallOutcome("client_unavailable")
    result = await worker._dispatch_client_tool("client:example", {},
        _job(tmp_path, tool_refs=("client:example",), client_tool_call=unavailable), invocation)
    assert result["isError"]
    assert json.loads(result["content"][0]["text"])["error"] == "client_unavailable"
    assert invocation.terminal is False


@pytest.mark.parametrize("failure", ["authentication failed", "subscription quota exhausted", "provider unavailable"])
async def test_custom_route_failure_has_no_alternate_credential_or_model(tmp_path, failure, monkeypatch):
    """Synthetic SDK failure tests Kotoyose behavior; upstream router policy is owner-declared."""
    class FailingClient(_Client):
        instances = []
        async def query(self, prompt):
            self.queried.append(prompt)
            raise RuntimeError(failure)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-unselected-api-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-unselected-oauth-token")
    monkeypatch.setattr("kotoyose.native_claude.ClaudeSDKClient", FailingClient)
    job = _job(tmp_path, model="opaque-model", base_url="http://127.0.0.1:19999/v1",
               credential_name="ANTHROPIC_AUTH_TOKEN", credential_value="synthetic-route-token")
    async def on_started(pid):
        pass
    result = await NativeClaudeWorker(Path("/repo/scripts/native-claude")).execute(
        job, asyncio.Event(), _successful_tool, on_process_started=on_started)
    assert result.status == "failed" and result.reason == "sdk_error"
    assert len(FailingClient.instances) == 1
    client = FailingClient.instances[0]
    assert len(client.queried) == 1
    assert client.options.model == job.model and client.options.fallback_model is None
    environment = provider_environment(client.options.env)
    assert environment["ANTHROPIC_BASE_URL"] == job.base_url
    assert environment["ANTHROPIC_AUTH_TOKEN"] == "synthetic-route-token"
    assert "ANTHROPIC_API_KEY" not in environment and "CLAUDE_CODE_OAUTH_TOKEN" not in environment

@pytest.mark.asyncio
async def test_claude_rejected_limit_and_legacy_request(tmp_path):
    _Client.messages = [RateLimitEvent(RateLimitInfo('rejected', rate_limit_type='five_hour', resets_at=1767229200), 'event', 'session')]
    with patch('kotoyose.native_claude.ClaudeSDKClient', _Client):
        result = await NativeClaudeWorker().execute(_job(tmp_path, limits_mode='agent_work_v1'), asyncio.Event(), _successful_tool, on_process_started=lambda _: asyncio.sleep(0))
        legacy = await NativeClaudeWorker().execute(_job(tmp_path), asyncio.Event(), _successful_tool, on_process_started=lambda _: asyncio.sleep(0))
    assert result.reason == 'provider_usage_limit' and result.turns == 0
    assert result.provider_limit.limit_window == 'five_hour'
    assert result.provider_limit.reset_at.isoformat() == '2026-01-01T01:00:00+00:00'
    assert legacy.reason == 'sdk_error' and legacy.provider_limit is None
    for status in (429, 401, 403, 500, True, '429'):
        message = _result_message(error=True)
        message.api_error_status = status
        message.terminal_reason = 'api_error'
        _Client.messages = [message]
        with patch('kotoyose.native_claude.ClaudeSDKClient', _Client):
            current = await NativeClaudeWorker().execute(_job(tmp_path, limits_mode='agent_work_v1'), asyncio.Event(), _successful_tool, on_process_started=lambda _: asyncio.sleep(0))
            old = await NativeClaudeWorker().execute(_job(tmp_path), asyncio.Event(), _successful_tool, on_process_started=lambda _: asyncio.sleep(0))
        assert old.reason == 'provider_error'
        assert current.reason == ('provider_usage_limit' if status == 429 and type(status) is int else 'provider_error')
        if status == 429 and type(status) is int:
            assert current.provider_limit.limit_window == 'rate' and current.provider_limit.reset_at is None
            assert current.provider_limit.resume == 'unavailable' and current.provider_limit.unavailable_reason == 'not_retained'



@pytest.mark.asyncio
async def test_claude_turn_limit_is_not_provider_error(tmp_path):
    message = _result_message(error=True)
    message.subtype = 'error_max_turns'
    _Client.messages = [message]
    with patch('kotoyose.native_claude.ClaudeSDKClient', _Client):
        result = await NativeClaudeWorker().execute(_job(tmp_path, limits_mode='agent_work_v1'), asyncio.Event(), _successful_tool, on_process_started=lambda _: asyncio.sleep(0))
    assert result.reason == 'turn_limit_exceeded' and result.provider_limit is None and result.turns == 0


@pytest.mark.asyncio
async def test_claude_output_limit_is_not_quota(tmp_path):
    _Client.messages = [AssistantMessage([TextBlock('abcdef')], 'fixture')]
    with patch('kotoyose.native_claude.ClaudeSDKClient', _Client):
        result = await NativeClaudeWorker().execute(_job(tmp_path, limits_mode='agent_work_v1', output_limit_bytes=3), asyncio.Event(), _successful_tool, on_process_started=lambda _: asyncio.sleep(0))
    assert result.reason == 'output_limit_exceeded' and result.provider_limit is None and result.output_bytes == 6
