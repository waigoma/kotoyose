"""追加観測の所有境界、旧形式互換、検証と永続性。"""

import os
from datetime import datetime, timezone

import asyncpg
import httpx
import pytest
from pydantic import ValidationError

from kotoyose.execution_contract import AgentExecution, ResumeRequest
from kotoyose.execution_limits import ExecutionTime, ProviderUsageLimit, AgentResourceUsage, AttemptLimits
from kotoyose.execution_resources import content_digest
from kotoyose.execution_store import ExecutionStore
from tests.fixtures.loop_limits import isolated_limits
from tests.test_execution_store import _admit_ready


@pytest.fixture
async def limits(tmp_path):
    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as fixture:
        store, service, app = fixture
        _, _, request, snapshot = await _admit_ready(store, tmp_path)
        attempt, _ = await store.admit('caller-a', request, snapshot, 1)
        yield store, service, app, attempt


async def test_legacy_attempt_is_unmeasured(limits):
    store, service, _, attempt = limits
    # 先行実装済みの列も nullable / default なしに戻せることを確認する。
    await store.pool.execute('UPDATE kotoyose.execution_attempts SET limits_sequence=0 WHERE id=$1', attempt['id'])
    await store.pool.execute('ALTER TABLE kotoyose.execution_attempts ALTER COLUMN limits_sequence SET DEFAULT 0')
    await store.pool.execute('ALTER TABLE kotoyose.execution_attempts ALTER COLUMN limits_sequence SET NOT NULL')
    await store.initialize()
    column = await store.pool.fetchrow("SELECT is_nullable, column_default FROM information_schema.columns "
                                       "WHERE table_schema='kotoyose' AND table_name='execution_attempts' "
                                       "AND column_name='limits_sequence'")
    assert column['is_nullable'] == 'YES' and column['column_default'] is None
    # 既存 row を残したまま旧 schema を再現し、列追加時に値を補完しないことを確認する。
    await store.pool.execute('ALTER TABLE kotoyose.execution_attempts DROP CONSTRAINT execution_attempt_limits_valid')
    columns = ('state', 'active_ms', 'started_at', 'observed_at', 'finished_at', 'output_bytes', 'turns',
               'provider', 'window', 'reset_at', 'detected_at', 'resume', 'unavailable_reason', 'sequence')
    await store.pool.execute('ALTER TABLE kotoyose.execution_attempts ' +
                             ', '.join('DROP COLUMN limits_' + column for column in columns))
    await store.initialize()
    row = await store.pool.fetchrow('SELECT ' + ', '.join('limits_' + column for column in columns) +
                                    ' FROM kotoyose.execution_attempts WHERE id=$1', attempt['id'])
    assert all(value is None for value in row.values())
    assert await store.mark_running(attempt['id'])
    row = await store.pool.fetchrow('SELECT ' + ', '.join('limits_' + column for column in columns) +
                                    ' FROM kotoyose.execution_attempts WHERE id=$1', attempt['id'])
    assert all(value is None for value in row.values())
    result = await service.describe_limits('caller-a', attempt['id'])
    assert result['execution_time']['state'] == 'unmeasured'
    assert result['execution_time']['active_ms'] is None
    assert result['resource_usage'] == {'output_bytes': None, 'turns': None}
    assert result['sequence'] == 0


async def test_v1_attempt_shape_is_unchanged(limits):
    store, _, app, attempt = limits
    expected_keys = {
        'schema_version', 'attempt_id', 'caller_run_ref', 'status', 'reason', 'stop_confirmation',
        'side_effect_resolution', 'exit_code', 'sequence', 'workspace_ref', 'workspace_generation',
        'usage', 'artifacts', 'artifact_manifest', 'required_action', 'created_at', 'finished_at',
    }
    statuses = ('accepted', 'dispatched', 'running', 'cancelling', 'succeeded', 'failed', 'cancelled',
                'timeout', 'worker_lost', 'budget_exceeded', 'approval_required')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        initial = await client.get(f"/v1/attempts/{attempt['id']}", headers={'Authorization': 'Bearer kys_caller-a'})
        assert initial.status_code == 200 and initial.json()['status'] == 'dispatched'
        assert await store.mark_running(attempt['id'])
        running = await client.get(f"/v1/attempts/{attempt['id']}", headers={'Authorization': 'Bearer kys_caller-a'})
        assert running.status_code == 200 and running.json()['status'] == 'running'
        for status in statuses:
            await store.pool.execute('UPDATE kotoyose.execution_attempts SET status=$2 WHERE id=$1', attempt['id'], status)
            response = await client.get(f"/v1/attempts/{attempt['id']}", headers={'Authorization': 'Bearer kys_caller-a'})
            assert response.status_code == 200
            assert set(response.json()) == expected_keys
            assert response.json()['status'] == status


