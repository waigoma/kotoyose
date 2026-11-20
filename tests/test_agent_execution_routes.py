"""Public router contract for the Hub and command entry points."""
import json
import os
import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from kotoyose.routes.executions import router
from tests.fixtures.agent_execution import ScriptedRuntime, isolated_agent_execution_harness
from tests.test_agent_execution_runner import wait_snapshot
from tests.test_execution_contract import _agent_execution, _command_execution


@pytest.fixture
async def public_agent_execution_harness(tmp_path, monkeypatch):
    async with isolated_agent_execution_harness(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL'), tmp_path, monkeypatch, execute=True) as hub:
        hub.runtime = ScriptedRuntime("wait")
        hub.service.agent_executor.runtime = hub.runtime
        app = FastAPI()
        app.state.execution_service = hub.service
        app.state.api_key_store = hub.app.state.api_key_store
        app.include_router(router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
                                     headers={'Authorization': 'Bearer kys_caller-a'}) as client:
            hub.client = client
            try:
                yield hub
            finally:
                for event in list(hub.service._cancels.values()):
                    event.set()
                if hub.service._tasks:
                    await asyncio.gather(*list(hub.service._tasks.values()), return_exceptions=True)


@pytest.mark.asyncio
async def test_agent_post_returns_agent_execution_ack(public_agent_execution_harness):
    hub = public_agent_execution_harness
    raw = hub.create_body(await hub.register_work())
    result = await hub.client.post('/v1/executions', json=raw)
    assert result.status_code == 202, result.text
    assert result.json()['schema_version'] == 'agent_execution_v1'
    capabilities = (await hub.client.get('/v1/capabilities')).json()['capabilities']
    assert capabilities['agent_execution_v1']['status'] == 'supported'
    assert all(name not in capabilities for name in ('agent_work_time_v1', 'provider_usage_limit_v1', 'native_resume_required_v1'))
    invalid = await hub.client.post('/v1/executions', json=raw, headers={'Authorization': 'Bearer invalid'})
    assert invalid.status_code == 401
    assert invalid.json() == {'detail': 'Invalid or missing API key'}
    assert invalid.headers['WWW-Authenticate'] == 'Bearer'
    unconfigured = FastAPI()
    unconfigured.state.api_key_store = hub.app.state.api_key_store
    unconfigured.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=unconfigured), base_url='http://test',
                                 headers=hub.client.headers) as client:
        response = await client.post('/v1/executions', json=raw)
        assert response.status_code == 501 and response.json()['error']['code'] == 'unsupported_capability'
        caps = (await client.get('/v1/capabilities')).json()['capabilities']
        assert caps['agent_execution_v1']['status'] == 'unsupported'


@pytest.mark.asyncio
async def test_old_agent_shape_is_rejected(public_agent_execution_harness):
    result = await public_agent_execution_harness.client.post('/v1/executions', json=_agent_execution())
    assert result.status_code == 422
    assert await public_agent_execution_harness.store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_attempts') == 0


@pytest.mark.asyncio
async def test_command_post_still_returns_attempt(public_agent_execution_harness):
    public_agent_execution_harness.service.dispatch = AsyncMock(return_value='attempt-1')
    result = await public_agent_execution_harness.client.post('/v1/executions', json=_command_execution())
    assert result.status_code == 202
    assert result.json() == {'schema_version': '1', 'attempt_id': 'attempt-1'}


@pytest.mark.asyncio
async def test_removed_routes_are_gone(public_agent_execution_harness):
    assert (await public_agent_execution_harness.client.post('/v1/attempts/id/resume', json={})).status_code in (404, 405)
    assert (await public_agent_execution_harness.client.get('/v1/attempts/id/limits')).status_code in (404, 405)


@pytest.mark.asyncio
async def test_agent_execution_routes_require_original_principal(public_agent_execution_harness):
    hub = public_agent_execution_harness
    raw = hub.create_body(await hub.register_work())
    assert (await hub.client.post('/v1/executions', json=raw)).status_code == 202
    path = '/v1/executions/by-key/' + raw['execution']['idempotency_key']
    await asyncio.wait_for(hub.runtime.started.wait(), 3)
    before = await wait_snapshot(hub, raw['execution']['idempotency_key'], lambda value: value['state'] == 'running')
    headers = {'Authorization': 'Bearer kys_caller-b'}
    result_body = {'client_key': 'x', 'generation': 1, 'activation_nonce': 'nonce', 'tool_ref': 'tool',
                   'arguments_digest': 'a' * 64, 'disposition': 'applied', 'result': {}}
    calls = [('GET', '', None), ('PUT', '/authorization', raw['execution']['initial_authorization']),
             ('POST', '/try-now', {'action_key': 'a', 'expected_revision': 1}),
             ('POST', '/client-connections', {'client_key': 'x', 'generation': 1}),
             ('GET', '/client-calls?client_key=x&generation=1', None),
             ('POST', '/client-calls/c/results', result_body),
             ('POST', '/client-calls/c/reconciliation', {**result_body, 'evidence_ref': 'evidence'})]
    for method, suffix, body in calls:
        result = await hub.client.request(method, path + suffix, json=body, headers=headers)
        assert result.status_code == 404, (suffix, result.text)
        assert result.json()['error']['current'] is None
    closed = await hub.client.post(path + '/cancel', json={'action_key': 'b', 'reason': 'owner_abort'}, headers=headers)
    assert closed.status_code == 200 and closed.json()['outcome'] == 'not_admitted'
    rows = await hub.store.pool.fetch('SELECT principal FROM kotoyose.agent_execution_cancellations WHERE idempotency_key=$1', raw['execution']['idempotency_key'])
    assert [row['principal'] for row in rows] == ['caller-b']
    later = await hub.client.post('/v1/executions', json=raw, headers=headers)
    assert later.status_code == 409
    assert later.json()['error']['code'] == 'request_conflict' and later.json()['error']['current'] is None
    for method, suffix, body in calls:
        result = await hub.client.request(method, path + suffix, json=body, headers=headers)
        assert result.status_code == 404, (suffix, result.text)
        assert result.json()['error']['current'] is None
    after = (await hub.client.get(path)).json()
    assert after['state'] == 'running' and not after['cancel_requested'] and not after['admission_closed']
    assert after['execution_id'] == before['execution_id'] and after['activation'] == before['activation']
    assert len(hub.runtime.jobs) == 1
    result = await hub.client.get(path, headers={'Authorization': 'Bearer invalid'})
    assert result.status_code == 401
    assert result.json() == {'detail': 'Invalid or missing API key'}
    assert result.headers['WWW-Authenticate'] == 'Bearer'


