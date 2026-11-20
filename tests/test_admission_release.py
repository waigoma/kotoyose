"""人が停止不明を受け入れた対象だけを、監査付きで受付枠から外す。"""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kotoyose.admission_quarantine import count_admission_reservations
from kotoyose.admission_release import AdmissionReleaseTarget, release_terminal_admissions
from kotoyose.execution_resources import ExecutionError, ResourceEnvelope
from tests.test_admission_quarantine import original_rows
from tests.test_codex_stop_store import journal as journal


async def terminal(journal):
    store, attempt, operation, _ = journal
    # 停止証拠のない履歴と未解決の外部操作を含め、元の不明状態を維持する。
    external = await store.prepare_operation(attempt['id'], 'client_tool')
    await store.finish(attempt['id'], 'worker_lost', 'worker_lost', 'unconfirmed', None, {})
    return store, attempt, await target_for(store, attempt)


async def target_for(store, attempt):
    row = await store.pool.fetchrow("SELECT id AS attempt_id,request_digest,workspace_id,generation,"
        "encode(sha256(convert_to(snapshot::text,'UTF8')),'hex') AS snapshot_digest "
        "FROM kotoyose.execution_attempts WHERE id=$1", attempt['id'])
    return AdmissionReleaseTarget(**dict(row))


async def fresh_admission(store, attempt, path, key, maximum=1):
    path.mkdir()
    workspace = await store.register_workspace('caller-a', key, path)
    request = {**attempt['request'], 'idempotency_key': key, 'workspace_ref': f"workspace:{workspace['id']}:0"}
    snapshot = {**attempt['snapshot'], 'workspace_id': workspace['id'], 'generation': 0, 'workspace_path': str(path),
                'workspace_device': workspace['device'], 'workspace_inode': workspace['inode']}
    return await store.admit('caller-a', request, snapshot, maximum)


async def test_explicit_release_preserves_unknown_state_writer_operations_and_budget_across_restart(journal, tmp_path):
    store, attempt, target = await terminal(journal)
    before = await original_rows(store, attempt)
    budgets = await store.pool.fetchval("SELECT jsonb_agg(payload ORDER BY ref)::text FROM kotoyose.execution_resources")
    assert await count_admission_reservations(store.pool, 'caller-a') == 1
    receipts = await release_terminal_admissions(store.pool, 'caller-a', [target], authorization_ref='human:test-release')
    assert await release_terminal_admissions(store.pool, 'caller-a', [target], authorization_ref='human:test-release') == receipts
    await store.initialize()
    assert await count_admission_reservations(store.pool, 'caller-a') == 0
    assert await original_rows(store, attempt) == before
    assert budgets == await store.pool.fetchval("SELECT jsonb_agg(payload ORDER BY ref)::text FROM kotoyose.execution_resources")
    with pytest.raises(ExecutionError):
        await store.authorize_provider_request(attempt['id'], 'caller-a')
    with pytest.raises(ExecutionError, match='writer or generation'):
        await store.admit('caller-a', {**attempt['request'], 'idempotency_key': 'same-workspace'}, attempt['snapshot'], 1)
    raw = json.loads(await store.pool.fetchval('SELECT payload FROM kotoyose.execution_admission_releases WHERE id=$1', receipts[0]))
    assert raw['reason'] == 'human_accepts_unknown_stop' and raw['attempt_id'] == attempt['id']
    with pytest.raises(ValidationError):
        ResourceEnvelope.model_validate({'resource': raw})
    await fresh_admission(store, attempt, tmp_path / 'new', 'new')
    # 管理解放を再送しても、新しい実行中 Attempt の枠や上限は変わらない。
    assert await release_terminal_admissions(store.pool, 'caller-a', [target], authorization_ref='human:test-release') == receipts
    with pytest.raises(ExecutionError, match='concurrency reservation'):
        await fresh_admission(store, attempt, tmp_path / 'excess', 'excess')


@pytest.mark.parametrize('drift', ['principal', 'request_digest', 'snapshot_digest', 'workspace_id', 'generation', 'running', 'confirmed'])
async def test_unapproved_changed_or_nonterminal_target_is_not_released(journal, drift):
    store, attempt, target = await terminal(journal)
    principal = 'caller-a'
    if drift == 'principal':
        principal = 'other-caller'
    elif drift in ('request_digest', 'snapshot_digest', 'workspace_id', 'generation'):
        target = target.model_copy(update={drift: target.generation + 1 if drift == 'generation' else '0' * 64})
    elif drift == 'running':
        await store.pool.execute('UPDATE kotoyose.execution_attempts SET finished_at=NULL WHERE id=$1', attempt['id'])
    else:
        await store.pool.execute("UPDATE kotoyose.execution_attempts SET stop_confirmation='confirmed' WHERE id=$1", attempt['id'])
    with pytest.raises(ExecutionError):
        await release_terminal_admissions(store.pool, principal, [target], authorization_ref='human:test-release')
    assert await store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_admission_releases') == 0


