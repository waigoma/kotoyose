"""ネット隔離 provider から固定 OpenAI endpoint への bounded TLS reverse proxy。"""

import asyncio
import contextlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import tempfile
import h11
from collections.abc import Awaitable, Callable

from .execution_delegation import TOOL_SPECS
from .codex_model_policy import validate_reasoning_effort
from .codex_broker_diagnostics import BrokerObservation
from .codex_request_policy import parse_json, validate_input
from .codex_subscription import read_subscription
from .execution_resources import codex_execution_failure, CodexEgressLimits
from .execution_images import MAX_IMAGE_COUNT, MAX_IMAGE_TOTAL_BYTES
from .codex_response_policy import validate_image_response


_HOST = "api.openai.com"
_MAX_HEADER = 16384
_MAX_BODY = 2 * 1024 * 1024
_METADATA_HEADERS = {"x-client-request-id", "x-codex-turn-metadata", "x-codex-beta-features",
                     "x-codex-window-id", "session-id", "thread-id", "x-openai-internal-codex-responses-lite"}
_HEADERS = {"host", "authorization", "content-length", "content-type", "accept", "user-agent", "connection",
            "originator"} | _METADATA_HEADERS
_FIELDS = {"model", "instructions", "input", "tools", "tool_choice", "parallel_tool_calls", "reasoning",
           "store", "stream", "include", "prompt_cache_key", "text", "max_output_tokens", "service_tier",
           "client_metadata"}


class _RequestLimitExceeded(ValueError):
    pass


def parse_request(header: bytes, *, image_bytes: int = 0, request_bytes: int = _MAX_BODY) -> tuple[bytes, int]:
    """request target/authority/framing を閉じ、Host と接続設定を固定する。"""
    if len(header) > _MAX_HEADER or not header.endswith(b"\r\n\r\n"):
        raise ValueError("egress_denied")
    lines = header.decode("ascii").split("\r\n")
    method, target, protocol = lines[0].split(" ")
    if (method, target) != ("POST", "/v1/responses") or protocol != "HTTP/1.1":
        raise ValueError("egress_denied")
    values = {}
    for line in lines[1:-2]:
        name, value = line.split(":", 1)
        name = name.lower()
        if (not name or not all(c.isalnum() or c == "-" for c in name)
                or name in values or any(ord(c) < 32 and c != "\t" for c in value)):
            raise ValueError("egress_denied")
        values[name] = value.strip()
    if (set(values) - _HEADERS or values.get("host") != "127.0.0.1:18080"
            or any(name in values for name in {"transfer-encoding", "upgrade", "proxy-authorization",
                                               "proxy-connection", "forwarded", "x-forwarded-host"})
            or values.get("connection", "close").lower() not in {"close", "keep-alive"}):
        raise ValueError("egress_denied")
    length = values.get("content-length", "0")
    if not length.isdecimal() or len(length) > 8:
        raise ValueError("egress_denied")
    if int(length) > request_bytes + image_bytes:
        raise _RequestLimitExceeded("egress_denied")
    if int(length) == 0:
        raise ValueError("egress_denied")
    # 固定 CLI の session/診断 metadata は受理するが upstream には転送しない。
    # 特に internal transport header は endpoint の API 動作を変更させない。
    values = {name: value for name, value in values.items() if name not in _METADATA_HEADERS}
    values.update({"host": _HOST, "connection": "close", "content-length": str(int(length))})
    result = f"{method} {target} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in values.items()) + "\r\n"
    return result.encode("ascii"), int(length)


async def connect_endpoint():
    """全 DNS 回答を検査し public IP に直接接続する。環境 proxy は解釈しない。"""
    return await _connect_fixed_endpoint(_HOST)


async def connect_subscription_endpoint():
    """サブスク要求を固定 ChatGPT host へ接続する。API endpoint に切り替えない。"""
    return await _connect_fixed_endpoint("chatgpt.com")


