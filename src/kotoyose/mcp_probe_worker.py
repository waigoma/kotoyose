"""Private short-lived MCP client. Only sanitized progress/result records go to stdout.

The parent owns a process group and hard deadline; stdio children stay in that group.
No provider, agent, config persistence, tool call, sampling or OAuth flow is started.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import logging
import sys
from urllib.parse import parse_qsl, unquote, urlsplit

import anyio
import httpx
from mcp import ClientSession, types
from mcp.client.sse import sse_client
from mcp.client.stdio import get_default_environment
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.shared.message import SessionMessage

from .mcp_probe import MAX_CONFIG_BYTES, MAX_METADATA_BYTES, MAX_PAGES, MAX_TOOLS, probe_error
from .mcp_transport import _BoundedStream, McpTransportError


def emit(payload):
    print(json.dumps(payload, ensure_ascii=True), flush=True)


class ProbeTransport(httpx.AsyncBaseTransport):
    def __init__(self, endpoint: str, sse: bool):
        self.endpoint, self.sse = httpx.URL(endpoint), sse
        self.inner = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request):
        target, origin = request.url, self.endpoint
        # SSE negotiates a POST path, but never another origin or credential-bearing URL.
        same_origin = (target.scheme, target.host, target.port) == (origin.scheme, origin.host, origin.port)
        if target.userinfo or target.fragment or (target != origin and not (self.sse and same_origin and request.method == 'POST')):
            raise McpTransportError('endpoint_rejected')
        response = await self.inner.handle_async_request(request)
        if response.headers.get('content-encoding', 'identity') != 'identity':
            await response.aclose()
            raise McpTransportError('compressed_response')
        response.stream = _BoundedStream(response.stream, MAX_METADATA_BYTES)
        if (request.method == 'POST' and response.status_code == 200
                and response.headers.get('content-type', '').split(';', 1)[0].strip() == 'application/json'):
            # MCP SDK 1.27 logs malformed JSON and can otherwise wait until request timeout.
            # Validate the bounded JSON envelope before handing it to the SDK, without logging it.
            payload = await response.aread()
            try:
                types.JSONRPCMessage.model_validate_json(payload)
            except Exception:
                raise McpTransportError('invalid_protocol') from None
        return response

    async def aclose(self):
        await self.inner.aclose()


@asynccontextmanager
async def stdio_streams(server):
    # Deliberately no shell and no new session: the parent can terminate the entire group.
    process = await asyncio.create_subprocess_exec(
        server['command'], *server.get('args', []), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env={**get_default_environment(), **server.get('env', {})}, limit=MAX_METADATA_BYTES,
    )
    incoming_writer, incoming = anyio.create_memory_object_stream(0)
    outgoing, outgoing_reader = anyio.create_memory_object_stream(0)

    async def read():
        async with incoming_writer:
            while line := await process.stdout.readline():
                try:
                    payload = types.JSONRPCMessage.model_validate_json(line)
                except Exception:
                    await incoming_writer.send(McpTransportError('invalid_protocol'))
                    return
                await incoming_writer.send(SessionMessage(payload))

    async def write():
        async with outgoing_reader:
            async for message in outgoing_reader:
                process.stdin.write((message.message.model_dump_json(by_alias=True, exclude_none=True) + '\n').encode())
                await process.stdin.drain()

    async with anyio.create_task_group() as group:
        group.start_soon(read)
        group.start_soon(write)
        try:
            yield incoming, outgoing
        finally:
            process.stdin.close()
            group.cancel_scope.cancel()
            # The parent performs bounded whole-group TERM/KILL even after a successful probe.
            await incoming.aclose()
            await outgoing.aclose()


@asynccontextmanager
async def streams(server):
    kind = server.get('type', 'stdio')
    if kind == 'stdio':
        async with stdio_streams(server) as pair:
            yield pair
        return
    headers = {**server.get('headers', {}), 'Accept-Encoding': 'identity'}
    def factory(headers=headers, timeout=None, auth=None):
        return httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(15, connect=5),
                                transport=ProbeTransport(server['url'], kind == 'sse'),
                                follow_redirects=False, trust_env=False)
    if kind == 'sse':
        async with sse_client(server['url'], headers=headers, timeout=5, sse_read_timeout=15, httpx_client_factory=factory) as pair:
            yield pair
    else:
        async with factory() as client:
            async with streamable_http_client(server['url'], http_client=client, terminate_on_close=True) as (read, write, _):
                yield read, write


def safe_name(name: str, server: dict) -> str:
    # Tool names are untrusted. No schemas/descriptions/server instructions are returned.
    url = urlsplit(server.get('url', ''))
    args = server.get('args', [])
    secrets = list(server.get('env', {}).values()) + list(server.get('headers', {}).values()) + args
    secrets += [arg.split('=', 1)[1] for arg in args if '=' in arg]
    secrets += [value for _, value in parse_qsl(url.query)]
    secrets += [unquote(part) for part in url.path.split('/') if part]
    if server.get('url'):
        secrets.append(server['url'])
    for value in list(secrets):
        if value.lower().startswith(('bearer ', 'basic ')):
            secrets.append(value.split(' ', 1)[1])
    # Normalize before redaction, so inserted control characters cannot reconstruct a secret.
    name = ''.join(char for char in name if char.isprintable())
    secrets = {''.join(char for char in value if char.isprintable()) for value in secrets}
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            name = name.replace(secret, '[redacted]')
    return name[:256]


def error_code(exc):
    if isinstance(exc, BaseExceptionGroup):
        return error_code(exc.exceptions[0])
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in (401, 403):
            return 'authentication_failed', '認証に失敗しました。MCP の headers / env を確認してください'
        if 300 <= status < 400:
            return 'redirect_rejected', 'リダイレクトは追跡しません。MCP の最終接続先 URL を指定してください'
        return 'http_error', f'MCP が HTTP {status} を返しました'
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return 'timeout', 'MCP の応答がタイムアウトしました'
    if isinstance(exc, FileNotFoundError):
        return 'command_not_found', 'Kotoyose ホストで command が見つかりません'
    if isinstance(exc, PermissionError):
        return 'permission_denied', 'Kotoyose ホストで command を実行できません'
    if isinstance(exc, McpError) and exc.error.code == httpx.codes.REQUEST_TIMEOUT:
        return 'timeout', 'MCP の応答がタイムアウトしました'
    if isinstance(exc, McpError):
        return 'protocol_error', 'MCP が protocol エラーを返しました'
    if isinstance(exc, McpTransportError):
        return 'transport_rejected', 'MCP の接続先または応答が安全上の制限を満たしていません'
    return 'connection_failed', 'MCP の接続または protocol 応答を確認できませんでした'


async def probe(server):
    stage = 'connect'
    try:
        emit({'stage': stage})
        async with streams(server) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as session:
                stage = 'initialize'
                emit({'stage': stage})
                initialized = await session.initialize()
                tools = []
                if initialized.capabilities.tools is None:
                    status, message = 'tools_unsupported', '接続と初期化に成功しました。tools capability は提供されていません'
                else:
                    stage = 'tools_list'
                    emit({'stage': stage})
                    cursor, cursors, names = None, set(), set()
                    for _ in range(MAX_PAGES):
                        page = await session.list_tools(cursor=cursor)
                        for tool in page.tools:
                            if tool.name in names or len(names) >= MAX_TOOLS:
                                raise McpTransportError('tool_inventory_limit')
                            names.add(tool.name)
                            tools.append({'name': safe_name(tool.name, server)})
                        cursor = page.nextCursor
                        if not cursor:
                            break
                        if cursor in cursors:
                            raise McpTransportError('repeated_cursor')
                        cursors.add(cursor)
                    else:
                        raise McpTransportError('pagination_limit')
                    status = 'ok' if tools else 'no_tools'
                    message = '接続・初期化・ツール一覧取得に成功しました' if tools else '接続・初期化に成功しました。ツールは 0 件です'
                result = {'status': status, 'stage': stage, 'tools': tools,
                          'tool_count': None if status == 'tools_unsupported' else len(tools), 'message': message}
                stage = 'cleanup'
                emit({'stage': stage})
        return result
    except Exception as exc:
        code, message = error_code(exc)
        return probe_error(stage, code, message)


async def main():
    logging.disable(logging.CRITICAL)
    server = json.loads(sys.stdin.buffer.readline(MAX_CONFIG_BYTES + 1))
    result = await probe(server)
    emit(result)
    # Keep the group leader alive until the owning parent cleans up all descendants.
    await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(main())
