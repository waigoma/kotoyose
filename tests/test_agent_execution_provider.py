import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from kotoyose.execution_limits import ProviderUsageLimit
from kotoyose.agent_execution_digest import agent_utc, parse_agent_utc
from kotoyose.agent_execution_provider import next_probe_at, provider_limit_wire
from tests.fixtures.agent_execution import FakeClock
from tests.test_agent_execution_runner import running_agent_execution_harness, wait_snapshot


@pytest.fixture
async def provider_agent_execution_harness(running_agent_execution_harness):
    hub = running_agent_execution_harness
    hub.clock = FakeClock()
    hub.agent_executions.clock = hub.clock
    hub.agent_executions.leases.clock = hub.clock
    hub.runtime.script = 'provider'
    hub.runtime.provider_limit = ProviderUsageLimit(provider='claude', limit_window='rate', reset_at=None,
        detected_at=hub.clock(), resume='native', unavailable_reason=None)
    return hub


async def wait_for_provider(hub):
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    response = await hub.client.post('/v1/executions', json=raw)
    assert response.status_code == 202, response.text
    key = raw['execution']['idempotency_key']
    snapshot = await wait_snapshot(hub, key, lambda value: value['state'] == 'waiting_provider')
    return raw, key, snapshot


@pytest.mark.asyncio
async def test_initial_activation_starts_with_frozen_clock_after_setup_delay(provider_agent_execution_harness):
    hub = provider_agent_execution_harness
    # Model a slow setup without sleeping or advancing the Hub clock during pickup.
    hub.clock.advance(seconds=-5)
    await wait_for_provider(hub)


def test_known_reset_waits_past_reset():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    limit = {'detected_at': agent_utc(now), 'reset_at': agent_utc(now + timedelta(seconds=20))}
    assert next_probe_at(limit, 0, now) == now + timedelta(seconds=25)
    assert next_probe_at(limit, 0, now + timedelta(seconds=30)) == now + timedelta(seconds=35)
    limit['reset_at'] = limit['detected_at']
    assert next_probe_at(limit, 0, now) == now + timedelta(seconds=60)
    wire = provider_limit_wire({'limits_provider': 'claude', 'limits_window': 'rate',
        'limits_reset_at': now, 'limits_detected_at': now, 'limits_resume': 'native',
        'limits_unavailable_reason': None})
    assert wire['reset_at'] is None


@pytest.mark.asyncio
async def test_unknown_reset_backoff_is_durable(provider_agent_execution_harness):
    hub = provider_agent_execution_harness
    raw, key, snap = await wait_for_provider(hub)
    assert snap['receipts'][0]['outcome'] == 'provider_zero_turn'
    assert snap['receipts'][0]['units'] == '0'
    assert snap['stop_confirmation'] == 'confirmed' and snap['side_effect_resolution'] == 'resolved'
    assert parse_agent_utc(snap['next_probe_at']) == hub.clock() + timedelta(seconds=60)
    active = snap['usage']['active_ms']
    for step, seconds in enumerate((60, 120, 240, 480, 900, 900), 1):
        row = await hub.store.pool.fetchrow('SELECT probe_step FROM kotoyose.agent_executions WHERE idempotency_key=$1', key)
        assert row['probe_step'] == step
        assert parse_agent_utc(snap['next_probe_at']) == hub.clock() + timedelta(seconds=seconds)
        if step == 6:
            break
        # Expire only the stored deadline, leaving the clock aligned with the DB lease guard.
        await hub.store.pool.execute('UPDATE kotoyose.agent_executions SET next_probe_at=$2 WHERE idempotency_key=$1', key, hub.clock())
        assert await hub.agent_executions.run_due() == 1
        resumed = (await hub.client.get(f'/v1/executions/by-key/{key}')).json()
        now = hub.clock()
        allow = hub.allow(resumed, revision=step + 1, activation_nonce=resumed['activation']['nonce'],
            issued_at=agent_utc(now), not_before=agent_utc(now), expires_at=agent_utc(now + timedelta(seconds=30)))
        response = await hub.client.put(f'/v1/executions/by-key/{key}/authorization', json=allow)
        assert response.status_code == 200, response.text
        snap = await wait_snapshot(hub, key, lambda value: value['state'] == 'waiting_provider')
    from kotoyose.agent_execution_service import AgentExecutionService
    reconstructed = AgentExecutionService(hub.service, clock=hub.clock)
    restored = await reconstructed.read('caller-a', key, 0)
    assert restored['next_probe_at'] == snap['next_probe_at'] and restored['usage']['active_ms'] >= active


