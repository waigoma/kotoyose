import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from kotoyose.agent_execution_client_tools import AgentExecutionClientConnectionRequest
from tests.test_agent_execution_runner import running_agent_execution_harness, wait_snapshot


async def start_tool(hub):
    refs = await hub.register_work(client_tool=True)
    raw = hub.create_body(refs)
    response = await hub.client.post('/v1/executions', json=raw)
    assert response.status_code == 202, response.text
    await asyncio.wait_for(hub.runtime.started.wait(), 3)
    key = raw['execution']['idempotency_key']
    attempt = await hub.store.pool.fetchval('SELECT attempt_id FROM kotoyose.agent_execution_activations WHERE nonce=$1', raw['execution']['initial_authorization']['activation_nonce'])
    return raw, refs, key, attempt


async def connect(hub, key, generation=0, client_key='client:fixture'):
    return await hub.client.post(f'/v1/executions/by-key/{key}/client-connections', json={'client_key': client_key, 'generation': generation})


async def create_call(hub, refs, attempt):
    return await hub.store.client_tools.create_call('caller-a', attempt, 'client:fixture', 0, refs['tool'], {'value': 'hello'}, datetime.now(timezone.utc) + timedelta(seconds=30))


async def delivery(hub, key, generation=0):
    return await hub.client.get(f'/v1/executions/by-key/{key}/client-calls', params={'client_key': 'client:fixture', 'generation': generation})


def result_body(call, nonce, **extra):
    return {'client_key': 'client:fixture', 'generation': 0, 'activation_nonce': nonce,
        'tool_ref': call['tool_ref'], 'arguments_digest': call['arguments_digest'],
        'disposition': 'applied', 'result': {'value': 'done'}, **extra}


@pytest.mark.asyncio
async def test_connection_binds_execution_and_current_activation(running_agent_execution_harness):
    hub = running_agent_execution_harness
    raw, _, key, attempt = await start_tool(hub)
    response = await connect(hub, key)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['schema_version'] == 'agent_execution_v1'
    assert body['connection']['activation_nonce'] == raw['execution']['initial_authorization']['activation_nonce']
    assert body['connection']['execution_id']
    assert attempt not in response.text and 'attempt_id' not in response.text
    assert (await connect(hub, key, client_key='other')).status_code == 403
    with pytest.raises(ValueError):
        AgentExecutionClientConnectionRequest.model_validate({'client_key': 'client', 'generation': 0, 'extra': True})


@pytest.mark.asyncio
async def test_calls_and_results_keep_operation_fencing(running_agent_execution_harness):
    hub = running_agent_execution_harness
    raw, refs, key, attempt = await start_tool(hub)
    await connect(hub, key)
    call = await create_call(hub, refs, attempt)
    listed = (await delivery(hub, key)).json()['calls'][0]
    nonce = raw['execution']['initial_authorization']['activation_nonce']
    assert listed['execution_id'] and listed['activation_nonce'] == nonce
    assert listed['arguments_digest'] == call['arguments_digest'] and listed['operation_id'] == call['operation_id']
    assert 'attempt_id' not in listed
    path = f'/v1/executions/by-key/{key}/client-calls/{call["id"]}/results'
    body = result_body(call, nonce)
    assert (await hub.client.post(path, json={**body, 'arguments_digest': 'b' * 64})).status_code == 409
    assert (await hub.client.post(path, json={**body, 'generation': 1})).status_code in (403, 409)
    first = await hub.client.post(path, json=body)
    assert first.status_code == 200, first.text
    assert (await hub.client.post(path, json=body)).json() == first.json()
    assert (await hub.client.post(path, json={**body, 'result': {'value': 'different'}})).status_code == 409


async def pause_for_approval(hub):
    hub.runtime.script = 'approval'
    raw = hub.create_body(await hub.register_work(approval=True))
    response = await hub.client.post('/v1/executions', json=raw)
    assert response.status_code == 202, response.text
    key = raw['execution']['idempotency_key']
    snapshot = await wait_snapshot(hub, key, lambda s: s['pause'] is not None)
    return raw, key, snapshot


def approval_allow(hub, raw, snap, decision='approved'):
    approval = {'call_id': snap['pause']['call_id'], 'subject_digest': snap['pause']['subject_digest'],
        'decision': decision, 'decision_ref': 'decision:1'}
    return hub.allow(raw, revision=2, activation_nonce=snap['activation']['nonce'], approval=approval)


