"""Provider egress broker の境界試験。実 provider へは接続せず、ローカルの fake upstream だけを使う。"""

import asyncio
import contextlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from kotoyose.execution_resources import AgentContainerConfig
from kotoyose.provider_egress import (EgressRoute, EgressSpec, EgressUpstream, ProviderEgress, RELAY_HOST)

CAPABILITY = "cap-" + "x" * 40
REAL_KEY = "sk\x2dreal-provider-secret-0123456789"
BODY = b'{"model":"m","messages":[]}'
ROUTES = (EgressRoute("POST", "/v1/messages"), EgressRoute("POST", "/v1/messages/count_tokens"))
DEFAULT_HEADERS = [("Host", RELAY_HOST), ("Content-Type", "application/json"),
                   ("anthropic-version", "2023-06-01"), ("x-api-key", CAPABILITY)]
PASS_HEADERS = frozenset({"anthropic-version", "anthropic-beta", "content-type", "accept", "user-agent"})


@pytest.fixture
def short_dir():
    # UNIX socket の path 上限に当たらないよう /tmp 直下に作る。
    path = tempfile.mkdtemp(dir="/tmp", prefix="pe")
    yield Path(path)
    shutil.rmtree(path, ignore_errors=True)


def _chunk(data: bytes) -> bytes:
    return f"{len(data):x}\r\n".encode() + data + b"\r\n"


class FakeUpstream:
    """plain TCP の固定応答 upstream。受けた request を記録し、接続数を数える。"""

    def __init__(self):
        self.requests: list[dict] = []
        self.connections = 0
        self.mode = "json"
        self.release = asyncio.Event()
        self.server = None
        self.port = 0

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        self.release.set()
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        self.connections += 1
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            headers = {}
            for line in lines[1:-2]:
                name, _, value = line.partition(":")
                headers[name.lower()] = value.strip()
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            self.requests.append({"line": lines[0], "headers": headers, "body": body})
            if self.mode == "sse":
                writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
                             b"transfer-encoding: chunked\r\nconnection: close\r\n\r\n")
                writer.write(_chunk(b"event: start\ndata: 1\n\n"))
                await writer.drain()
                # 2 本目は test が受信を確認してから流す。broker が応答を溜めると試験は止まる。
                await self.release.wait()
                writer.write(_chunk(b"event: stop\ndata: 2\n\n") + b"0\r\n\r\n")
            elif self.mode == "big":
                payload = b"a" * 997 + b"END"
                writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: application/octet-stream\r\n"
                             b"content-length: 1000\r\nconnection: close\r\n\r\n")
                for start in range(0, len(payload), 50):
                    writer.write(payload[start:start + 50])
                    await writer.drain()
            else:
                payload = b'{"id":"msg_1","type":"message"}'
                writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                             + f"content-length: {len(payload)}\r\nconnection: close\r\n\r\n".encode() + payload)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()


@pytest.fixture
async def upstream():
    fake = await FakeUpstream().start()
    yield fake
    await fake.close()


def make_spec(port: int, **overrides) -> EgressSpec:
    values = {
        "upstream": EgressUpstream(host="127.0.0.1", port=port, tls=False, allow_private=True),
        "routes": ROUTES,
        "inject_headers": {"x-api-key": REAL_KEY},
        "capability": CAPABILITY,
        "capability_header": "x-api-key",
        "pass_headers": PASS_HEADERS,
        "max_request_bytes": 65536,
        "max_response_bytes": 1 << 20,
        "idle_timeout_seconds": 2.0,
    }
    values.update(overrides)
    return EgressSpec(**values)


@pytest.fixture
async def broker(short_dir, upstream):
    started: list[ProviderEgress] = []

    async def make(socket_name: str = "egress.sock", **overrides) -> ProviderEgress:
        egress = ProviderEgress(make_spec(upstream.port, **overrides), short_dir / socket_name)
        await egress.start()
        started.append(egress)
        return egress

    yield make
    for egress in started:
        await egress.close()


def build_request(*, method="POST", target="/v1/messages", headers=None, body=BODY, keep_alive=False) -> bytes:
    fields = list(DEFAULT_HEADERS if headers is None else headers)
    fields.append(("Content-Length", str(len(body))))
    if not keep_alive:
        fields.append(("Connection", "close"))
    head = f"{method} {target} HTTP/1.1\r\n" + "".join(f"{name}: {value}\r\n" for name, value in fields) + "\r\n"
    return head.encode() + body


async def exchange(socket_path: Path, raw: bytes, timeout: float = 5.0) -> bytes:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        with contextlib.suppress(ConnectionError):
            writer.write(raw)
            await writer.drain()
        return await asyncio.wait_for(reader.read(), timeout)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def status_of(raw: bytes) -> int:
    return int(raw.split(b" ", 2)[1])


