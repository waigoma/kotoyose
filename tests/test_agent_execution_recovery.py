import asyncio
from unittest.mock import AsyncMock

import pytest

from tests.test_agent_execution_runner import running_agent_execution_harness, wait_snapshot  # noqa: F401


async def create(hub):
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    response = await hub.client.post('/v1/executions', json=raw)
    assert response.status_code == 202, response.text
    return raw, raw['execution']['idempotency_key']


def cancel_body():
    return {'action_key': 'cancel-1', 'reason': 'owner_abort'}


@pytest.mark.asyncio
async def test_wait_survives_restart(running_agent_execution_harness):
    hub = running_agent_execution_harness
    hub.runtime.script = 'provider'
    _, key = await create(hub)
    before = await wait_snapshot(hub, key, lambda s: s['state'] == 'waiting_provider')
    step = await hub.store.pool.fetchval('SELECT probe_step FROM kotoyose.agent_executions')
    await hub.crash()
    await hub.restart()
    after = (await hub.client.get('/v1/executions/by-key/' + key)).json()
    assert (after['next_probe_at'], after['activation']['nonce'], after['fault_retries_used']) == (before['next_probe_at'], before['activation']['nonce'], before['fault_retries_used'])
    assert await hub.store.pool.fetchval('SELECT probe_step FROM kotoyose.agent_executions') == step
    assert await hub.store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_attempts') == 1


@pytest.mark.asyncio
async def test_running_activation_restart_does_not_retry(running_agent_execution_harness):
    hub = running_agent_execution_harness
    _, key = await create(hub)
    await asyncio.wait_for(hub.runtime.started.wait(), 3)
    before = await wait_snapshot(hub, key, lambda s: s['usage']['state'] == 'running')
    await hub.crash()
    await hub.restart()
    final = await wait_snapshot(hub, key, lambda s: s['state'] == 'terminal')
    assert final['terminal'] == {'kind': 'execution_fault', 'cause': 'worker_lost', 'code': 'coordinator_restarted'}
    assert not final['usage']['complete'] and final['usage']['active_ms'] >= before['usage']['active_ms']
    assert final['fault_retries_used'] == 0
    assert final['receipts'][0]['outcome'] == 'executed' and final['receipts'][0]['execution_confirmed']
    assert await hub.store.pool.fetchval('SELECT count(*) FROM kotoyose.agent_execution_activations') == 1


@pytest.mark.asyncio
async def test_retry_record_survives_restart(running_agent_execution_harness):
    hub = running_agent_execution_harness
    hub.runtime.script = 'fault'
    _, key = await create(hub)
    before = await wait_snapshot(hub, key, lambda s: s['state'] == 'retrying')
    await hub.crash()
    await hub.restart()
    after = (await hub.client.get('/v1/executions/by-key/' + key)).json()
    assert after['fault_retries_used'] == 1 and after['activation']['nonce'] == before['activation']['nonce']
    assert after['activation']['authority_required']
    assert await hub.store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_attempts') == 1


@pytest.mark.asyncio
async def test_graceful_shutdown_keeps_the_unit(running_agent_execution_harness):
    hub = running_agent_execution_harness
    raw, key = await create(hub)
    await asyncio.wait_for(hub.runtime.started.wait(), 3)
    await hub.service.close()
    await hub.restart()
    resumed = (await hub.client.get('/v1/executions/by-key/' + key)).json()
    assert resumed['state'] == 'resuming' and resumed['activation']['reason'] == 'lease_resume'
    assert resumed['activation']['charge_ref'] == raw['execution']['initial_authorization']['charge']['charge_ref']
    assert resumed['activation']['authority_required'] and resumed['fault_retries_used'] == 0
    assert resumed['receipts'] == []


@pytest.mark.asyncio
async def test_cancel_before_admission_leaves_tombstone(running_agent_execution_harness):
    hub = running_agent_execution_harness
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    key = raw['execution']['idempotency_key']
    path = '/v1/executions/by-key/' + key
    first = await hub.client.post(path + '/cancel', json=cancel_body())
    assert first.status_code == 200 and first.json()['outcome'] == 'not_admitted'
    assert (await hub.client.post('/v1/executions/by-key/' + 'x' * 257 + '/cancel', json=cancel_body())).status_code == 422
    assert first.json()['admission_closed'] and first.json()['execution_id'] is None
    assert (await hub.client.post(path + '/cancel', json=cancel_body())).json() == first.json()
    response = await hub.client.post('/v1/executions', json=raw)
    assert response.status_code == 409 and response.json()['error']['code'] == 'request_conflict' and response.json()['error']['current'] is None
    assert (await hub.client.get(path)).status_code == 404 and not hub.runtime.jobs