@pytest.mark.asyncio
async def test_due_probe_opens_one_activation(provider_agent_execution_harness, monkeypatch):
    hub = provider_agent_execution_harness
    raw, key, snap = await wait_for_provider(hub)
    hub.clock.advance(seconds=60)
    action = {'action_key': str(uuid4()), 'expected_revision': snap['state_revision']}
    results = await asyncio.gather(hub.agent_executions.run_due(), hub.agent_executions.run_due(), hub.client.post(f'/v1/executions/by-key/{key}/try-now', json=action))
    assert sum(result for result in results[:2]) <= 1
    current = (await hub.client.get(f'/v1/executions/by-key/{key}')).json()
    assert current['state'] == 'resuming' and current['activation']['authority_required']
    assert current['activation']['reason'] == 'provider_resume' and current['activation']['ordinal'] == 2
    assert await hub.store.pool.fetchval('SELECT count(*) FROM kotoyose.agent_execution_activations') == 2
    assert await hub.store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_attempts') == 1
    monkeypatch.setattr(hub.agent_executions.runner, 'kick', pytest.fail)
    replay = await hub.client.put(f'/v1/executions/by-key/{key}/authorization', json=raw['execution']['initial_authorization'])
    assert replay.status_code == 200 and replay.json()['activation']['nonce'] == current['activation']['nonce']


@pytest.mark.asyncio
async def test_try_now_is_deduplicated_and_revision_checked(provider_agent_execution_harness, monkeypatch):
    hub = provider_agent_execution_harness
    raw, key, snap = await wait_for_provider(hub)
    path = f'/v1/executions/by-key/{key}/try-now'
    stale = await hub.client.post(path, json={'action_key': 'stale', 'expected_revision': snap['state_revision'] - 1})
    assert stale.status_code == 409 and stale.json()['error']['code'] == 'stale_revision'
    async def defer_probe(*args):
        return False
    monkeypatch.setattr(hub.agent_executions, '_probe', defer_probe)
    action = {'action_key': 'now', 'expected_revision': snap['state_revision']}
    first = await hub.client.post(path, json=action)
    assert first.status_code == 202
    repeat = await hub.client.post(path, json=action)
    assert repeat.status_code == 202 and repeat.json()['activation']['nonce'] == first.json()['activation']['nonce']
    second = await hub.client.post(path, json={'action_key': 'now-again', 'expected_revision': first.json()['state_revision']})
    assert second.status_code == 202 and second.json() == first.json()
    conflict = await hub.client.post(path, json={**action, 'expected_revision': 0})
    assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_provider_resume_continues_same_session(provider_agent_execution_harness):
    hub = provider_agent_execution_harness
    raw, key, waiting = await wait_for_provider(hub)
    action = {'action_key': str(uuid4()), 'expected_revision': waiting['state_revision']}
    assert (await hub.client.post(f'/v1/executions/by-key/{key}/try-now', json=action)).status_code == 202
    snap = (await hub.client.get(f'/v1/executions/by-key/{key}')).json()
    hub.runtime.script = 'success'
    now = hub.clock()
    allow = hub.allow(snap, revision=2, activation_nonce=snap['activation']['nonce'],
        issued_at=agent_utc(now), not_before=agent_utc(now), expires_at=agent_utc(now + timedelta(seconds=30)))
    result = await hub.client.put(f'/v1/executions/by-key/{key}/authorization', json=allow)
    assert result.status_code == 200, result.text
    final = await wait_snapshot(hub, key, lambda value: value['state'] == 'terminal')
    assert final['terminal'] == {'kind': 'succeeded'}
    assert [r['outcome'] for r in final['receipts']] == ['provider_zero_turn', 'executed']
    assert hub.runtime.jobs[-1].provider_session_ref == 'session'
    snapshot = await hub.store.pool.fetchval('SELECT snapshot FROM kotoyose.execution_attempts ORDER BY created_at DESC LIMIT 1')
    import json
    assert json.loads(snapshot)['resume_mode'] == 'native'


@pytest.mark.asyncio
async def test_lease_resume_continues_same_session_and_charge(running_agent_execution_harness):
    hub = running_agent_execution_harness
    from tests.test_agent_execution_runner import set_ttl
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    set_ttl(raw, 2)
    key = raw['execution']['idempotency_key']
    assert (await hub.client.post('/v1/executions', json=raw)).status_code == 202
    snap = await wait_snapshot(hub, key, lambda value: value['state'] == 'resuming')
    hub.runtime.script = 'success'
    allow = hub.allow(snap, revision=2, activation_nonce=snap['activation']['nonce'], charge=raw['execution']['initial_authorization']['charge'])
    assert (await hub.client.put(f'/v1/executions/by-key/{key}/authorization', json=allow)).status_code == 200
    final = await wait_snapshot(hub, key, lambda value: value['state'] == 'terminal')
    assert hub.runtime.jobs[-1].provider_session_ref == 'session'
    assert hub.runtime.jobs[-1].max_turns == raw['execution']['resources']['max_turns'] - 1
    assert hub.runtime.jobs[-1].output_limit_bytes == min(8192, raw['execution']['resources']['max_output_bytes'] - 6)
    assert len(final['receipts']) == 1
    assert final['receipts'][0]['activation_nonces'] == [raw['execution']['initial_authorization']['activation_nonce'], snap['activation']['nonce']]


