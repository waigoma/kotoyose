"""外部 Tool の送信前 journal と実行直前の pin / Grant 照合を接続する。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from jsonschema import ValidationError

from .client_tool_bridge import ClientToolCallOutcome
from .config import get_client
from .execution_resources import ExecutionError, NativeExecutionConfig, canonical_json
from .mcp_contract import McpBindingResource, PinnedMcpTool, validate_pinned_mcp_arguments
from .mcp_resources import server_for_pin, verify_mcp_surface
from .mcp_transport import StreamableMcpTransport


class ExternalMcpBroker:
    """Agent には pinned Tool だけを公開し、接続先・credential を渡さない。"""

    def __init__(self, config: NativeExecutionConfig, store, transport: StreamableMcpTransport | None = None):
        self.config, self.store = config, store
        self.transport = transport or StreamableMcpTransport()

    async def call(self, principal: str, attempt_id: str, tool_ref: str, arguments: dict, cancel: asyncio.Event) -> ClientToolCallOutcome:
        """一回の POST を送る。不明な結果では dependent operation を journal が止める。"""
        call = await self.store.mcp_calls.prepare(principal, attempt_id, tool_ref, arguments)
        sent = False
        try:
            attempt = await self.store.attempt(principal, attempt_id)
            spec = next(item for item in attempt['snapshot']['mcp_tools'] if item['tool_ref'] == tool_ref)
            pin = McpBindingResource.model_validate(spec['binding'])
            policy = get_client(principal)
            if policy is None:
                raise ExecutionError('permission_denied', 'MCP Client was revoked', 403)
            server = server_for_pin(self.config.mcp_servers, policy, pin)
            remaining = (datetime.fromisoformat(attempt['snapshot']['deadline']) - datetime.now(timezone.utc)).total_seconds()
            timeout = min(remaining, spec['tool']['ceiling']['timeout_seconds'])
            if cancel.is_set() or timeout <= 0:
                raise ExecutionError('permission_denied', 'MCP call was cancelled or expired', 403)
            async with asyncio.timeout(timeout):
                async with self.transport.connect(server) as session:
                    await verify_mcp_surface(session, pin)
                    if cancel.is_set():
                        raise ExecutionError('permission_denied', 'MCP call was cancelled', 403)
                    try:
                        validate_pinned_mcp_arguments(PinnedMcpTool.model_validate(spec['tool']), arguments)
                    except ValidationError as exc:
                        raise ExecutionError('permission_denied', 'MCP arguments exceed allowed schema', 403) from exc
                    await self.store.mcp_calls.mark_sent(principal, attempt_id, call['id'])
                    sent = True
                    result = await session.call_tool(spec['tool']['name'], arguments, call['id'], spec['tool']['ceiling']['timeout_seconds'])
            # isError は「外部変更が起きなかった」という証明ではない。
            if result.get('isError'):
                raise ExecutionError('provider_failure', 'MCP server reported an uncertain tool failure', 502)
            outcome = {'provenance': 'external_untrusted', 'result': result}
            if len(canonical_json(result).encode()) > spec['tool']['ceiling']['max_result_bytes'] or len(canonical_json(outcome).encode()) > 1024 * 1024:
                outcome = {'error': 'result_limit_exceeded', 'effect': 'applied', 'provenance': 'external_untrusted'}
            await self.store.mcp_calls.finish(principal, attempt_id, call['id'], 'applied', outcome)
            return ClientToolCallOutcome(state='failed' if 'error' in outcome else 'succeeded', result=outcome, call_id=call['id'])
        except BaseException as error:
            # CancelledError も送信の有無を先に保存する。終了後の再実行で埋め合わせない。
            state = 'unknown' if sent else 'failed'
            reason = 'mcp_result_unknown' if sent else 'mcp_not_sent'
            try:
                await asyncio.shield(self.store.mcp_calls.finish(principal, attempt_id, call['id'], None if sent else 'not_applied', {'error': reason}))
            except ExecutionError:
                pass
            if isinstance(error, asyncio.CancelledError):
                raise
            if not isinstance(error, Exception):
                raise
            return ClientToolCallOutcome(state=state, result={'error': reason}, call_id=call['id'])
