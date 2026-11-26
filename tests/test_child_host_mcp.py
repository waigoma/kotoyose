"""Host publications, owner approvals, and actual HTTP traffic across the child link."""

import asyncio
import json
import os
import signal
import socket
import sys
import tomllib
from pathlib import Path
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import uvicorn
from claude_agent_sdk import AssistantMessage, TextBlock
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from openai_codex.generated.v2_all import TurnStatus
from pydantic import ValidationError

from kotoyose import child_codex, child_launch, session
from kotoyose.child_client import child_session, enroll_child
from kotoyose.child_mcp import approved_versions
from kotoyose.child_mcp_host import publication_catalog, publication_version
from kotoyose.child_server import ChildHub
from kotoyose.config import HarnessConfig, McpDefinition, ModelPreset
from kotoyose.runtime import codex
from kotoyose.runtime.base import RuntimeTurnError
from kotoyose.runtime.child import ChildRuntimeSession
from tests.fixtures.child_runner_stub import exercise_http_mcp
from tests.test_child_k1 import _wait_child
from tests.test_child_k3 import _host_config, _policy
from tests.test_child_k8 import _sdk_with_config
from tests.test_claude_terminal_contracts import result as claude_result
from tests.test_codex_runtime import _FakeHandle, _FakeThread, _turn_completed

HOST_SECRET = "host-only-credential-7a3f"
CALLER_SECRET = "host-request-credential-2a5f"
SESSION_ID = "upstream-session-not-for-child"
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="host HTTP MCP relay is currently enabled on Linux")


async def until(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(0.01)


@asynccontextmanager
async def fake_http_mcp():
    app = FastAPI()
    observed = SimpleNamespace(
        requests=[], calls=[], deletes=[], max_active=0, started=asyncio.Event(), release=asyncio.Event(),
        cancelled=asyncio.Event(),
    )
    active = 0

    @app.delete("/mcp")
    async def delete_session(request: Request):
        observed.deletes.append(request.headers.get("mcp-session-id"))
        return Response(status_code=204)

    @app.post("/mcp")
    async def request_handler(request: Request):
        nonlocal active
        raw = await request.json()
        observed.requests.append((dict(request.headers), raw))
        method = raw["method"]
        if method == "initialize":
            result = {"protocolVersion": "2025-11-25", "serverInfo": {"name": "fixture", "version": "1"}, "capabilities": {"tools": {}}}
            return JSONResponse({"jsonrpc": "2.0", "id": raw["id"], "result": result}, headers={"Mcp-Session-Id": SESSION_ID})
        assert request.headers["Mcp-Session-Id"] == SESSION_ID
        assert request.headers["MCP-Protocol-Version"] == "2025-11-25"
        if method == "notifications/initialized":
            return Response(status_code=202)
        if method == "tools/list":
            result = {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in ("echo", "private")]}
        else:
            value = raw["params"]["arguments"]["value"]
            observed.calls.append(value)
            active += 1
            observed.max_active = max(observed.max_active, active)
            try:
                if value == "block":
                    observed.started.set()
                    while not observed.release.is_set():
                        if await request.is_disconnected():
                            observed.cancelled.set()
                            return Response(status_code=499)
                        await asyncio.sleep(0.01)
                elif value == "echo-secret":
                    value = request.headers["Authorization"]
                else:
                    await asyncio.sleep(0.05)
                result = {"content": [{"type": "text", "text": value}], "structuredContent": {"value": value}, "unrecognized": True}
            finally:
                active -= 1
        message = json.dumps({"jsonrpc": "2.0", "id": raw["id"], "result": result})
        return Response(f": keepalive\r\n\r\ndata: {message}\r\n\r\n", media_type="text/event-stream")

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            await until(lambda: server.started)
            observed.url = f"http://127.0.0.1:{port}/mcp"
            yield observed
        finally:
            observed.release.set()
            server.should_exit = True
            await task


def relay_config(tmp_path, endpoint, *, provider="claude", publish=True, refs=None, request_headers=True):
    config = _host_config(tmp_path, mcp_refs=["host:remote"] if refs is None else refs)
    config["mcps"]["remote"] = {
        "description": "Fixture tool",
        "publish_to_children": publish,
        "server": {
            "type": "http", "url": endpoint, "headers": {"Authorization": {"secret": "HOST_MCP_FIXTURE"}},
            "enabled_tools": ["echo"],
        },
    }
    if request_headers:
        config["models"]["dev"]["required_request_headers"] = ["X-Client-Proof"]
        config["mcps"]["remote"]["server"]["headers"]["X-Client-Proof"] = "{{header:X-Client-Proof}}"
    if provider == "codex":
        config["connections"]["dev-conn"].update(provider="codex")
        config["connections"]["dev-conn"].pop("config_dir")
        config["routes"]["dev-route"].update(driver="codex.app-server", model="gpt-5")
    return HarnessConfig.model_validate(config)


def approve(home, fingerprint, definitions, hub):
    versions = {name: publication_version(hub.identity.secret, name, definition) for name, definition in definitions.items()}
    path = home / "host-mcp-approvals.json"
    path.write_text(json.dumps({"fingerprint": fingerprint, "versions": versions}))
    path.chmod(0o600)
    return versions


@pytest.fixture
async def hub(tmp_path):
    value = ChildHub(tmp_path / "host", heartbeat_interval=0.08)
    await value.start("127.0.0.1", 0)
    try:
        yield value
    finally:
        await value.stop()


async def enrolled(hub, tmp_path, provider):
    home = tmp_path / "child"
    driver = "claude.agent-sdk" if provider == "claude" else "codex.app-server"
    _policy(home, tmp_path, adapters=[driver])
    await enroll_child(hub.issue_enrollment_code(f"127.0.0.1:{hub.port}"), home, name="dev")
    return home


def turn(cfg, tmp_path, provider):
    return ChildRuntimeSession(
        name="dev", provider=provider, alias="dev", config=cfg, preset=cfg.models["dev"],
        cwd=str(tmp_path), conversation_key="same-conversation", request_headers={"X-Client-Proof": CALLER_SECRET},
    )


def stub_secret(monkeypatch):
    get = AsyncMock(return_value=HOST_SECRET)
    monkeypatch.setattr("kotoyose.config.get_secret_store", lambda: SimpleNamespace(get=get))
    return get


def fake_sdk(monkeypatch, provider, tmp_path, invoke):
    configured = []
    resumed = []
    if provider == "claude":
        monkeypatch.setattr(child_launch, "_LAUNCH_RESOLVERS", {
            **child_launch._LAUNCH_RESOLVERS,
            next(key for key in child_launch._LAUNCH_RESOLVERS if key.value == "cli_path"): lambda _: tmp_path / "fake-claude",
        })

        class SDK:
            def __init__(self, *, options):
                assert options.strict_mcp_config
                server = options.mcp_servers["remote"]
                from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
                from kotoyose.child_mcp_proxy import AUTH_ENV

                argv = SubprocessCLITransport(prompt="", options=options)._build_command()
                token = options.env[AUTH_ENV]
                assert token not in "\n".join(argv)
                assert token not in json.dumps(server)
                configured.append({**server, "headers": {"Authorization": f"Bearer {token}"}})
                resumed.append(options.resume)
                self.server = configured[-1]

            async def connect(self):
                pass

            async def query(self, prompt):
                self.answer = await invoke(self.server, prompt)

            async def receive_response(self):
                yield AssistantMessage(content=[TextBlock(text=self.answer)], model="fake-model")
                yield claude_result()

            async def disconnect(self):
                pass

        monkeypatch.setattr(session, "ClaudeSDKClient", SDK)
    else:
        monkeypatch.setattr(child_codex, "_bundled_binary", lambda: "/fake/codex")

        class Thread(_FakeThread):
            async def turn(self, prompt, **kwargs):
                await invoke(configured[-1], prompt)
                return await super().turn(prompt, **kwargs)

        def sdk_factory(sdk_config):
            from kotoyose.child_mcp_proxy import AUTH_ENV

            override = next(arg for arg in sdk_config.config_overrides if arg.startswith("mcp_servers="))
            argv = [*(sdk_config.launch_args_override or ()), *sdk_config.config_overrides]
            table = tomllib.loads(override)["mcp_servers"]
            assert table["remote"]["bearer_token_env_var"] == AUTH_ENV
            token = sdk_config.env[AUTH_ENV]
            assert token not in "\n".join(argv)
            configured.append({"url": table["remote"]["url"], "headers": {"Authorization": f"Bearer {token}"}})
            sdk = _sdk_with_config({"mcp_servers": table})
            thread = Thread(handle=_FakeHandle([_turn_completed(TurnStatus.completed)]))
            sdk.thread_start.return_value = thread

            async def resume(thread_id, **kwargs):
                resumed.append(thread_id)
                return thread

            sdk.thread_resume = resume
            return sdk

        monkeypatch.setattr(codex, "AsyncCodex", sdk_factory)
    return configured, resumed