@pytest.mark.asyncio
async def test_approval_required_pauses_without_agent_time(running_agent_execution_harness):
    hub = running_agent_execution_harness
    _, key, snap = await pause_for_approval(hub)
    assert snap['state'] == 'queued' and snap['stop_confirmation'] == 'confirmed'
    assert snap['side_effect_resolution'] == 'resolved' and snap['activation']['reason'] == 'approval_continue'
    assert snap['activation']['authority_required'] and snap['receipts'][0]['outcome'] == 'executed'
    await asyncio.sleep(.15)
    again = (await hub.client.get(f'/v1/executions/by-key/{key}')).json()
    assert again['usage']['active_ms'] == snap['usage']['active_ms']
    assert await hub.store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_client_calls WHERE state IN ('pending','sent')") == 0


@pytest.mark.asyncio
async def test_poll_and_plain_allow_do_not_approve(running_agent_execution_harness):
    hub = running_agent_execution_harness
    raw, key, snap = await pause_for_approval(hub)
    path = f'/v1/executions/by-key/{key}/authorization'
    allow = approval_allow(hub, raw, snap)
    for approval in (None, {**allow['approval'], 'call_id': 'wrong'}, {**allow['approval'], 'subject_digest': 'b' * 64}):
        response = await hub.client.put(path, json={**allow, 'approval': approval})
        assert response.status_code == 409, response.text
    assert (await hub.client.get(f'/v1/executions/by-key/{key}')).json()['pause'] == snap['pause']
    assert len(hub.runtime.jobs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('decision,retained', [('approved', True), ('denied', True), ('approved', False)])
async def test_approval_continue_opens_new_charge_unit(running_agent_execution_harness, decision, retained):
    hub = running_agent_execution_harness
    raw, key, snap = await pause_for_approval(hub)
    hub.runtime.retained = retained
    allow = approval_allow(hub, raw, snap, decision)
    response = await hub.client.put(f'/v1/executions/by-key/{key}/authorization', json=allow)
    assert response.status_code == 200, response.text
    final = await wait_snapshot(hub, key, lambda s: s['state'] == 'terminal')
    assert len(final['receipts']) == 2
    if retained:
        assert final['terminal']['kind'] == 'succeeded'
        assert hub.runtime.jobs[1].provider_session_ref == hub.runtime.session_ref
        assert 'approval_decision' in hub.runtime.jobs[1].prompt and decision in hub.runtime.jobs[1].prompt
        assert final['receipts'][1]['outcome'] == 'executed'
    else:
        assert final['terminal']['kind'] == 'resume_unavailable' and final['terminal']['cause'] == 'not_retained'
        assert len(hub.runtime.jobs) == 1 and final['receipts'][1]['outcome'] == 'not_started_closed'


async def stopped_with_unknown(hub, *, stop='confirmed'):
    raw, refs, key, attempt = await start_tool(hub)
    await connect(hub, key)
    call = await create_call(hub, refs, attempt)
    assert len((await delivery(hub, key)).json()['calls']) == 1
    hub.runtime.stop_confirmation = stop
    execution_id = (await hub.client.get(f'/v1/executions/by-key/{key}')).json()['execution_id']
    hub.agent_executions.runner.interrupt(execution_id, 'lease_expired')
    async def wait_finished():
        while True:
            current = await hub.store.attempt('caller-a', attempt)
            if current['finished_at'] is not None:
                return current
            await asyncio.sleep(.02)
    current = await asyncio.wait_for(wait_finished(), 3)
    await asyncio.sleep(.1)
    return raw, refs, key, attempt, call, current


async def reconcile_call(hub, key, call, nonce, **changes):
    path = f'/v1/executions/by-key/{key}/client-calls/{call["id"]}/reconciliation'
    return await hub.client.post(path, json=result_body(call, nonce, evidence_ref='evidence:1', **changes))


@pytest.mark.asyncio
async def test_client_reconciliation_never_confirms_process_stop(running_agent_execution_harness):
    hub = running_agent_execution_harness
    raw, _, key, attempt, call, current = await stopped_with_unknown(hub, stop='unconfirmed')
    nonce = raw['execution']['initial_authorization']['activation_nonce']
    assert (await connect(hub, key, 1)).status_code == 200
    response = await reconcile_call(hub, key, call, nonce, generation=1)
    assert response.status_code == 200, response.text
    current = await hub.store.attempt('caller-a', attempt)
    assert current['stop_confirmation'] == 'unconfirmed' and current['side_effect_resolution'] == 'unresolved'
    snap = (await hub.client.get(f'/v1/executions/by-key/{key}')).json()
    assert snap['stop_confirmation'] == 'unconfirmed' and snap['state'] != 'terminal' and not snap['receipts']
    await hub.store.confirm_stop(attempt)
    await hub.agent_executions.runner.settle(current)
    snap = await wait_snapshot(hub, key, lambda s: s['state'] == 'resuming')
    assert snap['stop_confirmation'] == 'confirmed' and snap['side_effect_resolution'] == 'resolved'


async def resume_after_reconcile(hub, disposition='applied'):
    raw, refs, key, attempt, call, _ = await stopped_with_unknown(hub)
    nonce = raw['execution']['initial_authorization']['activation_nonce']
    # Reconciliation can commit before the runner records the unknown call.
    await hub.store.pool.execute('DELETE FROM kotoyose.agent_execution_client_replays WHERE call_id=$1', call['id'])
    response = await reconcile_call(hub, key, call, nonce, disposition=disposition)
    assert response.status_code == 200, response.text
    assert await hub.store.pool.fetchval('SELECT count(*) FROM kotoyose.agent_execution_client_replays WHERE call_id=$1', call['id']) == 1
    snap = await wait_snapshot(hub, key, lambda s: s['state'] == 'resuming')
    hub.runtime.script = 'wait'
    hub.runtime.started.clear()
    allow = {**raw['execution']['initial_authorization'], 'revision': 2, 'activation_nonce': snap['activation']['nonce']}
    result = await hub.client.put(f'/v1/executions/by-key/{key}/authorization', json=allow)
    assert result.status_code == 200, result.text
    await asyncio.wait_for(hub.runtime.started.wait(), 3)
    new_attempt = await hub.store.pool.fetchval('SELECT attempt_id FROM kotoyose.agent_execution_activations WHERE nonce=$1', snap['activation']['nonce'])
    return raw, refs, key, attempt, call, snap, new_attempt


@pytest.mark.asyncio
async def test_old_activation_cannot_receive_new_tool_calls(running_agent_execution_harness):
    hub = running_agent_execution_harness
    raw, _, key, old, call, snap, _ = await resume_after_reconcile(hub)
    assert (await connect(hub, key, 0)).status_code == 409
    assert (await connect(hub, key, 2)).status_code == 200
    assert (await connect(hub, key, 1)).status_code == 409
    nonce = raw['execution']['initial_authorization']['activation_nonce']
    path = f'/v1/executions/by-key/{key}/client-calls/{call["id"]}/results'
    stale = await hub.client.post(path, json=result_body(call, nonce, generation=2))
    assert stale.status_code == 409 and stale.json()['error']['code'] == 'stale_activation'
    assert (await delivery(hub, key, 2)).json()['calls'] == []
    assert (await reconcile_call(hub, key, call, nonce, generation=2)).status_code == 200
    assert hub.agent_executions.runner._running[snap['execution_id']][0][1] == snap['activation']['nonce']
    assert old != await hub.store.pool.fetchval('SELECT attempt_id FROM kotoyose.agent_execution_activations WHERE nonce=$1', snap['activation']['nonce'])


@pytest.mark.asyncio
@pytest.mark.parametrize('disposition', ['applied', 'not_applied'])
async def test_reconciled_call_is_not_redelivered_after_resume(running_agent_execution_harness, disposition):
    hub = running_agent_execution_harness
    _, refs, key, _, call, _, attempt = await resume_after_reconcile(hub, disposition)
    if disposition == 'applied':
        outcome = await hub.store.client_tools.call('caller-a', attempt, refs['tool'], {'value': 'hello'}, datetime.now(timezone.utc) + timedelta(seconds=30))
        assert outcome.state == 'succeeded' and outcome.result == {'value': 'done'} and outcome.call_id == call['id']
        assert (await connect(hub, key, 1)).status_code == 200
        assert (await delivery(hub, key, 1)).json()['calls'] == []
        assert await hub.store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_client_calls") == 1
    else:
        assert (await connect(hub, key, 1)).status_code == 200
    operation = asyncio.create_task(hub.store.client_tools.call('caller-a', attempt, refs['tool'], {'value': 'hello'}, datetime.now(timezone.utc) + timedelta(seconds=30)))
    try:
        async def pending():
            while True:
                calls = (await delivery(hub, key, 1)).json()['calls']
                if calls:
                    return calls
                await asyncio.sleep(.02)
        assert len(await asyncio.wait_for(pending(), 3)) == 1
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


def unresolved_path(key):
    return f'/v1/executions/by-key/{key}/client-calls/unresolved'


CALL_WIRE_FIELDS = {'id', 'execution_id', 'activation_nonce', 'client_key', 'generation', 'tool_ref', 'arguments',
                    'arguments_digest', 'state', 'deadline', 'created_at', 'operation_id', 'result'}


@pytest.mark.asyncio
async def test_delivered_call_uses_the_wire_date_format(running_agent_execution_harness):
    hub = running_agent_execution_harness
    raw, refs, key, attempt = await start_tool(hub)
    await connect(hub, key)
    await create_call(hub, refs, attempt)
    listed = (await delivery(hub, key)).json()['calls'][0]
    assert set(listed) == CALL_WIRE_FIELDS
    assert listed['deadline'].endswith('Z') and listed['created_at'].endswith('Z')


@pytest.mark.asyncio
async def test_unresolved_lists_unknown_calls_without_changing_state(running_agent_execution_harness):
    hub = running_agent_execution_harness
    raw, _, key, attempt, call, _ = await stopped_with_unknown(hub)
    nonce = raw['execution']['initial_authorization']['activation_nonce']
    sql = 'SELECT * FROM kotoyose.execution_client_calls WHERE id=$1'
    row_before = dict(await hub.store.pool.fetchrow(sql, call['id']))
    attempt_before = await hub.store.attempt('caller-a', attempt)
    response = await hub.client.get(unresolved_path(key))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['schema_version'] == 'agent_execution_v1' and len(body['calls']) == 1
    listed = body['calls'][0]
    assert listed['id'] == call['id'] and listed['state'] == 'unknown' and listed['activation_nonce'] == nonce
    assert listed['arguments_digest'] == call['arguments_digest'] and listed['tool_ref'] == call['tool_ref']
    assert 'attempt_id' not in listed
    assert set(listed) == CALL_WIRE_FIELDS and isinstance(listed['arguments'], dict)
    assert listed['deadline'].endswith('Z') and listed['created_at'].endswith('Z')
    assert (await hub.client.get(unresolved_path(key))).json() == body
    assert dict(await hub.store.pool.fetchrow(sql, call['id'])) == row_before
    assert await hub.store.attempt('caller-a', attempt) == attempt_before


@pytest.mark.asyncio
async def test_unresolved_is_empty_after_reconciliation(running_agent_execution_harness):
    hub = running_agent_execution_harness
    raw, _, key, _, call, _ = await stopped_with_unknown(hub)
    nonce = raw['execution']['initial_authorization']['activation_nonce']
    assert len((await hub.client.get(unresolved_path(key))).json()['calls']) == 1
    assert (await reconcile_call(hub, key, call, nonce)).status_code == 200
    assert (await hub.client.get(unresolved_path(key))).json() == {'schema_version': 'agent_execution_v1', 'calls': []}


@pytest.mark.asyncio
async def test_unresolved_returns_the_calls_own_activation_nonce(running_agent_execution_harness):
    # 古い activation の unknown call を安価に作れないため、別 activation の行へ状態を直接書いている。
    hub = running_agent_execution_harness
    raw, _, key, attempt, call, _ = await stopped_with_unknown(hub)
    old = raw['execution']['initial_authorization']['activation_nonce']
    await hub.store.pool.execute("UPDATE kotoyose.execution_client_calls SET state='unknown' WHERE id=$1", call['id'])
    execution_id = await hub.store.pool.fetchval('SELECT execution_id FROM kotoyose.agent_execution_activations WHERE nonce=$1', old)
    await hub.store.pool.execute('UPDATE kotoyose.agent_executions SET current_nonce=$2 WHERE id=$1', execution_id, 'newer-nonce')
    await hub.store.pool.execute('''INSERT INTO kotoyose.agent_execution_activations (SELECT (jsonb_populate_record(NULL::kotoyose.agent_execution_activations,
        to_jsonb(a) || jsonb_build_object('nonce','newer-nonce','ordinal',a.ordinal+1,'attempt_id',NULL))).* FROM kotoyose.agent_execution_activations a WHERE a.nonce=$1)''', old)
    response = await hub.client.get(unresolved_path(key))
    assert response.status_code == 200, response.text
    assert [c['activation_nonce'] for c in response.json()['calls']] == [old]


@pytest.mark.asyncio
async def test_unresolved_unknown_key_is_404(running_agent_execution_harness):
    assert (await running_agent_execution_harness.client.get(unresolved_path('no-such-key'))).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [0, 101, -1])
async def test_unresolved_rejects_limit_out_of_range(running_agent_execution_harness, limit):
    hub = running_agent_execution_harness
    _, _, key, _ = await start_tool(hub)
    assert (await hub.client.get(unresolved_path(key), params={'limit': limit})).status_code == 422
