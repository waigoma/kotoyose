from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest
from structlog.testing import capture_logs

from kotoyose.acp_client import AcpClient, AcpError
from kotoyose.acp_spec import AcpSpec
from kotoyose.acp_turn import AcpPermissionPolicy, AcpTurn, acp_mcp_servers
from kotoyose.print_cli_agent import _leader_exited
from kotoyose.print_cli_profiles import AGY_PROFILE
from kotoyose.runtime.acp import AcpPrompt, AcpRuntimeSession
from kotoyose.runtime.base import RuntimeEvent, RuntimeResult, RuntimeTurnError


FAKE_ACP = Path(__file__).with_name("fixtures") / "fake_acp_agent.py"


def seen(path: Path) -> list[dict]:
    return [json.loads(line) for line in (path / "acp-requests.jsonl").read_text().splitlines()]


def make_runtime(tmp_path, scenario="normal", *, idle=120, mcp=None, timeout=8, home=None):
    profile = replace(AGY_PROFILE, acp=AcpSpec((str(FAKE_ACP), scenario), ("local-home",)))
    return AcpRuntimeSession(
        profile, Path(sys.executable), home or tmp_path, tmp_path, "model-x", timeout, "instructions",
        acp_mcp_servers(mcp or {}), AcpPermissionPolicy("read", "host", ("Read", "Bash")), None,
        idle_seconds=idle,
    )


@pytest.mark.parametrize(("scenario", "model", "expected"), [
    ("agy_native", "gemini-4-hydrogen-high", [("set_model", {"modelId": "gemini-4-hydrogen-high"})]),
    ("agy_native", "gemini-4-hydrogen", [
        ("set_config_option", {"configId": "model", "value": "gemini-4-hydrogen"}),
    ]),
    ("normal", "tEsT", [("set_config_option", {"configId": "model", "value": "model-x"})]),
    ("cursor_native", "aUtO", []),
    ("cursor_native", "GROK-4.6", [("set_model", {"modelId": "grok-4.6[effort=high,fast=true]"})]),
    ("cursor_fallback", "grok-4.6[effort=high,fast=true]", [
        ("set_model", {"modelId": "grok-4.6[effort=high,fast=true]"}),
        ("set_config_option", {"configId": "model", "value": "grok-4.6[effort=high,fast=true]"}),
    ]),
    ("agy_fallback", "gemini-4-hydrogen-high", [
        ("set_model", {"modelId": "gemini-4-hydrogen-high"}),
        ("set_config_option", {"configId": "model", "value": "gemini-4-hydrogen"}),
        ("set_config_option", {"configId": "thought_level", "value": "high"}),
    ]),
])
async def test_resolves_advertised_model(tmp_path, scenario, model, expected):
    spec = AcpSpec((str(FAKE_ACP), scenario), ())
    client = await AcpClient.spawn([sys.executable, *spec.argv], cwd=tmp_path, env={})
    try:
        await client.start_session(spec, cwd=str(tmp_path), mcp_servers=[], model=model)
        assert [(item["method"], item["params"]) for item in seen(tmp_path)[2:]] == [
            (f"session/{method}", {"sessionId": "fake-session", **params}) for method, params in expected
        ]
    finally:
        await client.close()


@pytest.mark.parametrize(("scenario", "model", "agent_message", "model_requests"), [
    ("cursor_ambiguous", "auto", None, 0),
    ("cursor_native", "unknown", None, 0),
    ("agy_error", "gemini-4-hydrogen-high", "Model unavailable", 1),
    ("config_error", "model-x", "Config unavailable", 1),
    ("config_unconfirmed", "model-x", None, 1),
    ("agy_reset_model", "gemini-4-hydrogen-xhigh", None, 2),
])
async def test_rejects_unresolved_model_with_diagnostics(tmp_path, scenario, model, agent_message, model_requests):
    spec = AcpSpec((str(FAKE_ACP), scenario), ())
    client = await AcpClient.spawn([sys.executable, *spec.argv], cwd=tmp_path, env={})
    try:
        with capture_logs() as logs, pytest.raises(AcpError, match="^unsupported_acp_model$"):
            await client.start_session(spec, cwd=str(tmp_path), mcp_servers=[], model=model)
        diagnostic, = [entry for entry in logs if entry["event"] == "unsupported_acp_model"]
        assert diagnostic["requested_model"] == model
        assert diagnostic["agent_message"] == agent_message
        if scenario.startswith("cursor_"):
            assert diagnostic["available_models"]["default[]"] == "Auto"
            assert "grok-4.6[effort=high,fast=true]" in diagnostic["available_config_models"]
        assert len(seen(tmp_path)[2:]) == model_requests
    finally:
        await client.close()


