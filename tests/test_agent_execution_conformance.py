"""End-to-end traces through the actual public execution router."""
from datetime import timedelta

import pytest

from kotoyose.agent_execution_digest import parse_agent_utc

from tests.fixtures.agent_execution_scenarios import SCENARIOS, run_scenario
from tests.test_agent_execution_routes import public_agent_execution_harness  # noqa: F401


@pytest.mark.asyncio
async def test_provider_resume_trace(public_agent_execution_harness):
    trace = await run_scenario(public_agent_execution_harness, SCENARIOS['provider_resume'])
    waiting, resuming, final = trace
    assert parse_agent_utc(waiting['snapshot']['next_probe_at']) == parse_agent_utc(waiting['time']) + timedelta(seconds=25)
    assert parse_agent_utc(resuming['time']) == parse_agent_utc(waiting['time']) + timedelta(seconds=26)
    assert resuming['activation']['reason'] == 'provider_resume'
    assert final['snapshot']['terminal'] == {'kind': 'succeeded'}
    assert final['attempts'] == 2
    assert [receipt['outcome'] for receipt in final['receipts']] == ['provider_zero_turn', 'executed']
    assert public_agent_execution_harness.runtime.jobs[-1].provider_session_ref == public_agent_execution_harness.runtime.session_ref
    assert waiting['snapshot']['caller_run_ref'] == final['snapshot']['caller_run_ref']


@pytest.mark.asyncio
async def test_fault_retry_trace(public_agent_execution_harness):
    retry, final = await run_scenario(public_agent_execution_harness, SCENARIOS['fault_retry'])
    assert retry['activation']['reason'] == 'fault_retry'
    assert final['snapshot']['fault_retries_used'] == 1
    assert final['snapshot']['terminal'] == {'kind': 'succeeded'}
    assert final['attempts'] == 2


@pytest.mark.asyncio
async def test_agent_exec_gap_keeps_activation(public_agent_execution_harness):
    first, renewed = await run_scenario(public_agent_execution_harness, SCENARIOS['agent_exec_gap'])
    assert first['activation']['nonce'] == renewed['activation']['nonce']
    assert first['activation']['charge_ref'] == renewed['activation']['charge_ref']
    assert first['attempts'] == renewed['attempts'] == 1


async def _create(hub, *, script='wait', approval=False):
    hub.runtime.script = script
    refs = await hub.register_work(approval=approval, **({} if approval else {'tool_refs': []}))
    raw = hub.create_body(refs)
    response = await hub.client.post('/v1/executions', json=raw)
    assert response.status_code == 202, response.text
    return raw, refs, response.json()


async def _snapshot(hub, raw, predicate):
    from tests.test_agent_execution_runner import wait_snapshot
    return await wait_snapshot(hub, raw['execution']['idempotency_key'], predicate)


def _path(raw):
    return '/v1/executions/by-key/' + raw['execution']['idempotency_key']


@pytest.mark.asyncio
async def test_ac01_create_replay_conflict_and_closed_key(public_agent_execution_harness):
    import asyncio
    from copy import deepcopy

    hub = public_agent_execution_harness
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    replies = await asyncio.gather(*(hub.client.post('/v1/executions', json=raw) for _ in range(2)))
    assert [r.status_code for r in replies] == [202, 202]
    assert replies[0].json() == replies[1].json()
    await asyncio.wait_for(hub.runtime.started.wait(), 3)
    changed = deepcopy(raw)
    changed['execution']['initial_authorization']['charge']['reservation_ref'] = 'different'
    assert (await hub.client.post('/v1/executions', json=changed)).status_code == 409
    assert await hub.store.pool.fetchval('SELECT count(*) FROM kotoyose.agent_executions') == 1
    assert await hub.store.pool.fetchval('SELECT count(*) FROM kotoyose.agent_execution_activations') == 1
    assert await hub.store.pool.fetchval('SELECT count(*) FROM kotoyose.agent_execution_charge_units') == 1
    assert len(hub.runtime.jobs) == 1
    closed = hub.create_body(await hub.register_work(tool_refs=[]))
    result = await hub.client.post(_path(closed) + '/cancel', json={'action_key': 'close', 'reason': 'owner_abort'})
    assert result.status_code == 200 and result.json()['outcome'] == 'not_admitted'
    late = await hub.client.post('/v1/executions', json=closed)
    assert late.status_code == 409 and late.json()['error']['current'] is None


