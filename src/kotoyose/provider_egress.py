"""Provider 非依存の egress broker。

網隔離された container の CLI は、読み取り専用で bind された UNIX socket だけを外部への出口にする。
broker は HTTP request を route allowlist と per-Run capability で照合し、host 側だけが知る credential を
付けて固定 upstream へ転送する。container へ渡るのは capability だけで、実 credential は渡らない。

既知の境界:
- upstream の応答 body は redact せずそのまま client へ渡す。provider のエラーが、マスクされた鍵の
  断片を含んで返す可能性がある。秘密の反射を防ぐのは request 側の inject 値だけである。
- client の切断は、broker が次に書き込んだ時点で初めて検出する。送信中の無駄な upstream 読み取りは
  idle timeout と request deadline で打ち切る。
"""

import asyncio
import contextlib
import hmac
import json
import os
import re
import socket
import ssl
import stat
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import h11
import structlog

from .codex_egress import public_address

log = structlog.get_logger()

# container 内 relay が待ち受ける固定 address。CLI の base URL の Host と一致させる。
RELAY_HOST = "127.0.0.1:18080"
# UNIX socket path は OS 上限 108 byte。spike で踏んだため、安全側の 100 byte で拒否する。
MAX_SOCKET_PATH_BYTES = 100
# socket は group だけが読み書きできる。umask 0o117 で bind 時点から 0o660 にする。
_SOCKET_UMASK = 0o117
_SOCKET_MODE = 0o660
_MAX_HEADER = 16384
_READ_SIZE = 65536
_MIN_CAPABILITY_LENGTH = 16
# fullmatch を使う。match の `$` は末尾改行を許すため、"x-api-key\n" が通ってしまう。
_HEADER_NAME = re.compile(r"[a-z0-9-]+")
_HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
                         "trailer", "transfer-encoding", "upgrade"})
# 転送元 header から決して通さない名前。framing と接続先は broker が決める。
_NEVER_PASS = frozenset({"host", "content-length", "connection", "transfer-encoding"})
_REASONS = {400: "Bad Request", 403: "Forbidden", 413: "Payload Too Large", 502: "Bad Gateway",
            503: "Service Unavailable"}