async def _connect_fixed_endpoint(host):
    records = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(item[4][0] for item in records))
    if not addresses or any(not public_address(value) for value in addresses):
        raise ValueError("egress_denied")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile="/etc/ssl/certs/ca-certificates.crt")
    # IP を固定して DNS の再解決を避け、certificate/SNI は固定 endpoint を使う。
    return await asyncio.open_connection(addresses[0], 443, ssl=context, server_hostname=host,
                                         ssl_handshake_timeout=5)


def public_address(value):
    address = ipaddress.ip_address(value)
    return (address.is_global and not address.is_multicast and not address.is_unspecified
            and not address.is_reserved and not address.is_link_local and not address.is_loopback
            and not (isinstance(address, ipaddress.IPv6Address) and (address.ipv4_mapped or address.is_site_local)))


# 停止の観測で egress が未確認のまま残った proxy。attempt id ごとに、この process の中だけで保持する。
# 停止の照合は、ここに登録があり全 task が終わったときだけ「同じ process で収束した」と判断する。
_UNSETTLED_EGRESS: dict[str, "CodexEgress"] = {}


def retain_unsettled_egress(attempt_id: str, egress: "CodexEgress") -> None:
    """未確認の停止を記録する前に、照合のための参照を残す。"""
    _UNSETTLED_EGRESS[attempt_id] = egress


def egress_settled_in_process(attempt_id: str) -> bool:
    """この process に登録があり、その egress の全 task が終わっていれば True。登録が無ければ False。"""
    egress = _UNSETTLED_EGRESS.get(attempt_id)
    return egress is not None and egress.settled()


def forget_unsettled_egress(attempt_id: str) -> None:
    """照合が済んだ参照を手放す。残っていた socket directory もここで片付ける。"""
    egress = _UNSETTLED_EGRESS.pop(attempt_id, None)
    if egress is not None:
        with contextlib.suppress(Exception):
            egress.directory.cleanup()


