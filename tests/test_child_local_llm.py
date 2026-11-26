"""Stage D via the real child link and a loopback-only SSE fixture."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from kotoyose.child_client import _hello_features, child_session, enroll_child
from kotoyose.child_identity import load_policy
from kotoyose.child_local_llm import LOCAL_LLM_UI_PORT_FILE, parse_local_llm_catalog
from kotoyose.child_protocol import LOCAL_LLM_FEATURE, Frame
from kotoyose.child_server import ChildHub
from kotoyose.child_turn import ChildTurnError, ChildTurns, _build_runtime, _prepare
from kotoyose.cli_install_spec import platform_key
from kotoyose.config import HarnessConfig
from kotoyose.runtime.base import RuntimeChatHistory, RuntimeResult, RuntimeTurnError, SDKConnectionError
from kotoyose.runtime.child import build_turn_payload
from kotoyose.runtime.openai_chat import OpenAIChatSession, build_loopback_chat_session
from kotoyose.session import get_or_resume_client
from tests.test_child_k1 import _wait_child


def _config(*, model="tiny", data_policy="no_training"):
    return {
        "connections": {"child-chat": {
            "provider": "openai_compat", "kind": "local_llm", "local_llm": "desk",
            "on": "child:dev", "data_policy": data_policy,
        }},
        "routes": {"child-chat": {"driver": "openai.chat", "model": model, "connection": "child-chat"}},
        "models": {"dev-chat": {"route": "child-chat", "on": "child:dev"}},
    }


def _write_policy(home, catalog):
    path = home / "policy.json"
    path.write_text(json.dumps({"workspace_roots": [], "adapters": [], "mcp": {}, "local_llm": catalog}))
    return path


def _turn_payload(config):
    live = SimpleNamespace(name="dev", features={LOCAL_LLM_FEATURE}, local_llm={"desk": ("tiny",)})
    return build_turn_payload(
        alias="dev-chat", config=config, preset=config.preset_for("dev-chat"),
        conversation_key=None, cwd=None, prompt="test", live=live,
    )


@asynccontextmanager
async def _child(tmp_path, catalog, *, features=None, received=None):
    home = tmp_path / "child"
    hub = ChildHub(tmp_path / "host")
    await hub.start("127.0.0.1", 0)
    try:
        await enroll_child(hub.issue_enrollment_code(f"127.0.0.1:{hub.port}"), home, name="dev")
        if catalog is not None:
            _write_policy(home, catalog)
        async with child_session(home, features=features, received_turns=received):
            await _wait_child(hub, "dev", online=True)
            yield hub, home
    finally:
        await hub.stop()


@asynccontextmanager
async def _chat_endpoint():
    requests = []
    behavior = {"location": None}

    async def serve(reader, writer):
        try:
            request_line, *raw_headers = (await reader.readuntil(b"\r\n\r\n")).decode("ascii").splitlines()[:-1]
            headers = dict(line.lower().split(": ", 1) for line in raw_headers)
            body = json.loads(await reader.readexactly(int(headers["content-length"])))
            requests.append((request_line, headers, body))
            if (location := behavior["location"]) is not None:
                status = f"307 Temporary Redirect\r\nLocation: {location}\r\n"
                data = b""
            else:
                status = "200 OK\r\nContent-Type: text/event-stream\r\n"
                events = [
                    {"choices": [{"delta": {"content": "hel"}}]},
                    {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
                    {"choices": [], "usage": {"completion_tokens": 2}},
                ]
                data = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode() + b"data: [DONE]\n\n"
            writer.write(f"HTTP/1.1 {status}Connection: close\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1", requests, behavior


async def _client(config):
    runtime, _ = await get_or_resume_client("test-local-llm", "dev-chat", config_snapshot=config)
    return runtime


class _UpstreamBody(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __aiter__(self):
        return self.chunks

    async def aclose(self):
        self.closed = True


def _runtime_with_body(monkeypatch, chunks, *, timeout=2, encoding=None):
    config = HarnessConfig.model_validate(_config())
    preset = config.preset_for("dev-chat").model_copy(update={"timeout_seconds": timeout})
    runtime = build_loopback_chat_session(preset, config=config, loopback_url="http://127.0.0.1:1234/v1/chat/completions")
    body = _UpstreamBody(chunks)

    async def respond(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, stream=body, headers={"Content-Encoding": encoding} if encoding else None)

    client = httpx.AsyncClient

    def client_with_transport(**kwargs):
        return client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(httpx, "AsyncClient", client_with_transport)
    return runtime, body


async def _run_chat(runtime, stream):
    if stream:
        return [item async for item in runtime.stream("test")]
    return [await runtime.ask("test")]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("overflow", [False, True])
async def test_loopback_body_limit_covers_streaming_and_non_streaming(monkeypatch, stream, overflow):
    mib = 1024 * 1024
    suffix = (
        b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
        if stream else b'{"choices":[{"message":{"content":"ok"}}]}'
    )
    def padding(size):
        return b":" + b" " * (size - 2) + b"\n" if stream else b" " * size

    async def chunks():
        for _ in range(7):
            yield padding(mib)
        yield padding(mib - len(suffix) + overflow) + suffix
        if overflow:
            pytest.fail("response was still being read past the limit")

    runtime, body = _runtime_with_body(monkeypatch, chunks())
    if overflow:
        with pytest.raises(RuntimeTurnError, match="response exceeds the 8 MiB limit"):
            await _run_chat(runtime, stream)
    else:
        result = await _run_chat(runtime, stream)
        assert (result[0] if stream else result[0].text) == "ok"
    assert body.closed


@pytest.mark.parametrize("overflow", [False, True])
async def test_loopback_sse_line_limit_applies_before_newline(monkeypatch, overflow):
    async def chunks():
        yield b":" + b"x" * (512 * 1024 - 1)
        yield b"x" * (512 * 1024)
        if overflow:
            yield b"x"
            pytest.fail("overlong line was still being read")
        yield b"\r"
        yield b'\ndata: {"choices":[{"delta":{"content":"\xc3'
        yield b'\xa9"}}]}\r'
        yield b"\n\r\ndata: [DONE]\r\n\r\n"

    runtime, body = _runtime_with_body(monkeypatch, chunks())
    if overflow:
        with pytest.raises(RuntimeTurnError, match="SSE line exceeds the 1 MiB limit"):
            await _run_chat(runtime, True)
    else:
        assert (await _run_chat(runtime, True))[0] == "é"
    assert body.closed


@pytest.mark.parametrize("stream", [False, True])
async def test_loopback_wall_clock_timeout_stops_continuous_response(monkeypatch, stream):
    async def chunks():
        for _ in range(100):
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n' if stream else b" "
            await asyncio.sleep(0.01)

    runtime, body = _runtime_with_body(monkeypatch, chunks(), timeout=0.2)
    with pytest.raises(RuntimeTurnError, match="Chat endpoint timed out") as exc:
        await _run_chat(runtime, stream)
    assert exc.value.diagnostics["terminal_reason"] == "timeout"
    assert body.closed


async def test_loopback_refuses_compression_before_reading(monkeypatch):
    async def chunks():
        yield pytest.fail("compressed body was read")

    runtime, body = _runtime_with_body(monkeypatch, chunks(), encoding="gzip")
    with pytest.raises(RuntimeTurnError, match="must use identity encoding"):
        await runtime.ask("test")
    assert body.closed


async def test_buffered_body_limit_is_specific_to_loopback(monkeypatch):
    config = HarnessConfig.model_validate(_config())
    preset = config.preset_for("dev-chat")
    url = "http://127.0.0.1:1234/v1/chat/completions"
    content = b" " * (8 * 1024 * 1024) + b'{"choices":[{"message":{"content":"ok"}}]}'
    client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=content))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(**kwargs, transport=transport))

    loopback = build_loopback_chat_session(preset, config=config, loopback_url=url)
    with pytest.raises(RuntimeTurnError, match="response exceeds the 8 MiB limit"):
        await loopback.ask("test")
    host = OpenAIChatSession(url, "fixture-key", "tiny", None, 2)
    assert (await host.ask("test")).text == "ok"


async def test_local_llm_streams_history_without_keys_proxies_or_dns_and_refuses_redirect(tmp_path, monkeypatch):
    received = []
    config = HarnessConfig.model_validate(_config())
    async with _chat_endpoint() as (url, requests, behavior), _child(
        tmp_path, {"desk": {"url": url, "models": ["tiny"]}}, received=received,
    ) as (hub, home):
        async with _chat_endpoint() as (proxy, proxy_requests, _):
            for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                monkeypatch.setenv(key, proxy)
            for key in ("no_proxy", "NO_PROXY"):
                monkeypatch.setenv(key, "")

            async def refuse_dns(*args, **kwargs):
                raise AssertionError("the loopback endpoint must use a numeric address")

            monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", refuse_dns)
            client = await _client(config)
            prompt = RuntimeChatHistory((("user", "first"), ("assistant", "previous"), ("user", "again")))
            items = [item async for item in client.stream(prompt)]
            assert items == ["hel", "lo", RuntimeResult(text="", usage={"output_tokens": 2}, provider="openai_compat")]
            assert (await client.ask("sync answer")).text == "hello"
            assert hub.list_children()[0]["local_llm"] == {"desk": ["tiny"]}
            assert hub.connection_status("dev", "openai_compat", local_llm="desk").state == "online"
            assert url not in json.dumps(hub.hello_payloads)
            assert url not in json.dumps(received)
            line, headers, body = requests[0]
            assert line == "POST /v1/chat/completions HTTP/1.1"
            assert "authorization" not in headers
            assert body["model"] == "tiny" and body["stream"]
            assert body["messages"] == [{"role": role, "content": value} for role, value in prompt.messages]
            assert not proxy_requests

            behavior["location"] = proxy + "/chat/completions"
            with pytest.raises(RuntimeTurnError, match="endpoint rejected the request") as exc:
                await client.ask("redirect")
            assert exc.value.diagnostics["api_error_status"] == 307
            assert len(requests) == 3 and not proxy_requests

            with pytest.raises(RuntimeTurnError, match="model .* is not allowed"):
                await (await _client(HarnessConfig.model_validate(_config(model="other")))).ask("no match")
            assert len(received) == 3
            _write_policy(home, {"desk": {"url": url, "models": ["other"]}})
            with pytest.raises(RuntimeTurnError, match="not allowed .* by child policy"):
                await client.ask("stale hello")
            assert len(requests) == 3


@pytest.mark.parametrize("legacy", [False, True])
async def test_local_llm_is_off_by_default_and_old_child_is_refused(tmp_path, legacy):
    features = [f for f in _hello_features(platform_key()) if f != LOCAL_LLM_FEATURE] if legacy else None
    catalog = {"desk": {"url": "http://127.0.0.1:11434/v1", "models": ["tiny"]}} if legacy else None
    received = []
    config = HarnessConfig.model_validate(_config())
    async with _child(tmp_path, catalog, features=features, received=received) as (hub, home):
        assert hub.list_children()[0]["local_llm"] == {}
        if legacy:
            with pytest.raises(SDKConnectionError, match="does not advertise Local LLM support"):
                await _client(config)
        else:
            assert load_policy(home / "policy.json").local_llm == {}
            with pytest.raises(RuntimeTurnError, match="has not advertised Local LLM connection"):
                await (await _client(config)).ask("off")
        assert not received


@pytest.mark.parametrize("url", [
    "http://192.0.2.1:11434/v1", "http://localhost:11434/v1", "http://127.0.0.1/v1",
    "http://127.0.0.2:11434/v1", "http://[::ffff:127.0.0.1]:11434/v1", "http://[::1]:0/v1",
    "http://127.0.0.1:01023/v1",
    "http://127.0.0.1:65536/v1", "http://127.0.0.1:11434/v1?query=1",
    "http://user@127.0.0.1:11434/v1", "http://127.0.0.1:11434/v1/chat/completions",
])
def test_policy_rejects_unpinned_urls(tmp_path, url):
    path = _write_policy(tmp_path, {"desk": {"url": url, "models": ["tiny"]}})
    with pytest.raises(ValueError, match="explicit port, and /v1"):
        load_policy(path)


def test_policy_accepts_ipv6_literal_without_credentials(tmp_path):
    path = _write_policy(tmp_path, {"desk": {"url": "http://[::1]:1234/v1", "models": ["tiny"]}})
    assert load_policy(path).local_llm["desk"].models == ("tiny",)
    path = _write_policy(tmp_path, {"desk": {"url": "http://[::1]:1234/v1", "models": ["tiny"], "api_key_env": "KEY"}})
    with pytest.raises(ValueError, match="require only url and models"):
        load_policy(path)


def test_local_llm_policy_and_catalog_size_limits(tmp_path):
    url = "http://127.0.0.1:1024/v1"
    longest = "é" * 128
    models = [longest, *(f"model-{i}" for i in range(63))]
    connections = {f"connection-{i}": {"url": url, "models": models} for i in range(16)}
    path = _write_policy(tmp_path, connections)
    assert len(load_policy(path).local_llm) == 16
    assert parse_local_llm_catalog({name: row["models"] for name, row in connections.items()}) == {
        name: tuple(models) for name in connections
    }
    for entries, error in [
        ({**connections, "extra": {"url": url, "models": models}}, "at most 16 connections"),
        ({"desk": {"url": url, "models": [*models, "extra"]}}, "1 to 64 models"),
        ({"desk": {"url": url, "models": [longest + "a"]}}, "256 bytes"),
    ]:
        path = _write_policy(tmp_path, entries)
        with pytest.raises(ValueError, match=error):
            load_policy(path)
        assert parse_local_llm_catalog({name: row["models"] for name, row in entries.items()}) == {}


@pytest.mark.parametrize("port", [4321, 4322])
def test_local_llm_rechecks_reserved_ports_at_turn_time(tmp_path, port):
    path = _write_policy(tmp_path, {"desk": {"url": f"http://[::1]:0{port}/v1", "models": ["tiny"]}})
    assert load_policy(path).local_llm
    (tmp_path / "host.json").write_text(json.dumps({
        "address": "127.0.0.1:4321", "fingerprint": "fixture", "host_cert_pem": "fixture", "name": "dev",
    }))
    (tmp_path / LOCAL_LLM_UI_PORT_FILE).write_text("4322")
    payload = _turn_payload(HarnessConfig.model_validate(_config()))
    with pytest.raises(ChildTurnError, match="settings UI or control-plane port"):
        _prepare(tmp_path, payload)


@pytest.mark.parametrize(("supplied", "expected"), [(None, None), (-5, 1), (0.5, 1), (1, 1), (123, 123), (3600, 3600), (3601, 3600)])
def test_local_llm_clamps_child_turn_timeout(tmp_path, supplied, expected):
    _write_policy(tmp_path, {"desk": {"url": "http://127.0.0.1:11434/v1", "models": ["tiny"]}})
    payload = _turn_payload(HarnessConfig.model_validate(_config()))
    payload["preset"]["timeout_seconds"] = supplied
    prepared = _prepare(tmp_path, payload)
    try:
        assert prepared["config"].preset_for("dev-chat").timeout_seconds == expected
    finally:
        prepared["cleanup"]()


def test_local_llm_payload_requires_live_catalog():
    config = HarnessConfig.model_validate(_config())
    with pytest.raises(ValueError, match="live child catalog is required"):
        build_turn_payload(
            alias="dev-chat", config=config, preset=config.preset_for("dev-chat"),
            conversation_key=None, cwd=None, prompt="test",
        )


@pytest.mark.parametrize("mismatch", ["missing_url", "wrong_provider", "wrong_driver"])
async def test_local_llm_runtime_rejects_driver_provider_mismatch(monkeypatch, mismatch):
    config = HarnessConfig.model_validate(_config())
    prepared = {"config": config, "alias": "dev-chat", "cwd": "."}
    if mismatch != "missing_url":
        prepared["builder_options"] = {"loopback_url": "http://127.0.0.1:11434/v1/chat/completions"}
    if mismatch == "wrong_provider":
        preset = config.preset_for("dev-chat").model_copy(update={"provider": "claude"})
        monkeypatch.setattr(HarnessConfig, "preset_for", lambda self, alias: preset)
    if mismatch == "wrong_driver":
        preset = config.preset_for("dev-chat")
        monkeypatch.setattr(HarnessConfig, "preset_for", lambda self, alias: preset)
        config.routes["child-chat"] = config.routes["child-chat"].model_copy(update={"driver": "claude.sdk"})
    message = "requires a policy-approved loopback URL" if mismatch == "missing_url" else "driver/provider mismatch"
    with pytest.raises(ChildTurnError, match=message):
        await _build_runtime(prepared)


@pytest.mark.parametrize(("field", "value"), [
    ("url", "http://127.0.0.1:1234/v1"), ("endpoint", "http://127.0.0.1:1234/v1"),
    ("api_key", "test-key"), ("secret", "env:TEST_KEY"), ("auth_file", os.devnull),
    ("headers", {}), ("on", "local"),
])
def test_host_config_refuses_address_auth_and_host_placement(field, value):
    config = _config()
    config["connections"]["child-chat"][field] = value
    with pytest.raises(ValidationError):
        HarnessConfig.model_validate(config)


@pytest.mark.parametrize(("section", "field", "value"), [
    ("connection", "endpoint", "http://127.0.0.1:1234/v1"),
    ("connection", "secret", "env:TEST_KEY"),
    ("connection", "api_key", "test-key"),
    ("connection", "auth_file", os.devnull),
    ("preset", "anthropic_base_url", "http://127.0.0.1:1234/v1"),
    ("preset", "required_request_headers", ["Authorization"]),
    ("route", "url", "http://127.0.0.1:1234/v1"),
    ("prompt", "parts", [{"type": "image", "url": "test"}]),
])
async def test_child_refuses_injected_fields(tmp_path, section, field, value):
    _write_policy(tmp_path, {"desk": {"url": "http://127.0.0.1:1234/v1", "models": ["tiny"]}})
    config = HarnessConfig.model_validate(_config())
    payload = _turn_payload(config)
    payload[section][field] = value
    sent = []

    async def send(stream, kind, body):
        sent.append((stream, kind, body))

    turns = ChildTurns(tmp_path, send, None)
    await turns.handle(Frame(1, "turn", payload))
    [(stream, kind, body)] = sent
    assert (stream, kind) == (1, "error")
    assert ("support only text chat" if section == "prompt" else "refuse host-supplied URLs") in body["message"]


def test_host_alias_training_policy_applies_before_turn(tmp_path):
    config = _config(data_policy="unknown")
    config["models"]["dev-chat"]["allow_cwd_override"] = True
    config["models"]["dev-chat"]["cwd_allowed_roots"] = [str(tmp_path)]
    with pytest.raises(ValidationError, match="Acknowledge workspace in training_acks"):
        HarnessConfig.model_validate(config)
