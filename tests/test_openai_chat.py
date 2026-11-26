"""Chat Completions 互換の上流を実際の chat route から検証する。"""

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from kotoyose import app as app_module
from kotoyose import config, model_discovery, session
from kotoyose.app import app
from kotoyose.config import HarnessConfig, TrainingAck
from kotoyose.data_policy import training_digest
from kotoyose.routes import chat
from kotoyose.runtime.base import RuntimeChatHistory
from kotoyose.runtime.child import _prompt_payload
from kotoyose.runtime.claude import _claude_query_input
from kotoyose.runtime.openai_chat import OpenAIChatSession
from tests.fixtures.api_keys import TEST_API_HEADERS

KEY = "test-upstream-bearer-secret"
ENDPOINT = "https://gateway.invalid/api/v1/"
MESSAGES = [
    {"role": "system", "content": "Client instructions"},
    {"role": "user", "content": [{"type": "text", "text": "Earlier question"}]},
    {"role": "assistant", "content": "Earlier answer"},
    {"role": "user", "content": "Latest question"},
]


def settings():
    return {
        "connections": {"gateway": {
            "provider": "openai_compat", "kind": "endpoint",
            "endpoint": ENDPOINT, "secret": "env:TEST_OPENAI_COMPAT_KEY",
        }},
        "routes": {"remote": {"driver": "openai.chat", "connection": "gateway", "model": "vendor/model"}},
        "models": {"remote": {"route": "remote", "timeout_seconds": 2}},
    }