@pytest.mark.asyncio
async def test_ac02_known_reset_and_ac06_zero_turn(public_agent_execution_harness):
    waiting, resuming, final = await run_scenario(public_agent_execution_harness, SCENARIOS['provider_resume'])
    assert parse_agent_utc(waiting['snapshot']['next_probe_at']) == parse_agent_utc(waiting['time']) + timedelta(seconds=25)
    assert waiting['usage']['active_ms'] == resuming['usage']['active_ms']
    assert waiting['receipts'][0]['outcome'] == 'provider_zero_turn'
    assert waiting['receipts'][0]['units'] == '0'
    assert final['snapshot']['terminal'] == {'kind': 'succeeded'}
    assert len(final['receipts']) == 2 and final['receipts'][1]['outcome'] == 'executed'
    assert final['receipts'][1]['units'] == final['receipts'][1]['allocation_units']
    assert len({r['charge_ref'] for r in final['receipts']}) == 2
    assert public_agent_execution_harness.runtime.jobs[-1].provider_session_ref == public_agent_execution_harness.runtime.session_ref
    assert waiting['snapshot']['caller_run_ref'] == final['snapshot']['caller_run_ref']


@pytest.mark.asyncio
async def test_ac03_unknown_reset_try_now_and_ac09_old_grant(public_agent_execution_harness, monkeypatch):
    from datetime import timedelta
    from kotoyose.execution_limits import ProviderUsageLimit
    from kotoyose.agent_execution_digest import agent_utc
    from tests.fixtures.agent_execution import FakeClock

    hub = public_agent_execution_harness
    clock = FakeClock()
    hub.agent_executions.clock = hub.agent_executions.leases.clock = clock
    hub.runtime.provider_limit = ProviderUsageLimit(provider='claude', limit_window='rate', reset_at=None,
        detected_at=clock(), resume='native', unavailable_reason=None)
    raw, _, _ = await _create(hub, script='provider')
    waiting = await _snapshot(hub, raw, lambda s: s['state'] == 'waiting_provider')
    assert parse_agent_utc(waiting['next_probe_at']) == clock() + timedelta(seconds=60)
    probe = hub.agent_executions._probe
    async def defer(*args):
        return False
    monkeypatch.setattr(hub.agent_executions, '_probe', defer)
    action = {'action_key': 'now', 'expected_revision': waiting['state_revision']}
    first = await hub.client.post(_path(raw) + '/try-now', json=action)
    second = await hub.client.post(_path(raw) + '/try-now', json=action)
    assert first.status_code == second.status_code == 202
    assert first.json()['activation']['nonce'] == second.json()['activation']['nonce']
    monkeypatch.setattr(hub.agent_executions, '_probe', probe)
    await hub.store.pool.execute('UPDATE kotoyose.agent_executions SET next_probe_at=$2 WHERE idempotency_key=$1',
                                 raw['execution']['idempotency_key'], clock())
    await hub.agent_executions.run_due()
    resume = await _snapshot(hub, raw, lambda s: s['state'] == 'resuming')
    replay = await hub.client.put(_path(raw) + '/authorization', json=raw['execution']['initial_authorization'])
    assert replay.status_code == 200 and len(hub.runtime.jobs) == 1
    stale = {**raw['execution']['initial_authorization'], 'revision': 2}
    assert (await hub.client.put(_path(raw) + '/authorization', json=stale)).status_code == 409
    allow = hub.allow(resume, revision=2, activation_nonce=resume['activation']['nonce'],
        issued_at=agent_utc(clock()), not_before=agent_utc(clock()), expires_at=agent_utc(clock() + timedelta(seconds=30)))
    assert (await hub.client.put(_path(raw) + '/authorization', json=allow)).status_code == 200
    again = await _snapshot(hub, raw, lambda s: s['state'] == 'waiting_provider' and s['activation']['ordinal'] == 2)
    assert parse_agent_utc(again['next_probe_at']) == clock() + timedelta(seconds=120)
    assert again['fault_retries_used'] == 0 and len(hub.runtime.jobs) == 2
    assert len({r['activation_nonces'][0] for r in again['receipts']}) == 2
    for ordinal, seconds in enumerate((240, 480, 900, 900), 3):
        await hub.store.pool.execute('UPDATE kotoyose.agent_executions SET next_probe_at=$2 WHERE idempotency_key=$1',
                                     raw['execution']['idempotency_key'], clock())
        await hub.agent_executions.run_due()
        resume = await _snapshot(hub, raw, lambda s: s['state'] == 'resuming')
        allow = hub.allow(resume, revision=ordinal, activation_nonce=resume['activation']['nonce'],
            settled_receipt_cursor=resume['final_receipt_cursor'], issued_at=agent_utc(clock()),
            not_before=agent_utc(clock()), expires_at=agent_utc(clock() + timedelta(seconds=30)))
        assert (await hub.client.put(_path(raw) + '/authorization', json=allow)).status_code == 200
        again = await _snapshot(hub, raw, lambda s: s['state'] == 'waiting_provider' and s['activation']['ordinal'] == ordinal)
        assert parse_agent_utc(again['next_probe_at']) == clock() + timedelta(seconds=seconds)
        assert again['fault_retries_used'] == 0 and len(hub.runtime.jobs) == ordinal


