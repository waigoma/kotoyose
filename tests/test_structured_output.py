import asyncio
from unittest.mock import AsyncMock

import pytest

from kotoyose.config import ModelPreset
from kotoyose.structured_output import StructuredOutputSession, validate_output_schema, validate_structured_result
from kotoyose.runtime.base import RuntimeEvent, RuntimeResult, RuntimeTurnError
from kotoyose.session import _cache_identity, stream_claude
from kotoyose.chat_completion import record_turn_failure
from kotoyose.chat_usage import build_response_usage

SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"], "additionalProperties": False}


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_output_schema_opt_in_and_validated(provider):
    assert ModelPreset(provider=provider, output_schema=SCHEMA).output_schema == SCHEMA


@pytest.mark.parametrize("schema", [{"type": "array"}, {"type": "object", "$ref": "https://example.com/schema"},
                                     {"type": "object", "$id": "https://example.com"},
                                     {"type": "object", "properties": {"x": {"type": "bad"}}}])
def test_invalid_or_network_schema_rejected(schema):
    with pytest.raises(ValueError):
        validate_output_schema(schema)


@pytest.mark.parametrize("provider", ["cursor", "antigravity"])
def test_unsupported_provider_schema_fails_closed(provider):
    with pytest.raises(ValueError):
        ModelPreset(provider=provider, output_schema=SCHEMA)


def test_schema_output_data_is_validated_without_replacing_usage():
    result = RuntimeResult('commentary', usage={"output_tokens": 3}, structured_output={"answer": 42})
    actual = validate_structured_result(result, SCHEMA)
    assert actual.text == '{"answer":42}'
    assert actual.usage == result.usage
    with pytest.raises(RuntimeTurnError) as error:
        validate_structured_result(RuntimeResult('{"answer":"bad"}'), SCHEMA)
    assert error.value.terminal_reason == "output_contract_error"


def test_schema_change_changes_session_identity_but_none_keeps_legacy_digest():
    args = ("claude", "alias", "model", [], {})
    assert _cache_identity(*args) == _cache_identity(*args, output_schema=None)
    assert _cache_identity(*args) != _cache_identity(*args, output_schema=SCHEMA)


async def test_schema_stream_never_releases_invalid_text():
    class Session:
        provider = "claude"
        async def stream(self, prompt):
            yield 'invalid partial'
            yield RuntimeEvent("tool_start", tool_name="Read")
            yield RuntimeResult('invalid partial')
    stream = StructuredOutputSession(Session(), SCHEMA).stream("hello")
    items = []
    with pytest.raises(RuntimeTurnError):
        async for item in stream:
            items.append(item)
    assert len(items) == 1 and isinstance(items[0], RuntimeEvent)


async def test_runtime_facade_aclose_propagates_to_registered_provider():
    closed = asyncio.Event()
    class Session:
        provider = "cursor"
        async def disconnect(self): pass
        async def ask(self, prompt): return RuntimeResult("")
        async def stream(self, prompt):
            try:
                yield "partial"
                await asyncio.Event().wait()
            finally:
                closed.set()
    iterator = stream_claude(Session(), "hello")
    assert await anext(iterator) == "partial"
    await iterator.aclose()
    assert closed.is_set()


async def test_terminal_failure_records_partial_usage_without_success():
    record = AsyncMock()
    error = RuntimeTurnError("Provider failed", partial_result=RuntimeResult("partial", {"input_tokens": 8, "output_tokens": 3}),
                             diagnostics={"terminal_reason": "error"})
    await record_turn_failure(record, 3, error)
    assert record.await_args.kwargs["tokens_in"] == 8
    assert record.await_args.kwargs["tokens_out"] == 3
    assert record.await_args.kwargs["metadata"]["partial_response_chars"] == 7


def test_reasoning_is_subset_not_added_to_output():
    result = build_response_usage({"output_tokens": 10, "input_tokens": 5, "reasoning_tokens": 4}, 0, 0)
    assert result.completion_tokens == 10
    assert result.completion_tokens_details.reasoning_tokens == 4
    assert result.cost_usd is None


def test_codex_native_usage_aliases_preserve_reasoning_and_cache():
    result = build_response_usage({"output_tokens": 12, "input_tokens": 7, "reasoning_output_tokens": 4, "cached_input_tokens": 3}, 0, 0)
    assert result.completion_tokens == 12
    assert result.completion_tokens_details.reasoning_tokens == 4
    assert result.cache_read_tokens == 3


async def test_schema_failure_sse_is_error_not_successful_assistant_content():
    from tests.test_chat_streaming_service import _ports, _context
    from kotoyose.chat_streaming import stream_chat_response
    async def runtime(client, prompt):
        if False:
            yield ""
        raise RuntimeTurnError("Provider output failed the configured JSON schema", partial_result=RuntimeResult("bad"),
                               diagnostics={"terminal_reason": "output_contract_error"})
    ports = _ports(runtime)
    chunks = [item async for item in stream_chat_response(_context(), ports)]
    assert any('"error"' in chunk and 'output_contract_error' in chunk for chunk in chunks)
    assert not any('stream interrupted' in chunk or '"finish_reason":"stop"' in chunk or ('"content":' in chunk and '"content":null' not in chunk) for chunk in chunks)
    ports.completion.record_completed.assert_not_awaited()
    ports.completion.upsert_session.assert_not_awaited()
    assert chunks[-1] == 'data: [DONE]\n\n'


def test_response_usage_reads_cost_from_either_key():
    assert build_response_usage({"cost": 0.03}, 0, 0).cost_usd == 0.03
    assert build_response_usage({"cost_usd": 0.5, "cost": 0.03}, 0, 0).cost_usd == 0.5
    assert build_response_usage({"cost": None}, 0, 0).cost_usd is None