@pytest.fixture
async def upstream(monkeypatch, client_api_key, tmp_path):
    values = settings()
    prompt = tmp_path / "instructions.md"
    prompt.write_text("Owner instructions")
    values["prompt_files"] = {"instructions": {"path": str(prompt)}}
    values["models"]["remote"]["prompt_file_refs"] = ["instructions"]
    cfg = HarnessConfig.model_validate(values)
    monkeypatch.setattr(config, "_config", cfg)
    monkeypatch.setenv("TEST_OPENAI_COMPAT_KEY", KEY)
    send = AsyncMock()
    original_client = httpx.AsyncClient

    # Inject the transport while keeping the runtime's real authorization and client setup.
    def client_with_transport(*args, **kwargs):
        return original_client(*args, **kwargs, transport=httpx.MockTransport(send))

    monkeypatch.setattr("kotoyose.runtime.openai_chat.httpx.AsyncClient", client_with_transport)
    completed = AsyncMock()
    monkeypatch.setattr(chat, "record_completed", completed)
    monkeypatch.setattr(chat, "record_received", AsyncMock(return_value=None))
    monkeypatch.setattr(chat, "record_failed", AsyncMock())
    async with original_client(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as http:
        yield http, send, completed, cfg


async def post(http, **options):
    return await http.post(
        "/v1/chat/completions",
        headers={**TEST_API_HEADERS, "X-Kotoyose-Events": "1"},
        json={"model": "remote", "messages": MESSAGES, **options},
    )


def sse(*events):
    return "".join(
        f"data: {event if isinstance(event, str) else json.dumps(event)}\n\n" for event in events
    )


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"]


def streamed_text(chunks):
    return "".join(chunk["choices"][0]["delta"].get("content") or "" for chunk in chunks if "choices" in chunk)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("upstream_cost", [{"cost": 0.125}, {"cost_usd": 0}, {}, {"cost": "0.125"}])
async def test_text_history_usage_and_redaction(upstream, caplog, stream, upstream_cost):
    http, send, completed, cfg = upstream
    cost = next(iter(upstream_cost.values()), None)
    if isinstance(cost, str):
        cost = None
    usage = {"prompt_tokens": 42, "completion_tokens": 17, **upstream_cost}
    session_keys = set(session.sessions)
    if stream:
        send.return_value = httpx.Response(200, text=sse(
            {"choices": [{"delta": {"content": "Reply " + KEY[:11]}}]},
            {"choices": [{"delta": {"content": KEY[11:] + " done"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": usage},
            "[DONE]",
        ), headers={"Content-Type": "text/event-stream"})
    else:
        send.return_value = httpx.Response(200, json={
            "choices": [{"message": {"content": f"Reply {KEY} done"}}], "usage": usage,
        })

    caplog.set_level("DEBUG", logger="httpx")
    with capture_logs() as logs:
        response = await post(http, stream=stream)
    assert response.status_code == 200, response.text
    assert cfg.connections["gateway"].data_policy == "unknown"
    assert KEY not in response.text + str(logs) + caplog.text
    request = send.await_args.args[0]
    assert request.method == "POST"
    assert str(request.url) == "https://gateway.invalid/api/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert "x-api-key" not in request.headers
    assert request.extensions["timeout"]["read"] == 2
    assert json.loads(request.content) == {
        "model": "vendor/model", "stream": stream,
        "messages": [
            {"role": "system", "content": "Owner instructions"},
            MESSAGES[0], {"role": "user", "content": "Earlier question"},
            *MESSAGES[2:],
        ],
        **({"stream_options": {"include_usage": True}} if stream else {}),
    }
    if stream:
        assert response.text.endswith("data: [DONE]\n\n")
        data = events(response)
        answer = streamed_text(data)
        output_usage = data[-1]["usage"]
    else:
        data = response.json()
        answer = data["choices"][0]["message"]["content"]
        output_usage = data["usage"]
    assert answer == "Reply [REDACTED] done"
    assert (output_usage["prompt_tokens"], output_usage["completion_tokens"], output_usage["cost_usd"]) == (42, 17, cost)
    persisted = completed.await_args.kwargs
    assert (persisted["tokens_in"], persisted["tokens_out"], persisted["tokens_source"]) == (42, 17, "sdk")
    assert persisted.get("metadata") == ({"cost_usd": cost} if cost is not None else None)
    assert persisted["response"] == answer
    assert set(session.sessions) == session_keys


async def test_secret_store_key_is_resolved_for_chat(upstream, monkeypatch):
    http, send, _, cfg = upstream
    cfg.connections["gateway"].secret = "gateway-key"
    lookup = AsyncMock(return_value=KEY)
    monkeypatch.setattr(session, "resolve_secret_async", lookup)
    send.return_value = httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    assert (await post(http)).status_code == 200
    lookup.assert_awaited_once_with("gateway-key")
    assert send.await_args.args[0].headers["Authorization"] == f"Bearer {KEY}"


@pytest.mark.parametrize(("failure", "status", "code"), [
    (401, 401, "authentication_failed"),
    (403, 401, "authentication_failed"),
    (429, 429, "provider_usage_limit"),
    (400, 502, "provider_error"),
    (500, 502, "provider_error"),
    (302, 502, "provider_error"),
    (httpx.ConnectError, 503, "service_unavailable"),
    (httpx.ReadTimeout, 504, "timeout"),
])
@pytest.mark.parametrize("stream", [False, True])
async def test_upstream_errors_are_sanitized(upstream, stream, failure, status, code):
    http, send, completed, _ = upstream
    private = f"{KEY} https://username:password@gateway.example"
    if isinstance(failure, int):
        send.return_value = httpx.Response(failure, json={"error": {"message": private}}, headers={"location": ENDPOINT})
    else:
        send.side_effect = failure(private)
    with capture_logs() as logs:
        response = await post(http, stream=stream)
    assert response.status_code == (200 if stream else status)
    error = events(response)[-1]["error"] if stream else response.json()["error"]
    assert error["code"] == code
    assert KEY not in response.text + str(logs)
    assert "username:password" not in response.text + str(logs)
    send.assert_awaited_once()
    completed.assert_not_awaited()


@pytest.mark.parametrize("options", [
    {"tools": [{"type": "function", "function": {"name": "test"}}]},
    {"functions": [{"name": "test"}]},
    {"function_call": "none"},
    {"function_call": {}},
    {"tool_choice": "auto"},
    {"tool_choice": {"type": "function", "function": {"name": "test"}}},
    {"tools": [{"type": "function"}], "tool_choice": "none"},
    {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://image.invalid/"}}]}]},
])
async def test_non_text_requests_return_400_before_upstream(upstream, options):
    http, send, _, _ = upstream
    response = await post(http, **options)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    send.assert_not_awaited()


@pytest.mark.parametrize("options", [
    {"tools": [], "functions": [], "tool_choice": "none", "function_call": None},
    {"tools": None, "functions": None, "tool_choice": None},
])
async def test_disabled_tool_fields_are_accepted(upstream, options):
    http, send, _, _ = upstream
    send.return_value = httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    assert (await post(http, **options)).status_code == 200
    assert set(json.loads(send.await_args.args[0].content)) == {"model", "stream", "messages"}


async def test_developer_role_and_empty_assistant_history_are_normalized(upstream):
    http, send, _, _ = upstream
    send.return_value = httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    messages = [
        {"role": "developer", "content": "Developer instructions"},
        *MESSAGES[:2],
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": []},
        {"role": "assistant", "content": " \t"},
        *MESSAGES[2:],
    ]
    assert (await post(http, messages=messages)).status_code == 200
    assert json.loads(send.await_args.args[0].content)["messages"] == [
        {"role": "system", "content": "Owner instructions"},
        {"role": "system", "content": "Developer instructions"},
        MESSAGES[0], {"role": "user", "content": "Earlier question"}, *MESSAGES[2:],
    ]


@pytest.mark.parametrize("role", ["tool", "function", "unknown"])
async def test_unsupported_roles_are_rejected_locally(upstream, role):
    http, send, _, _ = upstream
    response = await post(http, messages=[{"role": role, "content": "earlier"}, MESSAGES[-1]])
    assert response.status_code == 400
    send.assert_not_awaited()


@pytest.mark.parametrize("content", ["", " \n", [], [{"type": "text", "text": ""}]])
async def test_empty_final_user_message_is_rejected_locally(upstream, content):
    http, send, _, _ = upstream
    response = await post(http, messages=[*MESSAGES, {"role": "user", "content": content}, MESSAGES[2]])
    assert response.status_code == 400
    send.assert_not_awaited()


@pytest.mark.parametrize(("field", "value"), [
    ("tools", []),
    ("mcp_refs", ["kotoyose"]),
    ("mcp_servers", {"demo": {"type": "stdio", "command": "unused"}}),
    ("agent", {"place": "host", "surface": "none"}),
    ("place", "jail"),
    ("execution", "native"),
    ("execution_environment", "isolated"),
    ("on", "child:remote"),
])
def test_alias_rejects_unsupported_config(field, value):
    values = settings()
    values["models"]["remote"][field] = value
    with pytest.raises(ValidationError, match=field):
        HarnessConfig.model_validate(values)


@pytest.mark.parametrize(("updates", "message"), [
    ({"endpoint": "https://private-user:private-password@gateway.example/v1"}, "userinfo"),
    ({"endpoint": "http://private-user:private-password@localhost/v1"}, "userinfo"),
    ({"endpoint": "http://gateway.invalid/v1"}, "requires HTTPS"),
    ({"endpoint": "http://128.0.0.1/v1"}, "requires HTTPS"),
    ({"endpoint": "http://127.0.0.1.gateway.invalid/v1"}, "requires HTTPS"),
    ({"endpoint": "http://[::ffff:127.0.0.1]/v1"}, "requires HTTPS"),
    ({"endpoint": "https://gateway.invalid/v1?key=private-password"}, "query"),
    ({"endpoint": "https://gateway.invalid/v1#private-password"}, "fragment"),
    ({"secret": None}, "requires secret"),
    ({"kind": "api_key", "endpoint": None}, "requires kind: endpoint"),
    ({"on": "child:remote"}, "on: local"),
])
def test_connection_rejects_invalid_config_without_echoing_url(updates, message):
    values = settings()
    values["connections"]["gateway"].update(updates)
    with pytest.raises(ValidationError, match=message) as error:
        HarnessConfig.model_validate(values)
    assert "private-user" not in str(error.value)
    assert "private-password" not in str(error.value)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.255.255.254", "[::1]", "localhost"])
def test_http_endpoints_allow_loopback(host):
    values = settings()
    endpoint = f"http://{host}:8080/v1"
    values["connections"]["gateway"]["endpoint"] = endpoint
    assert HarnessConfig.model_validate(values).connections["gateway"].endpoint == endpoint


@pytest.mark.parametrize("failure", [False, True])
async def test_discovery_uses_bearer_and_falls_back_to_manual_entry(upstream, monkeypatch, failure):
    _, _, _, cfg = upstream
    model_discovery.clear_connection_discovery_cache()

    async def fetch(url, headers, *, follow_redirects):
        assert url == "https://gateway.invalid/api/v1/models"
        assert headers == {"Authorization": f"Bearer {KEY}"}
        assert follow_redirects is False
        if failure:
            raise OSError(KEY)
        return json.dumps({"data": [{"id": "vendor/model"}, {"id": KEY}]}).encode()

    monkeypatch.setattr(model_discovery, "_fetch_url", fetch)
    try:
        with capture_logs() as logs:
            result = await model_discovery.discover_models("openai_compat", preset=cfg.preset_for("remote"))
        assert KEY not in str(result) + str(logs)
        if failure:
            assert result["models"] == []
            assert "手入力" in result["warning"]
        else:
            assert result["models"] == ["vendor/model"]
    finally:
        model_discovery.clear_connection_discovery_cache()


async def test_stream_enforces_idle_timeout(upstream):
    http, send, completed, cfg = upstream
    cfg.models["remote"].timeout_seconds = 0.05
    cfg.credential_leak_guard = False

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield sse({"choices": [{"delta": {"content": "partial reply"}}]}).encode()
            await asyncio.sleep(1)
            yield sse("[DONE]").encode()

    send.return_value = httpx.Response(200, stream=SlowStream())
    response = await post(http, stream=True)
    assert response.status_code == 200
    assert "partial reply" in response.text
    assert events(response)[-1]["error"]["code"] == "timeout"
    completed.assert_not_awaited()


async def test_stream_can_outlast_idle_timeout_while_receiving_lines(upstream):
    http, send, completed, cfg = upstream
    cfg.models["remote"].timeout_seconds = 0.20

    class ActiveStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices": [{"delta": {"content": "reply"}}]}\n'
            for _ in range(4):
                await asyncio.sleep(0.07)
                yield b": heartbeat\n"
            yield b"\n" + sse("[DONE]").encode()

    send.return_value = httpx.Response(200, stream=ActiveStream())
    response = await post(http, stream=True)
    assert response.status_code == 200
    assert streamed_text(events(response)) == "reply"
    completed.assert_awaited_once()


async def test_stream_flushes_unterminated_event_at_eof(upstream):
    http, send, completed, _ = upstream
    send.return_value = httpx.Response(200, text=sse({
        "choices": [{"delta": {"content": "final reply"}, "finish_reason": "stop"}],
    }).rstrip("\n"))
    response = await post(http, stream=True)
    assert response.status_code == 200
    assert streamed_text(events(response)) == "final reply"
    completed.assert_awaited_once()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(("reason", "diagnostic_reason", "success"), [
    ("stop", "stop", True),
    ("length", "length", True),
    ("content_filter", "content_filter", False),
    ("tool_calls", "tool_calls", False),
    (KEY, "unknown", False),
])
async def test_finish_reason_is_classified_and_recorded(upstream, stream, reason, diagnostic_reason, success):
    http, send, completed, _ = upstream
    payload = {"choices": [{
        "delta" if stream else "message": {"content": "reply" if success else None},
        "finish_reason": reason,
    }]}
    send.return_value = httpx.Response(200, text=sse(payload, "[DONE]")) if stream else httpx.Response(200, json=payload)
    with capture_logs() as logs:
        response = await post(http, stream=stream)
    finish = next(item for item in logs if item["event"] == "openai_chat.finish")
    assert finish["finish_reason"] == diagnostic_reason
    assert finish["truncated"] is (reason == "length")
    assert KEY not in str(logs) + response.text
    if success:
        assert response.status_code == 200
        assert "reply" in response.text
        completed.assert_awaited_once()
    else:
        assert response.status_code == (200 if stream else 502)
        error = events(response)[-1]["error"] if stream else response.json()["error"]
        assert error["code"] == "provider_error"
        diagnostics = chat.record_failed.await_args.kwargs["metadata"]["provider_failure"]
        assert diagnostics["finish_reason"] == diagnostic_reason
        completed.assert_not_awaited()


@pytest.mark.parametrize(("stream", "expected"), [(False, 300), (True, 60)])
async def test_default_http_timeouts(upstream, stream, expected):
    http, send, _, cfg = upstream
    cfg.models["remote"].timeout_seconds = None
    send.return_value = httpx.Response(200, text=sse("[DONE]")) if stream else httpx.Response(
        200, json={"choices": [{"message": {"content": "ok"}}]},
    )
    assert (await post(http, stream=stream)).status_code == 200
    timeouts = send.await_args.args[0].extensions["timeout"]
    assert timeouts["connect"] == timeouts["read"] == expected


async def test_ask_enforces_configured_total_timeout(upstream):
    http, send, completed, cfg = upstream
    cfg.models["remote"].timeout_seconds = 0.05

    async def wait_forever(_):
        await asyncio.Event().wait()

    send.side_effect = wait_forever
    response = await asyncio.wait_for(post(http), timeout=2)
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "timeout"
    completed.assert_not_awaited()


def test_other_adapters_fall_back_to_plain_text_for_chat_history():
    prompt = RuntimeChatHistory((("user", "question"), ("assistant", "reply"), ("user", "continue")))
    assert _claude_query_input(prompt) == "question\nreply\ncontinue"
    assert _prompt_payload(prompt) == {"text": "question\nreply\ncontinue"}


async def test_restricted_connection_includes_acknowledged_skill_text(upstream, tmp_path, monkeypatch):
    http, send, _, cfg = upstream
    plugin = tmp_path / "plugin"
    manifest = plugin / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"name":"demo"}')
    skill = plugin / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Approved skill text")
    values = cfg.model_dump()
    values["skill_definitions"] = {"demo": {"plugin": str(plugin), "skill": "demo"}}
    with_skill = HarnessConfig.model_validate(values)
    with_skill.models["remote"].skill_refs = ["demo"]
    with pytest.raises(ValidationError, match="Acknowledge skills"):
        HarnessConfig.model_validate(with_skill.model_dump())
    with_skill.training_acks = [TrainingAck(
        alias="remote", capabilities=["skills"], reasons=["unknown"],
        digest=training_digest(with_skill, "remote", ["skills"]), acknowledged_by="owner",
        at="2026-11-22T00:00:00Z", note="Approved to share this skill",
    )]
    monkeypatch.setattr(config, "_config", HarnessConfig.model_validate(with_skill.model_dump()))
    send.return_value = httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    assert (await post(http)).status_code == 200
    assert json.loads(send.await_args.args[0].content)["messages"][0] == {
        "role": "system", "content": "Owner instructions\nApproved skill text",
    }


async def test_http_logs_exclude_upstream_reason_phrase_and_headers(caplog, monkeypatch):
    async def upstream_response(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length_line = next(line for line in headers.splitlines() if line.lower().startswith(b"content-length:"))
            await reader.readexactly(int(length_line.split(b":", 1)[1]))
            writer.write(f"HTTP/1.1 500 {KEY}\r\nX-Echo-Key: {KEY}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.NOTSET, logger="httpcore")
    caplog.set_level(logging.NOTSET, logger="httpx")
    monkeypatch.setattr("kotoyose.logging_setup.LOG_LEVEL", "DEBUG")
    app_module.configure_logging()
    async with await asyncio.start_server(upstream_response, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        runtime = OpenAIChatSession(f"http://127.0.0.1:{port}/v1/chat/completions", KEY, "demo", None, 2)
        with pytest.raises(RuntimeError, match="Chat endpoint rejected the request"):
            await runtime.ask("hello")
    assert "HTTP Request: POST" not in caplog.text
    assert KEY not in caplog.text


async def test_unavailable_credential_does_not_contact_upstream(upstream, monkeypatch):
    http, send, completed, _ = upstream
    monkeypatch.delenv("TEST_OPENAI_COMPAT_KEY")
    response = await post(http)
    assert response.status_code == 503
    assert response.json()["error"]["message"] == "Chat endpoint credential is unavailable or invalid"
    send.assert_not_awaited()
    completed.assert_not_awaited()
