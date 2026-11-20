"""認証済み Caller の MCP pin と外部効果の照合 API。Tool 実行の入口は設けない。"""

from fastapi import APIRouter, Depends, Query, Request

from ..execution_http import authorize_execution, execution_service
from ..execution_resources import ExecutionError
from ..mcp_contract import McpPinRequest, McpReconciliationRequest
from ..mcp_operation_store import mcp_call_view


router = APIRouter(dependencies=[Depends(authorize_execution)])


@router.post('/v1/mcp-pins', status_code=201)
async def pin_mcp(body: McpPinRequest, request: Request) -> dict:
    """管理者の接続先と上限から immutable resource を発行する。"""
    service = execution_service(request)
    try:
        binding = await service.admission.mcp_resolver.pin(request.state.execution_policy, body)
    except ExecutionError:
        raise
    except Exception as exc:
        raise ExecutionError('provider_failure', 'MCP pin resolution failed', 502) from exc
    ref = await service.store.put_resource(request.state.execution_principal, binding)
    return {'schema_version': '1', 'resource_ref': ref, 'binding': binding.model_dump(mode='json')}


@router.get('/v1/attempts/{attempt_id}/mcp-calls')
async def get_mcp_calls(attempt_id: str, request: Request, after: str | None = Query(None, max_length=128),
                        limit: int = Query(100, ge=1, le=100)) -> dict:
    """送信 claim をせず、metadata だけを読み取る。"""
    calls = await execution_service(request).store.mcp_calls.list_calls(request.state.execution_principal, attempt_id, after, limit)
    return {'schema_version': '1', 'calls': calls, 'next_cursor': calls[-1]['id'] if calls else after}


@router.post('/v1/attempts/{attempt_id}/mcp-calls/{call_id}/reconciliation')
async def reconcile_mcp_call(attempt_id: str, call_id: str, body: McpReconciliationRequest, request: Request) -> dict:
    """遅い証拠で Attempt を復活させず、残った効果と保持だけを解決する。"""
    service = execution_service(request)
    principal = request.state.execution_principal
    row = await service.store.mcp_calls.reconcile(principal, attempt_id, call_id, **body.model_dump(exclude={'schema_version'}))
    attempt = await service.store.attempt(principal, attempt_id)
    if attempt['finished_at'] is not None and attempt['stop_confirmation'] == 'confirmed':
        await service.store.confirm_stop(attempt_id)
    return {'schema_version': '1', 'call': mcp_call_view(row)}
