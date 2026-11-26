import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock
from claude_agent_sdk.types import ToolPermissionContext
from fastapi import Depends, FastAPI, Request
from pydantic import ValidationError

from kotoyose import config, session
from kotoyose.chat_approvals import APPROVAL_REJECTED, ChatApprovals, OPT_IN_REQUIRED, TurnApprovals, bind_turn_approvals
from kotoyose.execution_http import authorize_execution
from kotoyose.print_cli_profiles import AGY_PROFILE
from kotoyose.routes import chat
from kotoyose.runtime.acp import AcpRuntimeSession, AcpPermissionPolicy
from kotoyose.runtime.base import RuntimeProviderSpec
from kotoyose.runtime.claude import ClaudeToolApprovals
from kotoyose.runtime.claude_options import claude_disallowed_tools
from tests.fixtures.api_keys import fixture_key_store

AUTH = {"Authorization": "Bearer kys_approval-test"}
EVENTS = {"X-Kotoyose-Events": "1"}
HEADERS = {**EVENTS, "X-Kotoyose-Approvals": "1"}
BODY = {"model": "demo", "messages": [{"role": "user", "content": "fixture"}], "stream": True}
PATH = "/v1/chat/completions"


@pytest.fixture(params=["claude", "cli_acp"])
async def harness(request, monkeypatch, tmp_path):
    provider = request.param
    decisions = []
    app = FastAPI()
    app.include_router(chat.router)
    app.state.chat_approvals = registry = ChatApprovals(timeout=0.5)
    store = await fixture_key_store(owner="approval-test", other="approval-other")
    with patch("kotoyose.api_key_store.secrets.token_urlsafe", return_value="second-key"):
        await store.create("rotated-test", "owner")
    app.state.api_key_store = store

    @app.post("/delegated", dependencies=[Depends(authorize_execution)])
    async def delegated(request: Request):
        return await chat.delegated_chat_turn(
            request, "demo", "fixture", "delegate:test:demo:1", {"delegated_by": "parent"},
        )

    models = {"demo": {"approval": "client", "tools": ["Read"], "deny": ["Bash"]}}
    if provider == "claude":
        models["demo"].update(permission_mode="default", tools=["Read", "Bash"], deny=["Bash(rm:*)"])
        cfg = config.HarnessConfig(models=models, clients={
            "owner": {"models": ["demo"]}, "other": {"models": ["demo"]},
        })

        class FakeClaude:
            def __init__(self, options):
                self.options = options

            connect = AsyncMock()
            disconnect = AsyncMock()
            query = AsyncMock()

            async def receive_response(self):
                # An automatic SDK allow reaches no callback.
                yield AssistantMessage([TextBlock("automatic ")], "fixture")
                if self.options.can_use_tool is None:
                    decisions.append(("server", None))
                else:
                    for name in ("Bash", "Read"):
                        tool_input = {"command": "rm body-only-marker"} if name == "Bash" else {}
                        yield AssistantMessage([ToolUseBlock(name, name, tool_input)], "fixture")
                        outcome = await self.options.can_use_tool(
                            name, tool_input,
                            ToolPermissionContext(tool_use_id=name, title=name),
                        )
                        decisions.append((outcome.behavior, getattr(outcome, "message", None)))
                yield AssistantMessage([TextBlock("finished")], "fixture")
                yield ResultMessage(
                    subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                    num_turns=1, session_id="fake-claude",
                )

        monkeypatch.setattr(session, "ClaudeSDKClient", FakeClaude)
        monkeypatch.setattr(session, "build_options", lambda *_, **__: ClaudeAgentOptions(
            tools=cfg.models["demo"].tools, disallowed_tools=claude_disallowed_tools(cfg.models["demo"].deny),
            permission_mode=cfg.models["demo"].permission_mode,
        ))
    else:
        models["demo"].update(route="local", place="host", allow={})
        cfg = config.HarnessConfig(
            models=models, clients={"owner": {"models": ["demo"]}, "other": {"models": ["demo"]}},
            connections={"local": {
                "provider": "antigravity", "kind": "subscription", "profile": "agy",
                "config_dir": str(tmp_path), "data_policy": "no_training",
            }},
            routes={"local": {"driver": "cli.acp", "profile": "agy", "connection": "local", "model": "agy-test"}},
        )

        class FakeAcp:
            async def prompt(self, text, *, timeout, update, permission):
                await update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "automatic "}})
                for name, kind in (("Bash", "execute"), ("Read", "read")):
                    outcome = await permission({
                        "toolCall": {"toolCallId": name, "title": name, "kind": kind},
                        "options": [
                            {"kind": "allow_once", "optionId": "yes"},
                            {"kind": "reject_once", "optionId": "no"},
                        ],
                    })
                    message = outcome.get("_meta", {}).get("kotoyose", {}).get("permission_denial")
                    decisions.append(("allow" if outcome["outcome"].get("optionId") == "yes" else "deny", message))
                await update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "finished"}})
                return {"stopReason": "end_turn"}

        async def run(self, prompt, turn):
            await turn.run(FakeAcp(), prompt, timeout=1)
            return self._result(turn)

        def build(preset, *_args, **_kwargs):
            return AcpRuntimeSession(
                AGY_PROFILE, tmp_path / "fake", tmp_path, tmp_path, "fake", 2, "", [],
                AcpPermissionPolicy("read", "host", tuple(preset.tools), tuple(preset.deny)), None,
            )

        monkeypatch.setattr(AcpRuntimeSession, "_run", run)
        monkeypatch.setitem(session.RUNTIME_PROVIDER_REGISTRY, "cli_acp", RuntimeProviderSpec(build, "cli_acp"))
    monkeypatch.setattr(config, "_config", cfg)
    for name in ("sessions", "session_last_used", "session_cache_identities"):
        monkeypatch.setattr(session, name, {})
    for name in ("lookup_session", "lookup_runtime_session"):
        monkeypatch.setattr(session, name, AsyncMock(return_value=None))
    for name in ("record_received", "record_completed", "record_failed", "_safe_upsert_session"):
        monkeypatch.setattr(chat, name, AsyncMock(return_value=None))
    for module in (chat, session):
        monkeypatch.setattr(module, "resolve_preset_secrets_with_versions_async", AsyncMock(return_value=({}, {})))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
        yield SimpleNamespace(provider=provider, app=app, cfg=cfg, http=http, registry=registry, decisions=decisions)


