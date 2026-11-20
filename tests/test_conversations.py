"""明示履歴、隔離、tool continuation、予算と終端競合。live provider は呼ばない。"""

import asyncio
import copy
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from kotoyose.app import app
from kotoyose.conversation.contracts import Config, Continue, Limits, Model, Start, ToolResult
from kotoyose.conversation.ports import Delta, Message, Round, ToolCall, ToolResult as ProviderToolResult
from kotoyose.conversation.provider import AnthropicProvider
from kotoyose.conversation.service import Conversations
from kotoyose.execution_resources import ExecutionError

from tests.fixtures.api_keys import fixture_key_store


def start(**changes):
    return Start.model_validate({"schema_version": "1", "model": "chat", "instructions": "Speak Japanese.",
        "messages": [{"role": "user", "content": "Remember amber."},
                     {"role": "assistant", "content": "Amber remembered."},
                     {"role": "user", "content": "What did I say?"}],
        "max_output_tokens": 100, **changes})


class Scripted:
    def __init__(self, rounds):
        self.rounds, self.requests = list(rounds), []

    async def stream(self, max_tokens, tool_results=()):
        self.requests.append((max_tokens, copy.deepcopy(tool_results)))
        for event in self.rounds.pop(0):
            yield event


async def collect(service, execution):
    return [event async for event in service.events(execution)]


def tool_round():
    return Round((ToolCall("call-1", "avatar.set_expression", {"name": "happy"}),), 10)


def with_tool():
    return start(tools=[{"name": "avatar.set_expression", "description": "Set expression", "input_schema": {
        "type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"], "additionalProperties": False}}])


@pytest.mark.asyncio
async def test_full_ordered_history_and_fresh_isolated_executions():
    inputs, providers = [], []

    def factory(request):
        inputs.append(request.model_dump())
        value = Scripted([[Delta("琥珀"), Round((), 3)]])
        providers.append(value)
        return value

    service = Conversations(Limits(), factory)
    first, second = service.start("caller", start()), service.start("caller", start())
    assert first.id != second.id and first.provider is not second.provider
    events = await collect(service, first)
    await collect(service, second)
    assert events == [{"type": "accepted", "execution_id": first.id}, {"type": "delta", "text": "琥珀"}, {"type": "completed"}]
    assert inputs == [start().model_dump(), start().model_dump()]
    assert [value.requests for value in providers] == [[(100, ())], [(100, ())]]


@pytest.mark.asyncio
async def test_tool_continuation_appends_only_new_results_and_reserves_remaining_tokens():
    provider = Scripted([[tool_round()], [Delta("done"), Round((), 2)]])
    service = Conversations(Limits(), lambda _: provider)
    execution = service.start("caller", with_tool())
    events = await collect(service, execution)
    assert events[-1]["type"] == "tool_calls"
    result = Continue(schema_version="1", round=1, results=[{"call_id": "call-1", "output": {"accepted": True}}])
    service.continue_tools("caller", execution.id, result)
    with pytest.raises(ExecutionError, match="pending round"):
        service.continue_tools("caller", execution.id, result)
    assert (await collect(service, execution))[-1] == {"type": "completed"}
    assert provider.requests == [(100, ()), (90, (ProviderToolResult("call-1", {"accepted": True}),))]


@pytest.mark.asyncio
async def test_previous_round_close_does_not_cancel_running_continuation():
    provider = Scripted([[tool_round()], [Delta("done"), Round((), 2)]])
    service = Conversations(Limits(), lambda _: provider)
    execution = service.start("caller", with_tool())
    first = service.events(execution)
    await anext(first)
    assert (await anext(first))["type"] == "tool_calls"
    service.continue_tools("caller", execution.id, Continue(schema_version="1", round=1,
        results=[{"call_id": "call-1", "output": {}}]))
    second = service.events(execution)
    assert await anext(second) == {"type": "delta", "text": "done"}
    await first.aclose()
    assert execution.state == "running" and execution.task is asyncio.current_task()
    assert await anext(second) == {"type": "completed"}
    await second.aclose()
    assert execution.state == "completed" and execution.task is None