def test_invalid_time_and_reset_are_rejected():
    now = datetime.now(timezone.utc)
    for state, active in [('running', None), ('unmeasured', 0), ('unknown', -1)]:
        with pytest.raises(ValidationError):
            ExecutionTime(state=state, active_ms=active, started_at=None, observed_at=now, finished_at=None)
    with pytest.raises(ValidationError):
        ProviderUsageLimit(provider='codex', limit_window='rate', reset_at=datetime(2026, 1, 1),
                           detected_at=now, resume='unavailable', unavailable_reason='no_session')
    with pytest.raises(ValidationError):
        AgentResourceUsage(output_bytes=-1, turns=0)


async def test_limits_survive_store_reopen(limits):
    store, service, _, attempt = limits
    now = datetime.now(timezone.utc)
    observation = AttemptLimits(schema_version='1', attempt_id=attempt['id'], sequence=0,
        execution_time=ExecutionTime(state='unknown', active_ms=17, started_at=now, observed_at=now, finished_at=None),
        resource_usage=AgentResourceUsage(output_bytes=2, turns=1),
        provider_limit=ProviderUsageLimit(provider='codex', limit_window='five_hour', reset_at=now,
                                           detected_at=now, resume='native', unavailable_reason=None))
    await store.limits.save('caller-a', observation)
    reopened = ExecutionStore(store.pool)
    await reopened.initialize()
    service.store = reopened
    saved = await service.describe_limits('caller-a', attempt['id'])
    expected = observation.model_dump(mode='json')
    expected['sequence'] = 1
    assert saved == expected
    assert (await reopened.attempt('caller-a', attempt['id']))['sequence'] == attempt['sequence']


def test_absent_limits_mode_preserves_request_digest():
    data = {'schema_version': '1', 'kind': 'agent', 'idempotency_key': 'key', 'caller_run_ref': 'run',
            'workspace_ref': 'workspace', 'execution_grant_ref': 'grant', 'budgets_ref': 'budget',
            'agent_definition_ref': 'definition', 'runtime_profile': 'profile',
            'inputs': [{'kind': 'task_request', 'provenance': 'project', 'content_ref': 'input'}]}
    request = AgentExecution.model_validate(data)
    serialized = request.model_dump(mode='json')
    assert 'limits_mode' not in serialized
    assert content_digest(serialized) == '6e0b47c8faf7955ffee38a75b0ae0c74ef14423925f310319abebe0cb874792b'
    resume = ResumeRequest(schema_version='1', execution=request, predecessor_snapshot_digest='a' * 64)
    assert 'required_resume_mode' not in resume.model_dump(mode='json')
    assert content_digest(resume.model_dump(mode='json')) == '9ccac84d988397d3e476bcb3f49c5b8c41133d85425b733bda4df3b4305812a0'


async def test_opted_in_start_is_unknown_until_measured(tmp_path):
    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, service, _):
        _, _, request, snapshot = await _admit_ready(store, tmp_path)
        request['limits_mode'] = 'agent_work_v1'
        attempt, _ = await store.admit('caller-a', request, snapshot, 1)
        initial = await service.describe_limits('caller-a', attempt['id'])
        assert initial['execution_time']['state'] == 'not_started'
        assert initial['execution_time']['active_ms'] == 0
        assert await store.mark_running(attempt['id'])
        running = await service.describe_limits('caller-a', attempt['id'])
        assert running['execution_time']['state'] == 'unknown'
        assert running['execution_time']['active_ms'] == 0
        assert running['sequence'] == initial['sequence'] + 1


async def test_database_rejects_invalid_observation(limits):
    store, _, _, attempt = limits
    for assignment in ["limits_state='legacy_unmeasured', limits_observed_at=now()", "limits_active_ms=-1", "limits_sequence=-1", "limits_output_bytes=-1", "limits_turns=-1",
                       "limits_state='unknown'", "limits_provider='codex'", "limits_observed_at=now()"]:
        with pytest.raises(asyncpg.CheckViolationError):
            await store.pool.execute(f'UPDATE kotoyose.execution_attempts SET {assignment} WHERE id=$1', attempt['id'])


async def test_initialize_rebuilds_limits_check_for_new_providers(limits):
    """古い CHECK (print_cli を知らない) が残った表でも、起動の初期化で新しい provider を受け付ける。"""
    store, _, _, attempt = limits
    old = await store.pool.fetchval(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid='kotoyose.execution_attempts'::regclass AND conname='execution_attempt_limits_valid'")
    narrowed = old.replace(", 'print_cli'::text", "")
    assert narrowed != old
    await store.pool.execute('ALTER TABLE kotoyose.execution_attempts DROP CONSTRAINT execution_attempt_limits_valid')
    await store.pool.execute(f'ALTER TABLE kotoyose.execution_attempts ADD CONSTRAINT execution_attempt_limits_valid {narrowed}')

    update = ("UPDATE kotoyose.execution_attempts SET limits_provider='print_cli', limits_window='unknown', "
              "limits_detected_at=now(), limits_resume='unavailable', limits_unavailable_reason='adapter_unsupported', "
              "limits_state='not_started', limits_observed_at=now(), limits_active_ms=0 WHERE id=$1")
    with pytest.raises(asyncpg.CheckViolationError):
        await store.pool.execute(update, attempt['id'])
    await store.initialize()
    await store.initialize()  # 再起動ごとに走っても壊れない
    await store.pool.execute(update, attempt['id'])
