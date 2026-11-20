"""期限切れ隔離の追記式延長は原停止証拠や会計を変更しない。"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kotoyose.admission_quarantine import count_admission_reservations, seal_admission_quarantine
from kotoyose.admission_quarantine_extensions import extend_admission_quarantine
from kotoyose.execution_resources import ExecutionError, ResourceEnvelope
from tests.test_admission_quarantine import original_rows, terminal, wait_past_expiry_while_caller_locked
from tests.test_codex_stop_store import journal as journal


async def expired_seal(journal):
    store, attempt, operation, args = await terminal(journal)
    args['expires_at'] = datetime.now(timezone.utc) + timedelta(seconds=1)
    ref = await seal_admission_quarantine(store.pool, 'caller-a', attempt['id'], **args)
    await asyncio.sleep(max(0, (args['expires_at']-datetime.now(timezone.utc)).total_seconds())+.02)
    assert await count_admission_reservations(store.pool, 'caller-a') == 1
    return store, attempt, operation, args, ref


def extension_args(ref, expiry):
    return dict(request_id=str(uuid4()), predecessor_ref=ref, expected_expires_at=expiry,
                expires_at=datetime.now(timezone.utc)+timedelta(hours=1), authorization_ref='test-explicit-extension')


async def test_expired_quarantine_extension_is_append_only_and_concurrent_replay_is_one_record(journal):
    store, attempt, _, args, ref = await expired_seal(journal)
    before = await original_rows(store, attempt)
    original = await store.pool.fetchrow('SELECT * FROM kotoyose.execution_admission_quarantines WHERE id=$1', ref)
    budgets = await store.pool.fetchval("SELECT jsonb_agg(payload ORDER BY ref)::text FROM kotoyose.execution_resources WHERE payload->>'kind'='budget'")
    kwargs = extension_args(ref, args['expires_at'])
    replies = await asyncio.gather(*(extend_admission_quarantine(store.pool,'caller-a',ref,**kwargs) for _ in range(2)))
    assert replies[0] == replies[1]
    assert await store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_admission_quarantine_extensions') == 1
    assert await count_admission_reservations(store.pool,'caller-a') == 0
    assert await original_rows(store, attempt) == before
    assert await store.pool.fetchrow('SELECT * FROM kotoyose.execution_admission_quarantines WHERE id=$1',ref) == original
    assert budgets == await store.pool.fetchval("SELECT jsonb_agg(payload ORDER BY ref)::text FROM kotoyose.execution_resources WHERE payload->>'kind'='budget'")
    payload = json.loads(await store.pool.fetchval('SELECT payload FROM kotoyose.execution_admission_quarantine_extensions WHERE id=$1',replies[0]))
    with pytest.raises(ValidationError):ResourceEnvelope.model_validate({'resource':payload})
    with pytest.raises(ExecutionError):await store.authorize_provider_request(attempt['id'],'caller-a')
    stale = {**kwargs,'request_id':str(uuid4()),'expires_at':kwargs['expires_at']+timedelta(hours=1)}
    with pytest.raises(ExecutionError,match='stale'):await extend_admission_quarantine(store.pool,'caller-a',ref,**stale)


@pytest.mark.parametrize('drift',['principal','writer','proof','snapshot','generation','external','running','revoked','base_expiry'])
async def test_extension_rejects_changed_or_live_seal_without_writing(journal, drift):
    store, attempt, operation, args, ref = await expired_seal(journal)
    caller='caller-a'
    if drift=='principal':caller='other-caller'
    elif drift=='writer':await store.pool.execute('UPDATE kotoyose.execution_workspaces SET writer_attempt=NULL WHERE id=$1',attempt['workspace_id'])
    elif drift=='proof':await store.pool.execute("UPDATE kotoyose.execution_operations SET result=jsonb_set(result,'{codex_stop_evidence,digest}',to_jsonb($2::text)) WHERE id=$1",operation,'0'*64)
    elif drift=='snapshot':await store.pool.execute("UPDATE kotoyose.execution_attempts SET snapshot=snapshot||'{\"changed\":true}'::jsonb WHERE id=$1",attempt['id'])
    elif drift=='generation':await store.pool.execute('UPDATE kotoyose.execution_workspaces SET generation=1 WHERE id=$1',attempt['workspace_id'])
    elif drift=='external':await store.pool.execute("INSERT INTO kotoyose.execution_operations(id,attempt_id,kind,state) VALUES($1,$2,'client_tool','unknown')",str(uuid4()),attempt['id'])
    elif drift=='running':await store.pool.execute("UPDATE kotoyose.execution_attempts SET finished_at=NULL,status='running' WHERE id=$1",attempt['id'])
    elif drift=='revoked':await store.pool.execute('UPDATE kotoyose.execution_admission_quarantines SET revoked_at=now() WHERE id=$1',ref)
    elif drift=='base_expiry':await store.pool.execute("UPDATE kotoyose.execution_admission_quarantines SET payload=jsonb_set(payload,'{expires_at}',to_jsonb((now()-interval '1 hour')::text)) WHERE id=$1",ref)
    with pytest.raises(ExecutionError):await extend_admission_quarantine(store.pool,caller,ref,**extension_args(ref,args['expires_at']))
    assert await store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_admission_quarantine_extensions')==0


async def test_extended_expiry_and_revocation_restore_raw_count(journal):
    store, _, _, args, ref = await expired_seal(journal)
    kwargs=extension_args(ref,args['expires_at']);kwargs['expires_at']=datetime.now(timezone.utc)+timedelta(seconds=1)
    extension=await extend_admission_quarantine(store.pool,'caller-a',ref,**kwargs)
    assert await count_admission_reservations(store.pool,'caller-a')==0
    await asyncio.sleep(max(0,(kwargs['expires_at']-datetime.now(timezone.utc)).total_seconds())+.02)
    assert await count_admission_reservations(store.pool,'caller-a')==1
    assert await extend_admission_quarantine(store.pool,'caller-a',ref,**kwargs)==extension
    assert await count_admission_reservations(store.pool,'caller-a')==1
    next_args=extension_args(extension,kwargs['expires_at'])
    await extend_admission_quarantine(store.pool,'caller-a',ref,**next_args)
    assert await count_admission_reservations(store.pool,'caller-a')==0
    await store.pool.execute('UPDATE kotoyose.execution_admission_quarantines SET revoked_at=now() WHERE id=$1',ref)
    assert await count_admission_reservations(store.pool,'caller-a')==1


async def test_extension_waiting_across_its_expiry_adds_no_record(journal):
    store, _, _, args, ref = await expired_seal(journal)
    kwargs=extension_args(ref,args['expires_at']);kwargs['expires_at']=datetime.now(timezone.utc)+timedelta(seconds=1)
    with pytest.raises(ExecutionError,match='expired'):
        await wait_past_expiry_while_caller_locked(store,kwargs['expires_at'],lambda:extend_admission_quarantine(store.pool,'caller-a',ref,**kwargs))
    assert await store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_admission_quarantine_extensions')==0


@pytest.mark.parametrize('drift',['proof','writer','generation','external','revoked','base_expiry'])
async def test_post_extension_drift_never_keeps_an_exclusion(journal,drift):
    store,attempt,operation,args,ref=await expired_seal(journal)
    await extend_admission_quarantine(store.pool,'caller-a',ref,**extension_args(ref,args['expires_at']))
    assert await count_admission_reservations(store.pool,'caller-a')==0
    if drift=='proof':await store.pool.execute("UPDATE kotoyose.execution_operations SET result=jsonb_set(result,'{codex_stop_evidence,digest}',to_jsonb($2::text)) WHERE id=$1",operation,'0'*64)
    elif drift=='writer':await store.pool.execute('UPDATE kotoyose.execution_workspaces SET writer_attempt=NULL WHERE id=$1',attempt['workspace_id'])
    elif drift=='generation':await store.pool.execute('UPDATE kotoyose.execution_workspaces SET generation=1 WHERE id=$1',attempt['workspace_id'])
    elif drift=='external':await store.pool.execute("INSERT INTO kotoyose.execution_operations(id,attempt_id,kind,state) VALUES($1,$2,'client_tool','unknown')",str(uuid4()),attempt['id'])
    elif drift=='revoked':await store.pool.execute('UPDATE kotoyose.execution_admission_quarantines SET revoked_at=now() WHERE id=$1',ref)
    elif drift=='base_expiry':await store.pool.execute("UPDATE kotoyose.execution_admission_quarantines SET payload=jsonb_set(payload,'{expires_at}',to_jsonb((now()-interval '1 hour')::text)) WHERE id=$1",ref)
    assert await count_admission_reservations(store.pool,'caller-a')==1