@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_provider_turn_relays_http_without_credentials(hub, tmp_path, monkeypatch, provider):
    observed = []

    async def invoke(server, prompt):
        async with httpx.AsyncClient(trust_env=False) as http:
            assert (await http.post(server["url"], json={})).status_code == 403
            assert (await http.get(server["url"], headers=server["headers"])).status_code == 405
        answers = await exercise_http_mcp(server, "first", "second", "echo-secret")
        observed.extend(answers)
        return answers[0]["result"]["content"][0]["text"]

    get_secret = stub_secret(monkeypatch)
    configured, resumed = fake_sdk(monkeypatch, provider, tmp_path, invoke)
    async with fake_http_mcp() as upstream:
        cfg = relay_config(tmp_path, upstream.url, provider=provider)
        monkeypatch.setattr("kotoyose.config._config", cfg)
        home = await enrolled(hub, tmp_path, provider)
        approve(home, hub.host_fingerprint, cfg.mcps, hub)
        wire_turns = []
        async with child_session(home, heartbeat_interval=0.05, received_turns=wire_turns):
            await _wait_child(hub, "dev", online=True)
            runtime = turn(cfg, tmp_path, provider)
            for _ in range(2):
                assert (await asyncio.wait_for(runtime.ask("use MCP"), 5)).provider == provider
        assert observed[0]["result"] == {
            "content": [{"type": "text", "text": "first"}], "structuredContent": {"value": "first"}, "unrecognized": True,
        }
        assert "unknown" in observed[2]["error"]["message"]
        assert upstream.max_active >= 2
        assert upstream.calls.count("echo-secret") == 2
        assert get_secret.await_count == len(upstream.requests)
        assert all(headers["authorization"] == HOST_SECRET and headers["x-client-proof"] == CALLER_SECRET for headers, _ in upstream.requests)
        assert configured[0]["headers"] != configured[1]["headers"]
        assert upstream.deletes == [SESSION_ID, SESSION_ID]
        assert resumed[-1] == ("result-session" if provider == "claude" else "thread_abc")
        async with httpx.AsyncClient(trust_env=False) as http:
            with pytest.raises(httpx.ConnectError):
                await http.post(configured[-1]["url"], headers=configured[-1]["headers"], json={})
        exposed = json.dumps([wire_turns, configured, observed, publication_catalog(hub.identity.secret)])
        for material in (HOST_SECRET, CALLER_SECRET, SESSION_ID, upstream.url, "HOST_MCP_FIXTURE"):
            assert material not in exposed


@pytest.mark.parametrize(("condition", "reason"), [
    ("unapproved", "requires child owner approval"),
    ("unpublished", "is not published"),
    ("changed", "requires child owner approval"),
    ("host-changed", "requires child owner approval"),
])
async def test_refusal_before_child_turn(hub, tmp_path, monkeypatch, condition, reason):
    async with fake_http_mcp() as upstream:
        cfg = relay_config(tmp_path, upstream.url, publish=condition != "unpublished")
        home = await enrolled(hub, tmp_path, "claude")
        if condition != "unapproved":
            fingerprint = "0" * 64 if condition == "host-changed" else hub.host_fingerprint
            approve(home, fingerprint, cfg.mcps, hub)
        if condition == "changed":
            cfg.mcps["remote"].publication_revision = "new-scope"
        monkeypatch.setattr("kotoyose.config._config", cfg)
        received = []
        async with child_session(home, received_turns=received):
            await _wait_child(hub, "dev", online=True)
            with pytest.raises(RuntimeTurnError, match=reason):
                await turn(cfg, tmp_path, "claude").ask("do not start")
            assert received == upstream.requests == []


@pytest.mark.parametrize("action", ["cancel", "link-drop", "config-change"])
async def test_host_cancels_or_rechecks_in_flight_calls(hub, tmp_path, monkeypatch, action):
    child_errors = []

    async def invoke(server, prompt):
        reply, = await exercise_http_mcp(server, "block")
        child_errors.append(reply["error"]["message"])
        return "handled"

    stub_secret(monkeypatch)
    fake_sdk(monkeypatch, "claude", tmp_path, invoke)
    async with fake_http_mcp() as upstream:
        cfg = relay_config(tmp_path, upstream.url)
        monkeypatch.setattr("kotoyose.config._config", cfg)
        home = await enrolled(hub, tmp_path, "claude")
        approve(home, hub.host_fingerprint, cfg.mcps, hub)
        async with child_session(home, heartbeat_interval=0.05):
            await _wait_child(hub, "dev", online=True)
            runtime = turn(cfg, tmp_path, "claude")
            task = asyncio.create_task(runtime.ask("possibly state-changing"))
            await asyncio.wait_for(upstream.started.wait(), 4)
            if action == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await asyncio.wait_for(upstream.cancelled.wait(), 4)
            elif action == "link-drop":
                await hub.disconnect("dev")
                with pytest.raises(RuntimeTurnError, match="result is unknown"):
                    await asyncio.wait_for(task, 4)
                await asyncio.wait_for(upstream.cancelled.wait(), 4)
                await until(lambda: child_errors)
                assert "unknown" in child_errors[0]
            else:
                cfg.models["dev"].mcp_refs = []
                upstream.release.set()
                await asyncio.wait_for(task, 4)
                assert child_errors == ["MCP tool execution result is unknown"]
            assert upstream.calls == ["block"]


def test_explicit_refs_and_publication_validation(tmp_path):
    for refs in (["files", "local:files"], ["host:files", "local:files"], ["host:"], ["else:files"], ["local:kotoyose"]):
        with pytest.raises(ValidationError, match="mcp_refs"):
            ModelPreset(mcp_refs=refs)
    assert ModelPreset(mcp_refs=["host:kotoyose"], on="child:dev").has_delegation_mcp is True
    with pytest.raises(ValidationError, match="kotoyose is not available"):
        ModelPreset(mcp_refs=["kotoyose"], on="child:dev")
    published = McpDefinition(publish_to_children=True, server={"command": "fixture", "args": ["--serve"]})
    assert published.publish_to_children is True
    with pytest.raises(ValidationError, match="Streamable HTTP"):
        McpDefinition(publish_to_children=True, server={"type": "sse", "url": "http://127.0.0.1/sse"})
    with pytest.raises(ValidationError, match="only valid on a child alias"):
        HarnessConfig.model_validate(_host_config(tmp_path, on="local", mcp_refs=["local:files"]))
    with pytest.raises(ValidationError, match="only valid on a child alias"):
        HarnessConfig.model_validate(_host_config(tmp_path, on="local", mcp_refs=["host:files"]))
    host = HarnessConfig.model_validate(_host_config(tmp_path, on="local", mcp_refs=["files"]))
    assert list(host.resolve_resources(host.models["dev"])[2]) == ["files"]
    local_only = _host_config(tmp_path, mcp_refs=["local:missing-from-host"])
    HarnessConfig.model_validate(local_only)
    local_only["models"]["dev"]["mcp_refs"] = ["host:missing-from-host"]
    with pytest.raises(ValidationError, match="missing-from-host"):
        HarnessConfig.model_validate(local_only)
    assert approved_versions(tmp_path, "0" * 64) == {}