async def test_batch_rejection_is_atomic_and_does_not_invent_an_unknown_attempt(journal):
    store, attempt, target = await terminal(journal)
    missing = target.model_copy(update={'attempt_id': str(uuid4())})
    with pytest.raises(ExecutionError):
        await release_terminal_admissions(store.pool, 'caller-a', [target, missing], authorization_ref='human:test-release')
    assert await store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_admission_releases') == 0
    assert await count_admission_reservations(store.pool, 'caller-a') == 1


async def test_one_human_decision_releases_three_histories_and_keeps_each_unknown(journal, tmp_path):
    store, attempt, first = await terminal(journal)
    attempts, targets = [attempt], [first]
    for index in range(2):
        added, _ = await fresh_admission(store, attempt, tmp_path / str(index), str(index), maximum=4)
        await store.finish(added['id'], 'cancelled', 'cancelled', 'unconfirmed', None, {})
        attempts.append(added)
        targets.append(await target_for(store, added))
    original = [await original_rows(store, item) for item in attempts]
    assert await count_admission_reservations(store.pool, 'caller-a') == 3
    refs = await release_terminal_admissions(store.pool, 'caller-a', targets, authorization_ref='human:release-all-three')
    assert len(set(refs)) == 3 and await count_admission_reservations(store.pool, 'caller-a') == 0
    assert [await original_rows(store, item) for item in attempts] == original


async def test_a_sealed_decision_cannot_be_rewritten_by_another_authorization(journal):
    store, _, target = await terminal(journal)
    refs = await release_terminal_admissions(store.pool, 'caller-a', [target], authorization_ref='human:first')
    before = await store.pool.fetchval('SELECT payload::text FROM kotoyose.execution_admission_releases WHERE id=$1', refs[0])
    with pytest.raises(ExecutionError, match='immutable'):
        await release_terminal_admissions(store.pool, 'caller-a', [target], authorization_ref='human:different')
    assert await store.pool.fetchval('SELECT payload::text FROM kotoyose.execution_admission_releases WHERE id=$1', refs[0]) == before


@pytest.mark.parametrize('drift', ['request', 'snapshot', 'generation', 'running', 'revoked'])
async def test_a_release_never_covers_a_changed_or_running_attempt(journal, drift):
    store, attempt, target = await terminal(journal)
    refs = await release_terminal_admissions(store.pool, 'caller-a', [target], authorization_ref='human:test-release')
    if drift == 'request':
        await store.pool.execute("UPDATE kotoyose.execution_attempts SET request_digest=$2 WHERE id=$1", attempt['id'], '0' * 64)
    elif drift == 'snapshot':
        await store.pool.execute("UPDATE kotoyose.execution_attempts SET snapshot=snapshot||'{\"changed\":true}'::jsonb WHERE id=$1", attempt['id'])
    elif drift == 'generation':
        await store.pool.execute('UPDATE kotoyose.execution_attempts SET generation=generation+1 WHERE id=$1', attempt['id'])
    elif drift == 'running':
        await store.pool.execute('UPDATE kotoyose.execution_attempts SET finished_at=NULL WHERE id=$1', attempt['id'])
    else:
        await store.pool.execute('UPDATE kotoyose.execution_admission_releases SET revoked_at=now() WHERE id=$1', refs[0])
    assert await count_admission_reservations(store.pool, 'caller-a') == 1


async def test_audit_is_immutable_and_new_release_requires_an_idle_caller(journal, tmp_path):
    store, attempt, target = await terminal(journal)
    await fresh_admission(store, attempt, tmp_path / 'active', 'active', maximum=2)
    with pytest.raises(ExecutionError, match='idle caller'):
        await release_terminal_admissions(store.pool, 'caller-a', [target], authorization_ref='human:test-release')
    assert await store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_admission_releases') == 0


@pytest.mark.parametrize('targets,authorization', [([], 'human:test'), (None, ''), (None, ' '), ('duplicate', 'human:test')])
async def test_empty_approval_or_ambiguous_target_list_cannot_release(journal, targets, authorization):
    store, _, target = await terminal(journal)
    selected = [target, target] if targets == 'duplicate' else [target] if targets is None else targets
    with pytest.raises(ExecutionError):
        await release_terminal_admissions(store.pool, 'caller-a', selected, authorization_ref=authorization)
    assert await store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_admission_releases') == 0
