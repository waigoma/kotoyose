"""Model-free coding-egress contract and byte-accounting boundaries.

All transport is in memory. These tests open no sockets, launch no provider or
host process, and use only synthetic subscription credentials.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from kotoyose import codex_egress
from kotoyose.codex_egress import CodexEgress
from kotoyose.execution_resources import NativeRuntimeProfile
from tests.test_codex_egress import header
from tests.test_codex_host_broker import tools as builtin_tools


_UNSET = object()
_LIMITS = {
    "version": 1,
    "total_bytes": "disabled",
    "total_connections": "disabled",
    "request_bytes": 4 * 1024 * 1024,
    "response_bytes": 4 * 1024 * 1024,
}
_METRICS = {
    "accounted_request_bytes", "request_bytes_read", "response_bytes_read",
    "connections_started", "peak_concurrent", "cap_reason",
}


def profile_data(**changes):
    return {
        "provider": "codex", "model": "gpt-6-luna", "cli_binary": "/fixture/codex",
        "codex_execution_mode": "host_native_coding_v1",
        "credential_mode": "chatgpt_subscription", "credential_name": "CODEX_CHATGPT_AUTH",
        "codex_auth_file": "/fixture/auth.json", **changes,
    }


def policy(**changes):
    return NativeRuntimeProfile.model_validate(profile_data(
        codex_egress_limits={**_LIMITS, **changes})).codex_egress_limits


def payload(**changes):
    return json.dumps({
        "model": "gpt-6-luna", "stream": True, "store": False,
        "reasoning": {"effort": "max"}, "tools": builtin_tools(), "input": [], **changes,
    }, separators=(",", ":")).encode()


def response(body=b"data: fixture\n\n", *, chunked=False):
    if chunked:
        return (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                + format(len(body), "x").encode() + b"\r\n" + body + b"\r\n0\r\n\r\n")
    return (b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode()
            + b"\r\n\r\n" + body)


class MemoryReader:
    """StreamReader-compatible application reads with inspectable byte delivery."""

    def __init__(self, data, *, chunk_size=None, fail_after=None):
        self.data = data
        self.position = 0
        self.chunk_size = chunk_size
        self.fail_after = fail_after
        self.read_sizes = []
        self.exact_reads = []

    def take(self, length):
        result = self.data[self.position:self.position + length]
        self.position += len(result)
        return result

    async def readuntil(self, separator):
        end = self.data.find(separator, self.position)
        if end == -1:
            raise asyncio.IncompleteReadError(self.take(len(self.data)), None)
        return self.take(end + len(separator) - self.position)

    async def readexactly(self, length):
        self.exact_reads.append(length)
        result = self.take(length)
        if len(result) != length:
            raise asyncio.IncompleteReadError(result, length)
        return result

    async def read(self, length=-1):
        self.read_sizes.append(length)
        if self.fail_after is not None and self.position >= self.fail_after:
            raise OSError("synthetic transport failure")
        length = len(self.data) if length < 0 else length
        if self.chunk_size is not None:
            length = min(length, self.chunk_size)
        if self.fail_after is not None:
            length = min(length, self.fail_after - self.position)
        return self.take(length)


class MemoryWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class MemoryEndpoint:
    def __init__(self, wire=None, *, chunk_size=None, fail_after=None):
        self.wire = response() if wire is None else wire
        self.chunk_size = chunk_size
        self.fail_after = fail_after
        self.readers = []
        self.writers = []

    async def __call__(self):
        reader = MemoryReader(self.wire, chunk_size=self.chunk_size, fail_after=self.fail_after)
        writer = MemoryWriter()
        self.readers.append(reader)
        self.writers.append(writer)
        return reader, writer


@pytest.fixture
def make_broker(monkeypatch):
    brokers = []
    monkeypatch.setattr(codex_egress, "read_subscription", lambda _: SimpleNamespace(
        identity="fixture-identity", access_token="synthetic-access-token", account_id="synthetic-account"))
    # Any accidental real transport or API-key fallback is a test failure.
    monkeypatch.setattr(asyncio, "open_connection", AsyncMock(side_effect=AssertionError("no sockets")))
    monkeypatch.setattr(codex_egress, "connect_endpoint", AsyncMock(side_effect=AssertionError("no API fallback")))

    def create(*, limits=_UNSET, byte_limit=4096, wire=None, chunk_size=None, fail_after=None, **kwargs):
        endpoint = MemoryEndpoint(wire, chunk_size=chunk_size, fail_after=fail_after)
        monkeypatch.setattr(codex_egress, "connect_subscription_endpoint", endpoint)
        options = {} if limits is _UNSET else {"limits": limits}
        broker = CodexEgress(
            1, byte_limit, "gpt-6-luna", "fake", None,
            auth_file=Path("/fixture/auth.json"), auth_identity="fixture-identity",
            reasoning_effort="max", coding=True, **options, **kwargs,
        )
        brokers.append(broker)
        return broker, endpoint

    yield create
    for broker in brokers:
        assert not broker.tasks
        assert not broker.writers
        broker.directory.cleanup()


async def exchange(broker, body=None, *, raw_header=None):
    body = payload() if body is None else body
    raw_header = header(length=len(body)) if raw_header is None else raw_header
    reader, writer = MemoryReader(raw_header + body), MemoryWriter()
    await broker.handle(reader, writer)
    assert writer.closed
    return reader, writer


def metrics(broker):
    result = broker.metrics()
    assert _METRICS <= result.keys()
    assert all(type(result[name]) is int and result[name] >= 0 for name in _METRICS)
    assert "synthetic" not in repr(result)
    return result


@pytest.mark.parametrize("total", ["disabled", 1, 1048576])
def test_limits_round_trip_preserves_explicit_finite_or_disabled_values(total):
    value = {**_LIMITS, "total_bytes": total, "total_connections": total}
    profile = NativeRuntimeProfile.model_validate(profile_data(codex_egress_limits=value))
    assert profile.model_dump(mode="json")["codex_egress_limits"] == value
    assert NativeRuntimeProfile.model_validate(profile.model_dump(mode="json")) == profile


def test_omission_preserves_legacy_profile_serialization():
    profile = NativeRuntimeProfile.model_validate(profile_data())
    assert profile.codex_egress_limits is None
    assert "codex_egress_limits" not in profile.model_dump()
    assert "codex_egress_limits" not in json.loads(profile.model_dump_json())


@pytest.mark.parametrize("field", list(_LIMITS))
def test_all_policy_fields_are_required(field):
    value = {key: item for key, item in _LIMITS.items() if key != field}
    with pytest.raises(ValidationError):
        NativeRuntimeProfile.model_validate(profile_data(codex_egress_limits=value))


@pytest.mark.parametrize("value", [None, False, True, [], "disabled", 1, {}])
def test_policy_object_cannot_be_null_coerced_or_incomplete(value):
    with pytest.raises(ValidationError):
        NativeRuntimeProfile.model_validate(profile_data(codex_egress_limits=value))


@pytest.mark.parametrize("field", ["total_bytes", "total_connections", "request_bytes", "response_bytes"])
@pytest.mark.parametrize("value", [None, True, False, 0, -1, 1.0, "1", "unlimited", "DISABLED"])
def test_numeric_policy_fields_are_strict_positive_integers(field, value):
    with pytest.raises(ValidationError):
        policy(**{field: value})


@pytest.mark.parametrize("field", ["request_bytes", "response_bytes"])
def test_per_message_caps_cannot_be_disabled(field):
    with pytest.raises(ValidationError):
        policy(**{field: "disabled"})


@pytest.mark.parametrize("value", [None, True, False, 1.0, "1", 0, 2])
def test_version_is_exact_integer_one(value):
    with pytest.raises(ValidationError):
        policy(version=value)


def test_unknown_policy_fields_cannot_relax_fixed_concurrency():
    with pytest.raises(ValidationError):
        policy(max_concurrent=100)


@pytest.mark.parametrize("changes", [
    {"codex_execution_mode": "sandbox_exec_v1"},
    {"codex_execution_mode": "sandbox_exec_v1", "credential_mode": "environment",
     "credential_name": "OPENAI_API_KEY", "credential_env": "FIXTURE_KEY", "codex_auth_file": None},
    {"provider": "claude", "codex_execution_mode": "sandbox_exec_v1", "credential_mode": "runtime_managed",
     "credential_name": "CLAUDE_CODE_OAUTH_TOKEN", "codex_auth_file": None},
])
def test_policy_is_rejected_outside_host_coding_profiles(changes):
    # Verify this is a valid legacy profile, so the failure is the new setting.
    NativeRuntimeProfile.model_validate(profile_data(**changes))
    with pytest.raises(ValidationError):
        NativeRuntimeProfile.model_validate(profile_data(**changes, codex_egress_limits=_LIMITS))


async def test_disabled_cumulative_caps_allow_33_requests_above_old_byte_limit(make_broker):
    broker, endpoint = make_broker(limits=policy(), byte_limit=64)
    body = payload()
    request_size = len(header(length=len(body))) + len(body)
    for _ in range(33):
        _, writer = await exchange(broker, body)
        assert writer.data.startswith(b"HTTP/1.1 200 OK")
    values = metrics(broker)
    assert len(endpoint.writers) == values["connections_started"] == 33
    assert values["peak_concurrent"] == 1
    assert values["accounted_request_bytes"] == values["request_bytes_read"] == request_size * 33
    assert values["response_bytes_read"] == len(endpoint.wire) * 33
    assert broker.bytes == request_size * 33 + len(endpoint.wire) * 33 > broker.byte_limit
    assert values["cap_reason"] == 0
    assert broker.failure_reason is None
    upstream = bytes(endpoint.writers[0].data)
    assert upstream.startswith(b"POST /backend-api/codex/responses HTTP/1.1\r\n")
    assert b"host: chatgpt.com\r\n" in upstream
    assert b"authorization: Bearer synthetic-access-token\r\n" in upstream
    assert b"chatgpt-account-id: synthetic-account\r\n" in upstream


@pytest.mark.parametrize("limit, expected", [(32, 32), (2, 2)])
async def test_finite_connection_cap_accepts_boundary_and_rejects_next(make_broker, limit, expected):
    broker, endpoint = make_broker(limits=policy(total_connections=limit))
    for _ in range(expected):
        _, writer = await exchange(broker)
        assert writer.data.startswith(b"HTTP/1.1 200 OK")
    _, writer = await exchange(broker)
    assert writer.data.startswith(b"HTTP/1.1 502")
    assert len(endpoint.writers) == expected
    assert metrics(broker)["connections_started"] == expected + 1
    assert metrics(broker)["cap_reason"] == 2
    assert broker.failure_reason == "egress_limit_exceeded"


async def test_omitted_policy_retains_legacy_32_connection_limit(make_broker):
    broker, endpoint = make_broker(byte_limit=1 << 30)
    for _ in range(32):
        _, writer = await exchange(broker)
        assert writer.data.startswith(b"HTTP/1.1 200 OK")
    _, writer = await exchange(broker)
    assert writer.data.startswith(b"HTTP/1.1 502")
    assert len(endpoint.writers) == 32


async def test_omitted_policy_retains_legacy_total_byte_limit(make_broker):
    broker, endpoint = make_broker(byte_limit=1)
    _, writer = await exchange(broker)
    assert writer.data.startswith(b"HTTP/1.1 502")
    assert not endpoint.writers
    assert broker.failure_reason == "egress_limit_exceeded"


@pytest.mark.parametrize("over", [False, True])
async def test_request_limit_is_full_json_body_with_exact_boundary(make_broker, over):
    body = payload(client_metadata={"route": "discard-me"})
    broker, endpoint = make_broker(limits=policy(request_bytes=len(body) - int(over)))
    reader, writer = await exchange(broker, body)
    values = metrics(broker)
    if over:
        assert writer.data.startswith(b"HTTP/1.1 502")
        assert not endpoint.writers and not reader.exact_reads
        assert values["request_bytes_read"] == len(header(length=len(body)))
        assert values["cap_reason"] == 3
        assert broker.failure_reason == "egress_limit_exceeded"
    else:
        assert writer.data.startswith(b"HTTP/1.1 200 OK")
        assert values["request_bytes_read"] == len(header(length=len(body))) + len(body)
        assert b"discard-me" not in endpoint.writers[0].data
        assert values["cap_reason"] == 0


async def test_explicit_request_cap_can_exceed_legacy_two_megabyte_body_limit(make_broker):
    body = payload(instructions="x" * (2 * 1024 * 1024))
    broker, _ = make_broker(limits=policy(request_bytes=len(body)))
    _, writer = await exchange(broker, body)
    assert writer.data.startswith(b"HTTP/1.1 200 OK")
    assert metrics(broker)["cap_reason"] == 0


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("over", [False, True])
async def test_response_limit_includes_http_headers_and_chunk_framing(make_broker, chunked, over):
    wire = response(b"data: content\n\n", chunked=chunked)
    broker, endpoint = make_broker(limits=policy(response_bytes=len(wire) - int(over)), wire=wire, chunk_size=7)
    _, writer = await exchange(broker)
    assert metrics(broker)["response_bytes_read"] == len(wire)
    assert endpoint.readers[0].position == len(wire)
    if over:
        assert writer.data.startswith(b"HTTP/1.1 502")
        assert b"data: content" not in writer.data
        assert metrics(broker)["cap_reason"] == 4
        assert broker.failure_reason == "egress_limit_exceeded"
    else:
        assert writer.data.startswith(b"HTTP/1.1 200 OK")
        assert b"data: content" in writer.data
        assert metrics(broker)["cap_reason"] == 0


async def test_large_response_stops_reading_before_unbounded_buffering(make_broker):
    cap = 128
    wire = response(b"x" * (1024 * 1024))
    broker, endpoint = make_broker(limits=policy(response_bytes=cap), wire=wire)
    _, writer = await exchange(broker)
    assert writer.data.startswith(b"HTTP/1.1 502")
    assert metrics(broker)["cap_reason"] == 4
    assert cap < metrics(broker)["response_bytes_read"] <= cap + 1
    assert endpoint.readers[0].position == metrics(broker)["response_bytes_read"]


@pytest.mark.parametrize("over", [False, True])
async def test_cumulative_bytes_account_request_reservation_and_raw_response(make_broker, over):
    body, wire = payload(), response()
    request_size = len(header(length=len(body))) + len(body)
    broker, _ = make_broker(limits=policy(total_bytes=request_size + len(wire) - int(over)), wire=wire)
    _, writer = await exchange(broker, body)
    values = metrics(broker)
    assert values["accounted_request_bytes"] == values["request_bytes_read"] == request_size
    assert values["response_bytes_read"] == len(wire)
    assert broker.bytes == request_size + len(wire)
    assert values["cap_reason"] == (1 if over else 0)
    assert writer.data.startswith(b"HTTP/1.1 502" if over else b"HTTP/1.1 200 OK")


async def test_finite_cumulative_bytes_apply_across_separate_requests(make_broker):
    body, wire = payload(), response()
    request_size = len(header(length=len(body))) + len(body)
    broker, endpoint = make_broker(limits=policy(total_bytes=request_size + len(wire)))
    _, first = await exchange(broker, body)
    assert first.data.startswith(b"HTTP/1.1 200 OK")
    _, second = await exchange(broker, body)
    assert second.data.startswith(b"HTTP/1.1 502")
    assert len(endpoint.writers) == 1
    assert metrics(broker)["cap_reason"] == 1
    assert metrics(broker)["accounted_request_bytes"] == request_size * 2
    assert metrics(broker)["request_bytes_read"] == request_size + len(header(length=len(body)))
    assert broker.bytes == request_size * 2 + len(wire)


async def test_partial_request_body_keeps_reserved_and_actually_read_counts_distinct(make_broker):
    body = payload()
    raw = header(length=len(body))
    broker, endpoint = make_broker(limits=policy())
    _, writer = await exchange(broker, body[:17], raw_header=raw)
    values = metrics(broker)
    assert writer.data.startswith(b"HTTP/1.1 502") and not endpoint.writers
    assert values["accounted_request_bytes"] == broker.bytes == len(raw) + len(body)
    assert values["request_bytes_read"] == len(raw) + 17
    assert values["response_bytes_read"] == 0
    assert values["cap_reason"] == 0
    assert broker.failure_reason == "provider_error"


async def test_partial_request_header_is_counted_without_reserving_unparsed_body(make_broker):
    raw = b"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1:18080\r\n"
    broker, endpoint = make_broker(limits=policy())
    _, writer = await exchange(broker, b"", raw_header=raw)
    values = metrics(broker)
    assert writer.data.startswith(b"HTTP/1.1 502") and not endpoint.writers
    assert values["request_bytes_read"] == len(raw)
    assert values["accounted_request_bytes"] == values["response_bytes_read"] == broker.bytes == 0
    assert values["cap_reason"] == 0


@pytest.mark.parametrize("failure", ["eof", "read_error"])
async def test_partial_response_is_accounted_but_never_forwarded(make_broker, failure):
    wire = response(b"data: sensitive unfinished output\n\n")
    partial = len(wire) - 5
    broker, endpoint = make_broker(
        limits=policy(), wire=wire[:partial] if failure == "eof" else wire,
        fail_after=partial if failure == "read_error" else None,
    )
    _, writer = await exchange(broker)
    values = metrics(broker)
    assert writer.data.startswith(b"HTTP/1.1 502")
    assert b"sensitive unfinished" not in writer.data
    assert values["response_bytes_read"] == partial == endpoint.readers[0].position
    assert broker.bytes == values["accounted_request_bytes"] + partial
    assert values["cap_reason"] == 0
    assert broker.failure_reason == "provider_error"


async def test_fixed_four_request_concurrency_survives_disabled_cumulative_limits(make_broker, monkeypatch):
    broker, endpoint = make_broker(limits=policy())
    entered, release = asyncio.Event(), asyncio.Event()
    waiting = 0

    async def blocked_connect():
        nonlocal waiting
        waiting += 1
        if waiting == 4:
            entered.set()
        await release.wait()
        return await endpoint()

    monkeypatch.setattr(codex_egress, "connect_subscription_endpoint", blocked_connect)
    running = [asyncio.create_task(exchange(broker)) for _ in range(4)]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        _, rejected = await exchange(broker)
        assert rejected.data.startswith(b"HTTP/1.1 502")
        assert waiting == 4
        assert metrics(broker)["peak_concurrent"] == 4
        assert metrics(broker)["connections_started"] == 5
        assert metrics(broker)["cap_reason"] == 5
        assert broker.failure_reason == "egress_denied"
    finally:
        release.set()
        await asyncio.gather(*running)
    assert not broker.tasks and not broker.writers


async def test_request_cap_includes_image_uri_even_when_cumulative_accounting_excludes_it(make_broker):
    uri = "data:image/png;base64," + "YQ==" * 100
    body = payload(input=[{"type": "message", "role": "user", "content": [
        {"type": "input_image", "image_url": uri, "detail": "high"},
    ]}])
    broker, endpoint = make_broker(
        limits=policy(request_bytes=len(body) - 1), image_uris=(uri,),
    )
    _, writer = await exchange(broker, body)
    assert writer.data.startswith(b"HTTP/1.1 502")
    assert not endpoint.writers
    assert metrics(broker)["cap_reason"] == 3


async def test_image_bytes_preserve_legacy_accounting_without_inflating_actual_read_counts(make_broker):
    uri = "data:image/png;base64,YQ=="
    body = payload(input=[{"type": "message", "role": "user", "content": [
        {"type": "input_image", "image_url": uri, "detail": "high"},
    ]}])
    # Valid text-only Responses completion also passes the image-output gate.
    content = (
        b'event: response.created\ndata: {"type":"response.created","response":{"id":"fixture","status":"in_progress"}}\n\n'
        b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"fixture","status":"completed","output":[]}}\n\n'
    )
    wire = response(content)
    broker, _ = make_broker(limits=policy(request_bytes=len(body)), image_uris=(uri,), wire=wire)
    _, writer = await exchange(broker, body)
    assert writer.data.startswith(b"HTTP/1.1 200 OK")
    values = metrics(broker)
    actual = len(header(length=len(body))) + len(body)
    assert values["request_bytes_read"] == actual
    assert values["accounted_request_bytes"] == actual - len(uri)
    assert values["response_bytes_read"] == len(wire)
    assert broker.bytes == actual - len(uri) + len(wire)
    assert values["cap_reason"] == 0


def test_direct_broker_policy_is_rejected_outside_coding_mode():
    with pytest.raises(ValueError, match="invalid_job"):
        CodexEgress(1, 4096, "gpt-6-luna", "fake", None, limits=policy())


async def test_per_message_budgets_reset_while_counters_remain_cumulative(make_broker):
    body, wire = payload(), response()
    broker, endpoint = make_broker(limits=policy(request_bytes=len(body), response_bytes=len(wire)), wire=wire)
    for _ in range(2):
        _, writer = await exchange(broker, body)
        assert writer.data.startswith(b"HTTP/1.1 200 OK")
    values = metrics(broker)
    assert values["request_bytes_read"] == 2 * (len(header(length=len(body))) + len(body))
    assert values["response_bytes_read"] == 2 * len(wire)
    assert values["cap_reason"] == 0 and len(endpoint.writers) == 2


async def test_first_cap_reason_is_not_overwritten_by_later_limit_failures(make_broker):
    body = payload()
    broker, endpoint = make_broker(limits=policy(request_bytes=len(body) - 1, total_connections=1))
    _, first = await exchange(broker, body)
    assert first.data.startswith(b"HTTP/1.1 502")
    assert metrics(broker)["cap_reason"] == 3
    _, second = await exchange(broker, body)
    assert second.data.startswith(b"HTTP/1.1 502")
    assert metrics(broker)["connections_started"] == 2
    assert metrics(broker)["cap_reason"] == 3
    assert not endpoint.writers


@pytest.mark.parametrize("fields", [
    {"Host": "other.invalid"}, {"Authorization": "Bearer wrong"},
    {"Transfer-Encoding": "chunked"}, {"X-Forwarded-Host": "other.invalid"},
])
async def test_disabled_caps_do_not_relax_request_auth_or_endpoint_policy(make_broker, fields):
    body = payload()
    broker, endpoint = make_broker(limits=policy())
    _, writer = await exchange(broker, body, raw_header=header(length=len(body), fields=fields))
    assert writer.data.startswith(b"HTTP/1.1 502")
    assert not endpoint.writers
    assert metrics(broker)["cap_reason"] == 0
    assert broker.failure_reason == "egress_denied"


@pytest.mark.parametrize("status, reason", [
    (401, "authentication_failed"), (403, "subscription_denied"), (429, "subscription_limit_reached"),
])
async def test_disabled_caps_do_not_change_subscription_failure_classification(make_broker, status, reason):
    wire = f"HTTP/1.1 {status} Fixture\r\nContent-Length: 0\r\n\r\n".encode()
    broker, _ = make_broker(limits=policy(), wire=wire)
    _, writer = await exchange(broker)
    assert writer.data.startswith(b"HTTP/1.1 502")
    assert metrics(broker)["response_bytes_read"] == len(wire)
    assert metrics(broker)["cap_reason"] == 0
    assert broker.failure_reason == reason


async def test_disabled_cumulative_caps_preserve_timeout_and_cleanup(make_broker):
    broker, endpoint = make_broker(limits=policy())
    broker.timeout = 0.01
    never = asyncio.Event()

    async def wait_for_header(_):
        await never.wait()

    writer = MemoryWriter()
    await asyncio.wait_for(broker.handle(SimpleNamespace(readuntil=wait_for_header), writer), 1)
    assert writer.data.startswith(b"HTTP/1.1 502") and writer.closed
    assert not endpoint.writers and not broker.tasks and not broker.writers
    assert metrics(broker)["request_bytes_read"] == metrics(broker)["response_bytes_read"] == 0
    assert metrics(broker)["cap_reason"] == 0
    assert broker.failure_reason == "provider_error"


async def test_concurrent_cumulative_reservation_rejects_over_budget_before_upstream(make_broker, monkeypatch):
    body = payload()
    raw = header(length=len(body))
    request_size = len(raw) + len(body)
    broker, endpoint = make_broker(limits=policy(total_bytes=request_size * 2))
    admitted, release = asyncio.Event(), asyncio.Event()
    connecting = 0

    async def blocked_connect():
        nonlocal connecting
        connecting += 1
        if connecting == 2:
            admitted.set()
        await release.wait()
        return await endpoint()

    monkeypatch.setattr(codex_egress, "connect_subscription_endpoint", blocked_connect)
    running = [asyncio.create_task(exchange(broker, body)) for _ in range(4)]
    try:
        await asyncio.wait_for(admitted.wait(), 1)
        denied = await asyncio.wait_for(asyncio.gather(*running[2:]), 1)
        assert all(writer.data.startswith(b"HTTP/1.1 502") for _, writer in denied)
        assert connecting == 2 and not endpoint.writers
        values = metrics(broker)
        assert values["connections_started"] == 4
        assert values["accounted_request_bytes"] == broker.bytes == request_size * 4
        assert values["request_bytes_read"] == request_size * 2 + len(raw) * 2
        assert values["response_bytes_read"] == 0 and values["cap_reason"] == 1
    finally:
        release.set()
        await asyncio.gather(*running)
    # The sticky failure also stops the earlier in-flight connections from
    # forwarding credentials or bodies after their connector resumes.
    assert connecting == 2
    assert all(not writer.data and writer.closed for writer in endpoint.writers)


async def test_header_limit_overrun_counts_only_bytes_actually_drained(make_broker):
    broker, endpoint = make_broker(limits=policy())
    reader = asyncio.StreamReader(limit=32)
    raw = b"x" * 128
    reader.feed_data(raw)
    writer = MemoryWriter()
    await broker.handle(reader, writer)
    reader.feed_eof()
    buffered = await reader.read()
    assert writer.data.startswith(b"HTTP/1.1 502") and not endpoint.writers
    assert len(buffered) < len(raw)
    values = metrics(broker)
    assert values["request_bytes_read"] == len(raw) - len(buffered)
    assert values["accounted_request_bytes"] == values["response_bytes_read"] == broker.bytes == 0
    assert values["cap_reason"] == 0


@pytest.mark.parametrize("phase", ["header", "body"])
async def test_cancelled_partial_reads_count_delivered_bytes_without_counting_buffered_header(make_broker, phase):
    broker, endpoint = make_broker(limits=policy())
    waiting = asyncio.Event()

    class ObservedReader(asyncio.StreamReader):
        body_reads = 0

        async def readuntil(self, separator):
            if phase == "header":
                waiting.set()
            return await super().readuntil(separator)

        async def read(self, length=-1):
            self.body_reads += 1
            if phase == "body" and self.body_reads == 2:
                waiting.set()
            return await super().read(length)

    body = payload()
    raw = header(length=len(body))
    reader = ObservedReader()
    partial = raw[:-2] if phase == "header" else raw + body[:17]
    reader.feed_data(partial)
    writer = MemoryWriter()
    task = asyncio.create_task(broker.handle(reader, writer))
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    reader.feed_eof()
    buffered = await reader.read()
    values = metrics(broker)
    assert values["request_bytes_read"] == (0 if phase == "header" else len(raw) + 17)
    assert values["accounted_request_bytes"] == broker.bytes == (0 if phase == "header" else len(raw) + len(body))
    assert buffered == (partial if phase == "header" else b"")
    assert values["response_bytes_read"] == values["cap_reason"] == 0
    assert not writer.data and writer.closed
    assert not endpoint.writers and not broker.tasks and not broker.writers


async def test_response_redaction_growth_cannot_exceed_explicit_response_cap(make_broker):
    wire = response(b"fake" * 100)
    broker, _ = make_broker(limits=policy(response_bytes=len(wire)), wire=wire)
    _, writer = await exchange(broker)
    assert writer.data.startswith(b"HTTP/1.1 502")
    assert b"[redacted]" not in writer.data
    assert metrics(broker)["response_bytes_read"] == len(wire)
    assert metrics(broker)["cap_reason"] == 4
    assert broker.failure_reason == "egress_limit_exceeded"


def test_redaction_expansion_is_rejected_before_allocating_replacement(make_broker):
    broker, _ = make_broker(limits=policy(response_bytes=512))
    replacements = []

    class ObservedBytes(bytes):
        def replace(self, old, new, *args):
            replacements.append((old, new))
            return super().replace(old, new, *args)

    with pytest.raises(ValueError, match="egress_limit_exceeded"):
        broker.redact(ObservedBytes(b"fake" * 100), byte_limit=512)
    assert not replacements
    assert metrics(broker)["cap_reason"] == 4


@pytest.mark.parametrize("text", ["\x7f" * 1024, "é" * 1024, "🙂" * 256])
async def test_explicit_policy_preserves_unicode_without_ascii_serialization_growth(make_broker, text):
    value = json.loads(payload(instructions=text))
    body = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    broker, endpoint = make_broker(limits=policy(request_bytes=len(body)))
    _, writer = await exchange(broker, body)
    assert writer.data.startswith(b"HTTP/1.1 200 OK")
    forwarded = bytes(endpoint.writers[0].data).split(b"\r\n\r\n", 1)[1]
    assert json.loads(forwarded) == value
    assert text.encode("utf-8") in forwarded
    assert len(forwarded) <= len(body)
    assert metrics(broker)["cap_reason"] == 0


async def test_normalized_request_overflow_is_rejected_before_upstream(make_broker):
    body = payload(max_output_tokens=1e9).replace(b'"max_output_tokens":1000000000.0', b'"max_output_tokens":1e9')
    assert b'"max_output_tokens":1e9' in body
    assert len(json.dumps(json.loads(body), separators=(",", ":")).encode()) > len(body)
    broker, endpoint = make_broker(limits=policy(request_bytes=len(body)))
    _, writer = await exchange(broker, body)
    assert writer.data.startswith(b"HTTP/1.1 502") and not endpoint.writers
    values = metrics(broker)
    assert values["request_bytes_read"] == values["accounted_request_bytes"] == len(header(length=len(body))) + len(body)
    assert values["response_bytes_read"] == 0 and values["cap_reason"] == 3
    assert broker.failure_reason == "egress_limit_exceeded"


async def test_lone_surrogate_utf8_error_is_rejected_before_upstream(make_broker):
    body = payload(instructions="\ud800")
    assert b"\\ud800" in body
    broker, endpoint = make_broker(limits=policy())
    _, writer = await exchange(broker, body)
    assert writer.data.startswith(b"HTTP/1.1 502") and not endpoint.writers
    assert metrics(broker)["response_bytes_read"] == metrics(broker)["cap_reason"] == 0
    assert broker.failure_event.is_set()


async def test_omitted_policy_keeps_legacy_ascii_serialization(make_broker):
    value = json.loads(payload(instructions="café\x7f🙂"))
    body = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    broker, endpoint = make_broker(byte_limit=1 << 20)
    _, writer = await exchange(broker, body)
    assert writer.data.startswith(b"HTTP/1.1 200 OK")
    forwarded = bytes(endpoint.writers[0].data).split(b"\r\n\r\n", 1)[1]
    assert json.loads(forwarded) == value
    assert b"\\u00e9" in forwarded and b"\\u007f" in forwarded and b"\\ud83d\\ude42" in forwarded
    assert b"\xc3\xa9" not in forwarded
