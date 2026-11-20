"""MCP metadata を固定し、現在の管理者上限と remote tool surface の一致を検査する。"""

from __future__ import annotations

import asyncio

from .execution_resources import CallerPolicy, ExecutionError
from .mcp_contract import (
    ExternalMcpServerConfig,
    McpBindingResource,
    McpPinRequest,
    PinnedMcpTool,
    mcp_content_digest,
    validate_narrowing_constraints,
)
from .mcp_transport import McpSessionPort, StreamableMcpTransport


def server_for_pin(servers: dict[str, ExternalMcpServerConfig], policy: CallerPolicy, pin: McpBindingResource) -> ExternalMcpServerConfig:
    """古い Grant があっても、現在の Caller 上限や管理者の接続先変更を迂回しない。"""
    server = servers.get(pin.server_id)
    if server is None or pin.server_id not in policy.mcp_servers:
        raise ExecutionError('permission_denied', 'MCP server is outside current Caller ceiling', 403)
    if (server.config_digest(), server.server_name, server.server_version, server.trust_profile_ref) != (
        pin.config_digest, pin.server_name, pin.server_version, pin.trust_profile_ref,
    ):
        raise ExecutionError('request_conflict', 'Pinned MCP server configuration changed', 409)
    for tool in pin.tools:
        ceiling = server.tools.get(tool.name)
        if ceiling is None or ceiling != tool.ceiling:
            raise ExecutionError('permission_denied', 'Pinned MCP tool exceeds current administrator ceiling', 403)
    return server


async def verify_mcp_surface(session: McpSessionPort, pin: McpBindingResource) -> None:
    """MCP annotation の信頼性に依存せず、保存した定義との一致を確かめる。"""
    if (session.server_name, session.server_version, session.protocol_version) != (pin.server_name, pin.server_version, pin.protocol_version):
        raise ExecutionError('request_conflict', 'Pinned MCP server identity changed', 409)
    tools = await session.list_tools()
    indexed = {tool['name']: tool for tool in tools}
    if len(indexed) != len(tools):
        raise ExecutionError('request_conflict', 'MCP server returned duplicate tool names', 409)
    for expected in pin.tools:
        current = indexed.get(expected.name)
        if current is None or mcp_content_digest(current) != expected.tool_digest or current.get('inputSchema') != expected.input_schema or (current.get('description') or '') != expected.description:
            raise ExecutionError('request_conflict', 'Pinned MCP tool surface changed', 409)


class McpResourceResolver:
    """read-only の pin 解決を提供する。Tool の副作用実行は Broker が担当する。"""

    def __init__(self, servers: dict[str, ExternalMcpServerConfig], transport: StreamableMcpTransport | None = None):
        self.servers = servers
        self.transport = transport or StreamableMcpTransport()

    async def pin(self, policy: CallerPolicy, request: McpPinRequest) -> McpBindingResource:
        """許可された Tool 群を一回の server snapshot へ固定する。"""
        server = self.servers.get(request.server_id)
        if server is None or request.server_id not in policy.mcp_servers:
            raise ExecutionError('permission_denied', 'MCP server is outside Caller ceiling', 403)
        if set(request.tools) - server.tools.keys():
            raise ExecutionError('permission_denied', 'MCP tool is outside administrator ceiling', 403)
        try:
            for name, narrowing in request.narrowing_constraints.items():
                validate_narrowing_constraints(server.tools[name].argument_constraints, narrowing)
        except ValueError as exc:
            raise ExecutionError('invalid_request', 'MCP narrowing does not monotonically restrict the administrator ceiling', 400) from exc
        async with asyncio.timeout(server.connect_timeout_seconds):
            async with self.transport.connect(server) as session:
                available = await session.list_tools()
                indexed = {tool['name']: tool for tool in available}
                if len(indexed) != len(available) or set(request.tools) - indexed.keys():
                    raise ExecutionError('request_conflict', 'Requested MCP tool surface is unavailable', 409)
                return McpBindingResource(
                    schema_version='1', kind='mcp_binding', server_id=request.server_id,
                    config_digest=server.config_digest(), server_name=session.server_name,
                    server_version=session.server_version, protocol_version=session.protocol_version,
                    trust_profile_ref=server.trust_profile_ref,
                    tools=[PinnedMcpTool(name=name, alias=alias, description=indexed[name].get('description') or '',
                                         input_schema=indexed[name]['inputSchema'], tool_digest=mcp_content_digest(indexed[name]),
                                         ceiling=server.tools[name],
                                         narrowing_constraints=request.narrowing_constraints.get(name),
                                         narrowing_digest=(mcp_content_digest(request.narrowing_constraints[name])
                                                           if name in request.narrowing_constraints else None))
                           for name, alias in request.tools.items()],
                )

    async def validate(self, policy: CallerPolicy, pin: McpBindingResource) -> None:
        """admission と実操作直前に同じ pin を照合する。"""
        server = server_for_pin(self.servers, policy, pin)
        async with asyncio.timeout(server.connect_timeout_seconds):
            async with self.transport.connect(server) as session:
                await verify_mcp_surface(session, pin)
