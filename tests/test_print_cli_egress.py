"""Tests for the pass-through HTTP CONNECT egress broker (PrintCliEgress)."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
import tempfile

import pytest
import structlog

from kotoyose.print_cli_egress import (
    DEFAULT_MAX_TUNNELS,
    PrintCliEgress,
)


async def _start_echo_server() -> tuple[asyncio.AbstractServer, int, list[tuple[str, int]]]:
    connections: list[tuple[str, int]] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connections.append(("connected", len(connections) + 1))
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    with contextlib.suppress(OSError, NotImplementedError, RuntimeError):
                        if writer.can_write_eof():
                            writer.write_eof()
                    break
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port, connections


async def _connect_unix_and_send(socket_path: Path, payload: bytes) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(payload)
    await writer.drain()
    return reader, writer


@pytest.mark.asyncio
async def test_allowed_host_tunnel_and_exact_byte_passthrough() -> None:
    echo_server, echo_port, connections = await _start_echo_server()
    with tempfile.TemporaryDirectory(prefix="pce-") as tmpdir:
        sock_path = Path(tmpdir) / "egress.sock"

        async def resolver(host: str, port: int) -> list[tuple[str, int]]:
            assert host == "daily-cloudcode-pa.googleapis.com"
            assert port == 443
            return [("127.0.0.1", echo_port)]

        broker = PrintCliEgress(
            sock_path,
            allowed_hosts=("daily-cloudcode-pa.googleapis.com",),
            allowed_ports=(443,),
            run_id="run-123",
            allow_private=True,
            resolver=resolver,
        )
        await broker.start()
        try:
            secret_payload = os.urandom(32 * 1024) + b"\x00\xff\r\nSECRET-BODY-BYTES"
            with structlog.testing.capture_logs() as cap:
                reader, writer = await _connect_unix_and_send(
                    sock_path,
                    (
                        b"CONNECT DAILY-CLOUDCODE-PA.GOOGLEAPIS.COM:443 HTTP/1.1\r\n"
                        b"Host: DAILY-CLOUDCODE-PA.GOOGLEAPIS.COM:443\r\n"
                        b"User-Agent: agy-secret-ua\r\n\r\n"
                    ),
                )
                status_head = await reader.readuntil(b"\r\n\r\n")
                assert status_head == b"HTTP/1.1 200 Connection Established\r\n\r\n"

                writer.write(secret_payload)
                writer.write_eof()
                await writer.drain()
                echoed = await reader.read()
                assert echoed == secret_payload
                writer.close()
                await writer.wait_closed()

            assert len(connections) == 1
            assert cap == [
                {
                    "event": "print_cli_egress.allowed",
                    "log_level": "info",
                    "run_id": "run-123",
                    "host": "daily-cloudcode-pa.googleapis.com",
                    "port": 443,
                }
            ]
            assert "agy-secret-ua" not in str(cap)
            assert "SECRET-BODY-BYTES" not in str(cap)
        finally:
            await broker.close()
            echo_server.close()
            await echo_server.wait_closed()


@pytest.mark.asyncio
async def test_pipelined_bytes_after_connect_are_preserved() -> None:
    echo_server, echo_port, _ = await _start_echo_server()
    with tempfile.TemporaryDirectory(prefix="pce-") as tmpdir:
        sock_path = Path(tmpdir) / "egress.sock"

        async def resolver(_host: str, _port: int) -> list[tuple[str, int]]:
            return [("127.0.0.1", echo_port)]

        broker = PrintCliEgress(
            sock_path,
            allowed_hosts=("aicode.googleapis.com",),
            run_id="run-pipe",
            allow_private=True,
            resolver=resolver,
        )
        await broker.start()
        try:
            pipelined = b"\x16\x03\x01\x00\x10PIPELINED_TLS_CLIENTHELLO"
            reader, writer = await _connect_unix_and_send(
                sock_path,
                b"CONNECT aicode.googleapis.com:443 HTTP/1.1\r\n\r\n" + pipelined,
            )
            status_head = await reader.readuntil(b"\r\n\r\n")
            assert status_head == b"HTTP/1.1 200 Connection Established\r\n\r\n"
            writer.write_eof()
            await writer.drain()
            echoed = await reader.read()
            assert echoed == pipelined
            writer.close()
            await writer.wait_closed()
        finally:
            await broker.close()
            echo_server.close()
            await echo_server.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_request",
    [
        b"CONNECT evil.example:443 HTTP/1.1\r\nHost: evil.example:443\r\n\r\n",
        b"CONNECT sub.aicode.googleapis.com:443 HTTP/1.1\r\n\r\n",
        b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n",
        b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n",
        b"CONNECT aicode.googleapis.com:80 HTTP/1.1\r\n\r\n",
        b"CONNECT aicode.googleapis.com:8443 HTTP/1.1\r\n\r\n",
        b"GET https://aicode.googleapis.com/v1/models HTTP/1.1\r\nAuthorization: secret-bearer\r\n\r\n",
        b"CONNECT aicode.googleapis.com:443 HTTP/1.0\r\n\r\n",
        b"CONNECT aicode.googleapis.com:443 HTTP/1.1\r\nX-Big: " + (b"a" * 9000) + b"\r\n\r\n",
    ],
)
async def test_denied_requests_return_403_without_upstream_connect(raw_request: bytes) -> None:
    echo_server, echo_port, connections = await _start_echo_server()
    with tempfile.TemporaryDirectory(prefix="pce-") as tmpdir:
        sock_path = Path(tmpdir) / "egress.sock"

        async def resolver(_host: str, _port: int) -> list[tuple[str, int]]:
            return [("127.0.0.1", echo_port)]

        broker = PrintCliEgress(
            sock_path,
            allowed_hosts=("aicode.googleapis.com",),
            allowed_ports=(443,),
            run_id="run-deny",
            max_header_bytes=8192,
            allow_private=True,
            resolver=resolver,
        )
        await broker.start()
        try:
            with structlog.testing.capture_logs() as cap:
                reader, writer = await _connect_unix_and_send(sock_path, raw_request)
                resp = await reader.read()
                assert resp.startswith(b"HTTP/1.1 403 ")
                assert connections == []
                writer.close()
                await writer.wait_closed()

            assert len(cap) == 1
            assert cap[0]["event"] == "print_cli_egress.denied"
            assert cap[0]["run_id"] == "run-deny"
            assert "secret-bearer" not in str(cap)
        finally:
            await broker.close()
            echo_server.close()
            await echo_server.wait_closed()


@pytest.mark.asyncio
async def test_private_ip_resolution_denied_by_default() -> None:
    echo_server, echo_port, connections = await _start_echo_server()
    with tempfile.TemporaryDirectory(prefix="pce-") as tmpdir:
        sock_path = Path(tmpdir) / "egress.sock"

        async def resolver(_host: str, _port: int) -> list[tuple[str, int]]:
            return [("127.0.0.1", echo_port)]

        broker = PrintCliEgress(
            sock_path,
            allowed_hosts=("aicode.googleapis.com",),
            run_id="run-private",
            allow_private=False,
            resolver=resolver,
        )
        await broker.start()
        try:
            reader, writer = await _connect_unix_and_send(
                sock_path,
                b"CONNECT aicode.googleapis.com:443 HTTP/1.1\r\n\r\n",
            )
            resp = await reader.read()
            assert resp.startswith(b"HTTP/1.1 403 ")
            assert connections == []
            writer.close()
            await writer.wait_closed()
        finally:
            await broker.close()
            echo_server.close()
            await echo_server.wait_closed()


@pytest.mark.asyncio
async def test_max_tunnels_limit_idle_timeout_and_close_cleanup() -> None:
    assert DEFAULT_MAX_TUNNELS == 32
    echo_server, echo_port, _ = await _start_echo_server()
    with tempfile.TemporaryDirectory(prefix="pce-") as tmpdir:
        sock_path = Path(tmpdir) / "egress.sock"

        async def resolver(_host: str, _port: int) -> list[tuple[str, int]]:
            return [("127.0.0.1", echo_port)]

        broker = PrintCliEgress(
            sock_path,
            allowed_hosts=("aicode.googleapis.com",),
            run_id="run-limits",
            max_tunnels=2,
            idle_timeout_seconds=0.15,
            allow_private=True,
            resolver=resolver,
        )
        await broker.start()
        try:
            r1, w1 = await _connect_unix_and_send(sock_path, b"CONNECT aicode.googleapis.com:443 HTTP/1.1\r\n\r\n")
            assert (await r1.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200 ")
            r2, w2 = await _connect_unix_and_send(sock_path, b"CONNECT aicode.googleapis.com:443 HTTP/1.1\r\n\r\n")
            assert (await r2.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200 ")

            # Third concurrent tunnel exceeds max_tunnels=2
            r3, w3 = await _connect_unix_and_send(sock_path, b"CONNECT aicode.googleapis.com:443 HTTP/1.1\r\n\r\n")
            assert (await r3.read()).startswith(b"HTTP/1.1 403 ")

            # Idle timeout closes w1/r1 and w2/r2
            assert await asyncio.wait_for(r1.read(), timeout=1.0) == b""
            assert await asyncio.wait_for(r2.read(), timeout=1.0) == b""
            for w in (w1, w2, w3):
                w.close()
                await w.wait_closed()
        finally:
            await broker.close()
            echo_server.close()
            await echo_server.wait_closed()

        assert not sock_path.exists()


def test_constructor_rejects_wildcards_and_ip_literals() -> None:
    with tempfile.TemporaryDirectory(prefix="pce-") as tmpdir:
        sock = Path(tmpdir) / "e.sock"
        with pytest.raises(ValueError):
            PrintCliEgress(sock, allowed_hosts=("*.googleapis.com",), run_id="r1")
        with pytest.raises(ValueError):
            PrintCliEgress(sock, allowed_hosts=("127.0.0.1",), run_id="r1")
        with pytest.raises(ValueError):
            PrintCliEgress(sock, allowed_hosts=("aicode.googleapis.com",), allowed_ports=(0,), run_id="r1")


@pytest.mark.parametrize(
    ("limits", "first_payload", "expected_echo", "second_status"),
    [
        ({"max_total_bytes": 6}, b"abc", b"abc", b"403"),
        ({"max_total_bytes": 5}, b"abc", b"", b"403"),
        ({"max_total_tunnels": 1}, b"abc", b"abc", b"403"),
        ({"wall_timeout_seconds": 0.2}, b"", b"", b"200"),
    ],
)
async def test_connection_budgets(limits, first_payload, expected_echo, second_status):
    server, port, _ = await _start_echo_server()

    async def resolver(_host: str, _port: int) -> list[tuple[str, int]]:
        return [("127.0.0.1", port)]

    request = b"CONNECT chatgpt.com:443 HTTP/1.1\r\n\r\n"
    with tempfile.TemporaryDirectory(prefix="pce-") as tmpdir:
        socket_path = Path(tmpdir) / "egress.sock"
        broker = PrintCliEgress(
            socket_path, allowed_hosts=("chatgpt.com",), run_id="budgets",
            allow_private=True, resolver=resolver, idle_timeout_seconds=10, **limits,
        )
        await broker.start()
        try:
            reader, writer = await _connect_unix_and_send(socket_path, request + first_payload)
            try:
                assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200 ")
                if first_payload:
                    writer.write_eof()
                assert await asyncio.wait_for(reader.read(), 2) == expected_echo
            finally:
                writer.close()
                await writer.wait_closed()
            reader, writer = await _connect_unix_and_send(socket_path, request)
            try:
                assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 " + second_status + b" ")
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await broker.close()
            server.close()
            await server.wait_closed()
