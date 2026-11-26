"""A Linux child alias delegates through the host's built-in MCP. No model calls."""

import asyncio
import json
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from claude_agent_sdk import AssistantMessage, TextBlock

from kotoyose import chat_session, child_launch, delegation, session
from kotoyose.app import app
from kotoyose.child_client import child_session, enroll_child
from kotoyose.child_mcp import MAX_BODY
from kotoyose.child_mcp_host import HostMcpTurn, delegation_publication, publication_catalog
from kotoyose.child_mcp_proxy import ChildMcpProxy
from kotoyose.child_protocol import StreamMux
from kotoyose.delegation import MAX_PER_HOUR, MCP_NAME, DelegationParent
from kotoyose.routes import chat
from kotoyose.runtime.base import RuntimeProviderSpec, RuntimeResult, RuntimeTurnError, prompt_text
from kotoyose.runtime.child import ChildRuntimeSession
from kotoyose.runtime.translation import translate_runtime_config
from tests.fixtures.api_keys import fixture_key_store
from tests.test_child_host_mcp import hub, until
from tests.test_child_k1 import _wait_child
from tests.test_child_k3 import _host_config, _policy
from tests.test_claude_terminal_contracts import result as claude_result

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="host MCP relay is currently enabled on Linux")
HOST_ONLY = "HOST_ONLY_CONFIG_DIR"


def config(tmp_path):
    raw = _host_config(tmp_path, mcp_refs=[f"host:{MCP_NAME}"])
    raw["models"]["dev"]["delegates"] = ["reader", "sibling", "hidden"]
    raw["models"]["reader"] = {
        "provider": "cursor", "model": "reader", "description": "Read the repository",
        "delegates": ["dev"], "mcp_refs": [MCP_NAME],
    }
    raw["models"]["sibling"] = {"route": "dev-route", "on": "child:dev", "description": "another child"}
    raw["models"]["hidden"] = {"provider": "cursor", "model": "hidden", "description": "outside this client"}
    raw["clients"] = {"owner": {"models": ["dev", "reader", "sibling"]}}
    from kotoyose.config import HarnessConfig

    return HarnessConfig.model_validate(raw)


def parent_for(cfg):
    return DelegationParent("owner", "owner-key", frozenset(cfg.models["dev"].delegates))


def approve(home, hub, cfg):
    offer = delegation_publication(hub.identity.secret, cfg)
    path = home / "host-mcp-approvals.json"
    path.write_text(json.dumps({"fingerprint": hub.host_fingerprint, "versions": {MCP_NAME: offer["version"]}}))
    path.chmod(0o600)
    return offer


def install_fakes(monkeypatch, cfg):
    runtimes = []
    actions = {}

    def builder(preset, cwd, previous, headers, **kwargs):
        translated = translate_runtime_config(cfg, preset, headers)
        runtime = FakeRuntime(preset.model, translated.mcp_servers, actions)
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr("kotoyose.config._config", cfg)
    monkeypatch.setitem(session.RUNTIME_PROVIDER_REGISTRY, "cursor", RuntimeProviderSpec(builder, "cursor"))
    for name in ("sessions", "session_last_used", "session_cache_identities"):
        cache = {}
        monkeypatch.setattr(session, name, cache)
        monkeypatch.setattr(chat_session, name, cache)
    monkeypatch.setattr(delegation, "_grants", {})
    monkeypatch.setattr(delegation, "_tasks", {})
    monkeypatch.setattr(delegation, "_parent_streams", {})
    monkeypatch.setattr(chat, "record_received", AsyncMock(return_value=None))
    monkeypatch.setattr(chat, "record_failed", AsyncMock())
    monkeypatch.setattr(chat, "record_completed", AsyncMock())
    return runtimes, actions


class FakeRuntime:
    provider = "cursor"

    def __init__(self, model, servers, actions):
        self.model = model
        self.servers = servers
        self.actions = actions
        self.prompts = []
        self.disconnected = False

    async def ask(self, prompt):
        text = prompt_text(prompt)
        self.prompts.append(text)
        action = self.actions.get(text)
        if action is None:
            action = next((callback for key, callback in self.actions.items() if key in text), None)
        if action is not None:
            await action(self)
        return RuntimeResult(f"{self.model}: {text}", usage={"input_tokens": 3, "output_tokens": 4})

    async def stream(self, prompt):
        result = await self.ask(prompt)
        yield result.text

    async def disconnect(self):
        self.disconnected = True