@pytest.mark.asyncio
async def test_ac04_session_disappears_after_probe(public_agent_execution_harness, monkeypatch):
    hub = public_agent_execution_harness
    raw, _, _ = await _create(hub, script='provider')
    waiting = await _snapshot(hub, raw, lambda s: s['state'] == 'waiting_provider')
    action = {'action_key': 'probe', 'expected_revision': waiting['state_revision']}
    assert (await hub.client.post(_path(raw) + '/try-now', json=action)).status_code == 202
    resume = await _snapshot(hub, raw, lambda s: s['state'] == 'resuming')
    checks = 0
    async def disappear(scope, ref):
        nonlocal checks
        checks += 1
        return checks == 1
    monkeypatch.setattr(hub.runtime, 'can_resume_session', disappear)
    allow = hub.allow(resume, revision=2, activation_nonce=resume['activation']['nonce'],
                      settled_receipt_cursor=resume['final_receipt_cursor'])
    assert (await hub.client.put(_path(raw) + '/authorization', json=allow)).status_code == 200
    final = await _snapshot(hub, raw, lambda s: s['state'] == 'terminal')
    assert final['terminal']['kind'] == 'resume_unavailable' and final['terminal']['cause'] == 'not_retained'
    assert [r['outcome'] for r in final['receipts']] == ['provider_zero_turn', 'not_started_closed']
    assert await hub.store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_attempts WHERE limits_started_at IS NOT NULL') == 1


@pytest.mark.asyncio
async def test_ac05_ac11_retry_claim_survives_restart(public_agent_execution_harness):
    before, after, final = await run_scenario(public_agent_execution_harness, SCENARIOS['retry_restart'])
    assert before['activation']['nonce'] == after['activation']['nonce']
    assert before['snapshot']['fault_retries_used'] == after['snapshot']['fault_retries_used'] == 1
    assert before['attempts'] == after['attempts'] == 1
    assert final['snapshot']['terminal']['kind'] == 'execution_fault'
    assert final['snapshot']['fault_retries_used'] == 1 and final['attempts'] == 2
    assert [r['outcome'] for r in final['receipts']] == ['executed', 'executed']