def test_state_machine_imports_without_http_or_provider_implementation():
    """独立プロセスで外側の import を拒否し、推移的な依存も検出する。"""
    result = subprocess.run([sys.executable, "-c", '''
import importlib.abc
import sys

class RejectAdapters(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"httpx", "fastapi"} or fullname == "kotoyose.conversation.provider":
            raise ImportError("State machine imported an adapter: " + fullname)

sys.meta_path.insert(0, RejectAdapters())
import kotoyose.conversation.service
'''], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_cancel_pending_tool_and_other_owner_cannot_deliver_late_results():
    service = Conversations(Limits(), lambda _: Scripted([[tool_round()]]))
    execution = service.start("caller", with_tool())
    await collect(service, execution)
    result = Continue(schema_version="1", round=1, results=[{"call_id": "call-1", "output": {}}])
    with pytest.raises(ExecutionError) as denied:
        service.continue_tools("other", execution.id, result)
    assert denied.value.status == 404
    assert service.cancel("caller", execution.id) == "cancelled"
    with pytest.raises(ExecutionError) as late:
        service.continue_tools("caller", execution.id, result)
    assert late.value.status == 409


@pytest.mark.asyncio
async def test_cancelled_old_provider_completion_does_not_finish_execution():
    entered, release = asyncio.Event(), asyncio.Event()

    class Late:
        async def stream(self, max_tokens, tool_results=()):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
            yield Delta("late")
            yield Round((), 1)

    service = Conversations(Limits(), lambda _: Late())
    execution = service.start("caller", start())
    task = asyncio.create_task(collect(service, execution))
    await entered.wait()
    service.cancel("caller", execution.id)
    release.set()
    events = await task
    assert execution.state == "cancelled"
    assert [event["type"] for event in events] == ["accepted"]


@pytest.mark.asyncio
async def test_stream_close_fences_execution_and_closes_provider():
    closed = asyncio.Event()

    class Long:
        async def stream(self, max_tokens, tool_results=()):
            try:
                yield Delta("first")
                await asyncio.Future()
            finally:
                closed.set()

    service = Conversations(Limits(), lambda _: Long())
    execution = service.start("caller", start())
    stream = service.events(execution)
    await anext(stream)
    await anext(stream)
    await stream.aclose()
    assert closed.is_set() and execution.state == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("events", [[], [Delta("partial")], [Round((ToolCall("call", "unknown", {}),), 1)], [Round((), 101)]])
async def test_no_terminal_or_invalid_round_fails_without_replay(events):
    provider = Scripted([events])
    service = Conversations(Limits(), lambda _: provider)
    execution = service.start("caller", start())
    result = await collect(service, execution)
    assert result[-1]["type"] == "failed"
    assert result[-1]["accepted"] is True and result[-1]["retryable"] is False
    assert execution.state == "failed" and len(provider.requests) == 1


def test_capacity_input_size_and_terminal_ttl(monkeypatch):
    service = Conversations(Limits(max_executions=1, ttl_seconds=2), lambda _: Scripted([]))
    execution = service.start("caller", start())
    with pytest.raises(ExecutionError) as full:
        service.start("caller", start())
    assert full.value.status == 429
    service.cancel("caller", execution.id)
    service.start("caller", start())
    monkeypatch.setattr("kotoyose.conversation.service.time.monotonic", lambda: execution.touched + 2)
    with pytest.raises(ExecutionError) as expired:
        service.get("caller", execution.id)
    assert expired.value.status == 404
    small = Conversations(Limits(max_input_bytes=1024), lambda _: Scripted([]))
    with pytest.raises(ExecutionError) as large:
        small.start("caller", start(instructions="a" * 1024))
    assert large.value.status == 413


def test_strict_input_contract_rejects_unknown_fields_and_secret_settings():
    with pytest.raises(ValueError):
        start(messages=[{"role": "assistant", "content": "only"}])
    with pytest.raises(ValueError):
        start(tools=[with_tool().tools[0]] * 2)
    with pytest.raises(ValueError):
        Config.model_validate({"schema_version": "1", "models": {}, "api_key": "secret"})
    with pytest.raises(ValueError):
        ToolResult(call_id="call", output={"invalid": float("nan")})


@pytest.mark.parametrize("schema", [{"type": "invalid", "sensitive": "fixture"},
                                    {"type": "array"}, {"type": "object", "$ref": "https://example/schema"}])
def test_invalid_tool_schema_rejected_before_provider_creation(schema):
    called = []
    service = Conversations(Limits(), lambda request: called.append(request))
    with pytest.raises(ExecutionError) as rejected:
        service.start("caller", start(tools=[{"name": "probe", "description": "", "input_schema": schema}]))
    assert rejected.value.status == 422 and "fixture" not in str(rejected.value)
    assert called == []


def sse(events):
    return "".join("event: " + event["type"] + "\r\ndata: " + json.dumps(event) + "\r\n\r\n" for event in events)


def provider_events(stop="end_turn", text="Hello"):
    return [{"type": "message_start", "message": {}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 2}},
            {"type": "message_stop"}]