def error_of(raw: bytes) -> str:
    return json.loads(raw.partition(b"\r\n\r\n")[2])["error"]


async def test_allowed_route_forwards_with_injected_credential_and_never_the_capability(upstream, broker):
    egress = await broker()
    fields = [*DEFAULT_HEADERS, ("anthropic-beta", "beta-flag"), ("X-Custom", "must-not-pass")]
    raw = await exchange(egress.socket_path, build_request(headers=fields))

    assert status_of(raw) == 200
    assert raw.endswith(b'{"id":"msg_1","type":"message"}')
    [seen] = upstream.requests
    assert seen["line"] == "POST /v1/messages HTTP/1.1"
    assert seen["headers"]["x-api-key"] == REAL_KEY
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["headers"]["anthropic-beta"] == "beta-flag"
    assert seen["headers"]["host"] == f"127.0.0.1:{upstream.port}"
    assert "x-custom" not in seen["headers"]
    assert CAPABILITY not in str(seen["headers"])
    assert seen["body"] == BODY


async def test_query_string_is_matched_out_and_forwarded(upstream, broker):
    egress = await broker()
    raw = await exchange(egress.socket_path, build_request(target="/v1/messages?beta=true"))

    assert status_of(raw) == 200
    assert upstream.requests[0]["line"] == "POST /v1/messages?beta=true HTTP/1.1"


@pytest.mark.parametrize("fields", [
    [field for field in DEFAULT_HEADERS if field[0] != "x-api-key"],
    [*DEFAULT_HEADERS[:3], ("x-api-key", "wrong-capability-value-0000")],
    [*DEFAULT_HEADERS[:3], ("x-api-key", REAL_KEY)],
    [*DEFAULT_HEADERS, ("x-api-key", CAPABILITY)],
], ids=["missing", "wrong", "real-key-in-capability-slot", "duplicated"])
async def test_missing_or_wrong_capability_is_denied_without_contacting_upstream(upstream, broker, fields):
    egress = await broker()
    raw = await exchange(egress.socket_path, build_request(headers=fields))

    assert status_of(raw) == 403
    assert error_of(raw) == "capability_denied"
    assert upstream.connections == 0
    assert egress.metrics()["denied"] == 1


async def test_bearer_capability_form_is_accepted_in_authorization_slot(upstream, broker):
    egress = await broker(capability_header="authorization", inject_headers={"authorization": f"Bearer {REAL_KEY}"})
    bearer = [("Host", RELAY_HOST), ("Authorization", f"Bearer {CAPABILITY}"), ("Content-Type", "application/json")]
    raw_form = [("Host", RELAY_HOST), ("Authorization", CAPABILITY), ("Content-Type", "application/json")]

    assert status_of(await exchange(egress.socket_path, build_request(headers=bearer))) == 200
    assert status_of(await exchange(egress.socket_path, build_request(headers=raw_form))) == 200
    assert [request["headers"]["authorization"] for request in upstream.requests] == [f"Bearer {REAL_KEY}"] * 2


@pytest.mark.parametrize("method,target", [
    ("GET", "/v1/messages"),
    ("PUT", "/v1/messages"),
    ("POST", "/v1/files"),
    ("POST", "/v1/messagesX"),
    ("POST", "/v1/messages/count_tokens/extra"),
    ("POST", "http://evil.example/v1/messages"),
])
async def test_disallowed_method_or_path_is_denied(upstream, broker, method, target):
    egress = await broker()
    raw = await exchange(egress.socket_path, build_request(method=method, target=target))

    assert status_of(raw) == 403
    assert error_of(raw) == "route_denied"
    assert upstream.connections == 0


async def test_prefix_route_rejects_dot_segments_and_escapes(upstream, broker):
    egress = await broker(routes=(EgressRoute("POST", "/v1/prefix/*"),))
    for target in ("/v1/prefix/../secret", "/v1/prefix/./x", "/v1/prefix//x", "/v1/%70refix/x"):
        assert status_of(await exchange(egress.socket_path, build_request(target=target))) == 403
    assert upstream.connections == 0
    assert status_of(await exchange(egress.socket_path, build_request(target="/v1/prefix/ok"))) == 200


@pytest.mark.parametrize("name,value", [
    ("Proxy-Authorization", "fake"),
    ("Proxy-Connection", "keep-alive"),
    ("X-Forwarded-For", "203.0.113.9"),
    ("X-Forwarded-Host", "metadata"),
    ("Forwarded", "for=203.0.113.9"),
    ("Upgrade", "websocket"),
])
async def test_forbidden_proxy_or_forward_header_is_denied(upstream, broker, name, value):
    egress = await broker()
    raw = await exchange(egress.socket_path, build_request(headers=[*DEFAULT_HEADERS, (name, value)]))

    assert status_of(raw) == 403
    assert error_of(raw) == "header_denied"
    assert upstream.connections == 0