@pytest.mark.asyncio
async def test_cancel_closes_all_nonces_and_wins(running_agent_execution_harness):
    hub = running_agent_execution_harness
    hub.agent_executions.runner.kick = AsyncMock()
    raw, key = await create(hub)
    path = '/v1/executions/by-key/' + key
    result = await hub.client.post(path + '/cancel', json=cancel_body())
    assert result.status_code == 202
    final = result.json()
    assert final['terminal'] == {'kind': 'cancelled'} and final['admission_closed']
    assert final['receipts'][0]['outcome'] == 'not_started_closed'
    assert (await hub.client.put(path + '/authorization', json=raw['execution']['initial_authorization'])).status_code == 409
    assert (await hub.client.post(path + '/try-now', json={'action_key': 'wake', 'expected_revision': final['state_revision']})).status_code == 409
    assert (await hub.client.post(path + '/cancel', json={'action_key': 'cancel-1', 'reason': 'task_cancelled'})).status_code == 409


@pytest.mark.asyncio
async def test_cancel_completes_without_complete_usage(running_agent_execution_harness, monkeypatch):
    hub = running_agent_execution_harness
    _, key = await create(hub)
    await asyncio.wait_for(hub.runtime.started.wait(), 3)
    monkeypatch.setattr(hub.service, '_stop_attempt', AsyncMock(return_value='unconfirmed'))
    hub.runtime.stop_confirmation = 'unconfirmed'
    result = await hub.client.post('/v1/executions/by-key/' + key + '/cancel', json=cancel_body())
    assert result.status_code == 202
    await wait_snapshot(hub, key, lambda s: s['cancel_requested'])
    await hub.crash()
    # The next coordinator cannot yet confirm physical stop.
    from kotoyose.execution_service import ExecutionService
    monkeypatch.setattr(ExecutionService, '_stop_attempt', AsyncMock(return_value='unconfirmed'))
    await hub.restart()
    pending = (await hub.client.get('/v1/executions/by-key/' + key)).json()
    assert pending['terminal'] is None and pending['cancel_requested'] and pending['stop_confirmation'] == 'unconfirmed'
    monkeypatch.setattr(hub.service, '_stop_attempt', AsyncMock(return_value='confirmed'))
    await hub.agent_executions.recovery.reconcile_pass(hub.agent_executions.clock())
    final = (await hub.client.get('/v1/executions/by-key/' + key)).json()
    assert final['terminal'] == {'kind': 'cancelled'} and not final['usage']['complete']
    assert final['stop_confirmation'] == 'confirmed' and final['side_effect_resolution'] == 'resolved' and final['admission_closed']


@pytest.mark.asyncio
async def test_unconfirmed_stop_is_settled_after_restart(running_agent_execution_harness):
    # settle が停止の未確認で途中で返った後に再起動すると、起動の照合が停止を確かめる。
    # 回復はその activation の settle をやり直し、execution を終端まで進める。
    hub = running_agent_execution_harness
    hub.runtime.script = 'success'
    hub.runtime.stop_confirmation = 'unconfirmed'
    _, key = await create(hub)
    pending = await wait_snapshot(hub, key, lambda s: s['usage']['state'] == 'unknown')
    assert pending['terminal'] is None and pending['workspace_ref'] is None
    await hub.crash()
    await hub.restart()
    final = await wait_snapshot(hub, key, lambda s: s['state'] == 'terminal')
    assert final['stop_confirmation'] == 'confirmed' and final['workspace_ref'] is not None
    generation = await hub.store.pool.fetchval('SELECT workspace_generation FROM kotoyose.agent_executions')
    await hub.agent_executions.recovery.reconcile_pass(hub.agent_executions.clock())
    assert await hub.store.pool.fetchval('SELECT workspace_generation FROM kotoyose.agent_executions') == generation


@pytest.mark.asyncio
async def test_recovery_settles_a_confirmed_stop_after_the_last_reconciliation(running_agent_execution_harness):
    # 最後の照合が call を保存した直後に落ちると settle は走らない。回復がそれを拾う。
    from tests.test_agent_execution_client_tools import stopped_with_unknown
    hub = running_agent_execution_harness
    raw, _, key, attempt, call, _ = await stopped_with_unknown(hub)
    nonce = raw['execution']['initial_authorization']['activation_nonce']
    execution_id = await hub.store.pool.fetchval('SELECT id FROM kotoyose.agent_executions')
    await hub.store.client_tools.save_result('caller-a', attempt, call['id'], 'client:fixture', 0, {'value': 'done'},
        tool_ref=call['tool_ref'], arguments_digest=call['arguments_digest'], disposition='applied',
        reconciliation_ref='evidence:1', agent_execution_context=(execution_id, nonce, attempt, True))
    before = await hub.store.attempt('caller-a', attempt)
    assert before['side_effect_resolution'] == 'unresolved'
    generation = await hub.store.pool.fetchval('SELECT workspace_generation FROM kotoyose.agent_executions')
    assert await hub.agent_executions.recovery.reconcile_pass(hub.agent_executions.clock()) == 1
    after = await hub.store.attempt('caller-a', attempt)
    assert after['side_effect_resolution'] == 'resolved'
    assert await hub.store.pool.fetchval('SELECT workspace_generation FROM kotoyose.agent_executions') > generation