@asynccontextmanager
async def streaming(harness, headers=HEADERS):
    outbound = asyncio.Queue()
    inbound = asyncio.Queue()
    inbound.put_nowait({"type": "http.request", "body": json.dumps(BODY).encode()})
    scope = {
        "type": "http", "asgi": {"spec_version": "2.3"}, "method": "POST",
        "path": PATH, "scheme": "http", "query_string": b"",
        "headers": [(key.lower().encode(), val.encode()) for key, val in {
            **AUTH, **headers, "Content-Type": "application/json",
        }.items()],
    }
    task = asyncio.create_task(harness.app(scope, inbound.get, outbound.put))

    async def messages():
        first = await outbound.get()
        assert first["status"] == 200
        while (message := await outbound.get()).get("more_body"):
            payload = message["body"].decode().removeprefix("data: ").strip()
            if payload != "[DONE]":
                yield json.loads(payload)

    async with asyncio.timeout(3):
        try:
            yield messages()
        finally:
            inbound.put_nowait({"type": "http.disconnect"})
            await task


async def approval_event(events):
    async for event in events:
        if event.get("kind") == "permission_request":
            assert event["tool_call_id"] == "Read"
            assert event["tool_kind"] == "read"
            assert event["title"] == "Read"
            assert event["options"] == ["allow_once", "reject_once"]
            return event
    pytest.fail("missing permission request")


async def reply(harness, event, decision="allow_once", headers=AUTH):
    return await harness.http.post(
        f"/v1/chat/approvals/{event['request_id']}", headers=headers, json={"decision": decision},
    )


def test_alias_approval_defaults_to_server_and_rejects_other_values():
    assert config.ModelPreset().approval == "server"
    with pytest.raises(ValidationError):
        config.ModelPreset(approval="always")