@pytest.mark.asyncio
async def test_ac06_receipt_and_authorization_replay(public_agent_execution_harness):
    hub = public_agent_execution_harness
    raw, _, ack = await _create(hub, script='success')
    final = await _snapshot(hub, raw, lambda s: s['state'] == 'terminal')
    receipt = final['receipts'][0]
    assert receipt['execution_confirmed'] and receipt['units'] == receipt['allocation_units']
    assert receipt['charge_ref'] == raw['execution']['initial_authorization']['charge']['charge_ref']
    replay = await hub.client.post('/v1/executions', json=raw)
    assert replay.json() == ack
    page = (await hub.client.get(_path(raw), params={'after_receipt': 0})).json()
    tail = (await hub.client.get(_path(raw), params={'after_receipt': receipt['cursor']})).json()
    assert page['receipts'] == [receipt] and tail['receipts'] == []
    assert tail['next_receipt_cursor'] == tail['final_receipt_cursor'] == receipt['cursor']
    assert len(hub.runtime.jobs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', ['run_seconds', 'task_seconds'])
async def test_ac08_allowed_active_time_limit(public_agent_execution_harness, limit):
    hub = public_agent_execution_harness
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    raw['execution']['initial_authorization'].update(allowed_active_ms=350, stop_limit=limit)
    assert (await hub.client.post('/v1/executions', json=raw)).status_code == 202
    final = await _snapshot(hub, raw, lambda s: s['state'] == 'terminal')
    assert final['terminal'] == {'kind': 'time_limit', 'limit': limit}
    assert final['stop_confirmation'] == 'confirmed' and final['side_effect_resolution'] == 'resolved'
    assert len(final['receipts']) == 1


@pytest.mark.asyncio
async def test_ac10_ac14_agent_exec_gap_keeps_nonce_charge_and_usage(public_agent_execution_harness):
    first, renewed = await run_scenario(public_agent_execution_harness, SCENARIOS['agent_exec_gap'])
    assert first['activation']['nonce'] == renewed['activation']['nonce']
    assert first['activation']['charge_ref'] == renewed['activation']['charge_ref']
    assert first['attempts'] == renewed['attempts'] == 1
    assert renewed['usage']['active_ms'] >= first['usage']['active_ms']
    assert renewed['receipts'] == []


@pytest.mark.asyncio
async def test_ac11_provider_wait_survives_restart(public_agent_execution_harness):
    before, after = await run_scenario(public_agent_execution_harness, SCENARIOS['wait_restart'])
    for field in ('next_probe_at', 'fault_retries_used', 'usage'):
        assert before['snapshot'][field] == after['snapshot'][field]
    assert before['activation']['nonce'] == after['activation']['nonce']
    assert before['attempts'] == after['attempts'] == 1


@pytest.mark.asyncio
async def test_ac12_cancel_fences_wait_and_resume(public_agent_execution_harness):
    import asyncio
    hub = public_agent_execution_harness
    raw, _, _ = await _create(hub, script='provider')
    waiting = await _snapshot(hub, raw, lambda s: s['state'] == 'waiting_provider')
    path = _path(raw)
    action = {'action_key': 'wake', 'expected_revision': waiting['state_revision']}
    cancel = {'action_key': 'cancel', 'reason': 'owner_abort'}
    wake, abort = await asyncio.gather(hub.client.post(path + '/try-now', json=action),
                                       hub.client.post(path + '/cancel', json=cancel))
    assert wake.status_code in (202, 409) and abort.status_code == 202
    final = await _snapshot(hub, raw, lambda s: s['state'] == 'terminal')
    assert final['admission_closed'] and final['terminal'] == {'kind': 'cancelled'}
    assert final['stop_confirmation'] == 'confirmed' and final['side_effect_resolution'] == 'resolved'
    assert len(hub.runtime.jobs) == 1
    assert (await hub.client.put(path + '/authorization', json=raw['execution']['initial_authorization'])).status_code == 409
    assert (await hub.client.post(path + '/cancel', json=cancel)).status_code == 202


@pytest.mark.asyncio
async def test_ac12_cancel_waits_for_stop_evidence(public_agent_execution_harness):
    import asyncio
    hub = public_agent_execution_harness
    hub.runtime.stop_confirmation = 'unconfirmed'
    raw, _, _ = await _create(hub)
    await asyncio.wait_for(hub.runtime.started.wait(), 3)
    response = await hub.client.post(_path(raw) + '/cancel', json={'action_key': 'cancel', 'reason': 'owner_abort'})
    assert response.status_code == 202
    pending = await _snapshot(hub, raw, lambda s: s['usage']['state'] == 'unknown')
    assert pending['terminal'] is None and pending['cancel_requested']
    assert pending['stop_confirmation'] == 'unconfirmed' and pending['receipts'] == []
    attempt_id = await hub.store.pool.fetchval('SELECT attempt_id FROM kotoyose.agent_execution_activations')
    # The fake process has exited; record the independent stop confirmation.
    assert hub.runtime.ended_at is not None
    await hub.store.confirm_stop(attempt_id)
    await hub.agent_executions.runner.settle(await hub.store.attempt('caller-a', attempt_id))
    final = await _snapshot(hub, raw, lambda s: s['state'] == 'terminal')
    assert final['admission_closed'] and final['terminal'] == {'kind': 'cancelled'}
    assert final['stop_confirmation'] == 'confirmed' and final['side_effect_resolution'] == 'resolved'


@pytest.mark.asyncio
async def test_ac16_crash_before_launch_evidence_then_cancel(public_agent_execution_harness, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from kotoyose.execution_service import ExecutionService
    hub = public_agent_execution_harness
    entered = asyncio.Event()
    async def interrupted_launch(job, cancel, execute_tool, *, on_process_started):
        hub.runtime.jobs.append(job)
        await job.validate_start()
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(hub.runtime, 'execute', interrupted_launch)
    raw, _, _ = await _create(hub)
    await asyncio.wait_for(entered.wait(), 3)
    await hub.crash()
    # The fake never started a process; an independent stop check can confirm that.
    monkeypatch.setattr(ExecutionService, '_stop_attempt', AsyncMock(return_value='confirmed'))
    await hub.restart()
    hub.client._transport.app.state.execution_service = hub.service
    response = await hub.client.post(_path(raw) + '/cancel', json={'action_key': 'cancel', 'reason': 'owner_abort'})
    assert response.status_code == 202
    final = await _snapshot(hub, raw, lambda s: s['state'] == 'terminal')
    assert final['admission_closed'] and not final['usage']['complete']
    assert final['stop_confirmation'] == 'confirmed' and final['side_effect_resolution'] == 'resolved'
    assert final['receipts'] == []


@pytest.mark.asyncio
async def test_ac16_crash_during_proven_start_then_cancel(public_agent_execution_harness):
    import asyncio
    hub = public_agent_execution_harness
    raw, _, _ = await _create(hub)
    await asyncio.wait_for(hub.runtime.started.wait(), 3)
    running = await _snapshot(hub, raw, lambda s: s['usage']['state'] == 'running')
    await hub.crash()
    await hub.restart()
    hub.client._transport.app.state.execution_service = hub.service
    response = await hub.client.post(_path(raw) + '/cancel', json={'action_key': 'cancel', 'reason': 'owner_abort'})
    assert response.status_code == 202
    final = await _snapshot(hub, raw, lambda s: s['state'] == 'terminal')
    assert final['admission_closed'] and not final['usage']['complete']
    assert final['usage']['active_ms'] >= running['usage']['active_ms']
    assert final['stop_confirmation'] == 'confirmed' and final['side_effect_resolution'] == 'resolved'
    assert len(final['receipts']) == 1 and final['receipts'][0]['execution_confirmed']


@pytest.mark.asyncio
async def test_ac17_lease_resume_closes_one_charge(public_agent_execution_harness):
    from tests.test_agent_execution_runner import set_ttl
    hub = public_agent_execution_harness
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    set_ttl(raw, 2)
    assert (await hub.client.post('/v1/executions', json=raw)).status_code == 202
    resume = await _snapshot(hub, raw, lambda s: s['state'] == 'resuming')
    assert resume['activation']['reason'] == 'lease_resume' and resume['receipts'] == []
    charge = raw['execution']['initial_authorization']['charge']
    assert resume['activation']['charge_ref'] == charge['charge_ref']
    hub.runtime.script = 'success'
    allow = hub.allow(resume, revision=2, activation_nonce=resume['activation']['nonce'], charge=charge)
    assert (await hub.client.put(_path(raw) + '/authorization', json=allow)).status_code == 200
    final = await _snapshot(hub, raw, lambda s: s['state'] == 'terminal')
    assert final['terminal'] == {'kind': 'succeeded'} and len(final['receipts']) == 1
    assert final['receipts'][0]['activation_nonces'] == [raw['execution']['initial_authorization']['activation_nonce'], resume['activation']['nonce']]
    assert final['receipts'][0]['units'] == charge['allocation_units']
    assert hub.runtime.jobs[-1].provider_session_ref == hub.runtime.session_ref


@pytest.mark.asyncio
async def test_ac19_approval_continue_and_safe_resume_successor(public_agent_execution_harness):
    from tests.test_agent_execution_faults import _successor
    hub = public_agent_execution_harness
    raw, refs, ack = await _create(hub, script='approval', approval=True)
    pause = await _snapshot(hub, raw, lambda s: s['pause'] is not None)
    assert pause['state'] == 'queued' and pause['activation']['reason'] == 'approval_continue'
    assert pause['stop_confirmation'] == 'confirmed' and pause['side_effect_resolution'] == 'resolved'
    approval = {'call_id': pause['pause']['call_id'], 'subject_digest': pause['pause']['subject_digest'],
                'decision': 'approved', 'decision_ref': 'decision:1'}
    allow = hub.allow(pause, revision=2, activation_nonce=pause['activation']['nonce'], approval=approval,
                      settled_receipt_cursor=pause['final_receipt_cursor'])
    assert (await hub.client.put(_path(raw) + '/authorization', json=allow)).status_code == 200
    first = await _snapshot(hub, raw, lambda s: s['state'] == 'terminal')
    assert first['terminal'] == {'kind': 'succeeded'}
    assert len(first['receipts']) == 2 and len({r['charge_ref'] for r in first['receipts']}) == 2
    assert hub.runtime.jobs[-1].provider_session_ref == hub.runtime.session_ref
    successor = await _successor(hub, refs, first, ack)
    assert (await hub.client.post('/v1/executions', json=successor)).status_code == 202
    final = await _snapshot(hub, successor, lambda s: s['state'] == 'terminal')
    assert final['terminal'] == {'kind': 'succeeded'}
    assert hub.runtime.jobs[-1].provider_session_ref == hub.runtime.session_ref
    assert len(hub.runtime.jobs) == 3