def provider(handler, tools=None, response_tool_name_suffix=""):
    return AnthropicProvider(url="https://gateway.example/v1", headers={"authorization": "Bearer test"},
        model="model", instructions="Speak Japanese.",
        messages=tuple(Message(item.role, item.content) for item in start().messages), tools=tools or [], timeout=2,
        max_bytes=4096, input_token_budget=1000, response_tool_name_suffix=response_tool_name_suffix,
        transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_messages_provider_preserves_roles_instructions_and_exact_output_budget():
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 20})
        return httpx.Response(200, text=sse(provider_events()), headers={"content-type": "text/event-stream"})

    events = [item async for item in provider(handler).stream(50)]
    assert events == [Delta("Hello"), Round((), 2)]
    assert requests[1]["messages"] == start().model_dump()["messages"]
    assert requests[1]["system"] == "Speak Japanese." and requests[1]["max_tokens"] == 50


@pytest.mark.asyncio
async def test_public_dotted_tool_name_and_partial_json_are_normalized_both_directions():
    tool = with_tool().tools[0].model_dump()
    requests = []

    async def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 20})
        events = [{"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "call-1", "name": "caller_tool_0", "input": {}}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"name":'}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '"happy"}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 10}},
            {"type": "message_stop"}]
        return httpx.Response(200, text=sse(events))

    value = provider(handler, [tool])
    events = [event async for event in value.stream(50)]
    assert events[-1] == tool_round()
    _ = [event async for event in value.stream(40, (ProviderToolResult("call-1", {}),))]
    messages = requests[-1]["messages"]
    assert messages[:3] == start().model_dump()["messages"]
    assert len(messages) == 5
    assert messages[-2]["content"][0]["name"] == "caller_tool_0"
    assert messages[-1] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call-1", "content": "{}"}]}


@pytest.mark.asyncio
async def test_thinking_blocks_are_preserved_for_continuation_without_text_deltas():
    requests = []
    thinking = [{"type": "content_block_start", "index": 0,
                 "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "private fixture"}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "signed fixture"}},
                {"type": "content_block_stop", "index": 0}]

    async def handler(request):
        requests.append(json.loads(request.content))
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 20})
        if len(requests) > 2:
            return httpx.Response(200, text=sse(provider_events()))
        events = [{"type": "content_block_start", "index": 1, "content_block": {
            "type": "tool_use", "id": "call-1", "name": "caller_tool_0", "input": {"name": "happy"}}},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 10}},
            {"type": "message_stop"}]
        return httpx.Response(200, text=sse([*thinking, *events]))

    value = provider(handler, [with_tool().tools[0].model_dump()])
    events = [event async for event in value.stream(50)]
    assert events == [tool_round()]
    continued = [event async for event in value.stream(40, (ProviderToolResult("call-1", {}),))]
    assert continued == [Delta("Hello"), Round((), 2)]
    messages = requests[-1]["messages"]
    assert len(messages) == 5
    assert messages[-2]["content"][0] == {
        "type": "thinking", "thinking": "private fixture", "signature": "signed fixture"}


