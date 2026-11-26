import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest

from kotoyose.agent_execution_digest import agent_request_digest, agent_utc, agent_work_digest, parse_agent_utc
from tests.fixtures.agent_execution import isolated_agent_execution_harness


@pytest.mark.asyncio
async def test_deny_before_launch_releases_input_pins(tmp_path, monkeypatch):
    async with isolated_agent_execution_harness(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL'), tmp_path, monkeypatch) as hub:
        refs = await hub.register_work()
        raw = hub.create_body(refs)
        ref = 'artifact:input:deny-pin'
        raw['execution']['work']['inputs'][0]['content_ref'] = ref
        raw['execution']['work_digest'] = agent_work_digest(raw)
        raw['execution']['initial_authorization']['work_digest'] = raw['execution']['work_digest']
        workspace = await hub.store.workspace('caller-a', refs['workspace'])
        now = datetime.now(timezone.utc)
        await hub.store.pool.execute('''INSERT INTO kotoyose.execution_input_artifacts
            (ref,principal,digest,content,media_type,byte_size,expires_at,state)
            VALUES($1,'caller-a',$2,$3,'image/png',3,$4,'active')''',
            ref, 'a' * 64, b'png', now + timedelta(minutes=1))
        row, _ = await hub.agent_executions.store.create('caller-a', raw, request_digest=agent_request_digest(raw),
            work_digest=raw['execution']['work_digest'], pinned_plan={'workspace_id': workspace['id'],
            'generation': workspace['generation']}, now=now)
        deny = hub.deny(raw, {'kind': 'cancelled'}, activation_nonce=raw['execution']['initial_authorization']['activation_nonce'])
        await hub.agent_executions.authorize('caller-a', raw['execution']['idempotency_key'], deny)
        assert await hub.store.pool.fetchval('SELECT pin_count FROM kotoyose.execution_input_artifacts WHERE ref=$1', ref) == 0
        assert await hub.store.pool.fetchval('SELECT released_at IS NOT NULL FROM kotoyose.agent_execution_input_pins WHERE execution_id=$1', row['id'])
        await hub.store.pool.execute('UPDATE kotoyose.execution_input_artifacts SET expires_at=$2 WHERE ref=$1', ref, now - timedelta(seconds=1))
        assert await hub.store.input_artifacts.gc_expired_input_artifacts() == 1
        assert await hub.store.pool.fetchval('SELECT state FROM kotoyose.execution_input_artifacts WHERE ref=$1', ref) == 'tombstone'


@pytest.fixture
async def running_agent_execution_harness(tmp_path, monkeypatch):
    async with isolated_agent_execution_harness(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL'), tmp_path, monkeypatch, execute=True) as hub:
        from tests.fixtures.agent_execution import ScriptedRuntime
        hub.runtime = ScriptedRuntime('wait')
        hub.service.agent_executor.runtime = hub.runtime
        yield hub
        for event in list(hub.service._cancels.values()):
            event.set()
        if hub.service._tasks:
            await asyncio.gather(*list(hub.service._tasks.values()), return_exceptions=True)


async def wait_snapshot(hub, key, predicate, timeout=15):
    # Returns as soon as the predicate holds. The bound is generous because GitHub-hosted runners
    # (2 cores, with Postgres in a service container) took more than 5 s to reach waiting_provider.
    last = {}

    async def poll():
        while True:
            response = await hub.client.get('/v1/executions/by-key/' + key)
            assert response.status_code == 200, response.text
            value = response.json()
            last['value'] = value
            if predicate(value):
                return value
            await asyncio.sleep(.02)
    try:
        return await asyncio.wait_for(poll(), timeout)
    except TimeoutError:
        value = last.get('value') or {}
        raise AssertionError(
            f"snapshot did not match within {timeout}s; last state={value.get('state')!r}, "
            f"sequence={value.get('sequence')!r}") from None


def set_ttl(raw, seconds):
    now = datetime.now(timezone.utc)
    raw['execution']['initial_authorization'].update(issued_at=agent_utc(now), not_before=agent_utc(now),
                                                       expires_at=agent_utc(now + timedelta(seconds=seconds)))


@pytest.mark.asyncio
async def test_activation_runs_once_and_settles(running_agent_execution_harness):
    hub = running_agent_execution_harness
    refs = await hub.register_work(tool_refs=[])
    raw = hub.create_body(refs)
    key = raw['execution']['idempotency_key']
    nonce = raw['execution']['initial_authorization']['activation_nonce']
    ack = await hub.client.post('/v1/executions', json=raw)
    assert ack.status_code == 202, ack.text
    await asyncio.wait_for(hub.runtime.started.wait(), 3)
    running = await wait_snapshot(hub, key, lambda s: s['usage']['state'] == 'running')
    assert running['workspace_ref'] is None
    assert (await hub.client.post('/v1/executions', json=raw)).json() == ack.json()
    await asyncio.gather(hub.agent_executions.runner.kick(ack.json()['execution_id']),
        hub.client.put(f'/v1/executions/by-key/{key}/authorization', json=raw['execution']['initial_authorization']))
    hub.runtime.release.set()
    final = await wait_snapshot(hub, key, lambda s: s['state'] == 'terminal')
    assert final['terminal'] == {'kind': 'succeeded'} and final['usage']['complete']
    assert len(final['receipts']) == 1
    assert final['receipts'][0]['outcome'] == 'executed'
    assert final['receipts'][0]['activation_nonces'] == [nonce]
    assert final['receipts'][0]['units'] == raw['execution']['initial_authorization']['charge']['allocation_units']
    assert final['receipts'][0]['execution_confirmed']
    assert final['workspace_ref'] == refs['workspace'].rsplit(':', 1)[0] + ':1'
    assert await hub.store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts WHERE api_operation='agent_activation'") == 1
    stale = hub.create_body(refs)
    result = await hub.client.post('/v1/executions', json=stale)
    assert result.status_code == 409, result.text


@pytest.mark.asyncio
async def test_renewal_keeps_nonce_and_reservation(running_agent_execution_harness, monkeypatch):
    hub = running_agent_execution_harness
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    set_ttl(raw, 2)
    key = raw['execution']['idempotency_key']
    response = await hub.client.post('/v1/executions', json=raw)
    assert response.status_code == 202, response.text
    await asyncio.wait_for(hub.runtime.started.wait(), 3)
    prepare = hub.agent_executions.leases.prepare
    def assert_running_lease(key, revision, **kwargs):
        prepare(key, revision, **kwargs)
        assert hub.agent_executions.leases.remaining_seconds(key, 0) > 0
    monkeypatch.setattr(hub.agent_executions.leases, 'prepare', assert_running_lease)
    for revision in range(2, 6):
        await asyncio.sleep(.9)
        now = datetime.now(timezone.utc)
        allow = {**raw['execution']['initial_authorization'], 'revision': revision, 'issued_at': agent_utc(now),
                 'not_before': agent_utc(now), 'expires_at': agent_utc(now + timedelta(seconds=2))}
        result = await hub.client.put(f'/v1/executions/by-key/{key}/authorization', json=allow)
        assert result.status_code == 200, result.text
    snapshot = await wait_snapshot(hub, key, lambda s: s['usage']['active_ms'] >= 3000)
    assert snapshot['activation']['nonce'] == raw['execution']['initial_authorization']['activation_nonce']
    assert len(hub.runtime.jobs) == 1
    deny = hub.deny(raw, {'kind': 'cancelled'}, revision=6, activation_nonce=snapshot['activation']['nonce'])
    assert (await hub.client.put(f'/v1/executions/by-key/{key}/authorization', json=deny)).status_code == 200
    final = await wait_snapshot(hub, key, lambda s: s['state'] == 'terminal')
    assert len(final['receipts']) == 1


@pytest.mark.asyncio
async def test_lease_expiry_resumes_in_same_unit(running_agent_execution_harness):
    hub = running_agent_execution_harness
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    set_ttl(raw, 2)
    key = raw['execution']['idempotency_key']
    result = await hub.client.post('/v1/executions', json=raw)
    assert result.status_code == 202, result.text
    await asyncio.wait_for(hub.runtime.started.wait(), 3)
    resumed = await wait_snapshot(hub, key, lambda s: s['state'] == 'resuming', timeout=4)
    expiry = parse_agent_utc(raw['execution']['initial_authorization']['expires_at'])
    assert 0 <= (hub.runtime.ended_at - expiry).total_seconds() <= .25
    assert resumed['activation']['reason'] == 'lease_resume'
    assert resumed['activation']['charge_ref'] == raw['execution']['initial_authorization']['charge']['charge_ref']
    assert resumed['usage']['active_ms'] > 0 and resumed['usage']['turns'] == 1
    deny = hub.deny(raw, {'kind': 'cancelled'}, activation_nonce=resumed['activation']['nonce'])
    result = await hub.client.put(f'/v1/executions/by-key/{key}/authorization', json=deny)
    assert result.status_code == 200, result.text
    receipt = result.json()['receipts'][0]
    assert receipt['activation_nonces'] == [raw['execution']['initial_authorization']['activation_nonce'], resumed['activation']['nonce']]
    assert receipt['units'] == raw['execution']['initial_authorization']['charge']['allocation_units']


@pytest.mark.asyncio
@pytest.mark.parametrize('ceiling', [False, True])
async def test_start_rechecks_authority_inside_admission(running_agent_execution_harness, monkeypatch, ceiling):
    from kotoyose.agent_execution_runner import _Admission
    hub = running_agent_execution_harness
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    now = datetime.now(timezone.utc)
    auth = raw['execution']['initial_authorization']
    if ceiling:
        expires = now + timedelta(seconds=4)
        end = now + timedelta(seconds=5)
        raw['execution']['authority_ceilings'] = [{'kind': 'approval', 'evidence_ref': 'approval:1', 'expires_at': agent_utc(end)}]
        raw['execution']['work_digest'] = agent_work_digest(raw)
        auth.update(work_digest=raw['execution']['work_digest'], issued_at=agent_utc(now), not_before=agent_utc(now),
                    expires_at=agent_utc(expires), authority_ceiling_at=agent_utc(end))
        shifted = now + timedelta(seconds=4)
    else:
        set_ttl(raw, 2)
        shifted = now + timedelta(seconds=3)
    original = _Admission.check
    async def expire_in_transaction(self, conn):
        hub.agent_executions.clock = lambda: shifted
        hub.agent_executions.leases.clock = hub.agent_executions.clock
        await original(self, conn)
    monkeypatch.setattr(_Admission, 'check', expire_in_transaction)
    response = await hub.client.post('/v1/executions', json=raw)
    assert response.status_code == 202, response.text
    result = await hub.client.get('/v1/executions/by-key/' + raw['execution']['idempotency_key'])
    assert result.json()['activation']['authority_required']
    assert await hub.store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_attempts') == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', ['run_seconds', 'task_seconds'])
async def test_stop_point_is_time_limit(running_agent_execution_harness, limit):
    hub = running_agent_execution_harness
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    raw['execution']['initial_authorization'].update(allowed_active_ms=350, stop_limit=limit)
    response = await hub.client.post('/v1/executions', json=raw)
    assert response.status_code == 202, response.text
    final = await wait_snapshot(hub, raw['execution']['idempotency_key'], lambda s: s['state'] == 'terminal')
    assert final['terminal'] == {'kind': 'time_limit', 'limit': limit}


@pytest.mark.asyncio
async def test_full_length_run_under_wall_cap_ends_as_time_limit(running_agent_execution_harness):
    """issued_at がコンテナ起動より前でも、3 * run_hard + grace の許可なら満了まで走り time_limit で終わる。"""
    from kotoyose.agent_execution_digest import agent_work_digest
    hub = running_agent_execution_harness
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    raw['execution']['time'] = {'run_hard_ms': 1000, 'grace_ms': 1000}
    digest = agent_work_digest(raw)
    raw['execution']['work_digest'] = digest
    issued = datetime.now(timezone.utc) - timedelta(seconds=2)
    raw['execution']['initial_authorization'].update(
        work_digest=digest, issued_at=agent_utc(issued), not_before=agent_utc(issued),
        expires_at=agent_utc(issued + timedelta(milliseconds=3 * 1000 + 1000)), allowed_active_ms=1000, stop_limit='run_seconds')
    response = await hub.client.post('/v1/executions', json=raw)
    assert response.status_code == 202, response.text
    final = await wait_snapshot(hub, raw['execution']['idempotency_key'], lambda s: s['state'] == 'terminal', timeout=5)
    assert final['terminal'] == {'kind': 'time_limit', 'limit': 'run_seconds'}
    assert final['activation']['reason'] == 'initial' and final['activation']['ordinal'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('script,limit', [('turns', 'turns'), ('output', 'output_bytes'), ('unknown', None), ('success', 'success')])
async def test_caps_and_launch_evidence(running_agent_execution_harness, script, limit):
    hub = running_agent_execution_harness
    hub.runtime.script = script
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    response = await hub.client.post('/v1/executions', json=raw)
    assert response.status_code == 202, response.text
    final = await wait_snapshot(hub, raw['execution']['idempotency_key'], lambda s: s['state'] == 'terminal')
    if limit == 'success':
        assert final['terminal'] == {'kind': 'succeeded'} and final['usage']['complete']
    elif limit:
        assert final['terminal'] == {'kind': 'cost_limit', 'limit': limit}
    else:
        assert final['terminal']['kind'] == 'execution_fault' and not final['usage']['complete']
        assert final['receipts'] == []
        assert await hub.store.pool.fetchval('SELECT state FROM kotoyose.agent_execution_charge_units') == 'unprovable'
    if hub.runtime.jobs:
        assert hub.runtime.jobs[0].max_turns == raw['execution']['resources']['max_turns']
        assert hub.runtime.jobs[0].output_limit_bytes == min(8192, raw['execution']['resources']['max_output_bytes'])


@pytest.mark.asyncio
async def test_deny_while_running_stops_then_closes(running_agent_execution_harness):
    hub = running_agent_execution_harness
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    response = await hub.client.post('/v1/executions', json=raw)
    assert response.status_code == 202, response.text
    await asyncio.wait_for(hub.runtime.started.wait(), 3)
    key = raw['execution']['idempotency_key']
    deny = hub.deny(raw, {'kind': 'cost_limit', 'limit': 'task_units'}, activation_nonce=raw['execution']['initial_authorization']['activation_nonce'])
    result = await hub.client.put(f'/v1/executions/by-key/{key}/authorization', json=deny)
    assert result.status_code == 200, result.text
    final = await wait_snapshot(hub, key, lambda s: s['state'] == 'terminal')
    assert final['terminal'] == deny['reason'] and final['stop_confirmation'] == 'confirmed'
    assert final['receipts'][0]['outcome'] == 'executed'


@pytest.mark.asyncio
async def test_unconfirmed_stop_can_be_reconciled(running_agent_execution_harness):
    hub = running_agent_execution_harness
    hub.runtime.script = 'success'
    hub.runtime.stop_confirmation = 'unconfirmed'
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    response = await hub.client.post('/v1/executions', json=raw)
    assert response.status_code == 202, response.text
    key = raw['execution']['idempotency_key']
    pending = await wait_snapshot(hub, key, lambda s: s['usage']['state'] == 'unknown')
    assert pending['terminal'] is None and pending['workspace_ref'] is None
    attempt_id = await hub.store.pool.fetchval('SELECT attempt_id FROM kotoyose.agent_execution_activations')
    await hub.store.confirm_stop(attempt_id)
    observed = (await hub.client.get('/v1/executions/by-key/' + key)).json()
    assert observed['sequence'] > pending['sequence']
    assert observed['stop_confirmation'] == 'confirmed' and observed['side_effect_resolution'] == 'resolved'
    attempt = await hub.store.attempt('caller-a', attempt_id)
    await hub.agent_executions.runner.settle(attempt)
    final = (await hub.client.get('/v1/executions/by-key/' + key)).json()
    assert final['terminal'] == {'kind': 'succeeded'}
    assert not final['usage']['complete'] and final['workspace_ref'] is not None
    await hub.agent_executions.runner.settle(attempt)
    assert (await hub.client.get('/v1/executions/by-key/' + key)).json() == final


@pytest.mark.asyncio
async def test_full_capacity_leaves_activation_waiting(running_agent_execution_harness, monkeypatch):
    """満杯 (capacity_exhausted) は失敗にせず、activation を awaiting のまま残して後で取り直す。"""
    from kotoyose.execution_resources import ExecutionError
    hub = running_agent_execution_harness
    store = hub.service.store
    original = store.admit
    calls = []

    async def full_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ExecutionError('capacity_exhausted', 'Agent room capacity is full', 429, reason_code='containers')
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, 'admit', full_once)
    raw = hub.create_body(await hub.register_work(tool_refs=[]))
    key = raw['execution']['idempotency_key']
    ack = await hub.client.post('/v1/executions', json=raw)
    assert ack.status_code == 202, ack.text
    waiting = (await hub.client.get('/v1/executions/by-key/' + key)).json()
    assert waiting['state'] != 'terminal'
    assert await hub.store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_attempts') == 0
    await hub.agent_executions.runner.kick(ack.json()['execution_id'])
    await asyncio.wait_for(hub.runtime.started.wait(), 3)
    hub.runtime.release.set()
    final = await wait_snapshot(hub, key, lambda s: s['state'] == 'terminal')
    assert final['terminal'] == {'kind': 'succeeded'}


class _ContainerIdentityRuntime:
    """host coding の開始は container identity を要求する。このテストは部屋を立てない。"""

    def __init__(self, script):
        from tests.fixtures.agent_execution import ScriptedRuntime
        self._inner = ScriptedRuntime(script)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def execute(self, job, cancel, execute_tool, *, on_process_started):
        async def started(pid, *, scope_identity=None, container_identity=None):
            await on_process_started(pid, container_identity={
                "name": "kotoyose-agent-room", "id": "a" * 12, "instance": "kotoyose",
            })
        return await self._inner.execute(job, cancel, execute_tool, on_process_started=started)


@pytest.mark.asyncio
async def test_agent_execution_waits_for_a_room_instead_of_refusing_creation(running_agent_execution_harness):
    """部屋が満杯でも Hub execution は作られ、activation は awaiting のまま、空いたら始まる。"""
    from kotoyose.agent_container import AgentRoomCapacity
    from kotoyose.codex_coding_policy import HOST_MODE, HOST_TOOL_REF
    from kotoyose.execution_resources import AgentContainerConfig
    hub = running_agent_execution_harness
    hub.runtime = _ContainerIdentityRuntime('wait')
    hub.service.agent_executor.runtime = hub.runtime
    hub.service.config.agent_container = AgentContainerConfig(
        agent_root="/opt/agent", home_root="/var/lib/kotoyose/home", max_containers=1, min_free_disk_bytes=1,
    )
    rooms = {"running": 1}

    async def count():
        return rooms["running"]

    hub.service.store.agent_room_capacity = AgentRoomCapacity(
        hub.service.config.agent_container, running=count, free_bytes=lambda path: 10_000,
    )
    raw = hub.create_body(await hub.register_work(tool_refs=[HOST_TOOL_REF]))
    key = raw['execution']['idempotency_key']
    ack = await hub.client.post('/v1/executions', json=raw)
    assert ack.status_code == 202, ack.text
    waiting = (await hub.client.get('/v1/executions/by-key/' + key)).json()
    assert waiting['state'] != 'terminal'
    assert await hub.store.pool.fetchval(
        "SELECT state FROM kotoyose.agent_execution_activations") == 'awaiting'
    assert await hub.store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_attempts') == 0
    pinned = await hub.store.pool.fetchval(
        "SELECT pinned_plan->>'codex_execution_mode' FROM kotoyose.agent_executions")
    assert pinned == HOST_MODE
    rooms["running"] = 0
    await hub.agent_executions.runner.kick(ack.json()['execution_id'])
    await asyncio.wait_for(hub.runtime.started.wait(), 3)
    hub.runtime.release.set()
    final = await wait_snapshot(hub, key, lambda s: s['state'] == 'terminal')
    assert final['terminal'] == {'kind': 'succeeded'}