class CodexEgress:
    """専用 UNIX socket だけを公開する。停止時は全 upstream connection の収束を確認する。"""

    def __init__(self, timeout: float, byte_limit: int, model: str, credential: str, tool_schema: dict | None,
                 *, auth_file: Path | None = None, auth_identity: str | None = None,
                 authorize: Callable[[], Awaitable[None]] | None = None,
                 image_uris: tuple[str, ...] = (), image_request_limit: int = 1,
                 reasoning_effort: str | None = None, coding: bool = False,
                 limits: CodexEgressLimits | None = None, delegation_refs: frozenset[str] = frozenset()):
        if coding and (auth_file is None or tool_schema is not None):
            raise ValueError("invalid_job")
        self.coding = coding
        if limits is not None and not coding:
            raise ValueError("invalid_job")
        self.limits = CodexEgressLimits.model_validate(limits) if limits is not None else None
        if (auth_file is None) != (auth_identity is None):
            raise ValueError("authentication_failed")
        if reasoning_effort is not None:
            validate_reasoning_effort(model, reasoning_effort)
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.byte_limit = byte_limit
        self.model = model
        self.credential = credential
        self.tool_schemas = {TOOL_SPECS[ref]["name"]: TOOL_SPECS[ref]["input_schema"] for ref in delegation_refs}
        if tool_schema is not None:
            self.tool_schemas["sandbox_exec"] = tool_schema
        self.bytes = 0
        self.accounted_request_bytes = 0
        self.request_bytes_read = 0
        self.response_bytes_read = 0
        self.peak_concurrent = 0
        self.cap_reason = 0
        self.tasks = set()
        self.writers = set()
        self.connections = 0
        self.directory = tempfile.TemporaryDirectory(prefix="kotoyose-codex-egress-")
        self.path = Path(self.directory.name) / "egress.sock"
        self.server = None
        self.closed = False
        self.auth_file = auth_file
        self.auth_identity = auth_identity
        self.failure_reason = None
        self.failure_http_status = None
        self.failure_limit = None
        from .codex_response_counter import ResponseCounter
        self.response_counter = ResponseCounter()
        self.failure_diagnostic = None
        self.failure_event = asyncio.Event()
        self.secrets = {credential.encode()}
        self.authorize = authorize
        # この追加枠は毎回照合する固定 image URI の入力だけ。応答・text 枠を増やさない。
        self.image_uris = image_uris
        self.image_bytes = sum(len(uri.encode("ascii")) for uri in image_uris)
        if (not isinstance(image_uris, tuple) or len(image_uris) > MAX_IMAGE_COUNT
                or self.image_bytes > 4 * ((MAX_IMAGE_TOTAL_BYTES + 2) // 3) + MAX_IMAGE_COUNT * 32
                or any(not uri.startswith(("data:image/png;base64,", "data:image/jpeg;base64,",
                                           "data:image/gif;base64,", "data:image/webp;base64,")) for uri in image_uris)
                or not isinstance(image_request_limit, int) or isinstance(image_request_limit, bool)
                or not 1 <= image_request_limit <= 32):
            self.directory.cleanup()
            raise ValueError("invalid_job")
        self.image_request_limit = image_request_limit
        self.image_input_bytes = 0
        self.image_requests_completed = 0
        self.secrets.update(uri.encode("ascii") for uri in image_uris)

    def metrics(self) -> dict[str, int]:
        """Application read counts, not TLS/wire traffic; no payloads, identities or headers."""
        return {
            "accounted_request_bytes": self.accounted_request_bytes,
            "request_bytes_read": self.request_bytes_read,
            "response_bytes_read": self.response_bytes_read,
            "connections_started": self.connections,
            "peak_concurrent": self.peak_concurrent,
            # 0 none, 1 total bytes, 2 total connections, 3 request body, 4 response, 5 concurrency.
            "cap_reason": self.cap_reason,
        }

    def _cap(self, reason: int, message: str = "egress_limit_exceeded"):
        self.cap_reason = self.cap_reason or reason
        raise ValueError(message)

    async def _read_header(self, reader):
        try:
            value = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError as exc:
            self.request_bytes_read += len(exc.partial)
            raise
        except asyncio.LimitOverrunError as exc:
            # readuntil leaves rejected bytes buffered. Count only bytes actually consumed.
            value = await reader.readexactly(exc.consumed)
            self.request_bytes_read += len(value)
            raise
        self.request_bytes_read += len(value)
        return value

    async def _read_body(self, reader, length):
        body = bytearray()
        while len(body) < length:
            chunk = await reader.read(min(16384, length - len(body)))
            self.request_bytes_read += len(chunk)
            if not chunk:
                raise asyncio.IncompleteReadError(b"", length)
            body.extend(chunk)
        return bytes(body)

    def _account_request(self, count):
        self.accounted_request_bytes += count
        self.consume(count)

    async def _read_response(self, remote, consumed):
        # Read at most the remaining per-response allowance plus one detection byte.
        size = min(16384, self.limits.response_bytes - consumed + 1) if self.limits is not None else 16384
        data = await remote.read(size)
        self.response_bytes_read += len(data)
        consumed += len(data)
        self.consume(len(data))
        if self.limits is not None and consumed > self.limits.response_bytes:
            self._cap(4)
        return data, consumed

    def redact(self, content: bytes, *, byte_limit: int | None = None) -> bytes:
        """provider の成功・部分出力と upstream response から broker 内の秘密を除去する。"""
        for secret in sorted(self.secrets, key=len, reverse=True):
            if secret:
                if byte_limit is not None and len(content) + content.count(secret) * (len(b"[redacted]") - len(secret)) > byte_limit:
                    self._cap(4)
                content = content.replace(secret, b"[redacted]")
        return content

    def subscription_request(self, request: bytes) -> bytes:
        """毎回 host 認証を再読取し、同一 account の更新だけを固定 endpoint へ送る。"""
        auth = read_subscription(self.auth_file)
        self.secrets.update({auth.access_token.encode(), auth.account_id.encode()})
        if auth.identity != self.auth_identity:
            raise ValueError("authentication_failed")
        lines = request.split(b"\r\n")
        lines[0] = b"POST /backend-api/codex/responses HTTP/1.1"
        for index, line in enumerate(lines):
            if line.startswith(b"host:"):
                lines[index] = b"host: chatgpt.com"
            elif line.startswith(b"authorization:"):
                lines[index] = b"authorization: Bearer " + auth.access_token.encode()
        lines.insert(-2, b"chatgpt-account-id: " + auth.account_id.encode())
        return b"\r\n".join(lines)

    async def subscription_response(self, remote, writer, observation=None, response_evidence=None):
        """bounded HTTP body を解釈し、失効・枠切れと secret の反射を越境させない。

        SSE は一要求分を検証後に渡す。chunked framing を解釈してから redact するため、
        chunk 境界による credential 漏洩も length 不一致も起こさない。
        """
        observation = observation or BrokerObservation()
        observation.phase = "upstream_response_headers"
        parser = h11.Connection(h11.CLIENT, max_incomplete_event_size=_MAX_HEADER)
        parser.send(h11.Request(method="POST", target="/", headers=[("host", "chatgpt.com")]))
        parser.send(h11.EndOfMessage())
        content = bytearray()
        status = None
        response_bytes = 0
        while True:
            event = parser.next_event()
            if event is h11.NEED_DATA:
                data, response_bytes = await self._read_response(remote, response_bytes)
                parser.receive_data(data)
            elif isinstance(event, h11.Response):
                status = event.status_code
                if response_evidence is not None:
                    response_evidence.status = status
                if status != 200:
                    # 最初の失敗に対応する数値だけを保持し、本文や headers は診断に渡さない。
                    if self.failure_reason is None:
                        self.failure_http_status = status
                        if status == 429:
                            from datetime import datetime, timezone
                            from .provider_usage_limits import normalize_provider_usage_limit
                            values = [v for k, v in event.headers if k.lower() == b"retry-after"]
                            value = values[0].decode("latin-1") if len(values) == 1 else None
                            self.failure_limit = normalize_provider_usage_limit("codex", {"http_status": 429,
                                "retry_after": value}, datetime.now(timezone.utc))
                    raise ValueError({401: "authentication_failed", 403: "subscription_denied",
                                      429: "subscription_limit_reached"}.get(status, "provider_error"))
                observation.phase = "upstream_response_body"
            elif isinstance(event, h11.Data):
                if response_evidence is not None:
                    response_evidence.feed_body(event.data)
                content.extend(event.data)
            elif isinstance(event, h11.EndOfMessage):
                observation.phase = "response_validation"
                if status != 200:
                    raise ValueError("provider_error")
                if self.image_uris:
                    validate_image_response(bytes(content))
                body = self.redact(bytes(content), byte_limit=self.limits.response_bytes if self.limits is not None else None)
                del content
                header = (b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\nContent-Length: "
                          + str(len(body)).encode() + b"\r\n\r\n")
                observation.phase = "downstream_response"
                if self.limits is None:
                    writer.write(header + body)
                else:
                    writer.write(header)
                    writer.write(body)
                await writer.drain()
                return
            elif isinstance(event, h11.ConnectionClosed) or event is h11.PAUSED:
                raise ValueError("provider_error")

    async def start(self):
        self.server = await asyncio.start_unix_server(self.handle, path=str(self.path), limit=_MAX_HEADER)
        os.chmod(self.path, 0o600)
        return self.path

    def consume(self, count):
        self.bytes += count
        limit = self.limits.total_bytes if self.limits is not None else self.byte_limit
        if limit != "disabled" and self.bytes > limit:
            self._cap(1)

    def validate_request(self, request, body):
        lines = request.decode("ascii").split("\r\n")
        headers = dict(line.split(": ", 1) for line in lines[1:] if line)
        if headers.get("authorization") != f"Bearer {self.credential}":
            raise ValueError("egress_denied")
        value = parse_json(body)
        if (not isinstance(value, dict) or set(value) - _FIELDS or value.get("model") != self.model
                or value.get("stream") is not True or value.get("store") is not False
                or value.get("service_tier") not in {None, "auto"}):
            raise ValueError("egress_denied")
        if self.reasoning_effort is not None:
            reasoning = value.get("reasoning")
            if not isinstance(reasoning, dict) or reasoning.get("effort") != self.reasoning_effort:
                raise ValueError("reasoning_effort_mismatch")
        tools = value.get("tools", [])
        if self.coding:
            from .codex_coding_policy import validate_builtin_tools
            validate_builtin_tools(tools)
        else:
            if not isinstance(tools, list) or len(tools) != len(self.tool_schemas):
                raise ValueError("egress_denied")
            remaining = dict(self.tool_schemas)
            for tool in tools:
                if (not isinstance(tool, dict) or tool.get("type") != "function"
                        or not isinstance(name := tool.get("name"), str) or name not in remaining
                        or tool.get("parameters") != remaining.pop(name)
                        or set(tool) - {"type", "name", "description", "parameters", "strict"}):
                    raise ValueError("egress_denied")
        if value.get("tool_choice", "auto") not in {"auto", "none"}:
            raise ValueError("egress_denied")
        validate_input(value.get("input", []), self.coding or bool(self.tool_schemas), self.image_uris,
                       coding=self.coding, function_names=self.tool_schemas.keys() if not self.coding else None)
        metadata = value.pop("client_metadata", None)
        if metadata is not None and (not isinstance(metadata, dict) or len(metadata) > 32
                or any(not isinstance(key, str) or not isinstance(item, str)
                       or len(key) > 128 or len(item) > 16384 for key, item in metadata.items())):
            raise ValueError("egress_denied")
        # metadata の routing hints/credit hints は固定 endpoint へ転送しない。
        # New pins bound both the received and normalized JSON body. Keep UTF-8
        # instead of inflating non-ASCII/DEL history into repeated ASCII escapes.
        encoded = json.dumps(value, separators=(",", ":"), allow_nan=False, ensure_ascii=self.limits is None).encode()
        if self.limits is not None and len(encoded) > self.limits.request_bytes:
            self._cap(3)
        return encoded

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        self.connections += 1
        upstream = None
        response_evidence = None
        observation = BrokerObservation()
        try:
            if self.closed:
                raise ValueError("egress_denied")
            limit = self.limits.total_connections if self.limits is not None else 32
            if limit != "disabled" and self.connections > limit:
                self._cap(2, "egress_denied" if self.limits is None else "egress_limit_exceeded")
            if len(self.tasks) > 4:
                self._cap(5, "egress_denied")
            if self.image_uris and self.connections > self.image_request_limit:
                raise ValueError("egress_denied")
            self.peak_concurrent = max(self.peak_concurrent, len(self.tasks))
            async with asyncio.timeout(self.timeout):
                observation.phase = "request_header"
                raw = await self._read_header(reader)
                observation.phase = "request_validation"
                try:
                    request, length = parse_request(raw, image_bytes=self.image_bytes if self.limits is None else 0,
                        request_bytes=self.limits.request_bytes if self.limits is not None else _MAX_BODY)
                except _RequestLimitExceeded:
                    self._cap(3, "egress_denied" if self.limits is None else "egress_limit_exceeded")
                if not self.image_uris:
                    self._account_request(len(raw) + length)
                observation.phase = "request_body"
                body = await self._read_body(reader, length)
                if self.failure_reason is not None:
                    raise ValueError("egress_denied")
                observation.phase = "request_validation"
                body = self.validate_request(request, body)
                if self.image_uris:
                    self.image_input_bytes += self.image_bytes
                    if self.image_input_bytes > self.image_bytes * self.image_request_limit:
                        raise ValueError("egress_limit_exceeded")
                    self._account_request(len(raw) + length - self.image_bytes)
                if self.authorize is not None:
                    observation.phase = "authorization"
                    await self.authorize()
                request = b"\r\n".join(b"content-length: " + str(len(body)).encode()
                    if line.startswith(b"content-length:") else line for line in request.split(b"\r\n"))
                observation.phase = "upstream_connect"
                if self.auth_file is not None:
                    remote, upstream = await connect_subscription_endpoint()
                else:
                    remote, upstream = await connect_endpoint()
                self.writers.add(upstream)
                # DNS/TLS の await 中の失効・profile 切替も、認証 header/body を送る前に拒む。
                if self.authorize is not None:
                    observation.phase = "authorization"
                    await self.authorize()
                if self.closed or self.failure_reason is not None:
                    raise ValueError("egress_denied")
                if self.auth_file is not None:
                    observation.phase = "request_validation"
                    request = self.subscription_request(request)
                observation.phase = "upstream_write"
                response_evidence = self.response_counter.begin()
                upstream.write(request + body)
                await upstream.drain()
                if self.auth_file is not None or self.image_uris:
                    await self.subscription_response(remote, writer, observation, response_evidence)
                    if self.image_uris:
                        self.image_requests_completed += 1
                    return
                response_bytes = 0
                observation.phase = "upstream_response_body"
                while True:
                    chunk, response_bytes = await self._read_response(remote, response_bytes)
                    response_evidence.feed_http(chunk)
                    if not chunk:
                        break
                    writer.write(chunk)
                    await writer.drain()
        except Exception as exc:
            if self.auth_file is not None or self.image_uris or self.reasoning_effort is not None:
                failure = codex_execution_failure(exc) or (str(exc) if isinstance(exc, ValueError) and str(exc) in {
                    "authentication_failed", "subscription_denied", "subscription_limit_reached",
                    "egress_denied", "egress_limit_exceeded", "reasoning_effort_mismatch",
                } else "provider_error")
                if self.failure_reason is None:
                    self.failure_diagnostic = observation.failure(exc)
                self.failure_reason = self.failure_reason or failure
                self.failure_event.set()
            # 例外、headers、TLS 本文、credential はログに残さない。
            with contextlib.suppress(Exception):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
        finally:
            if response_evidence is not None:
                response_evidence.finish()
            for item in (writer, upstream):
                if item is not None:
                    item.close()
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await item.wait_closed()
                    self.writers.discard(item)
            self.tasks.discard(task)

    def settled(self) -> bool:
        """close 済みで、接続を扱う task が一つも残っていなければ True。"""
        return self.closed and all(task.done() for task in tuple(self.tasks))

    async def close(self, grace):
        self.closed = True
        if self.server is not None:
            self.server.close()
        # Python 3.13 の server 待ちは active transport も含むため、先に処理を閉じる。
        for writer in tuple(self.writers):
            writer.close()
        for task in tuple(self.tasks):
            task.cancel()
        pending = set()
        server_wait = asyncio.create_task(self.server.wait_closed()) if self.server is not None else None
        waits = {*self.tasks, *([server_wait] if server_wait is not None else [])}
        if waits:
            _, pending = await asyncio.wait(waits, timeout=max(.1, grace))
        if server_wait is not None:
            if server_wait in pending:
                server_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await server_wait
            else:
                try:
                    server_wait.result()
                except Exception:
                    pending.add(server_wait)
        if not pending:
            self.directory.cleanup()
        return "unconfirmed" if pending else "confirmed"
