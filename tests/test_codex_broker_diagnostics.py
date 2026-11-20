"""秘密を含む失敗を模擬し、接続ごとの固定分類と既存の停止・制限を検証する。"""

import asyncio
import json
from pathlib import Path
import socket
import ssl
from unittest.mock import AsyncMock, Mock

import h11
import pytest

from kotoyose import codex_egress
from kotoyose.codex_broker_diagnostics import BrokerFailureDiagnostic, BrokerObservation
from kotoyose.codex_egress import CodexEgress
from kotoyose.native_codex_diagnostics import NativeCodexProviderError
from tests.test_codex_egress import header


CANARY = "PRIVATE_BROKER_CANARY_auth_header_body_stderr"


@pytest.mark.parametrize("error,category", [
    (socket.gaierror(-2, CANARY), "dns"),
    (ssl.SSLCertVerificationError(1, CANARY), "tls_certificate"),
    (ssl.SSLError(1, CANARY), "tls"),
    (TimeoutError(CANARY), "timeout"),
    (ConnectionResetError(CANARY), "connection_reset"),
    (ConnectionRefusedError(CANARY), "connection_refused"),
    (BrokenPipeError(CANARY), "connection"),
    (asyncio.IncompleteReadError(CANARY.encode(), 1000), "incomplete_read"),
    (h11.RemoteProtocolError(CANARY), "http_protocol"),
    (OSError(5, CANARY), "io"),
    (ValueError(CANARY), "validation"),
    (RuntimeError(CANARY), "unknown"),
])
def test_exception_category_does_not_reflect_message_attributes_or_type_name(error, category):
    diagnostic = BrokerObservation("upstream_connect").failure(error)
    assert diagnostic == BrokerFailureDiagnostic("upstream_connect", category)
    event = NativeCodexProviderError("broker", broker_diagnostic=diagnostic).as_event()
    assert event.payload == {"schema_version": 1, "failure_stage": "broker", "provider_code": None,
                             "http_status": None, "turn_status": None,
                             "broker_phase": "upstream_connect", "broker_error": category}
    assert CANARY not in repr(diagnostic) + repr(event)


def test_diagnostic_never_formats_unknown_exception():
    class UnformattableError(Exception):
        def __str__(self):
            raise AssertionError("Exception text must not be evaluated")

    assert BrokerObservation().failure(UnformattableError()).category == "unknown"


@pytest.mark.parametrize("phase,category", [(CANARY, CANARY), ([], {}), (None, 42)])
def test_untrusted_diagnostic_values_are_closed_vocabulary(phase, category):
    diagnostic = BrokerFailureDiagnostic(phase, category)
    assert diagnostic == BrokerFailureDiagnostic("unknown", "unknown")
    assert CANARY not in repr(NativeCodexProviderError("broker", broker_diagnostic=diagnostic).as_event())


def test_broker_observation_cannot_appear_on_another_failure_stage():
    event = NativeCodexProviderError("rpc_response", broker_diagnostic=BrokerFailureDiagnostic("upstream_connect", "dns")).as_event()
    assert "broker_phase" not in event.payload and "broker_error" not in event.payload


def request_reader():
    body = json.dumps({"model": "gpt-6-luna", "stream": True, "store": False,
                       "reasoning": {"effort": "high"}}).encode()
    reader = asyncio.StreamReader()
    reader.feed_data(header(length=len(body), fields={"Authorization": f"Bearer {CANARY}"}) + body)
    reader.feed_eof()
    return reader


def writer():
    return Mock(drain=AsyncMock(), wait_closed=AsyncMock())


@pytest.fixture
async def broker():
    value = CodexEgress(1, 65536, "gpt-6-luna", CANARY, None,
                        auth_file=Path("/fixture/not-read-auth"), auth_identity="fixture", reasoning_effort="high")
    value.subscription_request = lambda request: request
    try:
        yield value
    finally:
        assert await value.close(.2) == "confirmed"
        assert not value.tasks and not value.writers


