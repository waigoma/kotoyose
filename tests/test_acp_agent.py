import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from kotoyose.acp_agent import AcpAgent


KEY = "fake-kotoyose-secret"


class Wire:
    def __init__(self):
        self.reader = asyncio.StreamReader()
        self.output = asyncio.Queue()

    def send(self, method, params=None, request_id=None):
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if request_id is not None:
            message["id"] = request_id
        self.reader.feed_data(json.dumps(message).encode() + b"\n")

    async def receive(self):
        message = await asyncio.wait_for(self.output.get(), timeout=0.5)
        assert message["jsonrpc"] == "2.0"
        return message

    async def request(self, method, params=None, request_id=1):
        self.send(method, params, request_id)
        message = await self.receive()
        assert message["id"] == request_id
        return message

    async def new_session(self):
        reply = await self.request("session/new", {"cwd": "/workspace", "mcpServers": []})
        return reply["result"]


@asynccontextmanager
async def agent(handler, **options):
    wire = Wire()
    async with httpx.AsyncClient(
        base_url="http://kotoyose.test", transport=httpx.MockTransport(handler),
    ) as http:
        task = asyncio.create_task(AcpAgent(http, KEY, wire.output.put_nowait, **options).serve(wire.reader))
        try:
            yield wire
        finally:
            wire.reader.feed_eof()
            await asyncio.wait_for(task, timeout=0.5)


def models(request):
    assert request.method == "GET"
    assert request.url.path == "/v1/models"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    return httpx.Response(200, json={"data": [{"id": "one"}, {"id": "two"}]})


def sse(event):
    data = "[DONE]" if event == "[DONE]" else json.dumps(event)
    return f"data: {data}\n\n".encode()


def text_chunk(text):
    return {"object": "chat.completion.chunk", "choices": [{"delta": {"content": text}}]}


def prompt(session_id, text):
    return {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]}


async def test_session_model_and_streamed_prompt():
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return models(request)
        assert request.url.path == "/v1/chat/completions"
        tool_id = f"call-{len(calls)}"
        return httpx.Response(200, content=b"".join(map(sse, [
            text_chunk("ab"),
            {"object": "kotoyose.event", "kind": "tool_call", "tool_call_id": tool_id, "title": "Read: a.py",
             "tool_kind": "read", "status": "in_progress"},
            {"object": "ignored", "choices": [{"delta": {"content": "ignored"}}]},
            text_chunk("cd"),
            {"object": "kotoyose.event", "kind": "tool_call_update", "tool_call_id": tool_id, "status": "completed"},
            {"usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7, "cost_usd": 0.1}},
            "[DONE]",
        ])))

    async with agent(handler) as wire:
        init = (await wire.request("initialize"))["result"]
        assert init["protocolVersion"] == 1
        assert init["agentCapabilities"] == {
            "loadSession": False,
            "promptCapabilities": {"image": True, "audio": False, "embeddedContext": True},
            "mcpCapabilities": {"http": False, "sse": False},
        }
        assert init["agentInfo"]["version"]
        assert init["authMethods"] == []
        new = await wire.new_session()
        sid = new["sessionId"]
        assert new["models"] == {
            "availableModels": [{"modelId": "one", "name": "one"}, {"modelId": "two", "name": "two"}],
            "currentModelId": "one",
        }
        option, = new["configOptions"]
        assert option == {
            "id": "model", "name": "Model", "category": "model", "type": "select",
            "currentValue": "one", "options": [{"value": "one", "name": "one"}, {"value": "two", "name": "two"}],
        }
        params = {"sessionId": sid}
        invalid = await wire.request("session/set_model", {**params, "modelId": "missing"})
        assert invalid["error"]["code"] == -32602
        switched = await wire.request("session/set_model", {**params, "modelId": "two"})
        assert switched["result"] == {}
        for request_id, question in enumerate(("first", "second"), start=2):
            wire.send("session/prompt", prompt(sid, question), request_id)
            updates = []
            for _ in range(4):
                message = await wire.receive()
                assert message["method"] == "session/update"
                assert message["params"]["sessionId"] == sid
                updates.append(message["params"]["update"])
            assert updates == [
                {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "ab"}},
                {"sessionUpdate": "tool_call", "status": "in_progress", "toolCallId": f"call-{request_id}",
                 "title": "Read: a.py", "kind": "read"},
                {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "cd"}},
                {"sessionUpdate": "tool_call_update", "toolCallId": f"call-{request_id}", "status": "completed"},
            ]
            reply = await wire.receive()
            assert reply["id"] == request_id
            assert reply["result"] == {
                "stopReason": "end_turn", "usage": {"inputTokens": 3, "outputTokens": 4, "totalTokens": 7},
                "_meta": {"kotoyose": {"cost_usd": 0.1}},
            }
        configured = await wire.request(
            "session/set_config_option", {**params, "configId": "model", "value": "one"},
        )
        assert configured["result"]["configOptions"] == [option]

    _, first, second = calls
    assert first.headers["Authorization"] == f"Bearer {KEY}"
    for request in (first, second):
        assert request.headers["X-Conversation-Key"] == f"acp:{sid}:two"
        assert request.headers["X-Kotoyose-Events"] == "1"
    assert json.loads(first.content) == {
        "model": "two", "stream": True, "messages": [{"role": "user", "content": "first"}],
    }
    assert json.loads(second.content)["messages"] == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "abcd"},
        {"role": "user", "content": "second"},
    ]


