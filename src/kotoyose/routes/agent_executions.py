"""Unconfigured Hub execution route; mounted only by A1's isolated tests."""
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import ValidationError

from ..execution_http import authorize_execution, execution_service
from ..execution_resources import ExecutionError
from ..agent_execution_contract import EVENT_CURSOR_START, AgentExecutionError
from ..agent_execution_digest import read_agent_json


def agent_execution_error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, AgentExecutionError):
        code, status, message, current = exc.code, exc.status, str(exc), exc.current
    elif isinstance(exc, (ValidationError, RequestValidationError)):
        code, status, message, current = 'invalid_request', 422, 'Invalid request', None
    elif isinstance(exc, ExecutionError) and (exc.status < 500 or exc.status == 501):
        code, status = {404: ('not_found', 404), 401: ('permission_denied', 403),
            403: ('permission_denied', 403), 501: ('unsupported_capability', 501),
            409: ('request_conflict', 409), 413: ('payload_too_large', 413),
            400: ('invalid_request', 422), 422: ('invalid_request', 422)}.get(exc.status, ('invalid_request', 422))
        message, current = str(exc), None
    else:
        raise exc
    return JSONResponse(status_code=status, content={'error': {'type': 'agent_execution_error', 'code': code, 'message': message, 'current': current}})


class AgentExecutionRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def wrapped(request):
            try:
                return await handler(request)
            except (AgentExecutionError, ExecutionError, ValidationError, RequestValidationError) as exc:
                return agent_execution_error_response(exc)
        return wrapped


router = APIRouter(route_class=AgentExecutionRoute, dependencies=[Depends(authorize_execution)])


@router.get('/v1/executions/by-key/{key}')
async def agent_execution_read(key: str, request: Request, after_receipt: int = Query(default=0, ge=0, le=2**53 - 1)):
    return await execution_service(request).agent_executions.read(request.state.execution_principal, key, after_receipt)


@router.get('/v1/executions/by-key/{key}/events')
async def agent_execution_events(key: str, request: Request, after: str = Query(default=EVENT_CURSOR_START, max_length=32),
                     limit: int = Query(default=100, ge=1, le=500)):
    return await execution_service(request).agent_executions.events(request.state.execution_principal, key, after, limit)


@router.put('/v1/executions/by-key/{key}/authorization')
async def agent_execution_authorize(key: str, request: Request):
    raw = read_agent_json(await request.body())
    return await execution_service(request).agent_executions.authorize(request.state.execution_principal, key, raw)


@router.post('/v1/executions/by-key/{key}/try-now', status_code=202)
async def agent_execution_try_now(key: str, request: Request):
    raw = read_agent_json(await request.body())
    return await execution_service(request).agent_executions.try_now(request.state.execution_principal, key, raw)


@router.post('/v1/executions/by-key/{key}/cancel')
async def agent_execution_cancel(key: str, request: Request):
    status, result = await execution_service(request).agent_executions.cancel(
        request.state.execution_principal, key, read_agent_json(await request.body()))
    return JSONResponse(status_code=status, content=result)


@router.post('/v1/executions/by-key/{key}/client-connections')
async def agent_execution_client_connect(key: str, request: Request):
    return await execution_service(request).agent_executions.client_tools.connect(
        request.state.execution_principal, key, read_agent_json(await request.body()))


@router.get('/v1/executions/by-key/{key}/client-calls')
async def agent_execution_client_calls(key: str, request: Request, client_key: str, generation: int = Query(ge=0),
                           limit: int = Query(default=32, ge=1, le=100)):
    return await execution_service(request).agent_executions.client_tools.calls(request.state.execution_principal, key, client_key, generation, limit)


@router.get('/v1/executions/by-key/{key}/client-calls/unresolved')
async def agent_execution_client_unresolved(key: str, request: Request, limit: int = Query(default=100, ge=1, le=100)):
    return await execution_service(request).agent_executions.client_tools.unresolved(request.state.execution_principal, key, limit)


@router.post('/v1/executions/by-key/{key}/client-calls/{call_id}/results')
async def agent_execution_client_result(key: str, call_id: str, request: Request):
    return await execution_service(request).agent_executions.client_tools.result(
        request.state.execution_principal, key, call_id, read_agent_json(await request.body()))


@router.post('/v1/executions/by-key/{key}/client-calls/{call_id}/reconciliation')
async def agent_execution_client_reconcile(key: str, call_id: str, request: Request):
    return await execution_service(request).agent_executions.client_tools.reconcile(
        request.state.execution_principal, key, call_id, read_agent_json(await request.body()))


async def agent_execution_create(request: Request) -> JSONResponse:
    try:
        raw = read_agent_json(await request.body())
        result = await execution_service(request).agent_executions.create(
            request.state.execution_principal, request.state.execution_policy, raw, request.state.api_key_name)
        return JSONResponse(status_code=202, content=result)
    except (AgentExecutionError, ExecutionError, ValidationError, RequestValidationError) as exc:
        return agent_execution_error_response(exc)