@pytest.mark.parametrize("alias_mode", ["server", "client"])
@pytest.mark.parametrize("headers", [{}, EVENTS, {"X-Kotoyose-Approvals": "1"}, HEADERS])
async def test_alias_and_header_opt_in(harness, alias_mode, headers):
    harness.cfg.models["demo"].approval = alias_mode
    async with streaming(harness, headers) as events:
        if alias_mode == "client" and headers == HEADERS:
            event = await approval_event(events)
            assert (await reply(harness, event)).status_code == 204
        assert "permission_request" not in [event.get("kind") async for event in events]
    if alias_mode == "client" and headers != HEADERS:
        assert harness.decisions[-1] == ("deny", OPT_IN_REQUIRED)
    else:
        assert harness.decisions[-1] in {("server", None), ("allow", None)}
    assert not harness.registry.pending


@pytest.mark.parametrize("decision", ["allow_once", "reject_once", "timeout", "disconnect"])
async def test_permission_lifecycle(harness, decision):
    if decision == "timeout":
        harness.registry.timeout = 0.05
    async with streaming(harness) as events:
        event = await approval_event(events)
        assert harness.decisions[0][0] == "deny"
        if decision in {"allow_once", "reject_once"}:
            foreign = await reply(harness, event, decision, {"Authorization": "Bearer kys_approval-other"})
            unknown = await reply(harness, {"request_id": "unknown"}, decision)
            assert foreign.status_code == unknown.status_code == 404
            assert foreign.json() == unknown.json()
            accepted = await reply(harness, event, decision, {"Authorization": "Bearer kys_second-key"})
            assert accepted.status_code == 204
            assert (await reply(harness, event)).status_code == 404
        if decision != "disconnect":
            assert "permission_request" not in [item.get("kind") async for item in events]
            assert harness.decisions[-1][0] == ("allow" if decision == "allow_once" else "deny")
    assert not harness.registry.pending
    assert (await reply(harness, event)).status_code == 404
    if decision == "disconnect":
        assert ("allow", None) not in harness.decisions


async def test_synchronous_chat_fails_closed_and_delegated_turn_keeps_server_policy(harness):
    response = await harness.http.post(PATH, headers={**AUTH, **HEADERS}, json={**BODY, "stream": False})
    assert response.status_code == 200
    assert harness.decisions[-1] == ("deny", OPT_IN_REQUIRED)
    response = await harness.http.post("/delegated", headers={**AUTH, **HEADERS})
    assert response.status_code == 200
    assert harness.decisions[-1] in {("server", None), ("allow", None)}
    assert not harness.registry.pending


@pytest.mark.parametrize("harness", ["claude"], indirect=True)
async def test_claude_pattern_deny_never_asks(harness):
    async with streaming(harness) as events:
        event = await approval_event(events)
        assert (await reply(harness, event)).status_code == 204
        assert "permission_request" not in [item.get("kind") async for item in events]
    assert harness.decisions == [("deny", APPROVAL_REJECTED), ("allow", None)]


@pytest.mark.parametrize("harness", ["claude"], indirect=True)
async def test_claude_bypass_permissions_never_asks(harness):
    harness.cfg.models["demo"].permission_mode = "bypassPermissions"
    async with streaming(harness) as events:
        assert "permission_request" not in [item.get("kind") async for item in events]
    assert harness.decisions == [("server", None)]


@pytest.mark.parametrize("harness", ["claude"], indirect=True)
async def test_claude_approval_expires_within_alias_timeout(harness):
    harness.cfg.models["demo"].timeout_seconds = 0.3
    harness.registry.timeout = 300
    async with streaming(harness) as events:
        event = await approval_event(events)
        remaining = [item async for item in events]
    assert any(choice.get("delta", {}).get("content") == "finished"
               for item in remaining for choice in item.get("choices", []))
    assert harness.decisions[-1] == ("deny", APPROVAL_REJECTED)
    assert (await reply(harness, event)).status_code == 404
    assert not harness.registry.pending