async def test_handshake_auth_mcp_streaming_and_reuse(tmp_path):
    runtime = make_runtime(tmp_path, "auth", mcp={
        "local": {"command": "fake-command", "args": ["mcp"], "env": {"DEMO": "local"}},
        "remote": {"type": "http", "url": "https://example.invalid/mcp", "headers": {"X-Demo": "remote"}},
    })
    try:
        items = [item async for item in runtime.stream("first")]
        assert [item for item in items if isinstance(item, str)] == ["hello ", "world"]
        result = items[-1]
        assert isinstance(result, RuntimeResult)
        assert result.text == "hello world"
        assert result.runtime_session_id == "fake-session"
        assert result.usage == {
            "source": "provider", "input_tokens": 10, "output_tokens": 2,
            "context_tokens": 50, "context_window_tokens": 500,
        }
        events = [item for item in items if isinstance(item, RuntimeEvent)]
        assert [(event.kind, event.summary) for event in events] == [
            ("tool_call", "pending"), ("permission", "allowed"),
            ("permission", "denied"), ("tool_call_update", "completed"),
        ]
        assert events[0].payload == {"tool_call_id": "1", "title": "Read"}
        assert (await runtime.ask("next")).text == result.text
    finally:
        await runtime.disconnect()
    requests = seen(tmp_path)
    methods = [msg["method"] for msg in requests if "method" in msg]
    assert methods == [
        "initialize", "session/new", "authenticate", "session/new", "session/set_config_option",
        "session/prompt", "session/prompt",
    ]
    assert requests[1]["params"] == {"cwd": str(tmp_path), "mcpServers": [
        {"name": "local", "command": "fake-command", "args": ["mcp"],
         "env": [{"name": "DEMO", "value": "local"}]},
        {"type": "http", "name": "remote", "url": "https://example.invalid/mcp",
         "headers": [{"name": "X-Demo", "value": "remote"}]},
    ]}
    assert [msg["result"]["outcome"]["optionId"] for msg in requests if msg.get("id") in ("Read", "Bash")] == [
        "yes", "no", "yes", "no",
    ]


@pytest.mark.parametrize("scenario, method, second_prompt", [
    ("normal", "session/load", "latest"),
    ("no_load", "session/new", "client history"),
])
async def test_idle_reaps_and_reconnects(tmp_path, scenario, method, second_prompt):
    runtime = make_runtime(tmp_path, scenario, idle=0.02)
    try:
        await runtime.ask(AcpPrompt("first", "first history"))
        process = runtime._process.process
        async with asyncio.timeout(3):
            while runtime._process is not None:
                await asyncio.sleep(0.01)
        assert process.returncode is not None
        assert (await runtime.ask(AcpPrompt("latest", "client history"))).text == "hello world"
    finally:
        await runtime.disconnect()
    requests = seen(tmp_path)
    assert [msg["method"] for msg in requests if msg.get("method") in {"session/new", "session/load"}] == [
        "session/new", method,
    ]
    prompts = [msg["params"]["prompt"][0]["text"] for msg in requests if msg.get("method") == "session/prompt"]
    assert prompts == ["instructions\n\nfirst history", "instructions\n\n" + second_prompt]


async def test_cancellation_sends_notification_before_reaping(tmp_path):
    runtime = make_runtime(tmp_path, "cancel")
    async with asyncio.timeout(3), aclosing(runtime.stream("cancel me")) as stream:
        assert await anext(stream) == "hello "
        process = runtime._process.process
    assert process.returncode is not None
    assert runtime._process is None
    assert seen(tmp_path)[-1]["method"] == "session/cancel"


@pytest.mark.parametrize("scenario, reason, partial", [
    ("crash", "missing_completion", "hello "),
    ("helper", "missing_completion", "hello "),
    ("oversize", "output_limit_exceeded", ""),
    ("oversize_flood", "output_limit_exceeded", ""),
    ("protocol", "unsupported_acp_version", ""),
])
async def test_transport_failure_reaps_and_preserves_partial_output(tmp_path, scenario, reason, partial):
    runtime = make_runtime(tmp_path, scenario)
    with pytest.raises(RuntimeTurnError) as raised:
        async with asyncio.timeout(5):
            await runtime.ask("fail")
    assert raised.value.terminal_reason == reason
    assert raised.value.partial_result.text == partial
    assert runtime._process is None
    if scenario == "helper":
        assert _leader_exited(int((tmp_path / "helper.pid").read_text()))


async def test_unadvertised_http_fails_before_session_new(tmp_path):
    runtime = make_runtime(tmp_path, "no_http", mcp={
        "remote": {"type": "http", "url": "https://example.invalid/mcp"},
    })
    with pytest.raises(RuntimeTurnError) as raised:
        await runtime.ask("hello")
    assert raised.value.terminal_reason == "unsupported_mcp_transport"
    assert [msg["method"] for msg in seen(tmp_path)] == ["initialize"]