async def test_published_but_unreferenced_and_wrong_turn_grant_are_refused(hub, tmp_path, monkeypatch):
    ready = asyncio.Event()
    stop = asyncio.Event()

    async def invoke(server, prompt):
        ready.set()
        await stop.wait()
        return "done"

    stub_secret(monkeypatch)
    fake_sdk(monkeypatch, "claude", tmp_path, invoke)
    async with fake_http_mcp() as upstream:
        cfg = relay_config(tmp_path, upstream.url)
        cfg.mcps["other"] = cfg.mcps["remote"].model_copy(deep=True)
        monkeypatch.setattr("kotoyose.config._config", cfg)
        home = await enrolled(hub, tmp_path, "claude")
        versions = approve(home, hub.host_fingerprint, cfg.mcps, hub)
        received = []
        async with child_session(home, heartbeat_interval=0.05, received_turns=received) as child:
            await _wait_child(hub, "dev", online=True)
            task = asyncio.create_task(turn(cfg, tmp_path, "claude").ask("test"))
            await asyncio.wait_for(ready.wait(), 4)
            active = hub.live_for("dev").mux.mcp_turns
            stream, relay = next(iter(active.items()))
            request = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
            body = {"grant": received[0]["host_mcp_grant"], "call": 100, "name": "other",
                    "version": versions["other"], "request": request}
            try:
                await child.send_frame(stream, "mcp_request", body)
                await child.send_frame(stream, "mcp_request", {
                    **body, "grant": "0" * 64, "call": 101, "name": "remote", "version": versions["remote"],
                })
                await until(lambda: 100 in relay.seen and not relay.tasks)
                assert 101 not in relay.seen
                assert upstream.requests == []
            finally:
                stop.set()
                await asyncio.wait_for(task, 4)


def test_codex_child_http_effective_config_guard():
    from kotoyose.runtime.codex_child import _matches_mcp_policy, _mcp_config

    servers = {"relay": {"type": "http", "url": "http://127.0.0.1:49821/mcp/0", "headers": {"Authorization": "Bearer test"}}}
    effective, env = _mcp_config(servers)
    assert env == {} and _matches_mcp_policy(effective, effective)
    for field, value in (
        ("url", "https://example.invalid/mcp"),
        ("http_headers", {"Authorization": "Bearer wrong"}),
        ("bearer_token_env_var", "UNAPPROVED"),
        ("env_http_headers", {"Authorization": "UNAPPROVED"}),
        ("command", "local"),
        ("enabled", False),
    ):
        assert not _matches_mcp_policy({"relay": {**effective["relay"], field: value}}, effective)


def test_loopback_bearer_stays_off_the_codex_command_line():
    from kotoyose.child_mcp_proxy import AUTH_ENV, authorization_header, pop_relay_env, push_relay_env
    from kotoyose.runtime.codex_child import _matches_mcp_policy, _mcp_config

    raw = "loopback-token-value"
    token = push_relay_env({AUTH_ENV: raw})
    try:
        config, env = _mcp_config({
            "relay": {"type": "http", "url": "http://127.0.0.1:9/mcp/0", "headers": {"Authorization": authorization_header()}},
        })
    finally:
        pop_relay_env(token)
    assert raw not in json.dumps(config)
    assert config["relay"]["bearer_token_env_var"] == AUTH_ENV
    assert "http_headers" not in config["relay"]
    assert env[AUTH_ENV] == raw
    assert _matches_mcp_policy(config, config)
    changed = {"relay": {**config["relay"], "bearer_token_env_var": "OTHER"}}
    extra = {"relay": {**config["relay"], "env_http_headers": {"Authorization": "OTHER"}}}
    assert not _matches_mcp_policy(changed, config)
    assert not _matches_mcp_policy(extra, config)


def test_acp_expands_the_loopback_header_in_the_session_message():
    from kotoyose.acp_turn import acp_mcp_servers
    from kotoyose.child_mcp_proxy import AUTH_ENV, authorization_header, pop_relay_env, push_relay_env

    raw = "loopback-token-value"
    token = push_relay_env({AUTH_ENV: raw})
    try:
        servers = acp_mcp_servers({
            "remote": {"type": "http", "url": "http://127.0.0.1:9/mcp/0", "headers": {"Authorization": authorization_header()}},
        })
    finally:
        pop_relay_env(token)
    assert servers[0]["headers"] == [{"name": "Authorization", "value": f"Bearer {raw}"}]


def test_one_bad_approval_does_not_drop_the_rest():
    from kotoyose.child_mcp import versions

    good = "ab" * 32
    assert versions({"ok": good, "n" * 129: good, "bad": "zz"}) == {"ok": good}
    assert versions({f"n{index}": good for index in range(65)}) == {}


def test_catalog_says_all_tools_when_enabled_tools_is_unset(tmp_path, monkeypatch):
    cfg = relay_config(tmp_path, "http://127.0.0.1:9/mcp")
    monkeypatch.setattr("kotoyose.child_mcp_host.get_config", lambda: cfg)
    assert publication_catalog(b"k" * 32)[0]["all_tools"] is False
    assert publication_catalog(b"k" * 32)[0]["tools"] == ["echo"]
    cfg.mcps["remote"].server.pop("enabled_tools")
    row = publication_catalog(b"k" * 32)[0]
    assert row["all_tools"] is True and row["tools"] == []


async def test_encoded_credentials_are_rejected_after_tool_filtering():
    import base64
    from urllib.parse import quote

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    secret = "host/only credential"
    short = "tinykey"
    assert quote(secret, safe="") != secret
    app = FastAPI()

    @app.post("/mcp")
    async def handle(request: Request):
        body = await request.json()
        if body["method"] == "notifications/initialized":
            return Response(status_code=202)
        if body["method"] == "initialize":
            result = {"protocolVersion": "2025-11-25"}
        elif body["id"] == 2:
            result = {"tools": [{"name": "hidden", "description": secret}, {"name": "echo", "description": "safe"}]}
        elif body["id"] == 3:
            result = {"content": [{"type": "text", "text": quote(secret, safe="")}]}
        elif body["id"] == 4:
            result = {"content": [{"type": "text", "text": base64.b64encode(secret.encode()).decode()}]}
        else:
            result = {"content": [{"type": "text", "text": short}]}
        return JSONResponse(
            {"jsonrpc": "2.0", "id": body["id"], "result": result},
            headers={"Mcp-Session-Id": "session-id-0123456789"},
        )

    from kotoyose.child_mcp_host import _UpstreamSession

    session = _UpstreamSession()
    server = {"url": "http://upstream.test/mcp", "headers": {"Authorization": f"Bearer {secret}"}}

    def allowed(name):
        return name == "echo"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False) as client:
        await session.call(server, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, client, allowed, ())
        await session.call(server, {"jsonrpc": "2.0", "method": "notifications/initialized"}, client, allowed, ())
        listed = await session.call(server, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, client, allowed, ())
        assert [tool["name"] for tool in listed["result"]["tools"]] == ["echo"]
        for request_id in (3, 4):
            with pytest.raises(ValueError, match="credential"):
                await session.call(
                    server, {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": "echo"}},
                    client, allowed, (),
                )

    fresh = _UpstreamSession()
    short_server = {"url": "http://upstream.test/mcp", "headers": {"X-Test": short}}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), trust_env=False) as client:
        await fresh.call(short_server, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, client, allowed, ())
        await fresh.call(short_server, {"jsonrpc": "2.0", "method": "notifications/initialized"}, client, allowed, ())
        kept = await fresh.call(
            short_server, {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "echo"}}, client, allowed, (),
        )
    assert kept["result"]["content"][0]["text"] == short