@pytest.mark.asyncio
async def test_gateway_tool_suffix_is_allowed_only_when_explicitly_configured():
    async def handler(request):
        if request.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 20})
        events = [{"type": "content_block_start", "index": 0, "content_block": {
            "type": "tool_use", "id": "call-1", "name": "caller_tool_0_ide", "input": {"name": "happy"}}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 10}},
            {"type": "message_stop"}]
        return httpx.Response(200, text=sse(events))
    tool = with_tool().tools[0].model_dump()
    with pytest.raises(ExecutionError, match="Unknown provider tool"):
        _ = [event async for event in provider(handler, [tool]).stream(50)]
    events = [event async for event in provider(handler, [tool], "_ide").stream(50)]
    assert events[-1] == tool_round()


@pytest.mark.asyncio
async def test_provider_unterminated_large_frame_and_incomplete_block_do_not_complete():
    for large in (True, False):
        async def handler(request):
            if request.url.path.endswith("count_tokens"):
                return httpx.Response(200, json={"input_tokens": 20})
            events = provider_events()
            events = [event for event in events if event["type"] != "content_block_stop"]
            return httpx.Response(200, text="data: " + "x" * 5000 if large else sse(events))
        with pytest.raises(ExecutionError):
            _ = [event async for event in provider(handler).stream(50)]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["token_count_missing", "input_large", "max_tokens", "truncated", "error"])
async def test_provider_contract_failures_are_explicit_and_not_retried(mode):
    dispatches = []

    async def handler(request):
        if request.url.path.endswith("count_tokens"):
            if mode == "token_count_missing":
                return httpx.Response(404)
            return httpx.Response(200, json={"input_tokens": 1000 if mode == "input_large" else 20})
        dispatches.append(request)
        events = provider_events("max_tokens" if mode == "max_tokens" else "end_turn")
        if mode == "truncated": events.pop()
        if mode == "error": events = [{"type": "error"}]
        return httpx.Response(200, text=sse(events))

    with pytest.raises(ExecutionError):
        _ = [event async for event in provider(handler).stream(50)]
    assert len(dispatches) == (0 if mode in {"token_count_missing", "input_large"} else 1)