@pytest.mark.parametrize(("tools", "name", "denied", "agent_id", "allowed"), [
    (["Read"], "Read", [], None, True),
    (["Read"], "Write", [], None, False),
    ([], "mcp__server__tool", [], None, True),
    ([], "mcp_server__tool", [], None, False),
    ([], "mcp__server__tool", ["mcp__server__tool"], None, False),
    (["Read"], "Read", ["Read"], None, False),
    (["Read"], "Read", [], "subagent", False),
    (None, "Write", [], None, True),
])
async def test_claude_decide_tool_policy(tools, name, denied, agent_id, allowed):
    registry = ChatApprovals()
    turn = TurnApprovals(registry, "client")
    turn.queue = asyncio.Queue()
    callback = ClaudeToolApprovals(ClaudeAgentOptions(tools=tools, disallowed_tools=denied))
    with bind_turn_approvals(turn):
        async with callback.bind():
            callback.remember(["tool"])
            async with asyncio.timeout(1):
                task = asyncio.create_task(callback.decide(name, {}, ToolPermissionContext(
                    tool_use_id="tool", agent_id=agent_id,
                )))
                if allowed:
                    event = await turn.queue.get()
                    assert registry.decide("client", event.request_id, "allow_once")
                assert (await task).behavior == ("allow" if allowed else "deny")
            assert turn.queue.empty()


async def test_claude_denies_late_callbacks_between_and_during_turns():
    registry = ChatApprovals()
    first, second = (TurnApprovals(registry, "client") for _ in range(2))
    first.queue, second.queue = asyncio.Queue(), asyncio.Queue()
    callback = ClaudeToolApprovals(ClaudeAgentOptions())
    previous = ToolPermissionContext(tool_use_id="previous")
    with bind_turn_approvals(first):
        async with callback.bind():
            callback.remember(["previous"])
        assert callback.turn is None
        assert (await callback.decide("Read", {}, previous)).behavior == "deny"
        assert first.queue.empty()
    with bind_turn_approvals(second):
        async with callback.bind():
            callback.remember(["current"])
            stale = asyncio.create_task(callback.decide("Read", {}, previous))
            await asyncio.sleep(0.05)
            # An id from another turn is never seen in this turn: it waits, then is denied when the turn ends.
            assert not stale.done() and second.queue.empty()
        async with asyncio.timeout(1):
            assert (await stale).behavior == "deny"
        assert second.queue.empty()
    assert not registry.pending


async def test_claude_callback_before_its_tool_use_message_waits_for_it():
    # The SDK can deliver the permission callback before the stream consumer reads the AssistantMessage.
    registry = ChatApprovals()
    turn = TurnApprovals(registry, "client")
    turn.queue = asyncio.Queue()
    callback = ClaudeToolApprovals(ClaudeAgentOptions())
    with bind_turn_approvals(turn):
        async with callback.bind():
            task = asyncio.create_task(callback.decide("Read", {}, ToolPermissionContext(tool_use_id="early")))
            await asyncio.sleep(0.05)
            assert not task.done() and turn.queue.empty()
            callback.remember(["early"])
            async with asyncio.timeout(1):
                event = await turn.queue.get()
                assert registry.decide("client", event.request_id, "allow_once")
                assert (await task).behavior == "allow"
    assert not registry.pending


@pytest.mark.parametrize("tool_id", [None, 1, [], {}])
async def test_turn_approvals_reject_non_string_tool_id(tool_id):
    registry = ChatApprovals()
    turn = TurnApprovals(registry, "client")
    turn.queue = asyncio.Queue()
    assert await turn.request(tool_id, "Read", "read") == APPROVAL_REJECTED
    assert turn.queue.empty()
    assert not registry.pending


async def test_pending_limit_is_shared_by_turns_of_one_client():
    registry = ChatApprovals(max_per_client=1)
    first, second = (TurnApprovals(registry, "client") for _ in range(2))
    first.queue, second.queue = asyncio.Queue(), asyncio.Queue()
    task = asyncio.create_task(first.request("one", "Read", "read"))
    await first.queue.get()
    assert await second.request("two", "Read", "read") is not None
    assert second.queue.empty()
    first.close()
    assert await task is not None
    assert not registry.pending