@pytest.mark.parametrize("surface, place, kind, tools, deny, expected", [
    ("read", "host", "read", ("Read",), (), "allowed"),
    ("none", "host", "read", ("Read",), (), "denied"),
    ("write", "host", "edit", ("Read",), (), "denied"),
    ("write", "isolated", "edit", ("Read",), (), "allowed"),
    ("write", "isolated", "edit", ("Read",), ("edit",), "denied"),
    ("write", "isolated", "edit", ("Read",), ("Read",), "denied"),
    ("write", "isolated", "edit", (), (), "denied"),
    ("write", "isolated", "other", ("Read",), (), "denied"),
    # tools not set (None): surface and place decide, like the other drivers (Task 82, 2026-11-21).
    ("write", "isolated", "edit", None, (), "allowed"),
    ("write", "isolated", "execute", None, (), "allowed"),
    ("write", "isolated", "execute", None, ("execute",), "denied"),
    ("write", "isolated", "edit", None, ("Read",), "denied"),
    ("read", "host", "read", None, (), "allowed"),
    ("read", "host", "edit", None, (), "denied"),
    ("write", "host", "edit", None, (), "denied"),
])
def test_policy_requires_explicit_tool_and_surface_permission(surface, place, kind, tools, deny, expected):
    policy = AcpPermissionPolicy(surface, place, tools, deny)
    result, event = policy.decide({
        "toolCall": {"kind": kind, "title": "Read"},
        "options": [{"kind": "allow_once", "optionId": "yes"}, {"kind": "reject_once", "optionId": "no"}],
    })
    assert event.summary == expected
    assert result["outcome"]["optionId"] == ("yes" if expected == "allowed" else "no")


def test_policy_does_not_select_persistent_option():
    result, event = AcpPermissionPolicy("read", "host", ("Read",)).decide({
        "toolCall": {"kind": "read", "title": "Read"},
        "options": [{"kind": "allow_always", "optionId": "always"}],
    })
    assert result == {"outcome": {"outcome": "cancelled"}}
    assert event.summary == "denied"


async def test_live_process_cap_evicts_idle_session(tmp_path, monkeypatch):
    monkeypatch.setattr("kotoyose.runtime.acp.MAX_LIVE_PROCESSES", 1)
    one, two = [tmp_path / name for name in ("one", "two")]
    one.mkdir()
    two.mkdir()
    first, second = make_runtime(one), make_runtime(two)
    try:
        await first.ask("first")
        process = first._process.process
        await second.ask("second")
        assert process.returncode is not None
        assert first._process is None
        assert second._process.process.returncode is None
    finally:
        await first.disconnect()
        await second.disconnect()


async def test_prompt_timeout_sends_cancel_and_reaps(tmp_path):
    runtime = make_runtime(tmp_path, "cancel", timeout=1)
    with pytest.raises(RuntimeTurnError) as raised:
        await runtime.ask("wait until cancelled")
    assert raised.value.terminal_reason == "timeout"
    assert raised.value.partial_result.text == "hello "
    assert runtime._process is None
    assert seen(tmp_path)[-1]["method"] == "session/cancel"


def test_agy_http_mcp_uses_documented_worker_flag():
    assert AGY_PROFILE.acp.arguments("model-x") == ("acp",)
    assert AGY_PROFILE.acp.arguments("model-x", mcp_servers=[{"type": "http"}]) == (
        "acp", "--process-per-session",
    )


async def test_host_chat_without_cwd_uses_a_private_temporary_directory(tmp_path):
    """Like cli.print chat: no alias cwd on the host; a temp cwd per connection, removed on close (2026-11-21)."""
    profile = replace(AGY_PROFILE, acp=AcpSpec((str(FAKE_ACP), "normal"), ("local-home",)))
    runtime = AcpRuntimeSession(
        profile, Path(sys.executable), tmp_path, None, "model-x", 8, "instructions",
        acp_mcp_servers({}), AcpPermissionPolicy("read", "host", ("Read",)), None,
    )
    try:
        result = await runtime.ask("first")
        assert isinstance(result, RuntimeResult)
        used = runtime.cwd
        assert used is not None and used.is_dir() and used != tmp_path
        assert used.name.startswith("kotoyose-acp-chat-")
    finally:
        await runtime.disconnect()
    assert not used.exists()


class _ResultClient:
    def __init__(self, result):
        self.result = result

    async def prompt(self, text, *, timeout, update, permission):
        return self.result


@pytest.mark.parametrize(("result", "expected"), [
    ({"stopReason": "end_turn", "usage": {"inputTokens": 9, "outputTokens": 2, "cachedReadTokens": 4}},
     {"source": "provider", "input_tokens": 9, "output_tokens": 2, "cache_read_tokens": 4}),
    ({"stopReason": "end_turn", "_meta": {"tokens": 7}},
     {"source": "provider", "input_tokens": 7, "cost": 0.5}),
    ({"stopReason": "end_turn"}, {"source": "unknown"}),
])
async def test_turn_reads_standard_usage_before_profile_hook(result, expected):
    def hook(prompt_result):
        tokens = prompt_result.get("_meta", {}).get("tokens")
        return {"input_tokens": tokens, "cost": 0.5} if tokens else {}

    turn = AcpTurn(AcpPermissionPolicy("read", "host", None), 1024, lambda item: None, usage_parser=hook)
    await turn.run(_ResultClient(result), "hi", timeout=1)
    assert {key: value for key, value in turn.usage.items() if value is not None} == expected