@pytest.mark.parametrize("host", ["api.anthropic.com", "localhost:18080", "127.0.0.1"])
async def test_host_other_than_relay_address_is_denied(upstream, broker, host):
    egress = await broker()
    fields = [("Host", host), *DEFAULT_HEADERS[1:]]
    raw = await exchange(egress.socket_path, build_request(headers=fields))

    assert status_of(raw) == 403
    assert error_of(raw) == "host_denied"
    assert upstream.connections == 0


async def test_request_body_over_limit_is_rejected_before_upstream(upstream, broker):
    egress = await broker(max_request_bytes=16)
    raw = await exchange(egress.socket_path, build_request(body=b"x" * 100))

    assert status_of(raw) == 413
    assert upstream.connections == 0
    assert egress.metrics()["denied"] == 1


async def test_streaming_response_is_relayed_as_it_arrives(upstream, broker):
    upstream.mode = "sse"
    egress = await broker()
    reader, writer = await asyncio.open_unix_connection(str(egress.socket_path))
    try:
        writer.write(build_request())
        await writer.drain()
        received = b""
        # upstream は 2 本目を release 後にしか流さない。最初の event が届く時点で
        # broker が溜め込んでいれば、ここで wait_for がタイムアウトする。
        while b"event: start" not in received:
            received += await asyncio.wait_for(reader.read(4096), 5)
        assert not upstream.release.is_set()
        upstream.release.set()
        received += await asyncio.wait_for(reader.read(), 5)
    finally:
        writer.close()

    assert b"transfer-encoding: chunked" in received.lower()
    assert b"event: start" in received and b"event: stop" in received
    assert received.endswith(b"0\r\n\r\n")


async def test_response_over_limit_is_cut_without_completing_the_body(upstream, broker):
    upstream.mode = "big"
    egress = await broker(max_response_bytes=100)
    raw = await exchange(egress.socket_path, build_request())

    assert b"END" not in raw
    assert len(raw.partition(b"\r\n\r\n")[2]) < 1000


async def test_keep_alive_serves_two_requests_on_one_connection(upstream, broker):
    egress = await broker()
    reader, writer = await asyncio.open_unix_connection(str(egress.socket_path))
    try:
        for _ in range(2):
            writer.write(build_request(keep_alive=True))
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            length = int(next(line.split(b":")[1] for line in head.split(b"\r\n")
                              if line.lower().startswith(b"content-length")))
            body = await asyncio.wait_for(reader.readexactly(length), 5)
            assert status_of(head) == 200
            assert body == b'{"id":"msg_1","type":"message"}'
    finally:
        writer.close()
    assert upstream.connections == 2


async def test_inject_values_never_reach_metrics_or_denial_bodies(broker):
    egress = await broker()
    denied = await exchange(egress.socket_path, build_request(headers=[*DEFAULT_HEADERS[:3], ("x-api-key", REAL_KEY)]))

    assert status_of(denied) == 403
    assert REAL_KEY.encode() not in denied
    assert REAL_KEY not in json.dumps(egress.metrics())
    assert egress.redact(f"before {REAL_KEY} after {CAPABILITY}") == "before [redacted] after [redacted]"


async def test_base_path_is_prefixed_for_the_upstream_request(upstream, broker):
    egress = await broker(upstream=EgressUpstream(host="127.0.0.1", port=upstream.port, tls=False,
                                                  base_path="/anthropic", allow_private=True))
    raw = await exchange(egress.socket_path, build_request(target="/v1/messages?beta=true"))

    assert status_of(raw) == 200
    assert upstream.requests[0]["line"] == "POST /anthropic/v1/messages?beta=true HTTP/1.1"


async def test_socket_is_owner_group_only_and_removed_on_close(short_dir, upstream):
    egress = ProviderEgress(make_spec(upstream.port), short_dir / "mode.sock")
    path = await egress.start()
    try:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o660
    finally:
        await egress.close()
    assert not path.exists()


async def test_socket_path_longer_than_unix_limit_is_refused(short_dir, upstream):
    path = short_dir / ("s" * 120 + ".sock")
    egress = ProviderEgress(make_spec(upstream.port), path)

    with pytest.raises(ValueError):
        await egress.start()
    assert not path.exists()


