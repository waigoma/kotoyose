from dataclasses import replace

import pytest

from kotoyose.agent_execution_faults import classify_fault
from kotoyose.agent_execution_output import check_output, validate_result_schema
from kotoyose.agent_execution_contract import AgentExecutionError
from kotoyose.agent_execution_units import ActivationFacts


@pytest.mark.parametrize('launch,status,reason,cause,retryable', [
    ('not_started', 'failed', 'sdk_error', 'launch_failure', True),
    ('not_started', 'failed', 'authentication_failed', 'launch_failure', False),
    ('started', 'worker_lost', 'lost', 'worker_lost', True),
    ('started', 'failed', 'output_schema_mismatch', 'output_invalid', True),
    ('started', 'failed', 'sdk_error', 'agent_failure', True),
    ('started', 'failed', 'other', 'unknown_failure', False),
])
def test_fault_allowlist(launch, status, reason, cause, retryable):
    facts = ActivationFacts('n', launch, 'complete', 0, 0, 0, False, 'confirmed', 'resolved')
    result = classify_fault({'status': status, 'reason': reason}, facts)
    assert (result.cause, result.code, result.retryable) == (cause, reason, retryable)


def test_json_schema_local_only():
    schema = {'$defs': {'x': {'type': 'integer'}}, '$ref': '#/$defs/x'}
    validate_result_schema(schema)
    assert check_output({'format': 'json', 'json_schema': schema}, b'3') is None
    assert check_output({'format': 'json', 'json_schema': schema}, b'"wrong"') == 'output_schema_mismatch'
    for invalid in ({'$ref': 'https://example.test/schema'}, {'$schema': 'http://json-schema.org/draft-07/schema#'}, {'type': 'wrong'}):
        with pytest.raises(AgentExecutionError):
            validate_result_schema(invalid)


from kotoyose.agent_execution_digest import agent_work_digest
from tests.test_agent_execution_runner import running_agent_execution_harness, wait_snapshot  # noqa: F401


async def _authorize_retry(hub, raw, snapshot):
    allow = hub.allow(raw, revision=2, activation_nonce=snapshot['activation']['nonce'],
                      settled_receipt_cursor=snapshot['final_receipt_cursor'])
    result = await hub.client.put('/v1/executions/by-key/' + raw['execution']['idempotency_key'] + '/authorization', json=allow)
    assert result.status_code == 200, result.text


@pytest.mark.asyncio
@pytest.mark.parametrize('second_fault', [False, True])
async def test_fault_retry_is_durable_and_limited(running_agent_execution_harness, second_fault):
    hub = running_agent_execution_harness
    hub.runtime.script = 'fault'
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    key = raw['execution']['idempotency_key']
    assert (await hub.client.post('/v1/executions', json=raw)).status_code == 202
    retry = await wait_snapshot(hub, key, lambda s: s['state'] == 'retrying')
    assert retry['fault_retries_used'] == 1
    assert retry['activation']['reason'] == 'fault_retry'
    assert retry['activation']['authority_required']
    assert len(hub.runtime.jobs) == 1
    assert retry['receipts'][0]['outcome'] == 'executed'
    if not second_fault:
        hub.runtime.script = 'success'
    await _authorize_retry(hub, raw, retry)
    final = await wait_snapshot(hub, key, lambda s: s['state'] == 'terminal')
    assert final['terminal']['kind'] == ('execution_fault' if second_fault else 'succeeded')
    assert final['fault_retries_used'] == 1
    assert [r['outcome'] for r in final['receipts']] == ['executed', 'executed']
    assert len(hub.runtime.jobs) == 2
    assert hub.runtime.jobs[1].provider_session_ref is None


async def _successor(hub, refs, first, predecessor, *, change=None):
    grant = await hub.store.resource('caller-a', refs['grant'])
    run_ref = 'successor-run'
    grant = grant.model_copy(update={'grant_id': 'successor-grant', 'caller_run_ref': run_ref,
                                     'workspace_ref': first['workspace_ref']})
    grant_ref = await hub.store.put_resource('caller-a', grant)
    raw = hub.create_body(refs)
    body = raw['execution']
    body['caller_run_ref'] = run_ref
    body['work'].update(workspace_ref=first['workspace_ref'], scope_grant_ref=grant_ref,
                        continuation_mode='safe_resume')
    body['predecessor'] = {'idempotency_key': predecessor['idempotency_key'],
                           'execution_id': predecessor['execution_id'], 'relation': 'condition_change'}
    if change:
        change(body)
    body['work_digest'] = agent_work_digest(raw)
    body['initial_authorization'] = hub.allow(raw)
    return raw