@pytest.mark.asyncio
@pytest.mark.parametrize('retained,ref,cause', [(False, 'session', 'not_retained'),
    (True, None, 'no_session'), (True, 'session', 'adapter_unsupported')])
async def test_lost_session_ends_at_probe(provider_agent_execution_harness, retained, ref, cause):
    hub = provider_agent_execution_harness
    hub.runtime.session_ref = ref
    if ref is None:
        hub.runtime.provider_limit = hub.runtime.provider_limit.model_copy(update={'resume': 'unavailable', 'unavailable_reason': 'no_session'})
    raw, key, waiting = await wait_for_provider(hub)
    if cause == 'adapter_unsupported':
        await hub.store.pool.execute("UPDATE kotoyose.execution_attempts SET snapshot=jsonb_set(snapshot,'{backend}',to_jsonb('legacy'::text))")
    hub.runtime.retained = retained
    hub.clock.advance(seconds=60)
    await hub.agent_executions.run_due()
    final = (await hub.client.get(f'/v1/executions/by-key/{key}')).json()
    assert final['terminal']['kind'] == 'resume_unavailable' and final['terminal']['cause'] == cause
    assert final['terminal']['workspace_ref'] == waiting['workspace_ref']
    assert final['terminal']['work_digest'] == raw['execution']['work_digest']
    assert final['activation']['nonce'] == waiting['activation']['nonce']


@pytest.mark.asyncio
async def test_session_lost_before_start_closes_new_unit(provider_agent_execution_harness, monkeypatch):
    hub = provider_agent_execution_harness
    raw, key, waiting = await wait_for_provider(hub)
    action = {'action_key': str(uuid4()), 'expected_revision': waiting['state_revision']}
    assert (await hub.client.post(f'/v1/executions/by-key/{key}/try-now', json=action)).status_code == 202
    snap = (await hub.client.get(f'/v1/executions/by-key/{key}')).json()
    checks = 0
    async def disappear(scope, ref):
        nonlocal checks
        checks += 1
        return checks == 1
    monkeypatch.setattr(hub.runtime, 'can_resume_session', disappear)
    now = hub.clock()
    allow = hub.allow(snap, revision=2, activation_nonce=snap['activation']['nonce'],
        issued_at=agent_utc(now), not_before=agent_utc(now), expires_at=agent_utc(now + timedelta(seconds=30)))
    assert (await hub.client.put(f'/v1/executions/by-key/{key}/authorization', json=allow)).status_code == 200
    final = await wait_snapshot(hub, key, lambda value: value['state'] == 'terminal')
    assert final['terminal']['kind'] == 'resume_unavailable'
    assert final['terminal']['cause'] == 'not_retained'
    assert [r['outcome'] for r in final['receipts']] == ['provider_zero_turn', 'not_started_closed']
    assert await hub.store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts WHERE limits_started_at IS NOT NULL") == 1


@pytest.mark.asyncio
async def test_wait_keeps_counter_and_refuses_old_grant(provider_agent_execution_harness):
    hub = provider_agent_execution_harness
    raw, key, waiting = await wait_for_provider(hub)
    path = f'/v1/executions/by-key/{key}'
    first = (await hub.client.get(path)).json()
    second = (await hub.client.get(path)).json()
    hub.agent_executions.runner.kick = pytest.fail
    replay = await hub.client.put(path + '/authorization', json=raw['execution']['initial_authorization'])
    assert replay.status_code == 200
    assert first['sequence'] == second['sequence'] == replay.json()['sequence']
    assert replay.json()['fault_retries_used'] == 0
    old = {**raw['execution']['initial_authorization'], 'revision': 2}
    result = await hub.client.put(path + '/authorization', json=old)
    assert result.status_code == 409 and result.json()['error']['code'] == 'stale_activation'
    deny = hub.deny(raw, {'kind': 'authority_denied', 'cause': 'archived'}, activation_nonce=waiting['activation']['nonce'])
    result = await hub.client.put(path + '/authorization', json=deny)
    assert result.status_code == 200 and result.json()['terminal'] == deny['reason']