@pytest.mark.parametrize("exit_kind", ["error", "timeout", "closed", "cancel"])
async def test_dispatched_tool_call_exits_are_not_retryable(monkeypatch, tmp_path, exit_kind):
    from kotoyose.child_protocol import Frame
    from kotoyose.runtime import child as child_runtime

    frames = asyncio.Queue()
    mux = SimpleNamespace(mcp_turns={}, queue=lambda _stream: frames, discard=lambda _stream: None)

    async def send_frame(_name, stream, kind, _payload, link=None):
        if kind == "turn" and exit_kind != "before-tool":
            mux.mcp_turns[stream].sent_tool_call = True
        if exit_kind == "error":
            frames.put_nowait(Frame(stream, "error", {"message": "transient", "retryable": True}))
        elif exit_kind == "closed":
            frames.put_nowait(Frame(stream, "closed", {}))
        return True

    hub = SimpleNamespace(
        allocate_stream=lambda: 7, send_frame=send_frame, identity=SimpleNamespace(secret=b"secret"),
    )
    live = SimpleNamespace(name="dev", features=(), mux=mux)
    cfg = relay_config(tmp_path, "http://127.0.0.1:9/mcp")
    if exit_kind == "timeout":
        cfg.models["dev"].timeout_seconds = 0.2
    monkeypatch.setattr(child_runtime, "require_child_link", lambda *_args: (hub, live))
    monkeypatch.setattr(child_runtime, "get_config", lambda: cfg)
    monkeypatch.setattr(child_runtime, "select_publications", lambda *_args: {"remote": "ab" * 32})
    runtime = ChildRuntimeSession(
        name="dev", provider="claude", alias="dev", config=cfg, preset=cfg.models["dev"],
        cwd=str(tmp_path), conversation_key="same",
    )
    if exit_kind == "cancel":
        task = asyncio.create_task(runtime.ask("use the tool"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return
    with pytest.raises(RuntimeTurnError) as caught:
        await runtime.ask("use the tool")
    assert caught.value.retryable is False


async def test_child_retryable_flag_survives_when_no_tool_was_dispatched(monkeypatch, tmp_path):
    from kotoyose.child_protocol import Frame
    from kotoyose.runtime import child as child_runtime

    frames = asyncio.Queue()
    mux = SimpleNamespace(mcp_turns={}, queue=lambda _stream: frames, discard=lambda _stream: None)

    async def send_frame(_name, stream, kind, _payload, link=None):
        if kind == "turn":
            frames.put_nowait(Frame(stream, "error", {"message": "transient", "retryable": True}))
        return True

    hub = SimpleNamespace(allocate_stream=lambda: 7, send_frame=send_frame, identity=SimpleNamespace(secret=b"secret"))
    cfg = relay_config(tmp_path, "http://127.0.0.1:9/mcp")
    monkeypatch.setattr(child_runtime, "require_child_link", lambda *_args: (hub, SimpleNamespace(name="dev", features=(), mux=mux)))
    monkeypatch.setattr(child_runtime, "get_config", lambda: cfg)
    monkeypatch.setattr(child_runtime, "select_publications", lambda *_args: {"remote": "ab" * 32})
    runtime = ChildRuntimeSession(
        name="dev", provider="claude", alias="dev", config=cfg, preset=cfg.models["dev"],
        cwd=str(tmp_path), conversation_key="same",
    )
    with pytest.raises(RuntimeTurnError) as caught:
        await runtime.ask("no tool yet")
    assert caught.value.retryable is True

SCRIPT = Path(__file__).resolve().parent / "fixtures" / "mcp_stdio_relay_server.py"
HOST_ROLE = "host-role-9f3c"
CHILD_ROLE = "child-role-4b21"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _stdio_command(directory, name: str):
    path = directory / name
    if not path.exists():
        path.symlink_to(sys.executable)
    return path


def _host_stdio_definition(tmp_path, *, tools=("write", "leak"), secret=True, **env):
    command = _stdio_command(tmp_path, "host-only-stdio")
    values = {"MCP_STDIO_ROLE": HOST_ROLE, "MCP_STDIO_MARKER": HOST_ROLE, **env}
    if secret:
        values["MCP_STDIO_SECRET"] = {"secret": "HOST_MCP_FIXTURE"}
    return {
        "description": "Host files",
        "publish_to_children": True,
        "server": {
            "type": "stdio",
            "command": str(command),
            "args": ["-u", str(SCRIPT)],
            "env": values,
            "enabled_tools": list(tools),
        },
    }


def _stdio_host_config(tmp_path, server, *, refs, extra=None):
    config = _host_config(tmp_path, mcp_refs=refs)
    config["mcps"]["files"] = server
    if extra:
        config["mcps"].update(extra)
    return HarnessConfig.model_validate(config)


def _expand_http(server, env):
    headers = {}
    for key, value in (server.get("headers") or {}).items():
        if isinstance(value, str):
            for var, token in env.items():
                value = value.replace("${" + var + "}", token)
        headers[key] = value
    return {**server, "headers": headers}


async def _post_mcp(server, method, params, request_id=None):
    body = {"jsonrpc": "2.0", "method": method, "params": params}
    if request_id is not None:
        body["id"] = request_id
    async with httpx.AsyncClient(trust_env=False) as http:
        response = await http.post(server["url"], headers=server["headers"], json=body, timeout=4)
        response.raise_for_status()
        if request_id is None:
            assert response.status_code == 202
            return None
        payload = response.json()
        assert payload["id"] == request_id
        return payload


async def _open_stdio_relay(server):
    opened = await _post_mcp(
        server,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "fake-cli", "version": "1"},
        },
        1,
    )
    assert opened["result"]["serverInfo"]["name"] == "stdio-fixture"
    await _post_mcp(server, "notifications/initialized", {})
    return opened


def _install_claude(monkeypatch, tmp_path, invoke):
    monkeypatch.setattr(
        child_launch,
        "_LAUNCH_RESOLVERS",
        {
            **child_launch._LAUNCH_RESOLVERS,
            next(key for key in child_launch._LAUNCH_RESOLVERS if key.value == "cli_path"): lambda _: tmp_path / "fake-claude",
        },
    )

    class SDK:
        def __init__(self, *, options):
            assert options.strict_mcp_config
            from kotoyose.child_mcp_proxy import AUTH_ENV

            token = options.env[AUTH_ENV]
            self.servers = {}
            for name, server in options.mcp_servers.items():
                if "url" in server:
                    expanded = _expand_http(server, {AUTH_ENV: token})
                    assert token not in json.dumps(server)
                    self.servers[name] = expanded
                else:
                    self.servers[name] = server

        async def connect(self):
            pass

        async def query(self, prompt):
            self.answer = await invoke(self.servers, prompt)

        async def receive_response(self):
            yield AssistantMessage(content=[TextBlock(text=self.answer)], model="fake-model")
            yield claude_result()

        async def disconnect(self):
            pass

    monkeypatch.setattr(session, "ClaudeSDKClient", SDK)


