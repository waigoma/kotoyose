from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request

from kotoyose.agent_execution_service import AgentExecutionService
from kotoyose.execution_http import authorize_execution
from kotoyose.routes.agent_executions import router


@pytest.mark.asyncio
async def test_events_return_fractional_payload_and_preserve_pagination():
    cursor = '000001:0000000001'
    event = {'cursor': cursor, 'activation_ordinal': 1, 'attempt_id': 'attempt-1', 'sequence': 1,
             'type': 'context_usage', 'payload': {'percentage': 42.5}, 'created_at': '2026-11-18T00:00:00Z'}
    service = AgentExecutionService.__new__(AgentExecutionService)
    service.store = SimpleNamespace(events=AsyncMock(side_effect=[
        {'schema_version': 'agent_execution_v1', 'idempotency_key': 'key', 'events': [event],
         'next_cursor': cursor, 'complete': False},
        {'schema_version': 'agent_execution_v1', 'idempotency_key': 'key', 'events': [],
         'next_cursor': cursor, 'complete': True},
    ]))
    app = FastAPI()
    app.state.execution_service = SimpleNamespace(agent_executions=service)
    app.include_router(router)

    async def authorize(request: Request):
        request.state.execution_principal = 'test'

    app.dependency_overrides[authorize_execution] = authorize
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        path = '/v1/executions/by-key/key/events'
        first = await client.get(path, params={'after': '000000:0000000000', 'limit': 200})
        assert first.status_code == 200, first.text
        assert first.json()['events'] == [event]
        assert first.json()['next_cursor'] == cursor and first.json()['complete'] is False
        second = await client.get(path, params={'after': cursor, 'limit': 200})
        assert second.status_code == 200
        assert second.json()['events'] == []
        assert second.json()['next_cursor'] == cursor and second.json()['complete'] is True
    assert service.store.events.await_args_list[0].args == ('test', 'key', (0, 0), 200)
    assert service.store.events.await_args_list[1].args == ('test', 'key', (1, 1), 200)
