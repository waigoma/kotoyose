"""管理隔離は元の証拠・writer を保持し、Caller が自分で作成できない。"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kotoyose.admission_quarantine import count_admission_reservations, seal_admission_quarantine
from kotoyose.codex_stop_evidence import STAGES, StopComponents, make_stop_evidence, stop_evidence_context
from kotoyose.execution_resources import ExecutionError, ResourceEnvelope
from tests.test_codex_stop_store import journal as journal


async def terminal(journal):
    store, attempt, operation, _ = journal
    saved = next(x for x in await store.operations(attempt['id']) if x['id'] == operation)
    proof = make_stop_evidence(stop_evidence_context(attempt), saved['result'],
        StopComponents(**{**dict.fromkeys(STAGES, 'confirmed'), 'scope': 'unconfirmed'}), 0)
    await store.record_codex_stop(operation, proof.model_dump())
    await store.finish(attempt['id'], 'worker_lost', 'worker_lost', 'unconfirmed', None, {})
    row = await store.pool.fetchrow("SELECT request_digest,encode(sha256(convert_to(snapshot::text,'UTF8')),'hex') AS snapshot_digest "
                                    "FROM kotoyose.execution_attempts WHERE id=$1", attempt['id'])
    args = dict(request_digest=row['request_digest'], snapshot_digest=row['snapshot_digest'], proof_digest=proof.digest,
                authorization_ref='test-owned-approval', expires_at=datetime.now(timezone.utc)+timedelta(hours=1))
    return store, attempt, operation, args


async def original_rows(store, attempt):
    result = {}
    for table, where, value in [('execution_attempts','id=$1',attempt['id']),
            ('execution_operations','attempt_id=$1',attempt['id']),('execution_events','attempt_id=$1',attempt['id']),
            ('execution_workspaces','id=$1',attempt['workspace_id'])]:
        result[table] = await store.pool.fetchval(f"SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text)::text,'[]') "
                                                 f"FROM kotoyose.{table} t WHERE {where}", value)
    return result


async def test_quarantine_is_idempotent_and_preserves_all_original_rows_and_budget(journal):
    store, attempt, _, args = await terminal(journal)
    before = await original_rows(store, attempt)
    resources = await store.pool.fetchval("SELECT jsonb_agg(payload ORDER BY ref)::text FROM kotoyose.execution_resources "
                                         "WHERE payload->>'kind'='budget'")
    assert await count_admission_reservations(store.pool, 'caller-a') == 1
    ref = await seal_admission_quarantine(store.pool, 'caller-a', attempt['id'], **args)
    assert await seal_admission_quarantine(store.pool, 'caller-a', attempt['id'], **args) == ref
    assert await count_admission_reservations(store.pool, 'caller-a') == 0
    with pytest.raises(ExecutionError):
        await store.authorize_provider_request(attempt['id'],'caller-a')
    assert await original_rows(store, attempt) == before
    assert resources == await store.pool.fetchval("SELECT jsonb_agg(payload ORDER BY ref)::text FROM kotoyose.execution_resources "
                                                  "WHERE payload->>'kind'='budget'")
    sealed = json.loads(await store.pool.fetchval('SELECT payload FROM kotoyose.execution_admission_quarantines WHERE id=$1', ref))
    with pytest.raises(ValidationError):
        ResourceEnvelope.model_validate({'resource':sealed})
    assert await count_admission_reservations(store.pool, 'other-caller') == 0


@pytest.mark.parametrize('drift', ['principal','request','snapshot','proof','writer','generation','running','external','expired'])
async def test_wrong_or_live_target_never_gets_an_exclusion(journal, drift):
    store, attempt, operation, args = await terminal(journal)
    principal = 'caller-a'
    if drift == 'principal':principal = 'other-caller'
    elif drift in ['request','snapshot','proof']:args[drift+'_digest'] = '0'*64
    elif drift == 'writer':await store.pool.execute('UPDATE kotoyose.execution_workspaces SET writer_attempt=NULL WHERE id=$1',attempt['workspace_id'])
    elif drift == 'generation':await store.pool.execute('UPDATE kotoyose.execution_workspaces SET generation=1 WHERE id=$1',attempt['workspace_id'])
    elif drift == 'running':await store.pool.execute("UPDATE kotoyose.execution_attempts SET finished_at=NULL,status='running' WHERE id=$1",attempt['id'])
    elif drift == 'external':await store.pool.execute("INSERT INTO kotoyose.execution_operations(id,attempt_id,kind,state) VALUES($1,$2,'client_tool','unknown')",str(uuid4()),attempt['id'])
    elif drift == 'expired':args['expires_at'] = datetime.now(timezone.utc)-timedelta(seconds=1)
    with pytest.raises(ExecutionError):await seal_admission_quarantine(store.pool,principal,attempt['id'],**args)
    assert await store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_admission_quarantines") == 0


@pytest.mark.parametrize('drift', ['request','snapshot','proof','writer','generation','expired','revoked'])
async def test_post_seal_drift_restores_reservation_count(journal, drift):
    store, attempt, operation, args = await terminal(journal)
    ref = await seal_admission_quarantine(store.pool,'caller-a',attempt['id'],**args)
    assert await count_admission_reservations(store.pool,'caller-a') == 0
    if drift == 'request':await store.pool.execute("UPDATE kotoyose.execution_attempts SET request_digest=$2 WHERE id=$1",attempt['id'],'0'*64)
    elif drift == 'snapshot':await store.pool.execute("UPDATE kotoyose.execution_attempts SET snapshot=snapshot||'{\"changed\":true}'::jsonb WHERE id=$1",attempt['id'])
    elif drift == 'proof':await store.pool.execute("UPDATE kotoyose.execution_operations SET result=jsonb_set(result,'{codex_stop_evidence,digest}',to_jsonb($2::text)) WHERE id=$1",operation,'0'*64)
    elif drift == 'writer':await store.pool.execute('UPDATE kotoyose.execution_workspaces SET writer_attempt=NULL WHERE id=$1',attempt['workspace_id'])
    elif drift == 'generation':await store.pool.execute('UPDATE kotoyose.execution_workspaces SET generation=1 WHERE id=$1',attempt['workspace_id'])
    elif drift == 'expired':await store.pool.execute("UPDATE kotoyose.execution_admission_quarantines SET payload=jsonb_set(payload,'{expires_at}',to_jsonb((now()-interval '1 second')::text)) WHERE id=$1",ref)
    elif drift == 'revoked':await store.pool.execute('UPDATE kotoyose.execution_admission_quarantines SET revoked_at=now() WHERE id=$1',ref)
    assert await count_admission_reservations(store.pool,'caller-a') == 1
    if drift == 'revoked':
        with pytest.raises(ExecutionError):await seal_admission_quarantine(store.pool,'caller-a',attempt['id'],**args)


async def test_exclusion_does_not_raise_the_new_admission_limit(journal,tmp_path):
    store, attempt, _, args = await terminal(journal)
    await seal_admission_quarantine(store.pool,'caller-a',attempt['id'],**args)
    original = await store.attempt('caller-a',attempt['id']);request = dict(original['request']);snapshot = dict(original['snapshot'])
    async def new_request(index):
        path = tmp_path/('fresh-'+str(index));path.mkdir()
        ws = await store.register_workspace('caller-a','fresh-'+str(index),path)
        req = {**request,'idempotency_key':'new-'+str(index),'workspace_ref':f"workspace:{ws['id']}:0"}
        snap = {**snapshot,'workspace_id':ws['id'],'generation':0,'workspace_path':str(path)}
        return await store.admit('caller-a',req,snap,4)
    for index in range(3):
        held, _ = await new_request(index)
        await store.finish(held['id'],'cancelled','cancelled','unconfirmed',None,{})
    assert await count_admission_reservations(store.pool,'caller-a') == 3
    results = await asyncio.gather(new_request(3),new_request(4),return_exceptions=True)
    assert sum(not isinstance(x,Exception) for x in results) == 1
    assert sum(isinstance(x,ExecutionError) for x in results) == 1
    assert await count_admission_reservations(store.pool,'caller-a') == 4
    held = await store.attempt('caller-a',attempt['id'])
    assert held['stop_confirmation']=='unconfirmed' and held['side_effect_resolution']=='unresolved'


async def test_rechecking_unknown_does_not_rewrite_historical_operation_timestamp(journal):
    store, attempt, operation, _ = await terminal(journal)
    await store.resolve_operation(operation,'unconfirmed')
    before = await store.pool.fetchrow('SELECT * FROM kotoyose.execution_operations WHERE id=$1',operation)
    await store.resolve_operation(operation,'unconfirmed')
    after = await store.pool.fetchrow('SELECT * FROM kotoyose.execution_operations WHERE id=$1',operation)
    assert before == after


async def wait_past_expiry_while_caller_locked(store, expires_at, operation):
    task = None
    async with store.pool.acquire() as blocker:
        await blocker.execute("SELECT pg_advisory_lock(hashtextextended($1,0))",'kotoyose-caller:caller-a')
        try:
            task = asyncio.create_task(operation())
            async with asyncio.timeout(3):
                while not await blocker.fetchval("SELECT EXISTS(SELECT 1 FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
                                                   "WHERE l.locktype='advisory' AND NOT l.granted AND a.datname=current_database())"):
                    await asyncio.sleep(.005)
            assert await blocker.fetchval('SELECT clock_timestamp()') < expires_at
            await asyncio.sleep(max(0,(expires_at-datetime.now(timezone.utc)).total_seconds())+.03)
            assert await blocker.fetchval('SELECT clock_timestamp()') > expires_at
        finally:
            await blocker.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))",'kotoyose-caller:caller-a')
    assert task is not None
    return await task


async def test_admission_waiting_across_expiry_counts_the_original_reservation(journal,tmp_path):
    store, attempt, _, args = await terminal(journal)
    args['expires_at'] = datetime.now(timezone.utc)+timedelta(seconds=1)
    await seal_admission_quarantine(store.pool,'caller-a',attempt['id'],**args)
    path = tmp_path/'expiry-fresh';path.mkdir()
    ws = await store.register_workspace('caller-a','expiry-fresh',path)
    original = await store.attempt('caller-a',attempt['id'])
    request = {**original['request'],'idempotency_key':'after-expiry','workspace_ref':f"workspace:{ws['id']}:0"}
    snapshot = {**original['snapshot'],'workspace_id':ws['id'],'generation':0,'workspace_path':str(path)}
    with pytest.raises(ExecutionError,match='concurrency reservation'):
        await wait_past_expiry_while_caller_locked(store,args['expires_at'],
            lambda:store.admit('caller-a',request,snapshot,1))
    assert await store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts WHERE idempotency_key='after-expiry'") == 0


async def test_sealing_waiting_across_expiry_does_not_write_an_audit(journal):
    store, attempt, _, args = await terminal(journal)
    args['expires_at'] = datetime.now(timezone.utc)+timedelta(seconds=1)
    with pytest.raises(ExecutionError,match='expired'):
        await wait_past_expiry_while_caller_locked(store,args['expires_at'],
            lambda:seal_admission_quarantine(store.pool,'caller-a',attempt['id'],**args))
    assert await store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_admission_quarantines') == 0


@pytest.mark.parametrize('kind',['client_tool','mcp_tool','sandbox_command'])
async def test_external_operation_downgrade_after_sealing_restores_reservation(journal,kind):
    store, attempt, _, args = await terminal(journal)
    external = str(uuid4())
    await store.pool.execute("INSERT INTO kotoyose.execution_operations(id,attempt_id,kind,state) VALUES($1,$2,$3,'confirmed')",
                             external,attempt['id'],kind)
    await seal_admission_quarantine(store.pool,'caller-a',attempt['id'],**args)
    assert await count_admission_reservations(store.pool,'caller-a') == 0
    await store.resolve_operation(external,'unconfirmed')
    assert await count_admission_reservations(store.pool,'caller-a') == 1