async def test_multimodal_prompt_and_cwd():
    def handler(request):
        if request.method == "GET":
            return models(request)
        assert json.loads(request.content) == {
            "model": "two", "stream": True, "cwd": "/workspace",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}},
                {"type": "text", "text": "file:///one\nnotes"},
                {"type": "text", "text": "two: file:///two"},
            ]}],
        }
        return httpx.Response(200, content=sse("[DONE]"))

    async with agent(handler, model="two", forward_cwd=True) as wire:
        new = await wire.new_session()
        assert new["models"]["currentModelId"] == "two"
        response = await wire.request("session/prompt", {"sessionId": new["sessionId"], "prompt": [
            {"type": "text", "text": "look"},
            {"type": "image", "mimeType": "image/png", "data": "YQ=="},
            {"type": "resource", "resource": {"uri": "file:///one", "text": "notes"}},
            {"type": "resource_link", "uri": "file:///two", "name": "two"},
        ]})
        assert response["result"] == {"stopReason": "end_turn"}


class HangingStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        yield sse(text_chunk("partial"))
        await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


async def test_cancel_closes_stream_and_preserves_only_user_turn():
    stream = HangingStream()

    def handler(request):
        if request.method == "GET":
            return models(request)
        messages = json.loads(request.content)["messages"]
        if len(messages) == 1:
            return httpx.Response(200, stream=stream)
        assert messages == [{"role": "user", "content": text} for text in ("first", "next")]
        return httpx.Response(200, content=sse("[DONE]"))

    async with agent(handler) as wire:
        sid = (await wire.new_session())["sessionId"]
        wire.send("session/prompt", prompt(sid, "first"), 2)
        assert (await wire.receive())["method"] == "session/update"
        competing = await wire.request("session/prompt", prompt(sid, "competing"), 3)
        assert "already in progress" in competing["error"]["message"]
        wire.send("session/cancel", {"sessionId": sid})
        reply = await wire.receive()
        assert reply == {"jsonrpc": "2.0", "id": 2, "result": {"stopReason": "cancelled"}}
        assert stream.closed
        assert (await wire.request("session/prompt", prompt(sid, "next")))["result"]["stopReason"] == "end_turn"


async def test_eof_closes_active_stream():
    stream = HangingStream()

    async with agent(lambda request: models(request) if request.method == "GET" else
                     httpx.Response(200, stream=stream)) as wire:
        sid = (await wire.new_session())["sessionId"]
        wire.send("session/prompt", prompt(sid, "first"), 2)
        assert (await wire.receive())["method"] == "session/update"
    assert stream.closed


async def test_rpc_errors_and_parse_recovery():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": KEY}})

    async with agent(handler) as wire:
        wire.reader.feed_data(b"{malformed\n")
        error = await wire.receive()
        assert error["error"]["code"] == -32700
        assert error["id"] is None
        assert (await wire.request("missing"))["error"]["code"] == -32601
        failure = await wire.request("session/new", {"cwd": "/workspace"})
        message = failure["error"]["message"]
        assert "401" in message and "API key was rejected" in message
        assert KEY not in message


async def test_chat_http_error_redacts_key_and_limits_message():
    def handler(request):
        if request.method == "GET":
            return models(request)
        return httpx.Response(403, json={"error": {"message": f"denied {KEY} " + "a" * 700}})

    async with agent(handler) as wire:
        sid = (await wire.new_session())["sessionId"]
        failure = await wire.request("session/prompt", prompt(sid, "hello"))
        message = failure["error"]["message"]
        assert "403" in message and "denied [redacted]" in message
        assert KEY not in message and len(message) == 500