def claude_delegate(monkeypatch, tmp_path, invoke, configured):
    monkeypatch.setattr(child_launch, "_LAUNCH_RESOLVERS", {
        **child_launch._LAUNCH_RESOLVERS,
        next(key for key in child_launch._LAUNCH_RESOLVERS if key.value == "cli_path"): lambda _: tmp_path / "fake-claude",
    })

    class SDK:
        def __init__(self, *, options):
            server = options.mcp_servers[MCP_NAME]
            from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
            from kotoyose.child_mcp_proxy import AUTH_ENV

            argv = SubprocessCLITransport(prompt="", options=options)._build_command()
            token = options.env[AUTH_ENV]
            assert token not in "\n".join(argv)
            assert not token.startswith("kyd_")
            exposed = {**server, "headers": {"Authorization": f"Bearer {token}"}}
            assert "kyd_" not in json.dumps(exposed)
            configured.append(exposed)

        async def connect(self):
            pass

        async def query(self, prompt):
            self.answer = await invoke(configured[-1], prompt)

        async def receive_response(self):
            yield AssistantMessage(content=[TextBlock(text=self.answer)], model="fake-model")
            yield claude_result()

        async def disconnect(self):
            pass

    monkeypatch.setattr(session, "ClaudeSDKClient", SDK)


async def exchange(server, calls, *, initialize=True):
    async with httpx.AsyncClient(trust_env=False, timeout=None) as http:
        async def post(method, params, request_id=None):
            body = {"jsonrpc": "2.0", "method": method, "params": params}
            if request_id is not None:
                body["id"] = request_id
            response = await http.post(server["url"], headers=server["headers"], json=body)
            response.raise_for_status()
            if request_id is None:
                assert response.status_code == 202
                return None
            message = response.json()
            assert message["id"] == request_id
            return message

        if initialize:
            initialized = await post("initialize", {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "fake-cli", "version": "1"},
            }, 1)
            assert initialized["result"]["serverInfo"]["name"] == "Kotoyose delegation"
            await post("notifications/initialized", {})
            tools = await post("tools/list", {}, 2)
            assert [tool["name"] for tool in tools["result"]["tools"]] == ["list_agents", "ask_agent"]
        return [await post("tools/call", {"name": name, "arguments": arguments}, index)
                for index, (name, arguments) in enumerate(calls, start=3)]


def child_runtime(cfg, tmp_path):
    return ChildRuntimeSession(
        name="dev", provider="claude", alias="dev", config=cfg, preset=cfg.models["dev"],
        cwd=str(tmp_path), conversation_key="same-conversation",
        delegation_parent=parent_for(cfg), delegation_app=SimpleNamespace(state=SimpleNamespace()),
    )


def leaked(payload) -> bool:
    text = json.dumps(payload)
    return "kyd_" in text or HOST_ONLY in text or "/v1/mcp" in text


async def test_child_delegates_to_an_allowed_host_alias(hub, tmp_path, monkeypatch):
    cfg = config(tmp_path)
    runtimes, _ = install_fakes(monkeypatch, cfg)
    home = await enrolled(hub, tmp_path)
    approve(home, hub, cfg)
    configured = []

    async def invoke(server, prompt):
        replies = await exchange(server, [
            ("list_agents", {}),
            ("ask_agent", {"alias": "reader", "task": "summarize"}),
            ("ask_agent", {"alias": "hidden", "task": "no"}),
            ("ask_agent", {"alias": "sibling", "task": "loop"}),
            ("ask_agent", {"alias": "stranger", "task": "no"}),
        ])
        listed = replies[0]["result"]["structuredContent"]["result"]
        assert [item["alias"] for item in listed] == ["reader"]
        answer = replies[1]["result"]["structuredContent"]
        assert answer["text"] == "reader: summarize"
        for reply in replies[2:]:
            assert reply["result"]["isError"] is True
            assert "outside the delegation scope" in reply["result"]["content"][0]["text"]
        return answer["text"]

    claude_delegate(monkeypatch, tmp_path, invoke, configured)
    monkeypatch.setattr(app.state, "api_key_store", await fixture_key_store(owner="test-owner-key"), raising=False)
    wire = []
    async with child_session(home, heartbeat_interval=0.05, received_turns=wire):
        await _wait_child(hub, "dev", online=True)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            response = await asyncio.wait_for(http.post(
                "/v1/chat/completions", headers={"Authorization": "Bearer kys_test-owner-key"},
                json={"model": "dev", "cwd": str(tmp_path), "messages": [{"role": "user", "content": "summarize"}]},
            ), 8)
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "reader: summarize"
    assert not delegation._grants
    assert runtimes[-1].servers == {}
    assert configured[0]["url"].startswith("http://127.0.0.1:")
    assert "/mcp/" in configured[0]["url"]
    exposed = [wire, configured, publication_catalog(hub.identity.secret), runtimes[-1].servers]
    assert not leaked(exposed)
    async with httpx.AsyncClient(trust_env=False) as http:
        with pytest.raises(httpx.ConnectError):
            await http.post(configured[-1]["url"], headers=configured[-1]["headers"], json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
            })


