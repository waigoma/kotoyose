"""Plain pass-through HTTP CONNECT egress broker for isolated print-mode CLIs.

Unlike provider_egress (which terminates HTTP and injects credentials), print-mode
CLIs manage their own TLS sessions and credentials inside their login home. This
broker only validates the initial ``CONNECT <host>:<port> HTTP/1.1`` request
line against an exact hostname/port allowlist, opens a host-side TCP connection,
returns ``200 Connection Established``, and pipes bytes in both directions without
inspecting, modifying, or logging any traffic after the CONNECT request line.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import re
import socket
import stat
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

import structlog

from .codex_egress import public_address

log = structlog.get_logger()

MAX_SOCKET_PATH_BYTES = 100
_SOCKET_UMASK = 0o117
_SOCKET_MODE = 0o660
DEFAULT_MAX_HEADER_BYTES = 8192
DEFAULT_MAX_TUNNELS = 32
DEFAULT_IDLE_TIMEOUT_SECONDS = 300.0
DEFAULT_CONNECT_TIMEOUT_SECONDS = 15.0
_READ_SIZE = 65536

_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_CONNECT_ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"
_CONNECT_FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"

ResolverFn = Callable[[str, int], Awaitable[Sequence[tuple[str, int]]]]


def _is_valid_exact_hostname(host: str) -> bool:
    """Return True for a syntactic DNS hostname with no wildcards or IP literals."""
    if not host or len(host) > 253 or "*" in host:
        return False
    try:
        ipaddress.ip_address(host.strip("[]"))
        return False
    except ValueError:
        pass
    labels = host.split(".")
    return all(bool(_HOST_LABEL.match(label)) for label in labels)


def _parse_connect_line(request_line: bytes) -> tuple[str | None, int | None, bool]:
    """Parse ``CONNECT <host>:<port> HTTP/1.1``.

    Returns ``(host, port, valid_connect_syntax)`` where ``host``/``port`` are
    only populated when ``host:port`` parses cleanly without logging arbitrary
    input bytes.
    """
    try:
        text = request_line.decode("ascii")
    except UnicodeDecodeError:
        return None, None, False
    parts = text.split(" ")
    if len(parts) != 3:
        return None, None, False
    method, target, version = parts
    if method != "CONNECT" or version != "HTTP/1.1":
        return None, None, False
    host_raw, sep, port_str = target.rpartition(":")
    if not sep or not host_raw or not port_str.isdigit():
        return None, None, False
    port = int(port_str)
    if port < 1 or port > 65535:
        return None, None, False
    host = host_raw.lower()
    if not _is_valid_exact_hostname(host):
        return None, None, False
    return host, port, True


async def _read_connect_headers(
    reader: asyncio.StreamReader,
    max_header_bytes: int,
    timeout: float,
) -> tuple[bytes, bytes] | None:
    """Read until ``\\r\\n\\r\\n`` within ``max_header_bytes`` and ``timeout``.

    Returns ``(header_bytes_before_crlfcrlf, trailing_bytes)`` or ``None`` if the
    request exceeds the bound, closes early, or has invalid framing.
    """
    buf = bytearray()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        to_read = min(4096, max_header_bytes + 4 - len(buf))
        if to_read <= 0:
            return None
        try:
            chunk = await asyncio.wait_for(reader.read(to_read), timeout=remaining)
        except asyncio.TimeoutError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
        end = buf.find(b"\r\n\r\n")
        if end != -1:
            if end > max_header_bytes:
                return None
            return bytes(buf[:end]), bytes(buf[end + 4 :])
        if len(buf) > max_header_bytes + 4:
            return None


def _check_socket_parent(directory: Path) -> None:
    if directory.is_symlink():
        raise ValueError(f"socket directory must not be a symlink: {directory}")
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"socket path parent is not a directory: {directory}")
    if info.st_uid != os.geteuid():
        raise ValueError(f"socket directory must be owned by current user: {directory}")
    if info.st_mode & 0o002:
        raise ValueError(f"socket directory must not be world-writable: {directory}")


class PrintCliEgress:
    """Per-Run UNIX socket HTTP CONNECT allowlist broker."""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        allowed_hosts: Sequence[str],
        allowed_ports: Sequence[int] = (443,),
        run_id: str,
        max_header_bytes: int = DEFAULT_MAX_HEADER_BYTES,
        max_tunnels: int = DEFAULT_MAX_TUNNELS,
        max_total_tunnels: int | None = None,
        max_total_bytes: int | None = None,
        wall_timeout_seconds: float | None = None,
        idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        allow_private: bool = False,
        resolver: ResolverFn | None = None,
    ) -> None:
        self._socket_path = Path(socket_path)
        if len(str(self._socket_path).encode("utf-8")) > MAX_SOCKET_PATH_BYTES:
            raise ValueError(f"socket path exceeds {MAX_SOCKET_PATH_BYTES} bytes: {self._socket_path}")
        if not run_id:
            raise ValueError("run_id must be non-empty")
        normalized_hosts: set[str] = set()
        for raw_host in allowed_hosts:
            host = raw_host.strip().lower()
            if not _is_valid_exact_hostname(host):
                raise ValueError(f"invalid allowlist host (exact hostnames only): {raw_host!r}")
            normalized_hosts.add(host)
        normalized_ports = frozenset(int(port) for port in allowed_ports)
        if any(port < 1 or port > 65535 for port in normalized_ports):
            raise ValueError(f"invalid port in allowed_ports: {allowed_ports!r}")
        if max_header_bytes <= 0 or max_tunnels <= 0 or idle_timeout_seconds <= 0 or connect_timeout_seconds <= 0:
            raise ValueError("broker limits and timeouts must be positive")

        if any(limit is not None and limit <= 0
               for limit in (max_total_tunnels, max_total_bytes, wall_timeout_seconds)):
            raise ValueError("optional broker limits and timeouts must be positive")

        self._max_total_tunnels = max_total_tunnels
        self._max_total_bytes = max_total_bytes
        self._wall_timeout_seconds = wall_timeout_seconds
        self._total_tunnels = 0
        self._total_bytes = 0
        self._allowed_hosts = frozenset(normalized_hosts)
        self._allowed_ports = normalized_ports
        self._run_id = run_id
        self._max_header_bytes = max_header_bytes
        self._max_tunnels = max_tunnels
        self._idle_timeout_seconds = idle_timeout_seconds
        self._connect_timeout_seconds = connect_timeout_seconds
        self._allow_private = allow_private
        self._resolver = resolver

        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._active_tunnels = 0
        self._closed = False

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    async def start(self) -> None:
        if self._server is not None:
            return
        parent = self._socket_path.parent
        parent.mkdir(parents=True, mode=0o750, exist_ok=True)
        _check_socket_parent(parent)
        with contextlib.suppress(FileNotFoundError):
            self._socket_path.unlink()

        old_umask = os.umask(_SOCKET_UMASK)
        try:
            self._server = await asyncio.start_unix_server(
                self._accept_client,
                path=str(self._socket_path),
            )
            os.chmod(self._socket_path, _SOCKET_MODE)
            info = os.lstat(self._socket_path)
            self._socket_identity = (info.st_dev, info.st_ino)
        finally:
            os.umask(old_umask)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()

        tasks = list(self._handlers)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if self._socket_identity is not None:
            try:
                info = os.lstat(self._socket_path)
            except FileNotFoundError:
                pass
            else:
                if (info.st_dev, info.st_ino) == self._socket_identity:
                    with contextlib.suppress(FileNotFoundError):
                        self._socket_path.unlink()
            self._socket_identity = None

    def _accept_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closed:
            writer.close()
            return
        task = asyncio.create_task(self._handle_client(reader, writer))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _send_forbidden(self, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(OSError, ConnectionError):
            writer.write(_CONNECT_FORBIDDEN)
            await writer.drain()

    async def _resolve_targets(self, host: str, port: int) -> list[tuple[str, int]]:
        if self._resolver is not None:
            raw_targets = await self._resolver(host, port)
            targets = [(str(ip), int(resolved_port)) for ip, resolved_port in raw_targets]
        else:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
            targets = []
            for _family, _type, _proto, _canon, sockaddr in infos:
                ip = str(sockaddr[0]).split("%", 1)[0]
                targets.append((ip, port))
        if not targets:
            raise OSError(f"no addresses resolved for {host}")
        if not self._allow_private:
            for ip, _resolved_port in targets:
                if not public_address(ip):
                    raise PermissionError(f"resolved address is not public: {ip}")
        return targets

    async def _open_upstream(self, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        targets = await asyncio.wait_for(
            self._resolve_targets(host, port),
            timeout=self._connect_timeout_seconds,
        )
        last_exc: Exception | None = None
        for ip, target_port in targets:
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(ip, target_port),
                    timeout=self._connect_timeout_seconds,
                )
            except OSError as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise OSError("failed to connect upstream")

    def _account_tunnel_bytes(self, count: int) -> None:
        """Reserve bytes in either direction before forwarding them across any tunnel."""
        if self._max_total_bytes is None:
            return
        if count > self._max_total_bytes - self._total_bytes:
            self._total_bytes = self._max_total_bytes
            raise ConnectionError("tunnel byte budget exhausted")
        self._total_bytes += count

    async def _pipe_bidirectional(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
        trailing: bytes,
    ) -> None:
        if trailing:
            self._account_tunnel_bytes(len(trailing))
            upstream_writer.write(trailing)
            await asyncio.wait_for(upstream_writer.drain(), timeout=self._idle_timeout_seconds)

        loop = asyncio.get_running_loop()
        last_activity = loop.time()

        async def _pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            nonlocal last_activity
            while True:
                remaining = self._idle_timeout_seconds - (loop.time() - last_activity)
                if remaining <= 0:
                    raise TimeoutError("tunnel idle timeout")
                try:
                    chunk = await asyncio.wait_for(src.read(_READ_SIZE), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    if loop.time() - last_activity >= self._idle_timeout_seconds:
                        raise TimeoutError("tunnel idle timeout") from exc
                    continue
                if not chunk:
                    with contextlib.suppress(OSError, NotImplementedError, RuntimeError):
                        if dst.can_write_eof():
                            dst.write_eof()
                    return
                last_activity = loop.time()
                self._account_tunnel_bytes(len(chunk))
                dst.write(chunk)
                await asyncio.wait_for(dst.drain(), timeout=self._idle_timeout_seconds)

        c2u = asyncio.create_task(_pump(client_reader, upstream_writer))
        u2c = asyncio.create_task(_pump(upstream_reader, client_writer))
        tasks = [c2u, u2c]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for finished in done:
                exc = finished.exception()
                if exc is not None:
                    raise exc
            if pending:
                await asyncio.gather(*pending)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_client(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        tunnel_counted = False
        try:
            parsed_headers = await _read_connect_headers(
                client_reader,
                self._max_header_bytes,
                self._connect_timeout_seconds,
            )
            if parsed_headers is None:
                log.info("print_cli_egress.denied", run_id=self._run_id, host=None, port=None)
                await self._send_forbidden(client_writer)
                return

            header_bytes, trailing = parsed_headers
            request_line = header_bytes.split(b"\r\n", 1)[0]
            host, port, valid_syntax = _parse_connect_line(request_line)
            if not valid_syntax or host is None or port is None:
                log.info("print_cli_egress.denied", run_id=self._run_id, host=host, port=port)
                await self._send_forbidden(client_writer)
                return

            if host not in self._allowed_hosts or port not in self._allowed_ports:
                log.info("print_cli_egress.denied", run_id=self._run_id, host=host, port=port)
                await self._send_forbidden(client_writer)
                return

            if (self._active_tunnels >= self._max_tunnels
                    or self._max_total_tunnels is not None and self._total_tunnels >= self._max_total_tunnels
                    or self._max_total_bytes is not None and self._total_bytes >= self._max_total_bytes):
                log.info("print_cli_egress.denied", run_id=self._run_id, host=host, port=port)
                await self._send_forbidden(client_writer)
                return

            self._total_tunnels += 1
            self._active_tunnels += 1
            tunnel_counted = True

            async with asyncio.timeout(self._wall_timeout_seconds):
                try:
                    upstream_reader, upstream_writer = await self._open_upstream(host, port)
                except Exception:
                    log.info("print_cli_egress.denied", run_id=self._run_id, host=host, port=port)
                    await self._send_forbidden(client_writer)
                    return

                log.info("print_cli_egress.allowed", run_id=self._run_id, host=host, port=port)
                client_writer.write(_CONNECT_ESTABLISHED)
                await client_writer.drain()

                await self._pipe_bidirectional(
                    client_reader,
                    client_writer,
                    upstream_reader,
                    upstream_writer,
                    trailing,
                )
        except (asyncio.CancelledError, OSError, TimeoutError):
            # Drop queued data so an unresponsive peer cannot delay teardown.
            client_writer.transport.abort()
            if upstream_writer is not None:
                upstream_writer.transport.abort()
        finally:
            if tunnel_counted:
                self._active_tunnels = max(0, self._active_tunnels - 1)
            for writer in (upstream_writer, client_writer):
                if writer is not None:
                    with contextlib.suppress(Exception):
                        writer.close()
                        await writer.wait_closed()