def models_with_effort(request):
    assert request.method == "GET"
    return httpx.Response(200, json={"data": [
        {"id": "one", "reasoning_efforts": {"supported": ["low", "high"], "default": "low"}},
        {"id": "two"},
        {"id": "three", "reasoning_efforts": {"supported": ["low"], "default": None}},
    ]})


def _effort_option(current, supported):
    return {
        "id": "effort", "name": "Effort", "category": "thought_level", "type": "select",
        "currentValue": current,
        "options": [{"value": "default", "name": "Default"}, *[{"value": value, "name": value} for value in supported]],
    }


async def test_effort_option_follows_alias_and_request():
    """Effort is selectable, reset when the next alias cannot use it, and sent on the chat request."""
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return models_with_effort(request)
        return httpx.Response(200, content=sse("[DONE]"))

    async with agent(handler) as wire:
        new = await wire.new_session()
        sid = new["sessionId"]
        model_option = {
            "id": "model", "name": "Model", "category": "model", "type": "select", "currentValue": "one",
            "options": [{"value": alias, "name": alias} for alias in ("one", "two", "three")],
        }
        assert new["configOptions"] == [model_option, _effort_option("default", ["low", "high"])]
        configured = await wire.request("session/set_config_option", {
            "sessionId": sid, "configId": "effort", "value": "high",
        })
        assert configured["result"]["configOptions"][1]["currentValue"] == "high"
        unknown = await wire.request("session/set_config_option", {
            "sessionId": sid, "configId": "effort", "value": "max",
        })
        assert unknown["error"]["code"] == -32602
        await wire.request("session/prompt", prompt(sid, "think"))
        wire.send("session/set_model", {"sessionId": sid, "modelId": "three"}, 4)
        update = await wire.receive()
        assert update["method"] == "session/update"
        assert update["params"]["update"] == {
            "sessionUpdate": "config_option_update",
            "configOptions": [
                {**model_option, "currentValue": "three"},
                _effort_option("default", ["low"]),
            ],
        }
        switched = await wire.receive()
        assert switched["id"] == 4 and switched["result"] == {}
        kept = await wire.request("session/set_config_option", {
            "sessionId": sid, "configId": "effort", "value": "low",
        })
        assert kept["result"]["configOptions"][1]["currentValue"] == "low"
        wire.send("session/set_model", {"sessionId": sid, "modelId": "two"}, 5)
        cleared = await wire.receive()
        assert cleared["params"]["update"]["configOptions"] == [{**model_option, "currentValue": "two"}]
        assert (await wire.receive())["result"] == {}
        missing = await wire.request("session/set_config_option", {
            "sessionId": sid, "configId": "effort", "value": "low",
        })
        assert missing["error"]["code"] == -32602
        await wire.request("session/prompt", prompt(sid, "plain"))

    chat_calls = [call for call in calls if call.method != "GET"]
    first, second = chat_calls
    assert first.headers["X-Conversation-Key"] == f"acp:{sid}:one:high"
    assert json.loads(first.content)["reasoning_effort"] == "high"
    assert second.headers["X-Conversation-Key"] == f"acp:{sid}:two"
    assert "reasoning_effort" not in json.loads(second.content)


async def test_failed_prompt_is_not_resent():
    sent = []

    def handler(request):
        if request.method == "GET":
            return models(request)
        sent.append(json.loads(request.content)["messages"])
        if len(sent) == 1:
            return httpx.Response(503, json={"error": {"message": "busy"}})
        return httpx.Response(200, content=sse("[DONE]"))

    async with agent(handler) as wire:
        sid = (await wire.new_session())["sessionId"]
        failed = await wire.request("session/prompt", prompt(sid, "first"), 2)
        assert "HTTP 503: busy" in failed["error"]["message"]
        assert (await wire.request("session/prompt", prompt(sid, "again"), 3))["result"]["stopReason"] == "end_turn"
    assert sent[1] == [{"role": "user", "content": "again"}]


def test_api_key_from_env_or_private_file(tmp_path):
    from kotoyose.acp_agent import _api_key

    assert _api_key({"KOTOYOSE_API_KEY": "kys_env"}) == "kys_env"
    key = tmp_path / "key"
    key.write_text("kys_file\n")
    key.chmod(0o600)
    assert _api_key({"KOTOYOSE_API_KEY_FILE": str(key)}) == "kys_file"
    key.chmod(0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        _api_key({"KOTOYOSE_API_KEY_FILE": str(key)})
    with pytest.raises(ValueError, match="cannot be read"):
        _api_key({"KOTOYOSE_API_KEY_FILE": str(tmp_path / "missing")})
    with pytest.raises(ValueError, match="required"):
        _api_key({})