@pytest.mark.asyncio
async def test_http_contract_version_owner_isolation_and_existing_routes_unchanged(monkeypatch):
    monkeypatch.setattr(app.state, "execution_service", None, raising=False)
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    service = Conversations(Limits(), lambda _: Scripted([[Delta("hello"), Round((), 2)]]),
                            {"chat": Model(input_token_budget=1000, max_output_tokens=100)}, frozenset({"caller", "other"}))
    monkeypatch.setattr(app.state, "conversations", service, raising=False)
    from kotoyose.config import Client, HarnessConfig
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(clients={
        "caller": Client(models=["chat"]), "other": Client(models=["chat"]),
    }))
    monkeypatch.setattr(app.state, "api_key_store", await fixture_key_store(caller="caller", other="other"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        models = await client.get("/v1/conversations/models", headers={"Authorization": "Bearer kys_caller"})
        assert models.json()["contract"] == "explicit-history-v1"
        result = await client.post("/v1/conversations/executions", json=start().model_dump(), headers={"authorization": "Bearer kys_caller"})
        assert result.status_code == 200 and '"type": "completed"' in result.text
        id = result.headers["x-kotoyose-execution-id"]
        wrong = await client.post(f"/v1/conversations/executions/{id}/cancel", headers={"authorization": "Bearer kys_other"})
        assert wrong.status_code == 404
        missing = await client.get("/health")
        assert missing.status_code == 200
        assert "tools" not in app.openapi()["components"]["schemas"]["ChatCompletionRequest"]["properties"]


@pytest.mark.asyncio
async def test_http_streamed_oversized_body_rejected_before_json_parsing(monkeypatch):
    monkeypatch.setattr(app.state, "execution_service", None, raising=False)
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    created = []
    service = Conversations(Limits(max_input_bytes=1024), lambda request: created.append(request))
    monkeypatch.setattr(app.state, "conversations", service, raising=False)

    async def oversized():
        yield b'{"instructions":"'
        yield b"x" * 2000

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/conversations/executions", content=oversized(),
                                     headers={"content-type": "application/json"})
    assert response.status_code == 413 and created == [] and service.executions == {}


@pytest.mark.asyncio
async def test_native_callers_require_explicit_conversation_opt_in_and_existing_model_scope(monkeypatch):
    from kotoyose.config import Client, HarnessConfig

    monkeypatch.setenv("KOTOYOSE_CONV_EXAMPLE_TOKEN", "sample-client-key-fixture")
    monkeypatch.setenv("KOTOYOSE_CONV_TASKHUB_TOKEN", "taskhub-fixture")
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(clients={
        "sample-client-key": Client(models=["chat"]),
        "taskhub": Client(models=["chat"]),
    }))
    monkeypatch.setattr(app.state, "api_key_store", await fixture_key_store(**{'sample-client-key': 'sample-client-key-fixture', 'taskhub': 'taskhub-fixture'}), raising=False)
    service = Conversations(Limits(), lambda _: Scripted([]), {
        "chat": Model(input_token_budget=1000, max_output_tokens=100),
        "outside": Model(input_token_budget=1000, max_output_tokens=100)}, frozenset({"sample-client-key"}))
    monkeypatch.setattr(app.state, "conversations", service, raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.get("/v1/conversations/models", headers={"authorization": "Bearer kys_taskhub-fixture"})
        assert denied.status_code == 403
        allowed = await client.get("/v1/conversations/models", headers={"authorization": "Bearer kys_sample-client-key-fixture"})
        assert [model["id"] for model in allowed.json()["models"]] == ["chat"]
        outside = await client.post("/v1/conversations/executions", json=start(model="outside").model_dump(),
                                    headers={"authorization": "Bearer kys_sample-client-key-fixture"})
        assert outside.status_code == 403 and service.executions == {}


@pytest.mark.asyncio
async def test_training_risk_tools_and_attachments_are_checked_before_conversation_provider(monkeypatch, client_api_key):
    from tests.fixtures.api_keys import TEST_API_HEADERS
    from tests.test_data_policy import approve, config

    cfg = config()
    monkeypatch.setattr("kotoyose.config._config", cfg)
    monkeypatch.setattr(app.state, "execution_service", None, raising=False)
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    seen = []

    def factory(req):
        seen.append(req.tools)
        return Scripted([[tool_round()], [Delta("done"), Round((), 2)]])

    svc = Conversations(Limits(), factory, {"demo": Model(input_token_budget=1000, max_output_tokens=100)},
                        frozenset({"test-client"}))
    monkeypatch.setattr(app.state, "conversations", svc, raising=False)
    request = with_tool().model_dump()
    request["model"] = "demo"
    path = "/v1/conversations/executions"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=TEST_API_HEADERS) as client:
        denied = await client.post(path, json=request)
        assert denied.status_code == 403
        assert denied.json()["error"]["type"] == "permission_denied"
        assert seen == []
        approve(cfg, "tools")
        attachment = await client.post(path, json={**request, "attachments": ["file"]})
        assert attachment.status_code == 403
        structured_image = await client.post(path, json={
            **request, "messages": [{"role": "user", "content": [{"type": "image", "url": "https://demo.invalid"}]}],
        })
        assert structured_image.status_code == 403
        assert seen == []
        allowed = await client.post(path, json=request)
        assert allowed.status_code == 200 and '"type": "tool_calls"' in allowed.text
        assert seen[0][0].name == "avatar.set_expression"

        cfg.routes["gateway"].model = "changed"
        execution_id = allowed.headers["x-kotoyose-execution-id"]
        continued = await client.post(f"{path}/{execution_id}/tool-results", json={
            "schema_version": "1", "round": 1, "results": [{"call_id": "call-1", "output": {}}],
        })
        assert continued.status_code == 403