@pytest.mark.asyncio
async def test_recovery_leaves_an_unresolved_call_alone(running_agent_execution_harness):
    from tests.test_agent_execution_client_tools import stopped_with_unknown
    hub = running_agent_execution_harness
    _, _, _, attempt, call, _ = await stopped_with_unknown(hub)
    before = await hub.store.attempt('caller-a', attempt)
    generation = await hub.store.pool.fetchval('SELECT workspace_generation FROM kotoyose.agent_executions')
    assert await hub.agent_executions.recovery.reconcile_pass(hub.agent_executions.clock()) == 0
    assert await hub.store.attempt('caller-a', attempt) == before
    assert await hub.store.pool.fetchval('SELECT workspace_generation FROM kotoyose.agent_executions') == generation
    assert await hub.store.pool.fetchval('SELECT state FROM kotoyose.execution_client_calls WHERE id=$1', call['id']) == 'unknown'


@pytest.mark.asyncio
async def test_egress_only_stop_left_by_a_previous_coordinator_settles_after_restart(running_agent_execution_harness, monkeypatch):
    # 本番の Run 403 と同じ形: lease 失効で取消された coding attempt の停止証拠が egress だけ未確認、
    # activation は ended、execution は running のまま。再起動後の通常の照合だけで終端まで進む。
    from kotoyose import codex_egress_owner
    from kotoyose.codex_stop_evidence import STAGES, StopComponents, make_stop_evidence, stop_evidence_context
    from kotoyose.process_identity import capture_process_identity
    import json
    import os
    hub = running_agent_execution_harness
    hub.runtime.script = 'success'
    hub.runtime.stop_confirmation = 'unconfirmed'
    _, key = await create(hub)
    pending = await wait_snapshot(hub, key, lambda s: s['usage']['state'] == 'unknown')
    assert pending['state'] == 'running' and pending['terminal'] is None
    attempt_id = await hub.store.pool.fetchval('SELECT attempt_id FROM kotoyose.agent_execution_activations')
    attempt = await hub.store.attempt('caller-a', attempt_id)
    workspace = await hub.store.pool.fetchrow('SELECT device,inode FROM kotoyose.execution_workspaces WHERE id=$1', attempt['workspace_id'])
    snapshot = {**attempt['snapshot'], 'kind': 'agent', 'backend': 'native', 'provider': 'codex',
                'codex_execution_mode': 'host_native_coding_v1',
                'workspace_device': workspace['device'], 'workspace_inode': workspace['inode']}
    await hub.store.pool.execute('''UPDATE kotoyose.execution_attempts SET snapshot=$2::jsonb,status='cancelled',
        reason='cancelled',limits_state='unknown',limits_finished_at=NULL WHERE id=$1''', attempt_id, json.dumps(snapshot))
    attempt = await hub.store.attempt('caller-a', attempt_id)
    me = capture_process_identity(os.getpid())
    ownership = {'pid': 11, 'start_ticks': 12, 'boot_id': me.boot_id, 'uid': me.uid,
                 'codex_provider_scope': {'unit': f"kotoyose-native-{attempt_id}-provider-{attempt['generation']}.scope",
                     'control_group': '/gone-scope', 'device': 31, 'inode': 32,
                     'namespace_init': {'pid': 21, 'start_ticks': 22, 'boot_id': me.boot_id, 'uid': me.uid}}}
    # 実行中に記録された provider の identity を、host coding の所有 identity に置き換える。
    operation = await hub.store.pool.fetchval("SELECT id FROM kotoyose.execution_operations "
                                              "WHERE attempt_id=$1 AND kind='provider_process'", attempt_id)
    await hub.store.pool.execute('UPDATE kotoyose.execution_operations SET result=$2::jsonb WHERE id=$1',
                                 operation, json.dumps(ownership))
    source = make_stop_evidence(stop_evidence_context(attempt), ownership,
        StopComponents(**{**dict.fromkeys(STAGES, 'confirmed'), 'egress': 'unconfirmed'}), 0)
    await hub.store.record_codex_stop(operation, source.model_dump())
    await hub.store.resolve_operation(operation, 'unconfirmed')
    await hub.crash()
    await hub.restart()
    # 前の coordinator が期限内に終わるまでは確定しない。
    await hub.agent_executions.recovery.reconcile_pass(hub.agent_executions.clock())
    still = (await hub.client.get('/v1/executions/by-key/' + key)).json()
    assert still['state'] == 'running' and still['stop_confirmation'] == 'unconfirmed'
    monkeypatch.setattr(codex_egress_owner, 'PREDECESSOR_EXIT_BOUND_SECONDS', 0)
    final = await wait_snapshot(hub, key, lambda s: s['state'] == 'terminal')
    assert final['terminal'] == {'kind': 'execution_fault', 'cause': 'unknown_failure', 'code': 'cancelled'}
    assert final['stop_confirmation'] == 'confirmed' and final['side_effect_resolution'] == 'resolved'
    assert final['admission_closed'] and final['workspace_ref'] is not None
    saved = next(x for x in await hub.store.operations(attempt_id) if x['id'] == operation)
    assert saved['result']['codex_egress_stop_evidence']['basis'] == 'previous_coordinator'
    assert saved['result']['codex_stop_evidence'] == source.model_dump()