ConnectFn = Callable[["EgressUpstream"], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class _Deny(Exception):
    """client へ固定 reason だけを返して接続を閉じる。値や header を含めない。"""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class _Cut(Exception):
    """response が上限を超えた。HTTP framing を完結させず接続を閉じる。"""


class _UpstreamBroken(Exception):
    """upstream が応答を完結させずに閉じた。"""


class UpstreamAddressDenied(OSError):
    """upstream の解決先に公開されない address が含まれ、allow_private が無い。"""


def _check_header_name(name: str) -> None:
    if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
        raise ValueError("header name must be lowercase ASCII letters, digits or hyphen")


def _is_header_value(value: str) -> bool:
    return isinstance(value, str) and value != "" and value.isascii() and value.isprintable()


def _hop_by_hop(name: str) -> bool:
    return name in _HOP_BY_HOP or name.startswith("proxy-")


def _forbidden_request_header(name: str) -> bool:
    return name.startswith(("proxy-", "x-forwarded-")) or name in {"forwarded", "upgrade"}


@dataclass(frozen=True)
class EgressRoute:
    """許可する method と path。path は完全一致、または末尾 `*` の前方一致。query は照合しない。"""

    method: str
    path: str

    def __post_init__(self):
        if not (self.method.isascii() and self.method.isalpha() and self.method.isupper()):
            raise ValueError("route method must be an uppercase token")
        if not self.path.startswith("/") or "?" in self.path or "%" in self.path:
            raise ValueError("route path must be an absolute path without query or escapes")
        star = self.path.find("*")
        if star not in (-1, len(self.path) - 1):
            raise ValueError("route wildcard is only allowed at the end")

    def matches(self, method: str, path: str) -> bool:
        if method != self.method:
            return False
        if self.path.endswith("*"):
            return path.startswith(self.path[:-1])
        return path == self.path


@dataclass(frozen=True)
class EgressUpstream:
    """固定 upstream。base_path は転送時に request path の前へ付ける (例: "/anthropic")。"""

    host: str
    port: int = 443
    tls: bool = True
    base_path: str = ""
    # 既定では解決先がすべて公開 address であることを要求する。local LLM gateway 等の
    # private IP を指すときだけ True にする (通常は plain HTTP と組み合わせる)。
    allow_private: bool = False

    def __post_init__(self):
        if not self.host or any(c in self.host for c in "/@ \t\r\n"):
            raise ValueError("upstream host is invalid")
        if not 0 < self.port < 65536:
            raise ValueError("upstream port is invalid")
        if self.base_path and (not self.base_path.startswith("/") or self.base_path.endswith("/")
                               or "?" in self.base_path or "%" in self.base_path):
            raise ValueError("upstream base_path must be an absolute path without trailing slash")

    def host_header(self) -> str:
        default = 443 if self.tls else 80
        return self.host if self.port == default else f"{self.host}:{self.port}"


@dataclass(frozen=True)
class EgressSpec:
    """1 Run 分の broker 設定。inject_headers の値と capability は秘密として扱う。"""

    upstream: EgressUpstream
    routes: tuple[EgressRoute, ...]
    inject_headers: Mapping[str, str]
    capability: str
    capability_header: str
    pass_headers: frozenset[str]
    max_request_bytes: int
    max_response_bytes: int
    idle_timeout_seconds: float
    # 同時に受ける client 接続の上限。超過分は 503 で即座に閉じる。
    max_connections: int = 16
    # 1 request の受信から応答完了までの全体上限。idle timeout とは別に効く。
    request_deadline_seconds: float = 900

    def __post_init__(self):
        if not isinstance(self.routes, tuple) or not self.routes:
            raise ValueError("routes must be a non-empty tuple")
        inject = dict(self.inject_headers)
        for name, value in inject.items():
            _check_header_name(name)
            if not _is_header_value(value):
                raise ValueError("inject header value is invalid")
        object.__setattr__(self, "inject_headers", MappingProxyType(inject))
        if (not isinstance(self.capability, str) or len(self.capability) < _MIN_CAPABILITY_LENGTH
                or not self.capability.isascii() or not self.capability.isprintable()
                or any(c.isspace() for c in self.capability)):
            raise ValueError("capability must be a printable ASCII token of at least 16 characters")
        _check_header_name(self.capability_header)
        pass_headers = frozenset(self.pass_headers)
        for name in pass_headers:
            _check_header_name(name)
            if name in _NEVER_PASS or _hop_by_hop(name) or name == self.capability_header:
                raise ValueError("pass_headers contains a header the broker must control")
        object.__setattr__(self, "pass_headers", pass_headers)
        if (self.max_request_bytes <= 0 or self.max_response_bytes <= 0 or self.idle_timeout_seconds <= 0
                or self.request_deadline_seconds <= 0 or self.max_connections < 1):
            raise ValueError("limits must be positive")


def _public(value: str) -> bool:
    try:
        return public_address(value)
    except ValueError:
        return False


async def _open_upstream(upstream: EgressUpstream):
    """1 回だけ名前解決し、全解決先が公開 address (allow_private 無効時) であることを確かめてから接続する。

    接続は検査した address 向けに行い、DNS を再解決しない。TLS の証明書検証と SNI は host 名で行う。
    """
    records = await asyncio.get_running_loop().getaddrinfo(upstream.host, upstream.port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(item[4][0] for item in records))
    if not addresses:
        raise OSError("upstream has no address")
    if not upstream.allow_private and not all(_public(address) for address in addresses):
        raise UpstreamAddressDenied("upstream resolves to a non-public address")
    last_error: OSError | None = None
    for address in addresses:
        try:
            if upstream.tls:
                return await asyncio.open_connection(address, upstream.port, ssl=ssl.create_default_context(),
                                                     server_hostname=upstream.host, ssl_handshake_timeout=5)
            return await asyncio.open_connection(address, upstream.port)
        except OSError as exc:
            last_error = exc
    raise last_error or OSError("upstream connection failed")


class ProviderEgress:
    """UNIX socket で待ち受け、許可された request だけを固定 upstream へ転送する。"""

    def __init__(self, spec: EgressSpec, socket_path: Path, connect: ConnectFn | None = None):
        self.spec = spec
        self.socket_path = Path(socket_path)
        self._connect = connect or _open_upstream
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._closing = False
        # start() が作った socket の (dev, inode)。close() は同じ実体の時だけ unlink する。
        self._identity: tuple[int, int] | None = None
        self._capability = spec.capability.encode("ascii")
        self._secrets = tuple(sorted({value for value in (*spec.inject_headers.values(), spec.capability) if value},
                                     key=len, reverse=True))
        self._requests = 0
        self._denied = 0
        self._bytes_in = 0
        self._bytes_out = 0

    def redact(self, text: str) -> str:
        """log や例外文字列へ入れる前に、注入値と capability を取り除く。"""
        for secret in self._secrets:
            text = text.replace(secret, "[redacted]")
        return text

    def metrics(self) -> dict[str, int]:
        """client 側の観測値だけ。秘密・path・header は含めない。"""
        return {"requests": self._requests, "denied": self._denied,
                "bytes_in": self._bytes_in, "bytes_out": self._bytes_out}

    async def start(self) -> Path:
        if len(os.fsencode(self.socket_path)) > MAX_SOCKET_PATH_BYTES:
            raise ValueError("socket path is too long for a UNIX socket")
        if self._server is not None:
            raise RuntimeError("provider egress already started")
        parent_mode = os.stat(self.socket_path.parent).st_mode
        if parent_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise PermissionError("socket directory must not be group- or world-writable")
        try:
            mode = os.lstat(self.socket_path).st_mode
        except FileNotFoundError:
            mode = 0
        if mode and not stat.S_ISSOCK(mode):
            raise FileExistsError("socket path is occupied by a non-socket file")
        if mode:
            os.unlink(self.socket_path)
        previous = os.umask(_SOCKET_UMASK)
        try:
            self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))
        finally:
            os.umask(previous)
        os.chmod(self.socket_path, _SOCKET_MODE)
        info = os.lstat(self.socket_path)
        self._identity = (info.st_dev, info.st_ino)
        return self.socket_path

    def _owns_socket_path(self) -> bool:
        """path に今ある socket が start() で作った実体か。別物に差し替えられていたら False。"""
        if self._identity is None:
            return False
        try:
            info = os.lstat(self.socket_path)
        except FileNotFoundError:
            return False
        return stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == self._identity

    async def close(self) -> None:
        self._closing = True
        if self._server is not None:
            self._server.close()
        for writer in tuple(self._writers):
            writer.close()
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None
        if self._owns_socket_path():
            os.unlink(self.socket_path)
        self._identity = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closing:
            writer.close()
            return
        if len(self._tasks) >= self.spec.max_connections:
            # 上限超過は接続を task に載せず、固定 body の 503 だけ返して閉じる。
            log.warning("provider_egress.busy", limit=self.spec.max_connections)
            await self._respond(writer, 503, "busy")
            # 未読の request を残して閉じると RST になり、503 が client に届かないことがある。
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(reader.read(_READ_SIZE), 0.2)
            writer.close()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await writer.wait_closed()
            return
        task = asyncio.current_task()
        self._tasks.add(task)
        self._writers.add(writer)
        try:
            await self._serve(reader, writer)
        except Exception as exc:
            # 例外メッセージには header 値が混ざりうるため、型名だけを残す。
            log.warning("provider_egress.connection_failed", error_type=type(exc).__name__)
        finally:
            writer.close()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await writer.wait_closed()
            self._writers.discard(writer)
            self._tasks.discard(task)

    async def _serve(self, reader, writer) -> None:
        down = h11.Connection(h11.SERVER, max_incomplete_event_size=_MAX_HEADER)
        while True:
            try:
                # 1 request の受信から応答完了までを deadline で囲む。期限切れは両側を閉じる。
                async with asyncio.timeout(self.spec.request_deadline_seconds):
                    request = await self._read_request(down, reader)
                    if request is None:
                        return
                    head, body = request
                    forwarded = await self._forward(down, writer, head, body)
            except _Deny as deny:
                await self._deny(writer, deny)
                return
            except TimeoutError:
                # request deadline と idle timeout のどちらでも閉じる。区別は検査せず、型だけを残さない。
                log.warning("provider_egress.timed_out")
                return
            if not forwarded:
                return
            if down.our_state is not h11.DONE or down.their_state is not h11.DONE:
                return
            down.start_next_cycle()

    async def _read_client(self, reader) -> bytes:
        async with asyncio.timeout(self.spec.idle_timeout_seconds):
            data = await reader.read(_READ_SIZE)
        self._bytes_in += len(data)
        return data

    async def _read_request(self, conn: h11.Connection, reader):
        head = None
        body = bytearray()
        while True:
            try:
                event = conn.next_event()
            except h11.RemoteProtocolError as exc:
                raise _Deny(400, "malformed_request") from exc
            if event is h11.NEED_DATA:
                conn.receive_data(await self._read_client(reader))
            elif isinstance(event, h11.Request):
                self._requests += 1
                self._check_head(event)
                head = event
            elif isinstance(event, h11.Data):
                body.extend(event.data)
                if len(body) > self.spec.max_request_bytes:
                    raise _Deny(413, "request_too_large")
            elif isinstance(event, h11.EndOfMessage):
                return head, bytes(body)
            else:
                # ConnectionClosed または未処理状態。request を始める前の終了は正常な切断。
                return None

    def _check_head(self, head: h11.Request) -> None:
        if head.http_version != b"1.1":
            raise _Deny(400, "unsupported_http_version")
        method = head.method.decode("ascii")
        path = head.target.decode("ascii", "replace").partition("?")[0]
        if not self._route_allowed(method, path):
            raise _Deny(403, "route_denied")
        if any(_forbidden_request_header(name.decode("ascii")) for name, _ in head.headers):
            raise _Deny(403, "header_denied")
        if [value for name, value in head.headers if name == b"host"] != [RELAY_HOST.encode("ascii")]:
            raise _Deny(403, "host_denied")
        if not self._capability_ok(head.headers):
            raise _Deny(403, "capability_denied")
        lengths = [value for name, value in head.headers if name == b"content-length"]
        if lengths:
            if len(lengths) != 1 or not lengths[0].isdigit():
                raise _Deny(400, "malformed_request")
            if int(lengths[0]) > self.spec.max_request_bytes:
                raise _Deny(413, "request_too_large")

    def _route_allowed(self, method: str, path: str) -> bool:
        # 文字列照合の迂回 (dot segment, 空 segment, percent escape) を先に閉じる。
        if not path.startswith("/") or "%" in path or "\\" in path:
            return False
        if any(segment in ("", ".", "..") for segment in path.split("/")[1:]):
            return False
        return any(route.matches(method, path) for route in self.spec.routes)

    def _capability_ok(self, headers) -> bool:
        expected = self.spec.capability_header.encode("ascii")
        values = [value for name, value in headers if name == expected]
        if len(values) != 1:
            return False
        token = values[0]
        if token.startswith(b"Bearer "):
            token = token[len(b"Bearer "):]
        return hmac.compare_digest(token, self._capability)

    def _upstream_headers(self, headers, body_length: int) -> list[tuple[bytes, bytes]]:
        injected = {name.lower() for name in self.spec.inject_headers}
        skipped = {b"host", b"content-length", b"connection", self.spec.capability_header.encode("ascii")}
        kept = [(name, value) for name, value in headers
                if name.decode("ascii") in self.spec.pass_headers and name not in skipped
                and name.decode("ascii") not in injected]
        added = [(name.encode("ascii"), value.encode("ascii")) for name, value in self.spec.inject_headers.items()]
        return [(b"host", self.spec.upstream.host_header().encode("ascii")),
                (b"content-length", str(body_length).encode("ascii")),
                (b"connection", b"close"), *kept, *added]

    async def _forward(self, down: h11.Connection, writer, head: h11.Request, body: bytes) -> bool:
        spec = self.spec
        try:
            async with asyncio.timeout(spec.idle_timeout_seconds):
                remote_reader, remote_writer = await self._connect(spec.upstream)
        except (OSError, TimeoutError) as exc:
            log.warning("provider_egress.upstream_failed", error_type=type(exc).__name__, after_headers=False)
            await self._respond(writer, 502, "upstream_unavailable")
            return False
        sent_head = False
        try:
            up = h11.Connection(h11.CLIENT, max_incomplete_event_size=_MAX_HEADER)
            request = up.send(h11.Request(method=head.method,
                                          target=spec.upstream.base_path.encode("ascii") + head.target,
                                          headers=self._upstream_headers(head.headers, len(body))))
            remote_writer.write(request)
            if body:
                remote_writer.write(up.send(h11.Data(data=body)))
            remote_writer.write(up.send(h11.EndOfMessage()))
            async with asyncio.timeout(spec.idle_timeout_seconds):
                await remote_writer.drain()
            body_bytes = 0
            while True:
                event = up.next_event()
                if event is h11.NEED_DATA:
                    async with asyncio.timeout(spec.idle_timeout_seconds):
                        data = await remote_reader.read(_READ_SIZE)
                    up.receive_data(data)
                elif isinstance(event, h11.InformationalResponse):
                    continue
                elif isinstance(event, h11.Response):
                    await self._write(writer, down.send(h11.Response(
                        status_code=event.status_code,
                        headers=[(name, value) for name, value in event.headers if not _hop_by_hop(name.decode("ascii"))])))
                    sent_head = True
                elif isinstance(event, h11.Data):
                    body_bytes += len(event.data)
                    if body_bytes > spec.max_response_bytes:
                        raise _Cut()
                    await self._write(writer, down.send(h11.Data(data=event.data)))
                elif isinstance(event, h11.EndOfMessage):
                    await self._write(writer, down.send(h11.EndOfMessage()))
                    return True
                else:
                    raise _UpstreamBroken()
        except _Cut:
            log.warning("provider_egress.response_cut", limit=spec.max_response_bytes)
            return False
        except (h11.RemoteProtocolError, h11.LocalProtocolError, OSError, TimeoutError, _UpstreamBroken) as exc:
            log.warning("provider_egress.upstream_failed", error_type=type(exc).__name__, after_headers=sent_head)
            if not sent_head:
                await self._respond(writer, 502, "upstream_unavailable")
            return False
        finally:
            remote_writer.close()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await remote_writer.wait_closed()

    async def _write(self, writer, data: bytes) -> None:
        writer.write(data)
        self._bytes_out += len(data)
        async with asyncio.timeout(self.spec.idle_timeout_seconds):
            await writer.drain()

    async def _deny(self, writer, deny: _Deny) -> None:
        self._denied += 1
        log.warning("provider_egress.denied", status=deny.status, reason=self.redact(deny.reason))
        await self._respond(writer, deny.status, deny.reason)

    async def _respond(self, writer, status: int, reason: str) -> None:
        body = json.dumps({"error": reason}).encode("ascii")
        head = (f"HTTP/1.1 {status} {_REASONS[status]}\r\ncontent-type: application/json\r\n"
                f"content-length: {len(body)}\r\nconnection: close\r\n\r\n").encode("ascii")
        with contextlib.suppress(OSError, TimeoutError):
            await self._write(writer, head + body)
