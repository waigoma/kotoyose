"""A temporary Streamable HTTP endpoint for a child CLI."""

from __future__ import annotations

import asyncio
import contextvars
import hmac
import json
import secrets
from contextlib import suppress
from pathlib import Path

import h11

from .child_mcp import (
    CALL_TIMEOUT, DELEGATION_REPLY_MARGIN, DEFAULT_RELAY_TURN_TIMEOUT, MAX_BODY, MAX_CALLS, MAX_CONCURRENT,
    approved_versions, lost_result, rpc_body, rpc_error, validate_request,
)
from .delegation import MCP_NAME

# One name for the turn. The value is process environment for that CLI only, never argv.
AUTH_ENV = "KOTOYOSE_CHILD_MCP_AUTHORIZATION"
_AUTH_ENV: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar("kotoyose_child_mcp_auth_env", default={})


def authorization_header() -> str:
    """Claude expands this from the CLI environment. Codex reads the same variable by name."""
    return f"Bearer ${{{AUTH_ENV}}}"


def relay_process_env() -> dict[str, str]:
    return _AUTH_ENV.get()


def push_relay_env(env: dict[str, str]):
    return _AUTH_ENV.set(env)


def pop_relay_env(token) -> None:
    _AUTH_ENV.reset(token)


class ChildMcpProxy:
    """Serve a turn's published MCPs on loopback, with a deadline for delegation replies."""

    def __init__(
        self, home: Path, fingerprint: str, stream: int, send, publications: dict[str, str], grant: str,
        *, turn_timeout: float = DEFAULT_RELAY_TURN_TIMEOUT,
    ):
        self.home, self.fingerprint, self.stream, self.send = home, fingerprint, stream, send
        self.publications, self.grant = publications, grant
        self.delegation_deadline = asyncio.get_running_loop().time() + turn_timeout + DELEGATION_REPLY_MARGIN
        raw = secrets.token_urlsafe(32)
        self.token = "Bearer " + raw
        self.auth_env = {AUTH_ENV: raw}
        self.paths = {f"/mcp/{index}": name for index, name in enumerate(publications)}
        self.pending: dict[int, asyncio.Future] = {}
        self.clients: set[asyncio.Task] = set()
        self.call_id = 0
        self.server: asyncio.Server | None = None
        self.link_lost = False
        self.stopped = False

    async def start(self) -> dict[str, dict]:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0, backlog=16)
        self.address = "127.0.0.1:" + str(self.server.sockets[0].getsockname()[1])
        return {
            name: {"server": {"type": "http", "url": f"http://{self.address}{path}", "headers": {"Authorization": authorization_header()}}}
            for path, name in self.paths.items()
        }

    def receive(self, payload: dict) -> None:
        call_id = payload.get("call")
        if type(call_id) is int and (future := self.pending.get(call_id)) is not None and not future.done():
            future.set_result(payload.get("response"))

    def disconnect(self) -> None:
        self.link_lost = True
        for future in self.pending.values():
            if not future.done():
                future.set_result(None)

    async def close(self) -> None:
        self.stopped = True
        if self.server is not None:
            self.server.close()
        for task in self.clients:
            task.cancel()
        await asyncio.gather(*self.clients, return_exceptions=True)
        if self.server is not None:
            await self.server.wait_closed()

    async def _relay(self, name: str, request: dict) -> dict | None:
        version = self.publications[name]
        if approved_versions(self.home, self.fingerprint).get(name) != version:
            return rpc_error(request, "MCP relay approval was withdrawn or changed")
        if self.link_lost:
            return rpc_error(request, "MCP relay link is disconnected")
        if len(self.pending) >= MAX_CONCURRENT or self.call_id >= MAX_CALLS:
            return rpc_error(request, "MCP relay call limit exceeded")
        self.call_id += 1
        call_id = self.call_id
        future = self.pending[call_id] = asyncio.get_running_loop().create_future()
        try:
            now = asyncio.get_running_loop().time()
            deadline = self.delegation_deadline if name == MCP_NAME else now + CALL_TIMEOUT
            async with asyncio.timeout_at(deadline):
                await self.send(self.stream, "mcp_request", {
                    "call": call_id, "name": name, "version": version, "grant": self.grant, "request": request,
                })
                result = await future
                return rpc_error(request, lost_result(request)) if self.link_lost else result
        except (TimeoutError, OSError):
            return rpc_error(request, lost_result(request))
        finally:
            self.pending.pop(call_id, None)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.stopped or len(self.clients) >= MAX_CONCURRENT * 2:
            writer.close()
            return
        task = asyncio.current_task()
        self.clients.add(task)
        conn = h11.Connection(h11.SERVER, max_incomplete_event_size=8192)
        try:
            async with asyncio.timeout(5):
                status, name, body = await self._read(conn, reader)
            response = b""
            if name is not None:
                try:
                    raw = json.loads(body)
                    request = validate_request(raw)
                except (ValueError, RecursionError):
                    status, response = 400, rpc_body(rpc_error(None, "Invalid or unsupported MCP JSON-RPC request"))
                else:
                    result = await self._relay(name, request)
                    status, response = (202, b"") if result is None else (200, rpc_body(result))
            headers = [(b"Connection", b"close"), (b"Content-Length", str(len(response)).encode())]
            if response:
                headers.append((b"Content-Type", b"application/json"))
            writer.write(conn.send(h11.Response(status_code=status, headers=headers)))
            writer.write(conn.send(h11.Data(data=response)) + conn.send(h11.EndOfMessage()))
            async with asyncio.timeout(2):
                await writer.drain()
        except (h11.ProtocolError, OSError, ValueError, RecursionError, TimeoutError):
            pass
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            self.clients.discard(task)

    async def _read(self, conn: h11.Connection, reader: asyncio.StreamReader) -> tuple[int, str | None, bytes]:
        body = bytearray()
        name = None
        while True:
            event = conn.next_event()
            if event is h11.NEED_DATA:
                conn.receive_data(await reader.read(8192))
            elif isinstance(event, h11.Request):
                headers = dict(event.headers)
                if (headers.get(b"host") != self.address.encode() or b"origin" in headers
                        or not hmac.compare_digest(headers.get(b"authorization", b""), self.token.encode())):
                    return 403, None, b""
                name = self.paths.get(event.target.decode("ascii"))
                if name is None:
                    return 404, None, b""
                if event.method != b"POST":
                    return 405, None, b""
                if headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower() != b"application/json":
                    return 415, None, b""
            elif isinstance(event, h11.Data):
                body.extend(event.data)
                if len(body) > MAX_BODY:
                    return 413, None, b""
            elif isinstance(event, h11.EndOfMessage):
                return 200, name, bytes(body)
            else:
                raise ValueError("Incomplete MCP HTTP request")
