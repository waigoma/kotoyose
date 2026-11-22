import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest
import yaml
from pydantic import ValidationError
from structlog.testing import capture_logs

import kotoyose.app as app_module
from kotoyose import chat_session, config, delegation, session
from kotoyose.routes import admin, chat
from kotoyose.runtime.base import RuntimeProviderSpec, RuntimeResult
from kotoyose.runtime.translation import translate_runtime_config
from tests.fixtures.api_keys import fixture_key_store

API_KEY = "kys_delegation-test"


class FakeRuntime:
    provider = "cursor"

    def __init__(self, model, servers, actions):
        self.model = model
        self.servers = servers
        self.actions = actions
        self.prompts = []
        self.disconnected = False

    async def ask(self, prompt):
        self.prompts.append(prompt)
        if prompt in self.actions:
            await self.actions[prompt](self)
        return RuntimeResult(f"{self.model}: {prompt}", usage={"input_tokens": 3, "output_tokens": 4})

    async def stream(self, prompt):
        yield await self.ask(prompt)

    async def disconnect(self):
        self.disconnected = True


class MCPClient:
    def __init__(self, http, token):
        self.http = http
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }
        self.counter = 0

    async def post(self, method, params=None, *, notification=False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notification:
            self.counter += 1
            body["id"] = self.counter
        return await self.http.post("/v1/mcp", json=body, headers=self.headers)

    async def initialize(self):
        reply = await self.post("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "tests", "version": "1"},
        })
        assert reply.status_code == 200, reply.text
        self.headers["mcp-session-id"] = reply.headers["mcp-session-id"]
        self.headers["mcp-protocol-version"] = reply.json()["result"]["protocolVersion"]
        reply = await self.post("notifications/initialized", notification=True)
        assert reply.status_code == 202

    async def call(self, tool, **arguments):
        reply = await self.post("tools/call", {"name": tool, "arguments": arguments})
        assert reply.status_code == 200, reply.text
        return reply.json()["result"]

    async def ask(self, alias="b", task="task", **kwargs):
        return await self.call("ask_agent", alias=alias, task=task, **kwargs)


@pytest.fixture
async def harness(monkeypatch):
    cfg = config.HarnessConfig(
        models={
            "a": {"provider": "cursor", "model": "parent", "delegates": ["b", "c"], "mcp_refs": ["kotoyose"]},
            "b": {"provider": "cursor", "model": "child", "description": "Reviews code",
                  "delegates": ["a"], "mcp_refs": ["kotoyose"], "timeout_seconds": 1},
            "c": {"provider": "cursor", "model": "out-of-client-scope"},
            "d": {"provider": "cursor", "model": "out-of-parent-scope"},
            "e": {"provider": "cursor", "model": "persona", "delegable": False},
        },
        clients={"test": {"models": ["a", "b", "d", "e"]}, "other": {"models": ["b"]}},
    )
    runtimes = []
    actions = {}

    def builder(preset, cwd, previous, headers):
        translated = translate_runtime_config(cfg, preset, headers)
        runtime = FakeRuntime(preset.model, translated.mcp_servers, actions)
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(config, "_config", cfg)
    monkeypatch.setitem(session.RUNTIME_PROVIDER_REGISTRY, "cursor", RuntimeProviderSpec(builder, "cursor"))
    for name in ("sessions", "session_last_used", "session_cache_identities"):
        cache = {}
        for module in (session, chat_session, app_module):
            monkeypatch.setattr(module, name, cache)
    monkeypatch.setattr(delegation, "_grants", {})
    monkeypatch.setattr(chat, "record_received", AsyncMock(return_value=None))
    monkeypatch.setattr(chat, "record_failed", AsyncMock())
    store = await fixture_key_store(test="delegation-test", other="delegation-other")
    app = app_module.app
    monkeypatch.setattr(app.state, "api_key_store", store)

    @asynccontextmanager
    async def fake_lifespan(app):
        yield

    monkeypatch.setattr(app_module, "lifespan", fake_lifespan)
    ready = asyncio.Future()
    shutdown = asyncio.Event()

    async def run_lifespan():
        async with app.router.lifespan_context(app):
            ready.set_result(None)
            await shutdown.wait()

    lifespan_task = asyncio.create_task(run_lifespan())
    try:
        done, _ = await asyncio.wait({ready, lifespan_task}, return_when=asyncio.FIRST_COMPLETED)
        if lifespan_task in done:
            lifespan_task.result()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as http:
            yield SimpleNamespace(http=http, runtimes=runtimes, actions=actions, cfg=cfg)
    finally:
        shutdown.set()
        await lifespan_task