async def test_usage_limit_resets_after_each_turn(hub, tmp_path, monkeypatch):
    cfg = config(tmp_path)
    cfg.models["dev"].timeout_seconds = 30
    runtimes, _ = install_fakes(monkeypatch, cfg)
    home = await enrolled(hub, tmp_path)
    approve(home, hub, cfg)
    grants = []
    conversation_id = None

    async def invoke(server, prompt):
        nonlocal conversation_id
        grant, = delegation._grants.values()
        grants.append(grant)
        assert 25 < grant.expires_at - time.monotonic() <= 30
        args = {"alias": "reader", "task": "again"}
        if conversation_id:
            args["conversation_id"] = conversation_id
        reply, = await exchange(server, [("ask_agent", args)])
        answer = reply["result"]["structuredContent"]
        if conversation_id is None:
            conversation_id = answer["conversation_id"]
            grant.calls.extend([time.monotonic()] * (MAX_PER_HOUR - len(grant.calls)))
            reply, = await exchange(server, [("ask_agent", args)], initialize=False)
            assert reply["result"]["isError"] is True
            return reply["result"]["content"][0]["text"]
        assert answer["conversation_id"] == conversation_id
        return answer["text"]

    claude_delegate(monkeypatch, tmp_path, invoke, [])
    async with child_session(home, heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        runtime = child_runtime(cfg, tmp_path)
        assert not delegation._grants
        try:
            result = await asyncio.wait_for(runtime.ask("again"), 8)
            assert result.text == "Delegation call limit reached"
            assert not delegation._grants and grants[0].revoked
            result = await asyncio.wait_for(runtime.ask("again"), 8)
            assert result.text == "reader: again"
            assert not delegation._grants and grants[1].revoked
        finally:
            await runtime.disconnect()
    assert grants[0] is not grants[1]
    delegate, = runtimes
    assert len(delegate.prompts) == 2


async def test_cached_child_turn_recomputes_delegates_and_client_models(hub, tmp_path, monkeypatch):
    cfg = config(tmp_path)
    cfg.models["dev"].delegates.remove("hidden")
    install_fakes(monkeypatch, cfg)
    home = await enrolled(hub, tmp_path)
    approve(home, hub, cfg)

    async def invoke(server, prompt):
        grant, = delegation._grants.values()
        reply, = await exchange(server, [("list_agents", {})])
        granted = ",".join(sorted(grant.allowed_aliases))
        listed = ",".join(sorted(agent["alias"] for agent in reply["result"]["structuredContent"]["result"]))
        return f"{granted} / {listed}"

    claude_delegate(monkeypatch, tmp_path, invoke, [])
    async with child_session(home, heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        runtime = child_runtime(cfg, tmp_path)
        try:
            first = await asyncio.wait_for(runtime.ask("before"), 8)
            assert first.text == "reader,sibling / reader"
            cfg.models["dev"].delegates.append("hidden")
            cfg.clients["owner"].models = ["dev", "hidden", "sibling"]
            offer = approve(home, hub, cfg)
            await until(lambda: hub.live_for("dev").mcp_approvals.get(MCP_NAME) == offer["version"])
            second = await asyncio.wait_for(runtime.ask("after"), 8)
            assert second.text == "hidden,sibling / hidden"
        finally:
            await runtime.disconnect()


async def test_parent_cancel_cancels_the_delegate(hub, tmp_path, monkeypatch):
    cfg = config(tmp_path)
    runtimes, actions = install_fakes(monkeypatch, cfg)
    started = asyncio.Event()

    async def block(runtime):
        started.set()
        await asyncio.Future()

    actions["block"] = block
    home = await enrolled(hub, tmp_path)
    approve(home, hub, cfg)

    async def invoke(server, prompt):
        await exchange(server, [("ask_agent", {"alias": "reader", "task": "block"})])
        return "should-be-cancelled"

    claude_delegate(monkeypatch, tmp_path, invoke, [])
    async with child_session(home, heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        runtime = child_runtime(cfg, tmp_path)
        task = asyncio.create_task(runtime.ask("block"))
        try:
            await asyncio.wait_for(started.wait(), 8)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await until(lambda: runtimes and runtimes[-1].disconnected)
            assert not delegation._grants
        finally:
            await runtime.disconnect()


async def test_completed_delegate_keeps_its_result_when_approval_changes(hub, tmp_path, monkeypatch):
    cfg = config(tmp_path)
    _, actions = install_fakes(monkeypatch, cfg)
    home = await enrolled(hub, tmp_path)
    approve(home, hub, cfg)

    async def change_approval(runtime):
        cfg.models["dev"].delegates.remove("reader")

    actions["flip"] = change_approval

    async def invoke(server, prompt):
        reply, refused = await exchange(server, [
            ("ask_agent", {"alias": "reader", "task": "flip"}),
            ("ask_agent", {"alias": "reader", "task": "repeat"}),
        ])
        assert "error" in refused
        return reply["result"]["structuredContent"]["text"]

    claude_delegate(monkeypatch, tmp_path, invoke, [])
    async with child_session(home, heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        runtime = child_runtime(cfg, tmp_path)
        result = await asyncio.wait_for(runtime.ask("flip"), 8)
    assert result.text == "reader: flip"


async def test_oversized_delegate_reply_is_a_tool_error(hub, tmp_path, monkeypatch):
    cfg = config(tmp_path)
    install_fakes(monkeypatch, cfg)
    home = await enrolled(hub, tmp_path)
    approve(home, hub, cfg)

    async def huge_result(runtime, prompt):
        return RuntimeResult("😀" * (MAX_BODY // 8))

    monkeypatch.setattr(FakeRuntime, "ask", huge_result)

    async def invoke(server, prompt):
        reply, next_reply = await exchange(server, [
            ("ask_agent", {"alias": "reader", "task": "large"}),
            ("list_agents", {}),
        ])
        assert reply["result"]["isError"] is True
        assert next_reply["result"]["structuredContent"]["result"][0]["alias"] == "reader"
        return reply["result"]["content"][0]["text"]

    claude_delegate(monkeypatch, tmp_path, invoke, [])
    async with child_session(home, heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        runtime = child_runtime(cfg, tmp_path)
        result = await asyncio.wait_for(runtime.ask("large"), 8)
    assert "response exceeds the MCP size limit. Do not retry." in result.text


@pytest.mark.parametrize("failure", [False, TimeoutError])
async def test_reply_failure_cancels_the_child_turn(hub, tmp_path, monkeypatch, failure):
    cfg = config(tmp_path)
    install_fakes(monkeypatch, cfg)
    home = await enrolled(hub, tmp_path)
    approve(home, hub, cfg)
    cancelled = asyncio.Event()
    send_frame = hub.send_frame

    async def send(name, stream, kind, payload, **kwargs):
        if kind == "mcp_response" and isinstance(payload["response"], dict):
            if "structuredContent" in payload["response"].get("result", {}):
                if failure is False:
                    return False
                raise failure
        return await send_frame(name, stream, kind, payload, **kwargs)

    async def invoke(server, prompt):
        try:
            await exchange(server, [("ask_agent", {"alias": "reader", "task": "run"})])
            pytest.fail("a dropped reply should cancel the turn")
        finally:
            cancelled.set()

    monkeypatch.setattr(hub, "send_frame", send)
    claude_delegate(monkeypatch, tmp_path, invoke, [])
    async with child_session(home, heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        runtime = child_runtime(cfg, tmp_path)
        with pytest.raises(RuntimeTurnError, match="MCP relay could not deliver") as exc:
            await asyncio.wait_for(runtime.ask("run"), 8)
        assert exc.value.retryable is False
        await asyncio.wait_for(cancelled.wait(), 2)
    assert not delegation._grants


async def test_proxy_bounds_delegation_wait_by_the_turn_deadline(tmp_path, monkeypatch):
    version = "a" * 64
    monkeypatch.setattr("kotoyose.child_mcp_proxy.approved_versions", lambda *_: {MCP_NAME: version})
    monkeypatch.setattr("kotoyose.child_mcp_proxy.DELEGATION_REPLY_MARGIN", 0.01)
    send = AsyncMock()
    proxy = ChildMcpProxy(tmp_path, "fingerprint", 1, send, {MCP_NAME: version}, "grant", turn_timeout=0.01)
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ask_agent"}}

    reply = await asyncio.wait_for(proxy._relay(MCP_NAME, request), 1)

    send.assert_awaited_once()
    assert reply["error"]["message"] == "MCP tool execution result is unknown"
    assert not proxy.pending


async def test_delegation_slot_cap_leaves_room_for_upstream_calls(monkeypatch):
    from kotoyose import child_mcp_host

    live = SimpleNamespace(name="dev", mux=StreamMux(), mcp_active=0)
    hub = SimpleNamespace(send_frame=AsyncMock(return_value=True))
    started = []

    async def block(self, call_id, name, version, request):
        started.append(name)
        await asyncio.Future()

    def accept(turn, call_id, name):
        turn.accept({
            "call": call_id, "name": name, "version": "version", "grant": turn.grant,
            "request": {"id": call_id, "method": "tools/call", "params": {"name": "ask_agent"}},
        })

    monkeypatch.setattr(HostMcpTurn, "_serve", block)
    monkeypatch.setattr(child_mcp_host, "_MAX_ACTIVE", 1)
    limit = child_mcp_host._MAX_DELEGATION_ACTIVE
    turns = [HostMcpTurn(hub, live, stream, "dev", {}, {}, 30) for stream in range(1, limit + 2)]
    extra = turns[-1]
    try:
        for turn in turns[:-1]:
            accept(turn, 1, MCP_NAME)
        accept(extra, 1, MCP_NAME)
        accept(extra, 2, "files")
        await until(lambda: len(started) == limit + 1 and hub.send_frame.await_count == 1)
        assert started.count(MCP_NAME) == limit
        assert started[-1] == "files"
        payload = hub.send_frame.await_args.args[3]
        assert payload["response"]["error"]["message"] == "MCP relay concurrency limit reached"

        await turns[0].close()
        accept(extra, 3, MCP_NAME)
        await until(lambda: len(started) == limit + 2)
        assert started[-1] == MCP_NAME
    finally:
        await asyncio.gather(*(turn.close() for turn in turns))
    assert live.mcp_active == child_mcp_host._active == child_mcp_host._delegation_active == 0


@pytest.mark.parametrize("condition", ["unapproved", "delegates-changed"])
async def test_delegation_requires_the_current_approval(hub, tmp_path, monkeypatch, condition):
    cfg = config(tmp_path)
    install_fakes(monkeypatch, cfg)
    home = await enrolled(hub, tmp_path)
    if condition == "delegates-changed":
        approve(home, hub, cfg)
    async with child_session(home, heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        if condition == "delegates-changed":
            cfg.models["dev"].delegates.append("reader")
        runtime = child_runtime(cfg, tmp_path)
        try:
            with pytest.raises(RuntimeTurnError, match="requires child owner approval"):
                await runtime.ask("do not start")
        finally:
            await runtime.disconnect()


def test_publication_names_delegates_and_changes_with_them(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    monkeypatch.setattr("kotoyose.config._config", cfg)
    secret = b"catalog-test"
    absent = config(tmp_path)
    absent.models["dev"].mcp_refs = ["files"]
    assert delegation_publication(secret, absent) is None
    offer = delegation_publication(secret, cfg)
    assert offer["description"] == "Kotoyose delegation"
    assert offer["tools"] == ["hidden", "reader", "sibling"]
    assert offer["all_tools"] is False
    catalog = publication_catalog(secret)
    assert catalog[-1]["name"] == MCP_NAME
    assert "kyd_" not in json.dumps(catalog)
    cfg.models["dev"].delegates.remove("hidden")
    changed = delegation_publication(secret, cfg)
    assert changed["version"] != offer["version"]
    assert changed["tools"] == ["reader", "sibling"]
    cfg.models["dev"].on = "child:other"
    moved = delegation_publication(secret, cfg)
    assert moved["version"] != changed["version"]
    assert moved["tools"] == changed["tools"]
    extra = [f"reader-{index}" for index in range(65)] + ["long-delegate-" * 50]
    cfg.models["dev"].delegates.extend(extra)
    full = delegation_publication(secret, cfg)
    assert full["tools"] == sorted([*extra, "reader", "sibling"])
    assert full["all_tools"] is False


async def enrolled(hub, tmp_path):
    home = tmp_path / "child"
    _policy(home, tmp_path, adapters=["claude.agent-sdk"])
    await enroll_child(hub.issue_enrollment_code(f"127.0.0.1:{hub.port}"), home, name="dev")
    return home