@pytest.mark.asyncio
@pytest.mark.parametrize('retained', [True, False])
async def test_safe_resume_predecessor_session(running_agent_execution_harness, retained):
    hub = running_agent_execution_harness
    hub.runtime.script = 'success'
    refs = await hub.register_work(tool_refs=[])
    raw = hub.create_body(refs)
    ack = await hub.client.post('/v1/executions', json=raw)
    assert ack.status_code == 202, ack.text
    first = await wait_snapshot(hub, raw['execution']['idempotency_key'], lambda s: s['state'] == 'terminal')
    hub.runtime.retained = retained
    successor = await _successor(hub, refs, first, ack.json())
    response = await hub.client.post('/v1/executions', json=successor)
    assert response.status_code == 202, response.text
    result = await wait_snapshot(hub, successor['execution']['idempotency_key'], lambda s: s['state'] == 'terminal')
    if retained:
        assert result['terminal']['kind'] == 'succeeded'
        assert hub.runtime.jobs[-1].provider_session_ref == hub.runtime.session_ref
    else:
        assert result['terminal']['kind'] == 'resume_unavailable'
        assert result['terminal']['cause'] == 'not_retained'
        assert result['receipts'][0]['outcome'] == 'not_started_closed'
        assert len(hub.runtime.jobs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', ['key', 'principal', 'state', 'stop', 'effects', 'workspace', 'runtime', 'authentication', 'no_session'])
async def test_safe_resume_rejects_predecessor_mismatch(running_agent_execution_harness, mismatch):
    hub = running_agent_execution_harness
    hub.runtime.script = 'success'
    refs = await hub.register_work(tool_refs=[])
    raw = hub.create_body(refs)
    ack = await hub.client.post('/v1/executions', json=raw)
    first = await wait_snapshot(hub, raw['execution']['idempotency_key'], lambda s: s['state'] == 'terminal')
    if mismatch == 'principal':
        await hub.store.pool.execute("UPDATE kotoyose.agent_executions SET principal='caller-b' WHERE id=$1::uuid", ack.json()['execution_id'])
    elif mismatch == 'state':
        await hub.store.pool.execute("UPDATE kotoyose.agent_executions SET state='running' WHERE id=$1::uuid", ack.json()['execution_id'])
    elif mismatch in ('stop', 'effects'):
        column, value = ('stop_confirmation', 'unconfirmed') if mismatch == 'stop' else ('side_effect_resolution', 'unresolved')
        await hub.store.pool.execute(f"UPDATE kotoyose.execution_attempts SET {column}=$1 WHERE id=(SELECT attempt_id FROM kotoyose.agent_execution_activations WHERE execution_id=$2::uuid)", value, ack.json()['execution_id'])
    elif mismatch == 'no_session':
        await hub.store.pool.execute("UPDATE kotoyose.execution_attempts SET provider_session_ref=NULL WHERE id=(SELECT attempt_id FROM kotoyose.agent_execution_activations WHERE execution_id=$1::uuid)", ack.json()['execution_id'])
    elif mismatch == 'authentication':
        await hub.store.pool.execute("""UPDATE kotoyose.execution_attempts SET snapshot=jsonb_set(snapshot,'{expected_authentication_binding_digest}', '\"other\"')
            WHERE id=(SELECT attempt_id FROM kotoyose.agent_execution_activations WHERE execution_id=$1::uuid)""", ack.json()['execution_id'])
    elif mismatch == 'runtime':
        await hub.store.pool.execute("""UPDATE kotoyose.execution_attempts SET snapshot=jsonb_set(snapshot,'{model}', '"other"')
            WHERE id=(SELECT attempt_id FROM kotoyose.agent_execution_activations WHERE execution_id=$1::uuid)""", ack.json()['execution_id'])
    elif mismatch == 'workspace':
        refs = await hub.register_work(tool_refs=[])
        first['workspace_ref'] = refs['workspace']
    change = (lambda body: body['predecessor'].update(idempotency_key='wrong')) if mismatch == 'key' else None
    successor = await _successor(hub, refs, first, ack.json(), change=change)
    response = await hub.client.post('/v1/executions', json=successor)
    assert response.status_code == 202, response.text
    result = await wait_snapshot(hub, successor['execution']['idempotency_key'], lambda s: s['state'] == 'terminal')
    assert result['terminal']['cause'] == ('no_session' if mismatch == 'no_session' else 'predecessor_mismatch')
    assert result['receipts'][0]['outcome'] == 'not_started_closed'
    assert len(hub.runtime.jobs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('script,used,kind', [('fault', 1, 'execution_fault'), ('unknown', 0, 'execution_fault'), ('incomplete', 0, 'execution_fault'), ('turns', 0, 'cost_limit'), ('success', 0, 'succeeded')])
async def test_retry_gate_and_non_faults(running_agent_execution_harness, monkeypatch, script, used, kind):
    hub = running_agent_execution_harness
    hub.runtime.script = 'fault' if script == 'incomplete' else script
    if script == 'incomplete':
        execute = hub.runtime.execute
        async def incomplete(*args, **kwargs):
            return replace(await execute(*args, **kwargs), turns=None, output_bytes=None)
        monkeypatch.setattr(hub.runtime, 'execute', incomplete)
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    body = raw['execution']
    body['recovery']['fault_retries_used'] = used
    body['work_digest'] = agent_work_digest(raw)
    body['initial_authorization']['work_digest'] = body['work_digest']
    assert (await hub.client.post('/v1/executions', json=raw)).status_code == 202
    final = await wait_snapshot(hub, body['idempotency_key'], lambda s: s['state'] == 'terminal')
    assert final['terminal']['kind'] == kind
    assert final['fault_retries_used'] == used
    if script == 'unknown':
        assert not final['usage']['complete'] and final['receipts'] == []
    if script == 'incomplete':
        assert not final['usage']['complete']
        assert final['receipts'][0]['outcome'] == 'executed'
        assert final['receipts'][0]['execution_confirmed']
    assert len(hub.runtime.jobs) == 1


@pytest.mark.asyncio
async def test_output_schema_mismatch_retries_once(running_agent_execution_harness):
    hub = running_agent_execution_harness
    hub.runtime.script = 'success'
    refs = await hub.register_work(tool_refs=[])
    definition = await hub.store.resource('caller-a', refs['definition'])
    json_definition = definition.model_copy(update={'output_contract': 'json'})
    definition_ref = await hub.store.put_resource('caller-a', json_definition)
    grant = await hub.store.resource('caller-a', refs['grant'])
    grant = grant.model_copy(update={'grant_id': 'json-output-grant', 'resource_refs': [*grant.resource_refs, definition_ref]})
    refs['grant'] = await hub.store.put_resource('caller-a', grant)
    refs['definition'] = definition_ref
    raw = hub.create_body(refs)
    body = raw['execution']
    body['work']['result_contract'] = {'format': 'json', 'json_schema': {'type': 'object'}}
    body['work_digest'] = agent_work_digest(raw)
    body['initial_authorization']['work_digest'] = body['work_digest']
    response = await hub.client.post('/v1/executions', json=raw)
    assert response.status_code == 202, response.text
    retry = await wait_snapshot(hub, body['idempotency_key'], lambda s: s['state'] == 'retrying')
    await _authorize_retry(hub, raw, retry)
    final = await wait_snapshot(hub, body['idempotency_key'], lambda s: s['state'] == 'terminal')
    assert final['terminal'] == {'kind': 'execution_fault', 'cause': 'output_invalid', 'code': 'output_schema_mismatch'}
    assert len(hub.runtime.jobs) == 2 and final['fault_retries_used'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('schema', [{'$ref': 'https://example.test/schema'}, {'$schema': 'http://json-schema.org/draft-07/schema#'}, {'type': 'bad'}])
async def test_create_rejects_invalid_schema(running_agent_execution_harness, schema):
    hub = running_agent_execution_harness
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    body = raw['execution']
    body['work']['result_contract'] = {'format': 'json', 'json_schema': schema}
    body['work_digest'] = agent_work_digest(raw)
    body['initial_authorization']['work_digest'] = body['work_digest']
    response = await hub.client.post('/v1/executions', json=raw)
    assert response.status_code == 422
    assert await hub.store.pool.fetchval('SELECT count(*) FROM kotoyose.agent_executions') == 0
