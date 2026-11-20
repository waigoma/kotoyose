"""Installed SDK contracts without provider calls or provider credentials.

The Claude subprocess below is a local synthetic JSONL peer, not Claude Code.
These tests do not establish Native OS isolation or live-provider conformance.
"""

import asyncio
import inspect
from pathlib import Path
import sys

from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, TextBlock
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from cursor_sdk import (
    AgentOptions, AsyncAgent, AsyncClient, AsyncRun, LocalAgentOptions, SDKImage, UserMessage,
)
from openai_codex import (
    ApprovalMode, AsyncCodex, AsyncThread, AsyncTurnHandle, CodexConfig, Sandbox,
)
import pytest

from kotoyose.native_claude import (
    NativeAgentJob, NativeClaudeWorker, _build_options, _capture_process,
)
from kotoyose.runtime.base import RuntimeImageInput, RuntimeTextInput
from kotoyose.runtime.codex import _codex_turn_input
from kotoyose.runtime.cursor import _cursor_user_message


def test_codex_sdk_accepts_runtime_call_shapes(tmp_path: Path):
    """SDK fakes accept **kwargs; check the installed methods accept our keywords."""
    receiver = object()
    config = CodexConfig(
        codex_bin=None, cwd=str(tmp_path), config_overrides=(),
        env={"CODEX_HOME": str(tmp_path / "codex-home")},
    )
    inspect.signature(AsyncCodex).bind(config)
    kwargs = {
        "cwd": str(tmp_path), "developer_instructions": "fixture instructions",
        "sandbox": Sandbox.read_only, "approval_mode": ApprovalMode.deny_all,
    }
    inspect.signature(AsyncCodex.thread_start).bind(receiver, model="fixture-model", **kwargs)
    inspect.signature(AsyncCodex.thread_resume).bind(receiver, "fixture-thread", **kwargs)
    inspect.signature(AsyncThread.turn).bind(receiver, "fixture prompt", effort="high")
    inspect.signature(AsyncTurnHandle.stream).bind(receiver)
    inspect.signature(AsyncCodex.close).bind(receiver)
    prompt = [RuntimeTextInput("fixture"), RuntimeImageInput("data:image/png;base64,aGVsbG8=")]
    assert len(_codex_turn_input(prompt)) == 2


def test_cursor_sdk_accepts_runtime_call_shapes_and_options(tmp_path: Path):
    """Construct real options and image values, but never launch a Cursor bridge."""
    receiver = object()
    local = LocalAgentOptions(cwd=tmp_path, setting_sources=[], sandbox_options={"enabled": True})
    options = AgentOptions(
        model={"id": "fixture-model", "params": [{"id": "effort", "value": "high"}]},
        api_key="synthetic-test-key", local=local, mcp_servers=None,
        tools=[], disallowed_tools=["shell"], agents=None, mode="plan",
    )
    assert options.local is local
    assert options.tools == []
    assert options.disallowed_tools == ["shell"]
    inspect.signature(AsyncClient.launch_bridge).bind(workspace=str(tmp_path))
    inspect.signature(AsyncAgent.create).bind(options, client=receiver, api_key="synthetic-test-key")
    inspect.signature(AsyncAgent.resume).bind("fixture-agent", options, client=receiver)
    inspect.signature(AsyncAgent.send).bind(receiver, "fixture prompt")
    inspect.signature(AsyncRun.events).bind(receiver)
    inspect.signature(AsyncAgent.close).bind(receiver)
    inspect.signature(AsyncClient.aclose).bind(receiver)
    assert isinstance(SDKImage.from_url("https://example.invalid/image.png"), SDKImage)
    assert isinstance(SDKImage.from_data("aGVsbG8=", "image/png"), SDKImage)
    prompt = [RuntimeTextInput("fixture"), RuntimeImageInput("data:image/png;base64,aGVsbG8=")]
    assert isinstance(_cursor_user_message(prompt), UserMessage)


def _native_job(cli: Path) -> NativeAgentJob:
    return NativeAgentJob(
        attempt_id="sdk-contract-fixture", model="fixture-model", cli_binary=cli,
        credential_name="ANTHROPIC_API_KEY", credential_value="synthetic-test-key",
        instructions="fixture instructions", prompt="fixture prompt", output_contract="text",
        max_turns=1, timeout_seconds=5, grace_seconds=0.2, output_limit_bytes=1024, tool_refs=(),
    )