@pytest.mark.asyncio
async def test_by_key_events_paginate_with_cursor_and_complete_flag(public_agent_execution_harness):
    hub = public_agent_execution_harness
    raw = hub.create_body(await hub.register_work())
    key = raw['execution']['idempotency_key']
    assert (await hub.client.post('/v1/executions', json=raw)).status_code == 202
    await asyncio.wait_for(hub.runtime.started.wait(), 3)
    attempt_id = await hub.store.pool.fetchval("""SELECT a.attempt_id FROM kotoyose.agent_execution_activations a
        JOIN kotoyose.agent_executions e ON e.id=a.execution_id WHERE e.idempotency_key=$1 AND a.attempt_id IS NOT NULL
        ORDER BY a.ordinal DESC LIMIT 1""", key)
    base = await hub.store.pool.fetchval('SELECT COALESCE(MAX(sequence),0) FROM kotoyose.execution_events WHERE attempt_id=$1', attempt_id)
    await hub.store.pool.executemany('INSERT INTO kotoyose.execution_events(attempt_id,sequence,type,payload) VALUES($1,$2,$3,$4::jsonb)',
        [(attempt_id, base + i, 'provider_progress', json.dumps({'kind': 'text', 'text': str(i)})) for i in range(1, 6)])
    path = '/v1/executions/by-key/' + key + '/events'
    everything = (await hub.client.get(path, params={'limit': 500})).json()
    assert everything['schema_version'] == 'agent_execution_v1' and everything['idempotency_key'] == key
    expected = [event['cursor'] for event in everything['events']]
    assert len(expected) >= 5 and expected == sorted(expected) and len(set(expected)) == len(expected)
    assert everything['complete'] is False and everything['next_cursor'] == expected[-1]

    collected, after = [], '000000:0000000000'
    while True:
        page = (await hub.client.get(path, params={'after': after, 'limit': 2})).json()
        assert len(page['events']) <= 2
        collected += page['events']
        if not page['events']:
            assert page['next_cursor'] == after
            break
        assert page['next_cursor'] == page['events'][-1]['cursor'] and page['next_cursor'] > after
        after = page['next_cursor']
    assert [event['cursor'] for event in collected] == expected
    assert all(event['attempt_id'] == attempt_id for event in collected)

    await hub.store.pool.execute("UPDATE kotoyose.agent_executions SET state='terminal' WHERE idempotency_key=$1", key)
    done = (await hub.client.get(path, params={'after': everything['next_cursor']})).json()
    assert done['events'] == [] and done['complete'] is True and done['next_cursor'] == everything['next_cursor']
    partial = (await hub.client.get(path, params={'limit': 1})).json()
    assert partial['complete'] is False and len(partial['events']) == 1

    invalid = await hub.client.get(path, params={'after': 'not-a-cursor'})
    assert invalid.status_code == 422 and invalid.json()['error']['type'] == 'agent_execution_error'
    for limit in (0, 501):
        assert (await hub.client.get(path, params={'limit': limit})).status_code == 422


@pytest.mark.asyncio
async def test_by_key_events_enforce_ownership_and_404_shape(public_agent_execution_harness):
    hub = public_agent_execution_harness
    raw = hub.create_body(await hub.register_work())
    key = raw['execution']['idempotency_key']
    assert (await hub.client.post('/v1/executions', json=raw)).status_code == 202
    await asyncio.wait_for(hub.runtime.started.wait(), 3)
    path = '/v1/executions/by-key/' + key + '/events'
    other = {'Authorization': 'Bearer kys_caller-b'}
    result = await hub.client.get(path, headers=other)
    assert result.status_code == 404
    assert result.json()['error']['type'] == 'agent_execution_error' and result.json()['error']['code'] == 'not_found'
    assert result.json()['error']['current'] is None
    missing = await hub.client.get('/v1/executions/by-key/no-such-key/events')
    assert missing.status_code == 404 and missing.json()['error']['code'] == 'not_found'
    assert missing.json()['error']['current'] is None