@pytest.mark.parametrize("error,category", [
    (socket.gaierror(-2, CANARY), "dns"),
    (ssl.SSLCertVerificationError(1, CANARY), "tls_certificate"),
    (ConnectionResetError(CANARY), "connection_reset"),
    (TimeoutError(CANARY), "timeout"),
])
async def test_connect_failure_keeps_provider_error_and_first_failure(broker, monkeypatch, error, category):
    connect = AsyncMock(side_effect=error)
    monkeypatch.setattr(codex_egress, "connect_subscription_endpoint", connect)
    await broker.handle(request_reader(), writer())
    assert broker.failure_reason == "provider_error" and broker.failure_http_status is None
    assert broker.failure_diagnostic == BrokerFailureDiagnostic("upstream_connect", category)
    assert broker.failure_event.is_set()
    await broker.handle(request_reader(), writer())
    assert connect.await_count == 1
    assert broker.failure_diagnostic == BrokerFailureDiagnostic("upstream_connect", category)


@pytest.mark.parametrize("raw,phase,category,http,reason", [
    (b"not-http " + CANARY.encode(), "upstream_response_headers", "http_protocol", None, "provider_error"),
    (b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n" + CANARY.encode(),
     "upstream_response_body", "http_protocol", None, "provider_error"),
    (b"HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\n\r\n",
     "upstream_response_headers", "validation", 503, "provider_error"),
    (b"HTTP/1.1 429 Busy\r\nContent-Length: 0\r\n\r\n",
     "upstream_response_headers", "validation", 429, "subscription_limit_reached"),
    (b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n",
     "upstream_response_headers", "validation", 401, "authentication_failed"),
    (b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n",
     "upstream_response_headers", "validation", 403, "subscription_denied"),
])
async def test_response_phase_and_existing_status_classification(broker, monkeypatch, raw, phase, category, http, reason, caplog):
    remote = asyncio.StreamReader()
    remote.feed_data(raw)
    remote.feed_eof()
    upstream = writer()
    monkeypatch.setattr(codex_egress, "connect_subscription_endpoint", AsyncMock(return_value=(remote, upstream)))
    await broker.handle(request_reader(), writer())
    assert broker.failure_reason == reason and broker.failure_http_status == http
    assert broker.failure_diagnostic == BrokerFailureDiagnostic(phase, category)
    event = NativeCodexProviderError("broker", http_status=http, broker_diagnostic=broker.failure_diagnostic).as_event()
    assert CANARY not in repr(event) + caplog.text


async def test_upstream_write_failure_has_distinct_phase(broker, monkeypatch):
    upstream = writer()
    upstream.drain.side_effect = BrokenPipeError(CANARY)
    monkeypatch.setattr(codex_egress, "connect_subscription_endpoint", AsyncMock(return_value=(asyncio.StreamReader(), upstream)))
    await broker.handle(request_reader(), writer())
    assert broker.failure_reason == "provider_error"
    assert broker.failure_diagnostic == BrokerFailureDiagnostic("upstream_write", "connection")


async def test_clean_response_does_not_create_failure_or_diagnostic(broker, monkeypatch):
    remote = asyncio.StreamReader()
    remote.feed_data(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    remote.feed_eof()
    monkeypatch.setattr(codex_egress, "connect_subscription_endpoint", AsyncMock(return_value=(remote, writer())))
    await broker.handle(request_reader(), writer())
    assert broker.failure_reason is None and broker.failure_diagnostic is None
    assert not broker.failure_event.is_set()


async def test_cancelled_connection_has_no_false_provider_failure(broker, monkeypatch):
    monkeypatch.setattr(codex_egress, "connect_subscription_endpoint", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await broker.handle(request_reader(), writer())
    assert broker.failure_reason is None and broker.failure_diagnostic is None
    assert not broker.failure_event.is_set()