async def _connect(harness, token):
    client = MCPClient(harness.http, token)
    await client.initialize()
    return client


async def _parent(harness):
    response = await harness.http.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "X-Conversation-Key": "parent-thread"},
        json={"model": "a", "messages": [{"role": "user", "content": "private parent history"}]},
    )
    assert response.status_code == 200, response.text
    parent = harness.runtimes[-1]
    settings = parent.servers["kotoyose"]
    return settings["headers"]["Authorization"].removeprefix("Bearer "), settings


async def test_list_scope_and_child_continuation(harness, monkeypatch):
    monkeypatch.setenv("KOTOYOSE_SELF_URL", "http://host.test:1234/")
    with capture_logs() as logs:
        token, server = await _parent(harness)
        assert server == {
            "type": "http", "url": "http://host.test:1234/v1/mcp",
            "headers": {"Authorization": f"Bearer {token}"},
        }
        assert token.startswith("kyd_")
        client = await _connect(harness, token)
        available = await client.call("list_agents")
        assert available["structuredContent"] == {"result": [
            {"alias": "b", "model": "child", "place": "host", "description": "Reviews code"},
        ]}
        for alias in ("a", "c", "d"):
            assert (await client.ask(alias=alias))["isError"]
        result = await client.ask(task="review the patch")
        assert not result["isError"], result
        first = result["structuredContent"]
        assert first["text"] == "child: review the patch"
        assert first["usage"]["prompt_tokens"] == 3
        second = (await client.ask(task="review the tests", conversation_id=first["conversation_id"]))["structuredContent"]
        assert second["conversation_id"] == first["conversation_id"]
        assert len(harness.runtimes) == 2
        child = harness.runtimes[-1]
        assert child.prompts == ["review the patch", "review the tests"]
        assert "kotoyose" not in child.servers
        assert chat.record_received.await_args.kwargs["metadata"] == {"delegated_by": "a", "depth": 1}
        key = chat.record_received.await_args.kwargs["conversation_key"]
        assert key == f"delegate:{delegation.authenticate(token).scope}:b:{first['conversation_id']}"
        bypass = await harness.http.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}", "X-Conversation-Key": key},
            json={"model": "b", "messages": [{"role": "user", "content": "bypass"}]},
        )
        assert bypass.status_code == 403
    assert token not in json.dumps(logs, default=str)


async def test_api_key_auth_and_recording(harness):
    unauthorized = MCPClient(harness.http, "kys_bad")
    assert (await unauthorized.post("initialize")).status_code == 401
    client = await _connect(harness, API_KEY)
    listed = (await client.call("list_agents"))["structuredContent"]["result"]
    assert [item["alias"] for item in listed] == ["a", "b", "d"]
    assert (await client.ask(alias="c"))["isError"]
    assert (await client.ask(alias="e"))["isError"]
    assert (await client.ask(alias="a"))["structuredContent"]["text"] == "parent: task"
    assert not harness.runtimes[-1].servers
    assert chat.record_received.await_args.kwargs["metadata"] == {"delegated_by": "client"}
    other = MCPClient(harness.http, "kys_delegation-other")
    other.headers["mcp-session-id"] = client.headers["mcp-session-id"]
    assert (await other.post("tools/list")).status_code == 404


async def test_mcp_sdk_client_round_trip(harness):
    harness.http.headers["Authorization"] = f"Bearer {API_KEY}"
    url = str(harness.http.base_url.join("/v1/mcp"))
    async with streamable_http_client(url, http_client=harness.http) as (reader, writer, _):
        async with ClientSession(reader, writer) as client:
            await client.initialize()
            assert {tool.name for tool in (await client.list_tools()).tools} == {"list_agents", "ask_agent"}
            result = await client.call_tool("ask_agent", {"alias": "b", "task": "hello"})
            assert result.structuredContent["text"] == "child: hello"


async def test_disconnect_cancels_child_turn(harness):
    token, _ = await _parent(harness)
    client = await _connect(harness, token)
    entered = asyncio.Event()
    disconnected = asyncio.Event()

    async def wait_until_cancelled(runtime):
        entered.set()
        await asyncio.Future()

    harness.actions["wait for disconnect"] = wait_until_cancelled
    body = json.dumps({
        "jsonrpc": "2.0", "method": "tools/call", "id": 42,
        "params": {"name": "ask_agent", "arguments": {"alias": "b", "task": "wait for disconnect"}},
    }).encode()
    app = app_module.app
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/v1/mcp", "query_string": b"",
        "headers": [(name.lower().encode(), value.encode()) for name, value in client.headers.items()]
        + [(b"content-type", b"application/json")],
    }
    body_sent = False

    async def receive():
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    pending = asyncio.create_task(app(scope, receive, send))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        disconnected.set()
        await asyncio.wait_for(pending, 2)
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
    assert harness.runtimes[-1].disconnected
    assert delegation.authenticate(token).active == 0


