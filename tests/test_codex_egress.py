"""Run 固定 endpoint と runtime artifact の拒否境界。実 provider 通信はしない。"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kotoyose import codex_egress
from kotoyose.codex_egress import CodexEgress, parse_request, public_address
from kotoyose.codex_rootfs import rootfs_digest, validate_rootfs
from tests.codex_runtime_fixture import codex_rootfs  # noqa: F401


def header(target="/v1/responses", method="POST", fields=None, length=2):
    values = {"Host": "127.0.0.1:18080", "Authorization": "Bearer fake",
              "Content-Length": str(length), **(fields or {})}
    return (f"{method} {target} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in values.items()) + "\r\n").encode()


@pytest.mark.parametrize("target", ["http://127.0.0.1/", "https://api.openai.com/v1/responses",
    "//api.openai.com/v1/responses", "http://169.254.169.254/", "/v1/responses/../models",
    "/v1/%72esponses", "/v1/responses?url=http://localhost", "/v1/files"])
def test_arbitrary_targets_and_bypass_paths_are_rejected(target):
    with pytest.raises(ValueError):
        parse_request(header(target))


@pytest.mark.parametrize("fields", [{"Host": "api.openai.com"}, {"Host": "localhost"},
    {"Transfer-Encoding": "chunked"}, {"Proxy-Authorization": "fake"}, {"Upgrade": "websocket"},
    {"X-Forwarded-Host": "metadata"}, {"Content-Length": "-1"}, {"Content-Length": "999999999"},
    {"OpenAI-Organization": "other"}, {"OpenAI-Project": "other"}, {"X-Arbitrary": "anything"}])
def test_proxy_authority_framing_and_override_headers_are_rejected(fields):
    with pytest.raises(ValueError):
        parse_request(header(fields=fields))


def test_fixed_cli_metadata_is_accepted_but_never_changes_upstream_transport():
    request, _ = parse_request(header(fields={"session-id": "fixture", "thread-id": "fixture",
        "x-codex-window-id": "fixture", "x-codex-turn-metadata": "{}",
        "x-openai-internal-codex-responses-lite": "true"}))
    assert b"host: api.openai.com\r\n" in request
    assert b"session-id" not in request and b"x-codex" not in request
    assert b"responses-lite" not in request


@pytest.mark.parametrize("value", ["127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.0.1",
    "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1", "::", "fc00::1", "fe80::1",
    "ff02::1", "::ffff:8.8.8.8", "fec0::1", "192.0.2.1"])
def test_private_metadata_reserved_and_multicast_addresses_are_denied(value):
    assert not public_address(value)


async def test_dns_mixed_private_answer_is_rejected_before_connect(monkeypatch):
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[
        (2, 1, 6, "", ("8.8.8.8", 443)), (2, 1, 6, "", ("127.0.0.1", 443))]))
    connect = AsyncMock()
    monkeypatch.setattr(asyncio, "open_connection", connect)
    with pytest.raises(ValueError):
        await codex_egress.connect_endpoint()
    connect.assert_not_awaited()


async def test_fixed_ip_sni_and_public_ca_ignore_host_proxy_and_cert_overrides(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://localhost:1234")
    monkeypatch.setenv("SSL_CERT_FILE", "/fake/private-cert")
    resolve = AsyncMock(return_value=[(2, 1, 6, "", ("8.8.8.8", 443))])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    connect = AsyncMock(return_value=("reader", "writer"))
    monkeypatch.setattr(asyncio, "open_connection", connect)
    assert await codex_egress.connect_endpoint() == ("reader", "writer")
    assert resolve.call_args.args == ("api.openai.com", 443)
    assert connect.call_args.args == ("8.8.8.8", 443)
    assert connect.call_args.kwargs["server_hostname"] == "api.openai.com"
    assert connect.call_args.kwargs["ssl"].check_hostname


@pytest.mark.parametrize("payload", [{"model": "other"}, {"model": "fixed", "tools": [{"type": "web_search"}]},
    {"model": "fixed", "tools": [{"type": "function", "name": "view_image", "parameters": {}}]},
    {"model": "fixed", "tool_choice": "required"}, {"model": "fixed", "tools": "bad"},
    {"model": "fixed", "background": True}, {"model": "fixed", "stream": False},
    {"model": "fixed", "store": True}, {"model": "fixed", "service_tier": "priority"}])
async def test_broker_rejects_model_and_hosted_tool_escalation_before_upstream(monkeypatch, payload):
    broker = CodexEgress(1, 65536, "fixed", "fake", None)
    connect = AsyncMock()
    monkeypatch.setattr(codex_egress, "connect_endpoint", connect)
    path = await broker.start()
    reader, writer = await asyncio.open_unix_connection(path)
    body = json.dumps({"stream": True, "store": False, **payload}).encode()
    writer.write(header(length=len(body)) + body)
    await writer.drain()
    assert b"502" in await reader.read()
    writer.close()
    await writer.wait_closed()
    assert await broker.close(1) == "confirmed"
    connect.assert_not_awaited()


@pytest.mark.parametrize("item", [
    {"type": "additional_tools", "role": "developer", "tools": [{"type": "web_search"}]},
    {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "https://external.invalid"}]},
    {"type": "message", "role": "user", "content": [{"type": "input_file", "file_id": "arbitrary"}]},
    {"type": "tool_search_output", "tools": [{"type": "web_search"}]},
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x", "annotations": [{"url": "https://external.invalid"}]}]},
    {"type": "function_call", "name": "view_image", "arguments": "{}"},
])
def test_input_cannot_smuggle_new_tools_or_remote_references(item):
    broker = CodexEgress(1, 65536, "fixed", "fake", None)
    body = json.dumps({"model": "fixed", "stream": True, "store": False, "input": [item]}).encode()
    request, _ = parse_request(header(length=len(body)))
    try:
        with pytest.raises(ValueError):
            broker.validate_request(request, body)
    finally:
        broker.directory.cleanup()


@pytest.mark.parametrize("suffix", [',"model":"other"', ',"tools":[{"type":"web_search"}]',
    ',"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"x","text":"y"}]}]',
    ',"client_metadata":{"a":"x","a":"y"}', ',"max_output_tokens":NaN'])
def test_ambiguous_json_is_rejected_before_forwarding(suffix):
    broker = CodexEgress(1, 65536, "fixed", "fake", None)
    body = ('{"model":"fixed","stream":true,"store":false,"tools":[]' + suffix + '}').encode()
    request, _ = parse_request(header(length=len(body)))
    try:
        with pytest.raises(ValueError):
            broker.validate_request(request, body)
    finally:
        broker.directory.cleanup()


async def test_broker_close_converges_pending_connection_and_emits_no_secret(monkeypatch):
    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def connect():
        entered.set()
        await blocked.wait()

    monkeypatch.setattr(codex_egress, "connect_endpoint", connect)
    broker = CodexEgress(30, 65536, "fixed", "fake-secret", None)
    path = await broker.start()
    reader, writer = await asyncio.open_unix_connection(path)
    body = b'{"model":"fixed","stream":true,"store":false}'
    writer.write(header(length=len(body), fields={"Authorization": "Bearer fake-secret"}) + body)
    await writer.drain()
    await asyncio.wait_for(entered.wait(), 1)
    assert await broker.close(1) == "confirmed"
    assert not broker.tasks and not broker.writers and not path.exists()
    assert b"fake-secret" not in await reader.read()
    writer.close()
    await writer.wait_closed()


def test_reviewed_rootfs_pins_inventory_and_never_accepts_private_cert_or_controller(tmp_path):
    root = tmp_path.resolve()
    for name in ("tmp", "home", "runtime", "proc", "dev"):
        (root / name).mkdir(mode=0o755)
    file = root / "usr/bin/python3"
    file.parent.mkdir(parents=True)
    file.parent.chmod(0o755)
    file.parent.parent.chmod(0o755)
    for name in ("tmp", "home", "runtime", "proc", "dev"):
        (root / name).chmod(0o755)
    file.write_bytes(b"fake-interpreter")
    file.chmod(0o755)
    pin = rootfs_digest(root)
    assert validate_rootfs(root, pin) == root
    file.write_bytes(b"changed")
    with pytest.raises(ValueError):
        validate_rootfs(root, pin)
    private = root / "etc/ssl/private/sentinel"
    private.parent.mkdir(parents=True)
    private.write_text("nonsecret-fixture")
    with pytest.raises(ValueError):
        rootfs_digest(root)


def test_missing_rootfs_fails_closed():
    with pytest.raises(ValueError):
        validate_rootfs(None, None)


def test_delegate_tool_schemas_and_history_require_pinned_refs():
    from kotoyose.execution_delegation import LIST_AGENTS, TOOL_SPECS

    schema = TOOL_SPECS[LIST_AGENTS]["input_schema"]
    body = {"model": "fixed", "stream": True, "store": False,
            "tools": [{"type": "function", "name": "list_agents", "parameters": schema}],
            "input": [{"type": "function_call", "name": "list_agents", "arguments": "{}", "call_id": "1"},
                      {"type": "function_call_output", "call_id": "1", "output": "[]"}]}
    request = b"POST /v1/responses HTTP/1.1\r\nauthorization: Bearer fake\r\n\r\n"
    enabled = CodexEgress(1, 65536, "fixed", "fake", None, delegation_refs=frozenset({LIST_AGENTS}))
    disabled = CodexEgress(1, 65536, "fixed", "fake", None)
    try:
        enabled.validate_request(request, json.dumps(body).encode())
        with pytest.raises(ValueError, match="egress_denied"):
            disabled.validate_request(request, json.dumps(body).encode())
        body["input"][0]["name"] = "ask_agent"
        with pytest.raises(ValueError, match="egress_denied"):
            enabled.validate_request(request, json.dumps(body).encode())
    finally:
        enabled.directory.cleanup()
        disabled.directory.cleanup()