@pytest.mark.parametrize("overrides", [
    {"pass_headers": frozenset({"Anthropic-Version"})},
    {"pass_headers": frozenset({"authorization"}), "capability_header": "authorization"},
    {"pass_headers": frozenset({"host"})},
    {"capability": "short"},
    {"inject_headers": {"x-api-key": "bad\r\nvalue"}},
    {"routes": ()},
    {"pass_headers": frozenset({"accept\n"})},
    {"capability_header": "x-api-key\n"},
    {"inject_headers": {"x-api-key\n": REAL_KEY}},
    {"max_connections": 0},
    {"request_deadline_seconds": 0},
])
def test_spec_rejects_unsafe_configuration(overrides):
    with pytest.raises(ValueError):
        make_spec(1, **overrides)


def test_agent_container_config_defaults_and_short_socket_root():
    config = AgentContainerConfig(agent_root="/opt/agent", home_root="/var/lib/kotoyose/home")

    assert config.cpus == 2
    assert config.workspace_memory_bytes == 6 * 1024**3
    assert config.pids_limit == 2048
    assert config.egress_socket_root == "/run/kotoyose"
    overridden = AgentContainerConfig(
        agent_root="/opt/agent", home_root="/var/lib/kotoyose/home", cpus=4, workspace_memory_bytes=8 * 1024**3,
    )
    assert overridden.cpus == 4
    assert overridden.workspace_memory_bytes == 8 * 1024**3
    with pytest.raises(ValidationError):
        AgentContainerConfig(agent_root="/opt/agent", home_root="/var/lib/kotoyose/home",
                             egress_socket_root="/" + "r" * 60)
    with pytest.raises(ValidationError):
        AgentContainerConfig(agent_root="opt/agent", home_root="/var/lib/kotoyose/home")
    with pytest.raises(ValidationError):
        AgentContainerConfig(agent_root="/opt/agent", home_root="/var/lib/kotoyose/home", unknown=1)


async def test_connection_beyond_the_cap_gets_503_and_is_not_served(upstream, broker):
    egress = await broker(max_connections=1)
    # 1 本目は request を送らず保持する。受理済み task として枠を占める。
    holder_reader, holder_writer = await asyncio.open_unix_connection(str(egress.socket_path))
    try:
        await asyncio.sleep(0.05)
        raw = await exchange(egress.socket_path, build_request())

        assert status_of(raw) == 503
        assert error_of(raw) == "busy"
        assert upstream.connections == 0
    finally:
        holder_writer.close()
        with contextlib.suppress(Exception):
            await holder_writer.wait_closed()
    # 枠が空けば通常どおり処理される。
    assert status_of(await exchange(egress.socket_path, build_request())) == 200


async def test_private_upstream_address_is_refused_unless_allowed(upstream, broker):
    refused = await broker(socket_name="refused.sock",
                           upstream=EgressUpstream(host="127.0.0.1", port=upstream.port, tls=False))
    raw = await exchange(refused.socket_path, build_request())

    assert status_of(raw) == 502
    assert error_of(raw) == "upstream_unavailable"
    assert upstream.connections == 0


@pytest.mark.parametrize("name", ["x-api-key\n", "accept\r\n", "x-api-key\x00"])
def test_header_name_with_trailing_control_character_is_rejected(name):
    with pytest.raises(ValueError):
        make_spec(1, capability_header=name)


async def test_socket_in_group_or_world_writable_directory_is_refused(short_dir, upstream):
    for mode in (0o770, 0o707):
        directory = short_dir / f"open{mode:o}"
        directory.mkdir()
        os.chmod(directory, mode)
        egress = ProviderEgress(make_spec(upstream.port), directory / "egress.sock")
        with pytest.raises(PermissionError):
            await egress.start()
        assert not (directory / "egress.sock").exists()


async def test_close_does_not_unlink_a_socket_that_replaced_ours(short_dir, upstream):
    path = short_dir / "replaced.sock"
    egress = ProviderEgress(make_spec(upstream.port), path)
    await egress.start()
    os.unlink(path)
    replacement = await asyncio.start_unix_server(lambda reader, writer: writer.close(), path=str(path))
    try:
        await egress.close()
        assert path.exists()
        assert stat.S_ISSOCK(os.lstat(path).st_mode)
    finally:
        replacement.close()
        await replacement.wait_closed()


async def test_request_deadline_closes_a_stalled_exchange(upstream, broker):
    # SSE upstream は 2 本目を流さないため、idle timeout (2s) より先に request deadline が効く。
    upstream.mode = "sse"
    egress = await broker(request_deadline_seconds=0.5)
    loop = asyncio.get_running_loop()
    started = loop.time()
    raw = await exchange(egress.socket_path, build_request(), timeout=5)

    assert b"event: start" in raw
    assert b"event: stop" not in raw
    assert loop.time() - started < 1.5