async def _speak_local(server, path):
    process = await asyncio.create_subprocess_exec(
        server["command"],
        *[str(arg) for arg in server.get("args") or []],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, **{key: str(value) for key, value in (server.get("env") or {}).items()}},
    )

    async def send(method, params, request_id=None):
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if request_id is not None:
            body["id"] = request_id
        process.stdin.write((json.dumps(body) + "\n").encode())
        await process.stdin.drain()
        if request_id is None:
            return None
        payload = json.loads(await process.stdout.readline())
        assert payload["id"] == request_id
        return payload

    await send("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "child", "version": "1"}}, 1)
    await send("notifications/initialized", {})
    written = await send("tools/call", {"name": "write", "arguments": {"path": str(path)}}, 2)
    process.stdin.close()
    await process.wait()
    return written


def test_stdio_publication_version_ignores_secret_values(tmp_path, monkeypatch):
    """Changing a secret value must not force a new child approval; the command is public."""
    server = {
        "type": "stdio",
        "command": "/opt/kotoyose/host-only-stdio",
        "args": ["--serve"],
        "env": {"TOKEN": "secret-alpha-value-aaaaaaaa"},
        "enabled_tools": ["write"],
    }
    first = McpDefinition(description="Host files", publish_to_children=True, publication_revision="1", server=server)
    second = McpDefinition(
        description="Host files",
        publish_to_children=True,
        publication_revision="1",
        server={**server, "env": {"TOKEN": "secret-beta-value-bbbbbbbb"}},
    )
    moved = McpDefinition(
        description="Host files",
        publish_to_children=True,
        publication_revision="1",
        server={**server, "command": "/opt/kotoyose/other-stdio"},
    )
    secret = b"k" * 32
    assert publication_version(secret, "files", first) == publication_version(secret, "files", second)
    assert publication_version(secret, "files", first) != publication_version(secret, "files", moved)
    config = _host_config(tmp_path, mcp_refs=["host:files"])
    config["mcps"]["files"] = {
        "description": "Host files",
        "publish_to_children": True,
        "publication_revision": "1",
        "server": server,
    }
    monkeypatch.setattr("kotoyose.child_mcp_host.get_config", lambda: HarnessConfig.model_validate(config))
    catalog = json.dumps(publication_catalog(secret))
    row = publication_catalog(secret)[0]
    assert row["command"] == "/opt/kotoyose/host-only-stdio"
    assert row.get("args") == ["--serve"]
    assert row.get("env_names") == ["TOKEN"]
    assert "cwd" not in row
    assert "secret-alpha-value" not in catalog
    assert "secret-beta-value" not in catalog


async def test_child_turn_runs_published_stdio_on_the_host_and_local_stdio_on_the_child(hub, tmp_path, monkeypatch):
    host_file = tmp_path / "host-out" / "note.txt"
    child_file = tmp_path / "child-out" / "note.txt"
    pid_path = tmp_path / "host-pids.json"
    definition = _host_stdio_definition(tmp_path, MCP_STDIO_PIDS=str(pid_path))
    cfg = _stdio_host_config(tmp_path, definition, refs=["host:files", "local:notes"])
    monkeypatch.setattr("kotoyose.config._config", cfg)
    stub_secret(monkeypatch)
    observed = {}

    async def invoke(servers, _prompt):
        host, local = servers["files"], servers["notes"]
        assert host["url"].startswith("http://127.0.0.1:")
        assert "host-only-stdio" not in json.dumps(host)
        assert local["command"].endswith("child-only-stdio")
        assert HOST_ROLE not in json.dumps(local)
        await _open_stdio_relay(host)
        listed = await _post_mcp(host, "tools/list", {}, 2)
        assert [tool["name"] for tool in listed["result"]["tools"]] == ["write", "leak"]
        wrote = await _post_mcp(host, "tools/call", {"name": "write", "arguments": {"path": str(host_file)}}, 3)
        assert wrote["result"]["content"][0]["text"] == "wrote"
        assert host_file.read_text(encoding="utf-8") == f"{HOST_ROLE}\n{HOST_ROLE}\n"
        pids = json.loads(pid_path.read_text(encoding="utf-8"))
        assert _alive(pids["server"]) and _alive(pids["child"])
        leaked = await _post_mcp(host, "tools/call", {"name": "leak", "arguments": {}}, 4)
        assert HOST_SECRET not in json.dumps(leaked)
        assert "error" in leaked
        local_reply = await _speak_local(local, child_file)
        assert local_reply["result"]["content"][0]["text"] == "wrote"
        assert child_file.read_text(encoding="utf-8") == f"{CHILD_ROLE}\n{CHILD_ROLE}\n"
        observed["pids"] = pids
        return "wrote"

    _install_claude(monkeypatch, tmp_path, invoke)
    home = tmp_path / "child"
    _policy(
        home,
        tmp_path,
        adapters=["claude.agent-sdk"],
        mcp={
            "notes": {
                "description": "child notes",
                "command": str(_stdio_command(tmp_path, "child-only-stdio")),
                "args": ["-u", str(SCRIPT)],
                "env": {"MCP_STDIO_ROLE": CHILD_ROLE, "MCP_STDIO_MARKER": CHILD_ROLE},
            },
        },
    )
    await enroll_child(hub.issue_enrollment_code(f"127.0.0.1:{hub.port}"), home, name="dev")
    approve(home, hub.host_fingerprint, cfg.mcps, hub)
    received = []
    async with child_session(home, heartbeat_interval=0.05, received_turns=received):
        await _wait_child(hub, "dev", online=True)
        result = await turn(cfg, tmp_path, "claude").ask("write on the host")
    assert result.text == "wrote"
    wire = json.dumps(received)
    assert "host-only-stdio" not in wire
    assert HOST_ROLE not in wire
    assert HOST_SECRET not in wire
    assert not _alive(observed["pids"]["server"])
    assert not _alive(observed["pids"]["child"])


@pytest.mark.parametrize(
    ("condition", "reason"),
    [
        ("unapproved", "requires child owner approval"),
        ("unpublished", "is not published"),
    ],
)
async def test_stdio_relay_refuses_without_publication_or_approval(hub, tmp_path, monkeypatch, condition, reason):
    definition = _host_stdio_definition(tmp_path)
    if condition == "unpublished":
        definition = {**definition, "publish_to_children": False}
    cfg = _stdio_host_config(tmp_path, definition, refs=["host:files"])
    monkeypatch.setattr("kotoyose.config._config", cfg)
    home = await enrolled(hub, tmp_path, "claude")
    if condition != "unapproved":
        approve(home, hub.host_fingerprint, cfg.mcps, hub)
    received = []
    async with child_session(home, received_turns=received):
        await _wait_child(hub, "dev", online=True)
        with pytest.raises(RuntimeTurnError, match=reason):
            await turn(cfg, tmp_path, "claude").ask("do not start")
    assert received == []
    assert not (tmp_path / "host-pids.json").exists()


@pytest.mark.parametrize("action", ["turn-end", "link-drop"])
async def test_stdio_process_group_is_reaped_on_turn_end_and_disconnect(hub, tmp_path, monkeypatch, action):
    pid_path = tmp_path / "host-pids.json"
    started = tmp_path / "stdio-started"
    definition = _host_stdio_definition(
        tmp_path,
        tools=("block",),
        MCP_STDIO_PIDS=str(pid_path),
        MCP_STDIO_STARTED=str(started),
    )
    cfg = _stdio_host_config(tmp_path, definition, refs=["host:files"])
    monkeypatch.setattr("kotoyose.config._config", cfg)
    stub_secret(monkeypatch)
    child_errors = []

    async def invoke(servers, _prompt):
        await _open_stdio_relay(servers["files"])
        reply = await _post_mcp(servers["files"], "tools/call", {"name": "block", "arguments": {}}, 3)
        child_errors.append(json.dumps(reply))
        return "done"

    _install_claude(monkeypatch, tmp_path, invoke)
    home = await enrolled(hub, tmp_path, "claude")
    approve(home, hub.host_fingerprint, cfg.mcps, hub)
    async with child_session(home, heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        runtime = turn(cfg, tmp_path, "claude")
        task = asyncio.create_task(runtime.ask("block"))
        await until(lambda: started.is_file() and pid_path.is_file())
        pids = json.loads(pid_path.read_text(encoding="utf-8"))
        assert _alive(pids["server"]) and _alive(pids["child"])
        if action == "link-drop":
            await hub.disconnect("dev")
            with pytest.raises(RuntimeTurnError, match="result is unknown"):
                await asyncio.wait_for(task, 4)
            await until(lambda: child_errors)
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await until(lambda: not _alive(pids["server"]) and not _alive(pids["child"]))
    if action == "link-drop":
        assert child_errors and "unknown" in child_errors[0]


async def test_stdio_timeout_does_not_restart_the_process(hub, tmp_path, monkeypatch):
    from kotoyose import child_mcp_host

    spawn_log = tmp_path / "spawns.txt"
    definition = _host_stdio_definition(tmp_path, tools=("block", "write"), MCP_STDIO_SPAWNS=str(spawn_log))
    cfg = _stdio_host_config(tmp_path, definition, refs=["host:files"])
    monkeypatch.setattr("kotoyose.config._config", cfg)
    monkeypatch.setattr(child_mcp_host, "UPSTREAM_TIMEOUT", 0.4)
    stub_secret(monkeypatch)

    async def invoke(servers, _prompt):
        await _open_stdio_relay(servers["files"])
        first = await _post_mcp(servers["files"], "tools/call", {"name": "block", "arguments": {}}, 3)
        assert "unknown" in first["error"]["message"]
        assert len(spawn_log.read_text(encoding="utf-8").split()) == 1
        second = await _post_mcp(servers["files"], "tools/call", {"name": "write", "arguments": {"path": str(tmp_path / "again.txt")}}, 4)
        assert "error" in second
        assert not (tmp_path / "again.txt").exists()
        assert len(spawn_log.read_text(encoding="utf-8").split()) == 1
        return "once"

    _install_claude(monkeypatch, tmp_path, invoke)
    home = await enrolled(hub, tmp_path, "claude")
    approve(home, hub.host_fingerprint, cfg.mcps, hub)
    async with child_session(home, heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        result = await turn(cfg, tmp_path, "claude").ask("time out")
    assert result.text == "once"
    assert len(spawn_log.read_text(encoding="utf-8").split()) == 1


async def test_stdio_process_cap_refuses_a_second_server(hub, tmp_path, monkeypatch):
    from kotoyose import child_mcp_host

    spawn_log = tmp_path / "spawns.txt"
    first = _host_stdio_definition(tmp_path, tools=("write",), MCP_STDIO_SPAWNS=str(spawn_log))
    extra_command = _stdio_command(tmp_path, "host-extra-stdio")
    extra = {
        "description": "Extra",
        "publish_to_children": True,
        "server": {
            "type": "stdio",
            "command": str(extra_command),
            "args": ["-u", str(SCRIPT)],
            "env": {"MCP_STDIO_ROLE": "extra-role", "MCP_STDIO_SPAWNS": str(spawn_log)},
            "enabled_tools": ["write"],
        },
    }
    cfg = _stdio_host_config(tmp_path, first, refs=["host:files", "host:extra"], extra={"extra": extra})
    monkeypatch.setattr("kotoyose.config._config", cfg)
    monkeypatch.setattr(child_mcp_host, "_MAX_STDIO", 1)
    stub_secret(monkeypatch)

    async def invoke(servers, _prompt):
        await _open_stdio_relay(servers["files"])
        refused = await _post_mcp(
            servers["extra"],
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "fake-cli", "version": "1"},
            },
            1,
        )
        assert "concurrency" in refused["error"]["message"]
        assert len(spawn_log.read_text(encoding="utf-8").split()) == 1
        return "capped"

    _install_claude(monkeypatch, tmp_path, invoke)
    home = await enrolled(hub, tmp_path, "claude")
    approve(home, hub.host_fingerprint, cfg.mcps, hub)
    async with child_session(home, heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        result = await turn(cfg, tmp_path, "claude").ask("two servers")
    assert result.text == "capped"


def test_stdio_catalog_shows_args_cwd_and_env_names_but_not_secret_values(tmp_path, monkeypatch):
    """The approval offer shows the command line and cwd. Env values, including secrets, stay out."""
    hidden = "plain-secret-value-should-stay-hidden"
    server = {
        "type": "stdio",
        "command": "/opt/kotoyose/host-only-stdio",
        "args": ["--serve", "--root", "/var/lib/kotoyose"],
        "cwd": "/var/lib/kotoyose",
        "env": {"TOKEN": {"secret": "HOST_MCP_FIXTURE"}, "VISIBLE": hidden},
        "enabled_tools": ["write"],
    }
    definition = McpDefinition(description="Host files", publish_to_children=True, server=server)
    changed_value = McpDefinition(
        description="Host files",
        publish_to_children=True,
        server={**server, "env": {"TOKEN": {"secret": "HOST_MCP_FIXTURE"}, "VISIBLE": "other-value"}},
    )
    changed_args = McpDefinition(
        description="Host files",
        publish_to_children=True,
        server={**server, "args": ["--other"]},
    )
    secret = b"k" * 32
    assert publication_version(secret, "files", definition) == publication_version(secret, "files", changed_value)
    assert publication_version(secret, "files", definition) != publication_version(secret, "files", changed_args)
    config = _host_config(tmp_path, mcp_refs=["host:files"])
    config["mcps"]["files"] = {
        "description": "Host files",
        "publish_to_children": True,
        "server": server,
    }
    monkeypatch.setattr("kotoyose.child_mcp_host.get_config", lambda: HarnessConfig.model_validate(config))
    row = publication_catalog(secret)[0]
    assert row.get("args") == ["--serve", "--root", "/var/lib/kotoyose"]
    assert row.get("cwd") == "/var/lib/kotoyose"
    assert row.get("env_names") == ["TOKEN", "VISIBLE"]
    catalog = json.dumps(row)
    assert hidden not in catalog
    assert "HOST_MCP_FIXTURE" not in catalog


def _direct_stdio_server(env=None, **extra):
    server = {"command": sys.executable, "args": ["-u", str(SCRIPT)], "env": {} if env is None else env}
    server.update(extra)
    return server


async def _wait_dead(pid: int) -> None:
    for _ in range(40):
        if not _alive(pid):
            return
        await asyncio.sleep(0.05)
    assert not _alive(pid)


async def test_sigterm_ignoring_grandchild_dies_when_the_leader_exits(tmp_path):
    """Closing the session must SIGKILL descendants that survive the leader's SIGTERM."""
    from kotoyose import child_mcp_host

    pid_path = tmp_path / "pids.json"
    script = tmp_path / "leader.py"
    script.write_text(
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "pid_path = Path(sys.argv[1])\n"
        "ready = Path(str(pid_path) + '.ready')\n"
        "child = subprocess.Popen([sys.executable, '-c',\n"
        "    'import signal, time, pathlib, sys; '\n"
        "    'signal.signal(signal.SIGTERM, signal.SIG_IGN); '\n"
        "    'signal.signal(signal.SIGHUP, signal.SIG_IGN); '\n"
        "    'pathlib.Path(sys.argv[1]).write_text(\"ready\"); '\n"
        "    'time.sleep(300)', str(ready)])\n"
        "while not ready.exists():\n"
        "    time.sleep(0.01)\n"
        "pid_path.write_text(json.dumps({'server': os.getpid(), 'child': child.pid}), encoding='utf-8')\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    session = child_mcp_host._StdioSession(tmp_path / "state")
    grandchild = None
    try:
        await session._ensure({"command": sys.executable, "args": ["-u", str(script), str(pid_path)], "env": {}})
        await until(lambda: pid_path.is_file())
        pids = json.loads(pid_path.read_text(encoding="utf-8"))
        grandchild = pids["child"]
        assert _alive(grandchild)
        os.kill(pids["server"], signal.SIGTERM)
        await _wait_dead(pids["server"])
        assert _alive(grandchild)
        await session.close()
        await _wait_dead(grandchild)
    finally:
        await session.close()
        if grandchild is not None and _alive(grandchild):
            os.kill(grandchild, signal.SIGKILL)


async def test_stdio_group_record_is_removed_when_the_session_stops(tmp_path):
    from kotoyose import child_mcp_host

    session = child_mcp_host._StdioSession(tmp_path / "state")
    server = {"command": sys.executable, "args": ["-c", "import time; time.sleep(300)"], "env": {}}
    try:
        await session._ensure(server)
        record = tmp_path / "state" / "stdio-groups" / str(session.process.pid)
        assert record.is_file()
        await session.close()
        assert not record.exists()
    finally:
        await session.close()


async def test_startup_kills_a_stdio_group_named_by_a_fake_record(tmp_path):
    """A record left under the child-relay home is a stale group, even if this process did not spawn it."""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)",
        start_new_session=True,
    )
    home = tmp_path / "host"
    hub = ChildHub(home)
    record = home / "stdio-groups" / str(process.pid)
    record.parent.mkdir(mode=0o700)
    record.write_text("fake\n", encoding="utf-8")
    try:
        await hub.start("127.0.0.1", 0)
        await _wait_dead(process.pid)
        assert not record.exists()
    finally:
        await hub.stop()
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()


async def test_one_child_cannot_hold_every_stdio_process_and_a_crash_frees_its_slot(tmp_path):
    from kotoyose import child_mcp_host

    link = SimpleNamespace(stdio_live=0)
    other = SimpleNamespace(stdio_live=0)
    server = {"command": sys.executable, "args": ["-c", "import time; time.sleep(300)"], "env": {}}
    state = tmp_path / "state"
    baseline = child_mcp_host._stdio_live
    first = child_mcp_host._StdioSession(state, link)
    second = child_mcp_host._StdioSession(state, link)
    third = child_mcp_host._StdioSession(state, link)
    outsider = child_mcp_host._StdioSession(state, other)
    started = []
    try:
        await first._ensure(server)
        started.append(first)
        await second._ensure(server)
        started.append(second)
        assert link.stdio_live == 2
        assert child_mcp_host._stdio_live == baseline + 2
        with pytest.raises(ValueError, match="concurrency"):
            await third._ensure(server)
        await outsider._ensure(server)
        started.append(outsider)
        assert other.stdio_live == 1
        os.kill(first.process.pid, signal.SIGKILL)
        await until(lambda: link.stdio_live == 1 and child_mcp_host._stdio_live == baseline + 2)
        await third._ensure(server)
        started.append(third)
        assert link.stdio_live == 2
        assert child_mcp_host._stdio_live == baseline + 3
        assert child_mcp_host._stdio_live < child_mcp_host._MAX_STDIO
    finally:
        for session in started:
            await session.close()
        await third.close()


async def test_unhashable_stdio_response_id_kills_the_process_group(tmp_path):
    from kotoyose import child_mcp_host

    pid_path = tmp_path / "pids.json"
    script = tmp_path / "bad_id.py"
    script.write_text(
        "import json, os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "pid_path = Path(sys.argv[1])\n"
        "ready = Path(str(pid_path) + '.ready')\n"
        "child = subprocess.Popen([sys.executable, '-c',\n"
        "    'import signal, time, pathlib, sys; '\n"
        "    'signal.signal(signal.SIGTERM, signal.SIG_IGN); '\n"
        "    'signal.signal(signal.SIGHUP, signal.SIG_IGN); '\n"
        "    'pathlib.Path(sys.argv[1]).write_text(\"ready\"); '\n"
        "    'time.sleep(300)', str(ready)])\n"
        "while not ready.exists():\n"
        "    time.sleep(0.01)\n"
        "pid_path.write_text(json.dumps({'server': os.getpid(), 'child': child.pid}), encoding='utf-8')\n"
        "print('{\"id\":[1]}', flush=True)\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    session = child_mcp_host._StdioSession(tmp_path / "state")
    pids = {}
    try:
        await session._ensure({"command": sys.executable, "args": ["-u", str(script), str(pid_path)], "env": {}})
        await until(lambda: pid_path.is_file())
        pids = json.loads(pid_path.read_text(encoding="utf-8"))
        await _wait_dead(pids["child"])
        assert not _alive(pids["server"])
    finally:
        await session.close()
        for pid in pids.values():
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)


async def _stdio_report(tmp_path, server):
    from kotoyose import child_mcp_host

    report = tmp_path / "report.json"
    server = {**server, "env": {**server.get("env", {}), "MCP_STDIO_REPORT": str(report)}}
    session = child_mcp_host._StdioSession(tmp_path / "state")
    try:
        await session._ensure(server)
        await until(lambda: report.is_file())
        return json.loads(report.read_text(encoding="utf-8"))
    finally:
        await session.close()


async def test_stdio_process_cwd_and_home_default_to_an_empty_directory(tmp_path, monkeypatch):
    host_home = tmp_path / "service-home"
    host_home.mkdir()
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "relative-name").mkdir()
    (tmp_path / "relative-name" / "marker").write_text("host", encoding="utf-8")
    server = _direct_stdio_server({"MCP_STDIO_ROLE": "iso"}, cwd="relative-name")
    body = await _stdio_report(tmp_path, server)
    assert body["entries"] == []
    assert Path(body["cwd"]).is_absolute()
    assert body["home"] == body["cwd"]
    assert body["cwd"] not in {str(host_home), str(tmp_path / "relative-name")}