def test_native_sdk_serializes_restricted_options(tmp_path: Path):
    """Use the real SDK command builder; it does not launch any process."""
    cli = tmp_path / "unused-synthetic-cli"
    options = _build_options(_native_job(cli), cli, tmp_path, None)
    assert options.tools == []
    assert options.allowed_tools == []
    assert options.strict_mcp_config
    assert options.permission_mode == "dontAsk"
    assert options.setting_sources == []
    assert options.skills == []
    assert options.plugins == []
    assert options.agents is None
    assert options.fallback_model is None
    command = SubprocessCLITransport(prompt="fixture prompt", options=options)._build_command()
    assert command[0] == str(cli)
    assert command[command.index("--tools") + 1] == ""
    assert "--setting-sources=" in command
    assert "--strict-mcp-config" in command
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--model") + 1] == "fixture-model"


@pytest.fixture
def synthetic_claude_cli(tmp_path: Path) -> Path:
    """A credential-free local protocol peer; it never reads credentials or calls a provider."""
    cli = tmp_path / "synthetic-claude-cli"
    cli.write_text(
        f"#!{sys.executable}\n" + r'''
import json
import sys

if "--version" in sys.argv:
    print("99.0.0 (synthetic local test fixture)")
    sys.exit(0)

for line in sys.stdin:
    request = json.loads(line)
    if request["type"] == "control_request":
        print(json.dumps({
            "type": "control_response",
            "response": {
                "subtype": "success", "request_id": request["request_id"], "response": {},
            },
        }), flush=True)
    elif request["type"] == "user":
        print(json.dumps({
            "type": "assistant",
            "message": {
                "role": "assistant", "model": "fixture-model",
                "content": [{"type": "text", "text": "fixture-ok"}],
            },
        }), flush=True)
        print(json.dumps({
            "type": "result", "subtype": "success", "duration_ms": 1,
            "duration_api_ms": 1, "is_error": False, "num_turns": 1,
            "session_id": "synthetic-session", "total_cost_usd": 0,
            "usage": {"input_tokens": 1, "output_tokens": 2},
        }), flush=True)
''',
        encoding="utf-8",
    )
    cli.chmod(0o700)
    return cli


@pytest.mark.asyncio
async def test_native_real_sdk_synthetic_cli_lifecycle(tmp_path: Path, synthetic_claude_cli: Path):
    """Verify private PID/stop evidence with a real SDK subprocess and fake provider bytes."""
    job = _native_job(synthetic_claude_cli)
    options = _build_options(job, synthetic_claude_cli, tmp_path, None)
    client = ClaudeSDKClient(options=options)
    process = None
    try:
        async with asyncio.timeout(10):
            await client.connect()
            process = _capture_process(client)
            assert isinstance(process.pid, int) and process.pid > 0
            assert process.returncode is None
            assert all(callable(getattr(process, name, None)) for name in ("terminate", "kill", "wait"))
            await client.query("fixture prompt")
            messages = [message async for message in client.receive_response()]
            assert any(
                isinstance(message, AssistantMessage) and isinstance(message.content[0], TextBlock)
                and message.content[0].text == "fixture-ok" for message in messages
            )
            assert any(
                isinstance(message, ResultMessage) and message.session_id == "synthetic-session"
                for message in messages
            )
    finally:
        async with asyncio.timeout(10):
            await client.disconnect()
    assert client._transport is None
    assert process.returncode is not None

    started = []

    async def on_started(pid: int) -> None:
        started.append(pid)

    async def no_tool(_argv, _cwd):
        raise AssertionError("The synthetic provider must not invoke tools")

    result = await NativeClaudeWorker(synthetic_claude_cli).execute(
        job, asyncio.Event(), no_tool, on_process_started=on_started,
    )
    assert result.status == "succeeded"
    assert result.stop_confirmation == "confirmed"
    assert result.content == b"fixture-ok"
    assert result.usage == {"source": "reported", "cost": 0.0, "input_tokens": 1, "output_tokens": 2}
    assert len(started) == 1
