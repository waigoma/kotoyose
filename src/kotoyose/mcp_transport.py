"""MCP 1.27 の Streamable HTTP adapter。送信した tool call を自動再実行しない。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from typing import AsyncIterator, Protocol

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from . import process_env
from .mcp_contract import ExternalMcpServerConfig


class McpTransportError(Exception):
    """外部 server の応答本文や credential を公開しない transport 拒否。"""


class McpSessionPort(Protocol):
    """metadata の解決と、既に journal へ保存した一回の呼び出しを提供する。"""

    server_name: str
    server_version: str
    protocol_version: str

    async def list_tools(self) -> list[dict]: ...
    async def call_tool(self, name: str, arguments: dict, operation_id: str, timeout_seconds: int) -> dict: ...


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, limit: int):
        self.inner, self.limit = inner, limit

    async def __aiter__(self):
        size = 0
        async for chunk in self.inner:
            size += len(chunk)
            if size > self.limit:
                raise McpTransportError('MCP response exceeds transport limit')
            yield chunk

    async def aclose(self):
        await self.inner.aclose()


class _BoundedTransport(httpx.AsyncBaseTransport):
    def __init__(self, endpoint: str, limit: int):
        self.endpoint, self.limit = httpx.URL(endpoint), limit
        self.inner = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url != self.endpoint:
            raise McpTransportError('MCP request left the configured endpoint')
        response = await self.inner.handle_async_request(request)
        # Content-Encoding による展開後の上限回避を許さない。
        if response.headers.get('content-encoding', 'identity') != 'identity':
            await response.aclose()
            raise McpTransportError('Compressed MCP response is unsupported')
        response.stream = _BoundedStream(response.stream, self.limit)
        return response

    async def aclose(self):
        await self.inner.aclose()


class _SdkSession:
    def __init__(self, session: ClientSession, initialized, credential: str | None):
        self.session = session
        self.credential = credential
        self.server_name = initialized.serverInfo.name
        self.server_version = initialized.serverInfo.version
        self.protocol_version = initialized.protocolVersion

    async def list_tools(self) -> list[dict]:
        cursor = None
        cursors: set[str] = set()
        result = []
        for _ in range(8):
            page = await self.session.list_tools(cursor=cursor)
            tools = [tool.model_dump(mode='json', exclude_none=True, by_alias=True) for tool in page.tools]
            self._reject_credential_echo(tools)
            result.extend(tools)
            if len(result) > 256:
                raise McpTransportError('MCP tool inventory exceeds limit')
            cursor = page.nextCursor
            if not cursor:
                return result
            if cursor in cursors:
                raise McpTransportError('MCP pagination cursor repeated')
            cursors.add(cursor)
        raise McpTransportError('MCP pagination exceeds limit')

    async def call_tool(self, name: str, arguments: dict, operation_id: str, timeout_seconds: int) -> dict:
        result = await self.session.call_tool(name, arguments, read_timeout_seconds=timedelta(seconds=timeout_seconds), meta={'kotoyose_operation_id': operation_id})
        payload = result.model_dump(mode='json', exclude_none=True, by_alias=True)
        self._reject_credential_echo(payload)
        return payload

    def _reject_credential_echo(self, payload):
        if not self.credential:
            return
        if isinstance(payload, str) and self.credential in payload:
            raise McpTransportError('MCP response contains credential material')
        if isinstance(payload, dict):
            for key, value in payload.items():
                self._reject_credential_echo(key)
                self._reject_credential_echo(value)
        elif isinstance(payload, list):
            for value in payload:
                self._reject_credential_echo(value)


class StreamableMcpTransport:
    """credential は gateway 内の HTTP header にだけ渡し、provider や snapshot へ渡さない。"""

    @asynccontextmanager
    async def connect(self, config: ExternalMcpServerConfig) -> AsyncIterator[McpSessionPort]:
        try:
            async with self._connect(config) as session:
                yield session
        except BaseExceptionGroup as group:
            # SDK task group の一重化だけを行う。複数の失敗を勝手に成功へ分類しない。
            error = group
            while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
                error = error.exceptions[0]
            raise error from group

    @asynccontextmanager
    async def _connect(self, config: ExternalMcpServerConfig) -> AsyncIterator[McpSessionPort]:
        headers = {'Accept-Encoding': 'identity'}
        token = None
        if config.credential_env:
            token = process_env.get(config.credential_env)
            if not token:
                raise McpTransportError('MCP credential is unavailable')
            headers['Authorization'] = f'Bearer {token}'
        limit = max(config.max_metadata_bytes, *(tool.max_result_bytes for tool in config.tools.values()))
        transport = _BoundedTransport(config.endpoint, limit)
        async with httpx.AsyncClient(transport=transport, headers=headers, follow_redirects=False, trust_env=False,
                                    timeout=httpx.Timeout(300, connect=config.connect_timeout_seconds)) as client:
            # GET stream の再接続は観測だけ。SDK の tools/call POST は再送しない版へ pin する。
            async with streamable_http_client(config.endpoint, http_client=client, terminate_on_close=False) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=config.connect_timeout_seconds)) as session:
                    initialized = await session.initialize()
                    if (initialized.serverInfo.name, initialized.serverInfo.version) != (config.server_name, config.server_version):
                        raise McpTransportError('MCP server identity changed')
                    yield _SdkSession(session, initialized, token)