async def test_stdio_absolute_cwd_is_the_process_cwd_and_home(tmp_path, monkeypatch):
    host_home = tmp_path / "service-home"
    host_home.mkdir()
    monkeypatch.setenv("HOME", str(host_home))
    work = tmp_path / "given"
    work.mkdir()
    (work / "marker").write_text("keep", encoding="utf-8")
    server = _direct_stdio_server({"MCP_STDIO_ROLE": "iso"}, cwd=str(work))
    body = await _stdio_report(tmp_path, server)
    assert body["cwd"] == str(work)
    assert body["home"] == str(work)
    assert body["entries"] == ["marker"]


async def test_stdio_environment_is_safe_defaults_plus_declared_keys(tmp_path, monkeypatch):
    from mcp.client.stdio import DEFAULT_INHERITED_ENV_VARS

    host_home = tmp_path / "service-home"
    host_home.mkdir()
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(tmp_path / "config.yml"))
    monkeypatch.setenv("DATABASE_URL", "postgres://host-leak")
    monkeypatch.setenv("LEAK_TOKEN", "host-only-token")
    server = _direct_stdio_server({"MCP_STDIO_ROLE": "iso", "MCP_STDIO_MARKER": "marker-value"})
    body = await _stdio_report(tmp_path, server)
    declared = {"MCP_STDIO_ROLE", "MCP_STDIO_MARKER", "MCP_STDIO_REPORT"}
    allowed = set(DEFAULT_INHERITED_ENV_VARS) | declared | {"HOME"}
    leaked = sorted(key for key in body["env"] if key not in allowed and not key.startswith("LC_"))
    assert leaked == []
    assert body["env"]["HOME"] == body["cwd"]
    assert body["home"] != str(host_home)
    assert body["env"]["MCP_STDIO_ROLE"] == "iso"
    assert body["env"]["MCP_STDIO_MARKER"] == "marker-value"
    rendered = json.dumps(body["env"])
    assert "postgres://host-leak" not in rendered
    assert "host-only-token" not in rendered
    assert str(tmp_path / "config.yml") not in rendered
    assert not any(key.startswith("KOTOYOSE_") for key in body["env"])