async def test_delegation_token_is_redacted_from_runtime_errors(harness):
    token, _ = await _parent(harness)

    async def fail_with_header(runtime):
        raise RuntimeError(f"Authorization: Bearer {token}")

    harness.actions["fail"] = fail_with_header
    client = await _connect(harness, token)
    with capture_logs() as logs:
        result = await client.ask(task="fail")
    assert result["isError"]
    assert token not in json.dumps(result)
    assert token not in json.dumps(logs, default=str)
    assert token not in repr(chat.record_failed.await_args)


async def test_disconnection_and_eviction_revoke_token(harness):
    token, _ = await _parent(harness)
    client = await _connect(harness, token)
    await session.sessions["parent-thread"].disconnect()
    assert harness.runtimes[0].disconnected
    assert (await client.post("tools/list")).status_code == 401

    new_token, _ = await _parent(harness)
    assert new_token != token
    await session.evict_expired_sessions(-1)
    assert harness.runtimes[-1].disconnected
    assert delegation.authenticate(new_token) is None


def test_no_grandchild_token_and_hourly_limit():
    with pytest.raises(ValueError, match="cannot issue"):
        delegation.issue("test", "b", "child", frozenset({"c"}), parent_depth=1, ttl=1800)
    token, grant = delegation.issue("test", "a", "parent", frozenset({"b"}), parent_depth=0, ttl=1800)
    try:
        for _ in range(32):
            with grant.reserve_call():
                assert grant.active == 1
        with pytest.raises(ValueError, match="limit"), grant.reserve_call():
            pytest.fail("The 33rd call must be rejected")
    finally:
        delegation.revoke(grant)
    assert delegation.authenticate(token) is None


async def test_concurrency_limit_and_mcp_cancel(harness):
    token, _ = await _parent(harness)
    client = await _connect(harness, token)
    entered = asyncio.Queue()
    finish = asyncio.Event()

    async def block(runtime):
        entered.put_nowait(runtime)
        await finish.wait()

    harness.actions["block"] = block
    pending = [asyncio.create_task(client.ask(task="block")) for _ in range(4)]
    runtimes = [await asyncio.wait_for(entered.get(), 2) for _ in range(4)]
    limited = await client.ask()
    assert limited["isError"]
    assert "call limit reached" in limited["content"][0]["text"]
    cancel = await client.post(
        "notifications/cancelled", {"requestId": client.counter - 4, "reason": "test"}, notification=True,
    )
    assert cancel.status_code == 202
    for _ in range(100):
        if any(runtime.disconnected for runtime in runtimes):
            break
        await asyncio.sleep(0.01)
    assert sum(runtime.disconnected for runtime in runtimes) == 1
    finish.set()
    await asyncio.gather(*pending, return_exceptions=True)
    assert delegation.authenticate(token).active == 0


@pytest.mark.parametrize(("raw", "error"), [
    ({"mcps": {"kotoyose": {"server": {"type": "http", "url": "http://localhost"}}}}, "reserved"),
    ({"mcp_definitions": {"kotoyose": {}}}, "reserved"),
    ({"models": {"a": {"mcp_refs": ["kotoyose"], "execution": "native"}}}, "direct host"),
    ({"models": {"a": {"mcp_refs": ["kotoyose"], "execution_environment": "isolated"}}}, "not supported for claude"),
    ({"models": {"a": {"delegates": ["missing"]}}}, "delegates"),
    ({"models": {"a": {"delegates": ["a"]}}}, "delegates"),
    ({"models": {"a": {"delegates": ["b"]}, "b": {"delegable": False}}}, "not delegable"),
])
def test_config_validation(raw, error):
    with pytest.raises(ValidationError, match=error):
        config.HarnessConfig.model_validate(raw)


def test_fields_round_trip(monkeypatch, tmp_path):
    path = tmp_path / "test-config.yaml"
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    cfg = config.HarnessConfig(models={
        "a": {"description": "Example alias", "delegates": ["b"], "mcp_refs": ["kotoyose"]}, "b": {},
    })
    config.save_config(cfg)
    loaded = config.HarnessConfig.model_validate(yaml.safe_load(path.read_text()))
    monkeypatch.setattr(config, "_config", loaded)
    assert loaded.models["a"].description == "Example alias"
    assert loaded.models["a"].delegates == ["b"]
    visible = admin._config_to_dict()["models"]["a"]
    assert visible["description"] == "Example alias"
    assert visible["delegates"] == ["b"]
