import os
from datetime import datetime, timedelta, timezone

import pytest

from kotoyose.agent_execution_authority import AgentExecutionLeaseRegistry
from kotoyose.agent_execution_units import ActivationFacts, close_unit_outcome
from tests.fixtures.agent_execution import isolated_agent_execution_harness


@pytest.fixture
async def hub(tmp_path, monkeypatch):
    async with isolated_agent_execution_harness(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL'), tmp_path, monkeypatch) as kotoyose:
        yield kotoyose


@pytest.mark.asyncio
async def test_allow_revisions_are_idempotent_and_monotonic(hub):
    raw = hub.create_body(await hub.register_work())
    assert (await hub.client.post('/v1/executions', json=raw)).status_code == 202
    key = raw['execution']['idempotency_key']
    path = f'/v1/executions/by-key/{key}/authorization'
    allow = raw['execution']['initial_authorization']
    first = await hub.client.put(path, json=allow)
    assert first.status_code == 200
    assert (await hub.client.put(path, json=allow)).json() == first.json()
    updated = {**allow, 'revision': 2}
    assert (await hub.client.put(path, json=updated)).status_code == 200
    for decision, code in [({**updated, 'allowed_active_ms': 1}, 'request_conflict'),
                           ({**allow, 'revision': 0}, 'stale_revision'),
                           ({**updated, 'revision': 3, 'activation_nonce': 'other'}, 'stale_activation')]:
        response = await hub.client.put(path, json=decision)
        assert response.status_code == 409 and response.json()['error']['code'] == code
    await hub.store.pool.execute("UPDATE kotoyose.agent_execution_activations SET state='ended' WHERE nonce=$1", allow['activation_nonce'])
    response = await hub.client.put(path, json={**updated, 'revision': 3})
    assert response.status_code == 409 and response.json()['error']['code'] == 'stale_activation'


@pytest.mark.asyncio
async def test_deny_ends_execution_with_typed_terminal(hub):
    for reason in [{'kind': 'cost_limit', 'limit': 'run_budget'}, {'kind': 'time_limit', 'limit': 'run_seconds'},
                   {'kind': 'authority_denied', 'cause': 'archived'}, {'kind': 'cancelled'}]:
        raw = hub.create_body(await hub.register_work())
        assert (await hub.client.post('/v1/executions', json=raw)).status_code == 202
        body = raw['execution']
        decision = hub.deny(raw, reason, activation_nonce=body['initial_authorization']['activation_nonce'])
        path = f"/v1/executions/by-key/{body['idempotency_key']}/authorization"
        response = await hub.client.put(path, json=decision)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['terminal'] == reason and result['admission_closed']
        assert result['cancel_requested'] == (reason['kind'] == 'cancelled')
        assert result['receipts'][0]['outcome'] == 'not_started_closed'
        assert (await hub.client.put(path, json={**body['initial_authorization'], 'revision': 3})).status_code == 409


def test_unit_outcomes_follow_evidence():
    def fact(**kw):
        return ActivationFacts(nonce='n', launch=kw.get('launch', 'started'), measurement=kw.get('measurement', 'complete'),
            active_ms=1, turns=kw.get('turns', 0), output_bytes=2, provider_rejected_only=kw.get('rejected', False),
            stop=kw.get('stop', 'confirmed'), effects=kw.get('effects', 'resolved'))
    for item, expected in [(fact(stop='unconfirmed'), 'pending'), (fact(effects='unresolved'), 'pending'),
                           (fact(launch='unknown'), 'unprovable')]:
        assert close_unit_outcome([item], allocation_units='1') == expected
    for item, outcome, units, confirmed in [(fact(launch='not_started'), 'not_started_closed', '0', False),
        (fact(rejected=True), 'provider_zero_turn', '0', True), (fact(measurement='unknown'), 'executed', '1', True)]:
        result = close_unit_outcome([item], allocation_units='1')
        assert (result.outcome, result.units, result.execution_confirmed) == (outcome, units, confirmed)
    assert close_unit_outcome([fact()], allocation_units=None).units is None
    result = close_unit_outcome([fact(turns=None, measurement='unknown'), fact(turns=3)], allocation_units='1')
    assert (result.units, result.turns, result.active_ms, result.output_bytes) == ('1', 3, 2, 4)


def test_lease_registry_rejects_expiry():
    from kotoyose.agent_deadlines import AgentStartRejected
    from kotoyose.agent_execution_digest import agent_utc
    now = datetime.now(timezone.utc).replace(microsecond=0)
    clock = [now]
    registry = AgentExecutionLeaseRegistry(lambda: clock[0])
    key = ('execution', 'n')
    registry.update(key, {'not_before': agent_utc(now - timedelta(seconds=1)), 'expires_at': agent_utc(now + timedelta(seconds=5)),
                          'allowed_active_ms': 10000}, base_active_ms=2000, grace_ms=1000, ceiling_at=None)
    assert registry.remaining_seconds(key, 1000) == 7
    registry.assert_startable(key)
    assert registry.lease_expires_at(key) == now + timedelta(seconds=5)
    clock[0] = now + timedelta(seconds=4)
    with pytest.raises(AgentStartRejected):
        registry.assert_startable(key)
    clock[0] = now
    ceiling = now + timedelta(seconds=3)
    registry.update(key, {'revision': 1, 'not_before': agent_utc(now + timedelta(seconds=1)),
        'expires_at': agent_utc(now + timedelta(seconds=5)), 'allowed_active_ms': 10000},
        base_active_ms=2000, grace_ms=1000, ceiling_at=ceiling)
    assert registry.call_deadline(key) == ceiling - timedelta(seconds=1)
    with pytest.raises(AgentStartRejected):
        registry.assert_startable(key)
    clock[0] = now + timedelta(seconds=1)
    registry.assert_startable(key)
    clock[0] = now + timedelta(seconds=2)
    with pytest.raises(AgentStartRejected):
        registry.assert_startable(key)
    clock[0] = now
    update = {'revision': 2, 'not_before': agent_utc(now), 'expires_at': agent_utc(now + timedelta(seconds=5)),
              'allowed_active_ms': 10000}
    registry.prepare(key, 2)
    registry.update(key, {**update, 'revision': 1}, base_active_ms=0, grace_ms=1000, ceiling_at=None)
    with pytest.raises(AgentStartRejected):
        registry.assert_startable(key)
    registry.update(key, update, base_active_ms=0, grace_ms=1000, ceiling_at=None)
    registry.assert_startable(key)
    registry.fence(key, 3)
    registry.update(key, update, base_active_ms=0, grace_ms=1000, ceiling_at=None)
    with pytest.raises(AgentStartRejected):
        registry.assert_startable(key)
    registry.forget(key)
    with pytest.raises(AgentStartRejected):
        registry.assert_startable(key)


@pytest.mark.asyncio
async def test_guard_follows_latest_lease(hub):
    from uuid import UUID
    from kotoyose.execution_operation_guard import authorize_operation
    from kotoyose.client_tool_bridge import ClientToolRepository
    from kotoyose.execution_resources import ExecutionError
    from kotoyose.agent_execution_digest import parse_agent_utc

    raw = hub.create_body(await hub.register_work())
    response = await hub.client.post('/v1/executions', json=raw)
    attempt, refs = await hub.bind_internal_attempt(UUID(response.json()['execution_id']))
    await hub.store.pool.execute("UPDATE kotoyose.execution_resources SET payload=jsonb_set(payload,'{expires_at}',to_jsonb((now()-interval '1 second')::text)) WHERE ref=$1", refs['grant'])
    deadline = parse_agent_utc(raw['execution']['initial_authorization']['expires_at'])
    repo = ClientToolRepository(hub.store.pool)
    async with hub.store.pool.acquire() as conn, conn.transaction():
        await authorize_operation(conn, attempt['id'], now=deadline - timedelta(milliseconds=1))
        await repo._authorized_attempt(conn, 'caller-a', attempt['id'], now=deadline - timedelta(milliseconds=1))
    for guard in (lambda conn: authorize_operation(conn, attempt['id'], now=deadline),
                  lambda conn: repo._authorized_attempt(conn, 'caller-a', attempt['id'], now=deadline)):
        async with hub.store.pool.acquire() as conn, conn.transaction():
            with pytest.raises(ExecutionError) as err:
                await guard(conn)
            assert err.value.status == 403
    body = raw['execution']
    deny = hub.deny(raw, {'kind': 'cancelled'}, activation_nonce=body['initial_authorization']['activation_nonce'])
    denied = await hub.client.put(f"/v1/executions/by-key/{body['idempotency_key']}/authorization", json=deny)
    assert denied.status_code == 200 and denied.json()['terminal'] is None
    assert denied.json()['admission_closed'] and denied.json()['receipts'] == []
    for guard in (lambda conn: authorize_operation(conn, attempt['id'], now=deadline - timedelta(milliseconds=1)),
                  lambda conn: repo._authorized_attempt(conn, 'caller-a', attempt['id'], now=deadline - timedelta(milliseconds=1))):
        async with hub.store.pool.acquire() as conn, conn.transaction():
            with pytest.raises(ExecutionError):
                await guard(conn)


@pytest.mark.asyncio
async def test_non_agent_execution_guard_is_unchanged(hub):
    from kotoyose.execution_operation_guard import authorize_operation
    from kotoyose.client_tool_bridge import ClientToolRepository
    from kotoyose.execution_resources import ExecutionError
    from tests.test_execution_store import _admit_ready

    class RecordingConnection:
        def __init__(self, conn):
            self.conn, self.queries = conn, []

        async def fetchrow(self, query, *args):
            self.queries.append(query)
            return await self.conn.fetchrow(query, *args)

        async def fetchval(self, query, *args):
            self.queries.append(query)
            return await self.conn.fetchval(query, *args)

    repo = ClientToolRepository(hub.store.pool)
    for kind in ('command', 'agent'):
        root = hub.root / kind
        root.mkdir()
        principal = 'caller-' + kind
        _, refs, request, snapshot = await _admit_ready(hub.store, root, principal=principal)
        request['kind'] = kind
        attempt, _ = await hub.store.admit(principal, request, snapshot, 1)
        await hub.store.pool.execute("UPDATE kotoyose.execution_attempts SET status='running' WHERE id=$1", attempt['id'])
        async with hub.store.pool.acquire() as conn, conn.transaction():
            recording = RecordingConnection(conn)
            await authorize_operation(recording, attempt['id'])
            await repo._authorized_attempt(recording, principal, attempt['id'])
            assert all('agent_execution' not in query for query in recording.queries)
        await hub.store.pool.execute("UPDATE kotoyose.execution_resources SET payload=jsonb_set(payload,'{expires_at}',to_jsonb((now()-interval '1 second')::text)) WHERE ref=$1", refs['grant'])
        for guard in (lambda conn: authorize_operation(conn, attempt['id']),
                      lambda conn: repo._authorized_attempt(conn, principal, attempt['id'])):
            async with hub.store.pool.acquire() as conn, conn.transaction():
                with pytest.raises(ExecutionError):
                    await guard(conn)
        await hub.store.pool.execute("UPDATE kotoyose.execution_resources SET payload=jsonb_set(payload,'{expires_at}',to_jsonb((now()+interval '1 hour')::text)),revoked_at=now() WHERE ref=$1", refs['grant'])
        for guard in (lambda conn: authorize_operation(conn, attempt['id']),
                      lambda conn: repo._authorized_attempt(conn, principal, attempt['id'])):
            async with hub.store.pool.acquire() as conn, conn.transaction():
                with pytest.raises(ExecutionError):
                    await guard(conn)


@pytest.mark.asyncio
async def test_fresh_allow_unblocks_expired_initial(hub):
    from kotoyose.agent_execution_digest import agent_utc
    raw = hub.create_body(await hub.register_work())
    old = datetime.now(timezone.utc) - timedelta(minutes=2)
    allow = raw['execution']['initial_authorization']
    allow.update(issued_at=agent_utc(old), not_before=agent_utc(old), expires_at=agent_utc(old + timedelta(seconds=30)))
    assert (await hub.client.post('/v1/executions', json=raw)).status_code == 202
    now = datetime.now(timezone.utc)
    renewed = {**allow, 'revision': 2, 'issued_at': agent_utc(now), 'not_before': agent_utc(now), 'expires_at': agent_utc(now + timedelta(seconds=30))}
    response = await hub.client.put(f"/v1/executions/by-key/{raw['execution']['idempotency_key']}/authorization", json=renewed)
    assert response.status_code == 200 and not response.json()['activation']['authority_required']


@pytest.mark.asyncio
async def test_lease_and_charge_bounds(hub):
    from kotoyose.agent_execution_digest import agent_utc, parse_agent_utc
    raw = hub.create_body(await hub.register_work())
    assert (await hub.client.post('/v1/executions', json=raw)).status_code == 202
    body = raw['execution']
    allow = body['initial_authorization']
    path = f"/v1/executions/by-key/{body['idempotency_key']}/authorization"
    issued = parse_agent_utc(allow['issued_at'])
    for change, code in [({'expires_at': agent_utc(issued + timedelta(seconds=31, milliseconds=1))}, 422),
                         ({'charge': None}, 422),
                         ({'charge': {**allow['charge'], 'allocation_units': '2'}}, 422),
                         ({'charge': {**allow['charge'], 'charge_ref': 'other'}}, 409)]:
        response = await hub.client.put(path, json={**allow, 'revision': 2, **change})
        assert response.status_code == code, response.text
    response = await hub.client.put(path, json={**allow, 'revision': 2, 'expires_at': agent_utc(issued + timedelta(seconds=31))})
    assert response.status_code == 200, response.text

    ceiling = issued + timedelta(seconds=20)
    bounded = hub.create_body(await hub.register_work(), authority_ceilings=[
        {'kind': 'human_repair', 'evidence_ref': 'evidence', 'expires_at': agent_utc(ceiling)}])
    auth = bounded['execution']['initial_authorization']
    auth.update(issued_at=agent_utc(issued), not_before=agent_utc(issued), expires_at=agent_utc(ceiling - timedelta(seconds=1)),
                authority_ceiling_at=agent_utc(ceiling))
    for change in ({'authority_ceiling_at': None}, {'authority_ceiling_at': agent_utc(ceiling + timedelta(milliseconds=1))},
                   {'expires_at': agent_utc(ceiling - timedelta(seconds=1) + timedelta(milliseconds=1))}):
        candidate = {**bounded, 'execution': {**bounded['execution'], 'initial_authorization': {**auth, **change}}}
        assert (await hub.client.post('/v1/executions', json=candidate)).status_code == 422
    assert (await hub.client.post('/v1/executions', json=bounded)).status_code == 202
    bounded_path = f"/v1/executions/by-key/{bounded['execution']['idempotency_key']}/authorization"
    assert (await hub.client.put(bounded_path, json={**auth, 'revision': 2})).status_code == 200
    assert (await hub.client.put(bounded_path, json={**auth, 'revision': 3,
        'expires_at': agent_utc(ceiling - timedelta(seconds=1) + timedelta(milliseconds=1))})).status_code == 422

    unbudgeted = hub.create_body(await hub.register_work(), money={'mode': 'unbudgeted'})
    assert (await hub.client.post('/v1/executions', json=unbudgeted)).status_code == 422
    unbudgeted['execution']['initial_authorization']['charge'] = None
    assert (await hub.client.post('/v1/executions', json=unbudgeted)).status_code == 202
    unbudgeted_path = f"/v1/executions/by-key/{unbudgeted['execution']['idempotency_key']}/authorization"
    assert (await hub.client.put(unbudgeted_path, json={**unbudgeted['execution']['initial_authorization'],
        'revision': 2, 'charge': allow['charge']})).status_code == 422

    from uuid import UUID
    execution_id = UUID((await hub.client.get(f"/v1/executions/by-key/{body['idempotency_key']}")).json()['execution_id'])
    async with hub.store.pool.acquire() as conn, conn.transaction():
        for ordinal in (2, 3):
            await conn.execute("INSERT INTO kotoyose.agent_execution_charge_units(execution_id,unit_ordinal,charge_ref,reservation_ref,allocation_units,state) VALUES($1,$2,$3,$4,$5,'open')",
                               execution_id, ordinal, f'old-charge-{ordinal}', f'old-reservation-{ordinal}', allow['charge']['allocation_units'])
        await conn.execute("INSERT INTO kotoyose.agent_execution_charge_units(execution_id,unit_ordinal,state) VALUES($1,4,'open')", execution_id)
        await conn.execute("INSERT INTO kotoyose.agent_execution_activations(execution_id,nonce,ordinal,reason,unit_ordinal,state,launch,measurement) VALUES($1,'next',2,'fault_retry',4,'awaiting','not_started','none')", execution_id)
        await conn.execute("UPDATE kotoyose.agent_executions SET current_nonce='next' WHERE id=$1", execution_id)
    fresh = {**allow, 'revision': 3, 'activation_nonce': 'next',
             'charge': {**allow['charge'], 'charge_ref': 'new-charge', 'reservation_ref': 'new-reservation'}}
    result = await hub.client.put(path, json=fresh)
    assert result.status_code == 409 and result.json()['error']['code'] == 'request_conflict'


@pytest.mark.asyncio
async def test_same_nonce_has_independent_execution_leases(hub):
    from kotoyose.agent_deadlines import AgentStartRejected

    bodies = [hub.create_body(await hub.register_work()) for _ in range(2)]
    for raw in bodies:
        raw['execution']['initial_authorization']['activation_nonce'] = 'shared-nonce'
    responses = [await hub.client.post('/v1/executions', json=raw) for raw in bodies]
    assert all(r.status_code == 202 for r in responses)
    first, second = [(r.json()['execution_id'], 'shared-nonce') for r in responses]
    now = datetime.now(timezone.utc)
    hub.agent_executions.clock = lambda: now
    hub.agent_executions.leases.clock = hub.agent_executions.clock
    deadline = hub.agent_executions.leases.call_deadline(second)
    hub.agent_executions.leases.assert_startable(first)
    hub.agent_executions.leases.assert_startable(second)
    body = bodies[0]['execution']
    path = f"/v1/executions/by-key/{body['idempotency_key']}/authorization"
    allow = {**body['initial_authorization'], 'revision': 2, 'allowed_active_ms': 0}
    assert (await hub.client.put(path, json=allow)).status_code == 200
    with pytest.raises(AgentStartRejected):
        hub.agent_executions.leases.assert_startable(first)
    hub.agent_executions.leases.assert_startable(second)
    assert hub.agent_executions.leases.call_deadline(second) == deadline
    deny = hub.deny(bodies[0], {'kind': 'cancelled'}, revision=3, activation_nonce='shared-nonce')
    assert (await hub.client.put(path, json=deny)).status_code == 200
    with pytest.raises(AgentStartRejected):
        hub.agent_executions.leases.assert_startable(first)
    hub.agent_executions.leases.assert_startable(second)
    assert hub.agent_executions.leases.call_deadline(second) == deadline