async def test_cancel_during_stdio_drain_kills_the_group(tmp_path):
    from kotoyose import child_mcp_host

    started = tmp_path / "started"
    session = child_mcp_host._StdioSession(tmp_path / "state")
    server = _direct_stdio_server({"MCP_STDIO_MODE": "stall", "MCP_STDIO_STARTED": str(started)})
    task = None
    try:
        state = child_mcp_host._CallState()
        opened = await session.call(
            server,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
            },
            lambda _name: True,
            (),
            state,
        )
        assert opened["result"]["serverInfo"]["name"] == "stdio-fixture"
        await session.call(server, {"jsonrpc": "2.0", "method": "notifications/initialized"}, lambda _name: True, (), state)
        await until(lambda: started.is_file())
        tool_state = child_mcp_host._CallState()
        request = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "write", "arguments": {"path": "x", "blob": "A" * 90000}},
        }
        task = asyncio.create_task(session.call(server, request, lambda _name: True, (), tool_state))
        await asyncio.sleep(0.1)
        assert not task.done()
        assert tool_state.tool_sent
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _wait_dead(session.process.pid)
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if session.process is not None and session.process.pid and _alive(session.process.pid):
            try:
                os.killpg(session.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await session.close()


async def test_stdio_tool_timeout_during_drain_is_unknown_not_refused(hub, tmp_path, monkeypatch):
    from kotoyose import child_mcp_host

    started = tmp_path / "stdio-started"
    definition = _host_stdio_definition(tmp_path, tools=("write",), MCP_STDIO_MODE="stall", MCP_STDIO_STARTED=str(started))
    cfg = _stdio_host_config(tmp_path, definition, refs=["host:files"])
    monkeypatch.setattr("kotoyose.config._config", cfg)
    monkeypatch.setattr(child_mcp_host, "UPSTREAM_TIMEOUT", 0.4)
    stub_secret(monkeypatch)
    seen = {}

    async def invoke(servers, _prompt):
        await _open_stdio_relay(servers["files"])
        await until(lambda: started.is_file())
        reply = await _post_mcp(
            servers["files"],
            "tools/call",
            {"name": "write", "arguments": {"path": "x", "blob": "A" * 90000}},
            3,
        )
        seen["message"] = reply["error"]["message"]
        return "once"

    _install_claude(monkeypatch, tmp_path, invoke)
    home = await enrolled(hub, tmp_path, "claude")
    approve(home, hub.host_fingerprint, cfg.mcps, hub)
    async with child_session(home, heartbeat_interval=0.05):
        await _wait_child(hub, "dev", online=True)
        result = await turn(cfg, tmp_path, "claude").ask("block the pipe")
    assert result.text == "once"
    assert "unknown" in seen["message"]
    assert "refused" not in seen["message"]


async def test_grok_acp_lists_host_loopback_beside_local_stdio(tmp_path, monkeypatch):
    from kotoyose import provider_data_runtime
    from kotoyose.child_turn import _build_runtime, _prepare
    from kotoyose.provider_data_child import approve as approve_declaration
    from kotoyose.provider_data_child import use_approved
    from kotoyose.runtime import acp
    from tests.test_acp import FAKE_ACP, seen
    from tests.test_provider_data import (
        _GROK_DECLARATION,
        _child_home,
        _grok_account,
        _grok_host_config,
        _grok_write_alias,
        _mock_landlock,
        _offer,
    )

    monkeypatch.setattr("kotoyose.cli_install_spec.platform_key", lambda: "linux-x64")
    (tmp_path / "child-parent").mkdir(mode=0o700)
    home = _child_home(tmp_path / "child-parent/child")
    real_home = tmp_path / "owner-parent/owner"
    real_home.mkdir(mode=0o700, parents=True)
    (real_home / ".grok").mkdir(mode=0o700)
    _grok_account(monkeypatch, real_home)
    _mock_landlock(monkeypatch)
    row = _offer(_GROK_DECLARATION.read_bytes(), home)
    approve_declaration(home, row.id, sha256=row.sha256, capability="write")
    (home / "policy.json").write_text(
        json.dumps(
            {
                "workspace_roots": [str(tmp_path)],
                "adapters": [row.id],
                "mcp": {},
                "logins": [],
            }
        ),
        encoding="utf-8",
    )
    config = _grok_host_config()
    _grok_write_alias(config, tmp_path)
    payload = {
        "alias": "grok-write",
        "preset": config["models"]["grok-write"],
        "route": config["routes"]["grok"],
        "connection": config["connections"]["dev"],
        "declaration_sha256": row.sha256,
        "capability": "write",
    }
    approve_declaration(home, row.id, sha256=row.sha256, capability="mcp")
    workspace = tmp_path / "work"
    workspace.mkdir()
    policy_file = home / "policy.json"
    policy = json.loads(policy_file.read_text(encoding="utf-8"))
    policy["mcp"] = {"notes": {"command": "child-local-notes", "args": ["--from-child"], "env": {"CHILD_ONLY": "1"}}}
    policy_file.write_text(json.dumps(policy), encoding="utf-8")
    payload.update(cwd=str(workspace), capability="mcp")
    payload["preset"]["mcp_refs"] = ["host:files", "local:notes"]
    fake = tmp_path / "fake-grok"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import runpy, sys\n"
        "assert sys.argv[1:3] == ['--sandbox', 'strict']\n"
        "sys.argv[1:] = ['grok_mcp_empty']\n"
        f"runpy.run_path({str(FAKE_ACP)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    fake.chmod(0o700)
    monkeypatch.setattr(acp, "resolve_print_cli_binary", lambda *_args: fake)
    monkeypatch.setattr(provider_data_runtime, "managed_binary", lambda *_args: fake)
    relay = {
        "files": {
            "server": {
                "type": "http",
                "url": "http://127.0.0.1:49821/mcp/0",
                "headers": {"Authorization": "Bearer fixture"},
            }
        }
    }
    with use_approved(home):
        prepared = _prepare(home, payload, relay_mcps=relay)
        runtime = await _build_runtime(prepared)
        try:
            await runtime.ask("question")
            messages = seen(runtime.cwd)
            (session_new,) = [msg["params"] for msg in messages if msg.get("method") == "session/new"]
            by_name = {item["name"]: item for item in session_new["mcpServers"]}
            assert by_name["files"]["type"] == "http"
            assert "command" not in by_name["files"]
            assert by_name["notes"]["command"] == "child-local-notes"
            assert "host-only-stdio" not in json.dumps(session_new)
        finally:
            await runtime.disconnect()
            prepared["cleanup"]()
