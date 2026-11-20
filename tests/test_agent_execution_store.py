import asyncio
import copy
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest

from kotoyose.agent_execution_digest import agent_utc, agent_work_digest
from tests.fixtures.agent_execution import isolated_agent_execution_harness


@pytest.fixture
async def hub(tmp_path, monkeypatch):
    async with isolated_agent_execution_harness(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL'), tmp_path, monkeypatch) as kotoyose:
        yield kotoyose


def redigest(raw):
    body = raw['execution']
    body['work_digest'] = agent_work_digest(raw)
    body['initial_authorization']['work_digest'] = body['work_digest']


@pytest.mark.asyncio
async def test_create_is_idempotent_under_race(hub):
    raw = hub.create_body(await hub.register_work())
    responses = await asyncio.gather(*(hub.client.post('/v1/executions', json=raw) for _ in range(4)))
    assert [x.status_code for x in responses] == [202] * 4, [x.text for x in responses]
    assert all(x.json() == responses[0].json() for x in responses)
    for table, count in [('agent_executions', 1), ('agent_execution_activations', 1), ('agent_execution_charge_units', 1),
                         ('agent_execution_authorizations', 1), ('execution_attempts', 0)]:
        assert await hub.store.pool.fetchval(f'SELECT count(*) FROM kotoyose.{table}') == count
    plan = await hub.store.pool.fetchval('SELECT pinned_plan FROM kotoyose.agent_executions')
    assert raw['execution']['work']['resource_refs'][0] in plan


@pytest.mark.asyncio
async def test_same_key_with_other_body_conflicts(hub):
    raw = hub.create_body(await hub.register_work())
    assert (await hub.client.post('/v1/executions', json=raw)).status_code == 202
    other = copy.deepcopy(raw)
    other['execution']['initial_authorization']['activation_nonce'] = 'another'
    result = await hub.client.post('/v1/executions', json=other)
    assert result.status_code == 409 and result.json()['error']['current']['idempotency_key'] == raw['execution']['idempotency_key']
    key = str(uuid4())
    await hub.agent_executions.store.close_key('caller-a', key, {'action_key': 'cancel', 'reason': 'owner_abort'}, datetime.now(timezone.utc))
    other = copy.deepcopy(raw)
    other['execution']['idempotency_key'] = key
    result = await hub.client.post('/v1/executions', json=other)
    assert result.status_code == 409 and result.json()['error']['current'] is None
    for i in range(2):
        successor = copy.deepcopy(raw)
        successor['execution']['idempotency_key'] = str(uuid4())
        successor['execution']['predecessor'] = {'idempotency_key': 'prior', 'execution_id': 'prior-id', 'relation': 'rerun'}
        redigest(successor)
        response = await hub.client.post('/v1/executions', json=successor)
        assert response.status_code == (202 if i == 0 else 409), response.text


@pytest.mark.asyncio
async def test_create_validates_before_storing(hub):
    refs = await hub.register_work()
    raw = hub.create_body(refs)
    variants = []
    other = copy.deepcopy(raw); other['execution']['work']['policy_ref'] = 'other'; redigest(other); variants.append((other, 403, 'permission_denied'))
    other = copy.deepcopy(raw); other['execution']['work']['resource_refs'] = ['outside']; redigest(other); variants.append((other, 403, 'permission_denied'))
    other = copy.deepcopy(raw); other['execution']['work']['resource_refs'] = []; redigest(other); variants.append((other, 422, 'invalid_request'))
    other = copy.deepcopy(raw); other['execution']['idempotency_key'] = 'a/b'; variants.append((other, 422, 'invalid_request'))
    other = copy.deepcopy(raw); other['execution']['work_digest'] = '0' * 64; variants.append((other, 422, 'invalid_request'))
    other = copy.deepcopy(raw); other['execution']['work']['requirements']['capabilities'] = ['missing']; redigest(other); variants.append((other, 501, 'unsupported_capability'))
    other = copy.deepcopy(raw); other['execution']['work']['workspace_ref'] = refs['workspace'].rsplit(':', 1)[0] + ':999'; redigest(other); variants.append((other, 409, 'request_conflict'))
    for value, status, code in variants:
        response = await hub.client.post('/v1/executions', json=value)
        assert response.status_code == status and response.json()['error']['code'] == code, response.text
    assert await hub.store.pool.fetchval('SELECT count(*) FROM kotoyose.agent_executions') == 0
    now = datetime.now(timezone.utc) - timedelta(minutes=2)
    raw['execution']['initial_authorization'].update(issued_at=agent_utc(now), not_before=agent_utc(now), expires_at=agent_utc(now + timedelta(seconds=30)))
    response = await hub.client.post('/v1/executions', json=raw)
    assert response.status_code == 202, response.text
    snapshot = (await hub.client.get('/v1/executions/by-key/' + raw['execution']['idempotency_key'])).json()
    assert snapshot['state'] == 'queued' and snapshot['activation']['authority_required'] is True and snapshot['workspace_ref'] is None


@pytest.mark.asyncio
async def test_receipts_page_by_cursor(hub):
    raw = hub.create_body(await hub.register_work())
    result = await hub.client.post('/v1/executions', json=raw)
    assert result.status_code == 202, result.text
    eid = UUID(result.json()['execution_id'])
    receipt = {'charge_ref': None, 'reservation_ref': None, 'allocation_units': None, 'activation_nonces': [],
               'outcome': 'not_started_closed', 'units': '0', 'active_ms': 0, 'turns': 0, 'output_bytes': 0,
               'execution_confirmed': False, 'stop_confirmation': 'confirmed', 'side_effect_resolution': 'resolved',
               'finished_at': agent_utc(datetime.now(timezone.utc))}
    async with hub.store.pool.acquire() as conn, conn.transaction():
        for i in range(101):
            await hub.agent_executions.store.append_receipt(conn, eid, i + 1, receipt)
    key = raw['execution']['idempotency_key']
    first = (await hub.client.get('/v1/executions/by-key/' + key)).json()
    second = (await hub.client.get('/v1/executions/by-key/' + key, params={'after_receipt': 100})).json()
    assert len(first['receipts']) == 100 and first['next_receipt_cursor'] == 100 and first['final_receipt_cursor'] == 101
    assert len(second['receipts']) == 1 and second['next_receipt_cursor'] == 101
    assert (await hub.client_b.get('/v1/executions/by-key/' + key)).status_code == 404
    assert (await hub.client.get('/v1/executions/by-key/' + key, params={'after_receipt': -1})).json()['error']['code'] == 'invalid_request'
    for part in ('receipts', 'next_receipt_cursor'):
        first.pop(part); second.pop(part)
    assert first == second
    from tests.test_execution_store import _admit_ready
    artifact_root = hub.root / 'artifact-fixture'
    artifact_root.mkdir()
    _, _, attempt_request, attempt_plan = await _admit_ready(hub.store, artifact_root)
    attempt, _ = await hub.store.admit('caller-a', attempt_request, attempt_plan, 1)
    await hub.store.finish(attempt['id'], 'succeeded', 'done', 'confirmed', 0, {'result': b'answer'})
    await hub.store.pool.execute("UPDATE kotoyose.agent_execution_activations SET attempt_id=$2,state='ended',launch='started',measurement='complete',turns=1,output_bytes=6 WHERE execution_id=$1", eid, attempt['id'])
    snapshot = (await hub.client.get('/v1/executions/by-key/' + key)).json()
    assert snapshot['artifacts'][0]['name'] == 'result' and snapshot['artifacts'][0]['size'] == 6
    for command in ('UPDATE kotoyose.agent_execution_receipts SET digest=digest', 'DELETE FROM kotoyose.agent_execution_receipts'):
        with pytest.raises(asyncpg.RaiseError):
            await hub.store.pool.execute(command)


@pytest.mark.asyncio
async def test_input_pin_is_atomic_and_replay_does_not_increment(hub):
    from kotoyose.agent_execution_contract import AgentExecutionError
    from kotoyose.agent_execution_digest import agent_request_digest

    refs = await hub.register_work()
    raw = hub.create_body(refs)
    ref = 'artifact:input:pin-test'
    raw['execution']['work']['inputs'][0]['content_ref'] = ref
    redigest(raw)
    workspace = await hub.store.workspace('caller-a', refs['workspace'])
    plan = {'workspace_id': workspace['id'], 'generation': workspace['generation']}
    args = {'request_digest': agent_request_digest(raw), 'work_digest': raw['execution']['work_digest'],
            'pinned_plan': plan, 'now': datetime.now(timezone.utc)}
    with pytest.raises(AgentExecutionError):
        await hub.agent_executions.store.create('caller-a', raw, **args)
    assert await hub.store.pool.fetchval('SELECT count(*) FROM kotoyose.agent_executions') == 0
    await hub.store.pool.execute('''INSERT INTO kotoyose.execution_input_artifacts
        (ref,principal,digest,content,media_type,byte_size,expires_at,state)
        VALUES($1,'caller-a',$2,$3,'image/png',3,$4,'active')''',
        ref, 'a' * 64, b'png', datetime.now(timezone.utc) + timedelta(minutes=1))
    first, created = await hub.agent_executions.store.create('caller-a', raw, **args)
    again, replay_created = await hub.agent_executions.store.create('caller-a', raw, **args)
    assert created and not replay_created and first['id'] == again['id']
    assert await hub.store.pool.fetchval('SELECT pin_count FROM kotoyose.execution_input_artifacts WHERE ref=$1', ref) == 1
    assert await hub.store.pool.fetchval('SELECT count(*) FROM kotoyose.agent_execution_input_pins') == 1


@pytest.mark.asyncio
async def test_initialize_agent_execution_schema_migrates_legacy_hub_tables(hub):
    from kotoyose.agent_execution_schema import initialize_agent_execution_schema

    pool = hub.store.pool
    await pool.execute("""
        DROP TRIGGER IF EXISTS agent_execution_receipts_immutable ON kotoyose.agent_execution_receipts;
        DROP TRIGGER IF EXISTS agent_execution_authorizations_immutable ON kotoyose.agent_execution_authorizations;
        DROP TRIGGER IF EXISTS agent_execution_cancellations_immutable ON kotoyose.agent_execution_cancellations;
        DROP TABLE IF EXISTS kotoyose.agent_execution_client_replays, kotoyose.agent_execution_input_pins,
            kotoyose.agent_execution_cancellations, kotoyose.agent_execution_actions,
            kotoyose.agent_execution_authorizations, kotoyose.agent_execution_receipts,
            kotoyose.agent_execution_charge_units, kotoyose.agent_execution_activations,
            kotoyose.agent_execution_snapshot_digests, kotoyose.agent_executions CASCADE;
        CREATE TABLE kotoyose.hub_executions (id uuid PRIMARY KEY, principal text NOT NULL, idempotency_key text NOT NULL,
            request_digest text NOT NULL, work_digest text NOT NULL, caller_run_ref text NOT NULL, request jsonb NOT NULL,
            pinned_plan jsonb NOT NULL, predecessor_execution_id text, predecessor_key text, predecessor_relation text,
            state text NOT NULL, state_revision bigint NOT NULL, sequence bigint NOT NULL, current_nonce text NOT NULL,
            authorization_revision bigint NOT NULL, settled_receipt_cursor bigint NOT NULL, fault_retries_used integer NOT NULL,
            cancel_requested boolean NOT NULL, admission_closed boolean NOT NULL, authority_required boolean NOT NULL,
            pause jsonb, provider_limit jsonb, next_probe_at timestamptz, probe_step integer NOT NULL DEFAULT 0,
            last_try_now_at timestamptz, terminal jsonb, workspace_id text NOT NULL, workspace_generation bigint NOT NULL,
            workspace_ref text, created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL, UNIQUE (principal,idempotency_key));
        CREATE UNIQUE INDEX hub_predecessor_once ON kotoyose.hub_executions(principal,predecessor_execution_id) WHERE predecessor_execution_id IS NOT NULL;
        CREATE TABLE kotoyose.hub_execution_snapshot_digests (execution_id uuid PRIMARY KEY REFERENCES kotoyose.hub_executions(id), digest text NOT NULL, sequence bigint NOT NULL);
        CREATE TABLE kotoyose.hub_execution_activations (execution_id uuid NOT NULL REFERENCES kotoyose.hub_executions(id), nonce text NOT NULL, ordinal bigint NOT NULL, reason text NOT NULL, unit_ordinal bigint NOT NULL, state text NOT NULL, launch text NOT NULL, measurement text NOT NULL, PRIMARY KEY (execution_id,nonce));
        CREATE TABLE kotoyose.hub_execution_charge_units (execution_id uuid NOT NULL REFERENCES kotoyose.hub_executions(id), unit_ordinal bigint NOT NULL, state text NOT NULL, PRIMARY KEY (execution_id,unit_ordinal));
        CREATE TABLE kotoyose.hub_execution_receipts (execution_id uuid NOT NULL REFERENCES kotoyose.hub_executions(id), cursor bigint NOT NULL, receipt_id text NOT NULL UNIQUE, unit_ordinal bigint NOT NULL, body jsonb NOT NULL, digest text NOT NULL, PRIMARY KEY (execution_id,cursor));
        CREATE TABLE kotoyose.hub_execution_authorizations (execution_id uuid NOT NULL REFERENCES kotoyose.hub_executions(id), revision bigint NOT NULL, nonce text NOT NULL, decision text NOT NULL, body jsonb NOT NULL, digest text NOT NULL, PRIMARY KEY (execution_id,revision));
        CREATE TABLE kotoyose.hub_execution_actions (execution_id uuid NOT NULL REFERENCES kotoyose.hub_executions(id), action_key text NOT NULL, kind text NOT NULL, body_digest text NOT NULL, created_at timestamptz NOT NULL, PRIMARY KEY (execution_id,action_key));
        CREATE TABLE kotoyose.hub_execution_cancellations (principal text NOT NULL, idempotency_key text NOT NULL, action_key text NOT NULL, reason text NOT NULL, proof_ref text NOT NULL UNIQUE, created_at timestamptz NOT NULL, PRIMARY KEY (principal,idempotency_key));
        CREATE TABLE kotoyose.hub_execution_input_pins (execution_id uuid NOT NULL REFERENCES kotoyose.hub_executions(id), artifact_ref text NOT NULL, principal text NOT NULL, released_at timestamptz, PRIMARY KEY (execution_id,artifact_ref));
        CREATE TABLE kotoyose.hub_execution_client_replays (execution_id uuid NOT NULL REFERENCES kotoyose.hub_executions(id), call_id text NOT NULL, tool_ref text NOT NULL, arguments_digest text NOT NULL, PRIMARY KEY (execution_id,call_id));
        CREATE FUNCTION kotoyose.reject_hub_immutable_mutation() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'hub execution record is immutable'; END; $$ LANGUAGE plpgsql;
        CREATE TRIGGER hub_execution_receipts_immutable BEFORE UPDATE OR DELETE ON kotoyose.hub_execution_receipts FOR EACH ROW EXECUTE FUNCTION kotoyose.reject_hub_immutable_mutation();
        INSERT INTO kotoyose.hub_executions VALUES ('00000000-0000-0000-0000-000000000001','caller-a','k-old','a','b','run:1','{}','{}',NULL,NULL,NULL,'terminal',1,1,'n',1,0,0,false,true,false,NULL,NULL,NULL,0,NULL,'{"kind":"succeeded"}','w',1,NULL,now(),now());
    """)
    await initialize_agent_execution_schema(pool)
    await initialize_agent_execution_schema(pool)
    assert await pool.fetchval("SELECT to_regclass('kotoyose.hub_executions')") is None
    assert await pool.fetchval("SELECT count(*) FROM kotoyose.agent_executions WHERE idempotency_key='k-old'") == 1
