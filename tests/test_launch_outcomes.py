"""Launch evidence survives runtime cleanup, service and the limits endpoint."""
import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from kotoyose.native_codex import NativeCodexWorker
from tests.codex_runtime_fixture import codex_rootfs  # noqa: F401
from tests.fixtures.loop_limits import isolated_limits
from tests.test_execution_agent import _resources, _service_for_admission
from tests.test_native_codex import job
from tests.fixtures.clients import clients, set_client


@pytest.mark.parametrize('case', ['cancel_during_broker_start', 'ambiguous_launch'])
async def test_launch_outcome_reaches_limits_and_stop_evidence(tmp_path, monkeypatch, codex_rootfs, case):
    entered, release, cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Egress:
        def __init__(self, *args, **kwargs):
            self.directory = tempfile.TemporaryDirectory(dir=tmp_path)
            self.failure_reason = None
            self.failure_limit = None
        async def start(self):
            entered.set()
            await release.wait()
            return Path(self.directory.name) / 'fixture.sock'
        async def close(self, grace):
            self.directory.cleanup()
            return 'unconfirmed' if case == 'cancel_during_broker_start' else 'confirmed'
        def redact(self, content):
            return content
    monkeypatch.setattr('kotoyose.native_codex.CodexEgress', Egress)
    if case == 'ambiguous_launch':
        async def unknown_spawn(*args, **kwargs):
            raise TimeoutError
        monkeypatch.setattr('kotoyose.native_codex.asyncio.create_subprocess_exec', unknown_spawn)

    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, _, app):
        service, _ = await _service_for_admission(store, tmp_path)
        policy = clients()['caller-a']
        request, _, _ = await _resources(service, policy, tmp_path / 'workspace', tool_refs=[])
        request = request.model_copy(update={'limits_mode': 'agent_work_v1'})
        snapshot = await service.admission.resolve('caller-a', policy, request)
        attempt, _ = await store.admit('caller-a', request.model_dump(mode='json'), snapshot, 2)
        results = []
        async def run_runtime(attempt, cancellation, *, worker, interaction, tracker, runtime_entry, delegation_call=None):
            operation = await store.prepare_operation(attempt['id'], 'provider_process')
            async def started(_pid):
                await tracker.start()
            runtime_entry.entered = True
            result = await NativeCodexWorker().execute(job(tmp_path, limits_mode='agent_work_v1',
                codex_rootfs=codex_rootfs[0], codex_rootfs_digest=codex_rootfs[1],
                deadline_monotonic=asyncio.get_running_loop().time() + 60,
                work_remaining=lambda: 10, validate_start=AsyncMock()), cancellation, AsyncMock(),
                on_process_started=started)
            results.append(result)
            await store.resolve_operation(operation, result.stop_confirmation)
            return result
        monkeypatch.setattr(service.agent_executor, '_execute', run_runtime)
        task = asyncio.create_task(service._execute(attempt, cancel))
        await asyncio.wait_for(entered.wait(), 5)
        if case == 'cancel_during_broker_start':
            cancel.set()
        release.set()
        await asyncio.wait_for(task, 5)
        row = await store.attempt('caller-a', attempt['id'])
        assert results[0].stop_confirmation == row['stop_confirmation'] == 'unconfirmed'
        response = await service.describe_limits('caller-a', attempt['id'])
        observed = response['execution_time']
        if case == 'ambiguous_launch':
            assert results[0].launch_outcome == 'unknown'
            assert (observed['state'], observed['active_ms'], observed['started_at'], observed['finished_at']) == ('unknown', 0, None, None)
        else:
            assert results[0].launch_outcome == 'not_started'
            assert observed['state'] == 'ended' and observed['active_ms'] == 0
            assert observed['started_at'] is None and observed['finished_at'] is not None
            assert response['resource_usage'] == {'output_bytes': 0, 'turns': 0}


def test_docker_suite_missing_database_fails_instead_of_skipping():
    import subprocess
    import sys

    env = dict(os.environ)
    env.pop('KOTOYOSE_EXECUTION_SERVICE_TEST_DATABASE_URL', None)
    env.pop('KOTOYOSE_EXECUTION_AGENT_TEST_DATABASE_URL', None)
    env['KOTOYOSE_DOCKER_TEST_IMAGE'] = 'fixture'
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:randomly', '--tb=short',
        'tests/test_execution_service.py::test_cancel_by_key_fences_late_dispatch_without_starting_a_worker[sandbox]',
        'tests/test_execution_agent.py::test_caller_cannot_issue_trusted_definition_or_input_without_provenance_authority'],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert '2 errors' in result.stdout
    assert result.stdout.count('Dedicated test database connection is required') >= 2
    assert 'skipped' not in result.stdout


@pytest.mark.parametrize(('state', 'known_ms'), [('not_started', 0), ('unknown', 7)])
async def test_store_preserves_unknown_launch_lower_bound(tmp_path, monkeypatch, state, known_ms):
    from tests.test_execution_store import _admit_ready

    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, service, app):
        _, _, request, snapshot = await _admit_ready(store, tmp_path)
        request['limits_mode'] = 'agent_work_v1'
        snapshot.update(kind='agent', limits_mode='agent_work_v1')
        attempt, _ = await store.admit('caller-a', request, snapshot, 1)
        await store.mark_running(attempt['id'])
        await store.pool.execute('UPDATE kotoyose.execution_attempts SET limits_state=$2, limits_active_ms=$3 WHERE id=$1',
                                 attempt['id'], state, known_ms)
        await store.finish(attempt['id'], 'failed', 'launch_unknown', 'unconfirmed', None, {}, launch_outcome='unknown')
        observed = (await service.describe_limits('caller-a', attempt['id']))['execution_time']
        assert (observed['state'], observed['active_ms'], observed['started_at'], observed['finished_at']) == (
            'unknown', known_ms, None, None)
        await _check_prelaunch_refusal(store, app, tmp_path, monkeypatch, state)


async def _check_prelaunch_refusal(store, app, tmp_path, monkeypatch, state):
    from types import SimpleNamespace
    from kotoyose.execution_resources import ExecutionError

    runner, _ = await _service_for_admission(store, tmp_path)
    request, _, _ = await _resources(runner, clients()['caller-a'], tmp_path / 'preflight', tool_refs=[])
    request = request.model_copy(update={'limits_mode': 'agent_work_v1'})
    snapshot = await runner.admission.resolve('caller-a', clients()['caller-a'], request)
    attempt, _ = await store.admit('caller-a', request.model_dump(mode='json'), snapshot, 2)
    runtime = SimpleNamespace(execute=AsyncMock())
    runner.agent_executor.runtime = runtime
    runner._stop_attempt = AsyncMock(return_value='confirmed')
    finish_calls = []
    finish = store.finish
    async def capture_finish(*args, **kwargs):
        finish_calls.append(kwargs.get('launch_outcome'))
        return await finish(*args, **kwargs)
    monkeypatch.setattr(store, 'finish', capture_finish)
    if state == 'not_started':
        attempt['snapshot']['runtime_profile'] = 'retired-route'
        expected = ('request_conflict', 409, 'Runtime profile changed after admission')
    else:
        monkeypatch.delenv('KOTOYOSE_AGENT_PROVIDER_TOKEN', raising=False)
        expected = ('authentication_failed', 401, 'Runtime credential is unavailable')
    errors = []
    execute = runner.agent_executor.execute
    async def capture(*args, **kwargs):
        try:
            return await execute(*args, **kwargs)
        except Exception as exc:
            errors.append(exc)
            raise
    runner.agent_executor.execute = capture
    await runner._execute(attempt, asyncio.Event())
    assert len(errors) == 1 and type(errors[0]) is ExecutionError
    assert (errors[0].code, errors[0].status, str(errors[0])) == expected
    runtime.execute.assert_not_awaited()
    assert finish_calls[-1] == 'not_started'
    row = await store.attempt('caller-a', attempt['id'])
    assert (row['status'], row['reason']) == ('failed', expected[0])
    assert (row['limits_state'], row['limits_active_ms'], row['limits_started_at']) == ('ended', 0, None)
    assert row['limits_finished_at'] is not None
    response = await runner.describe_limits('caller-a', attempt['id'])
    observed = response['execution_time']
    assert (observed['state'], observed['active_ms'], observed['started_at']) == ('ended', 0, None)
    assert observed['finished_at'] is not None
    if state == 'unknown':
        await _check_runtime_entered_failure(store, app, runner, tmp_path, monkeypatch, errors, finish_calls)


async def _check_runtime_entered_failure(store, app, runner, tmp_path, monkeypatch, errors, finish_calls):
    from kotoyose.agent_deadlines import AgentStartRejected
    from kotoyose.execution_resources import ExecutionError
    from kotoyose.native_claude import NativeClaudeWorker
    from types import SimpleNamespace
    from unittest.mock import Mock

    monkeypatch.setenv('KOTOYOSE_AGENT_PROVIDER_TOKEN', 'fixture-token')
    request, _, _ = await _resources(runner, clients()['caller-a'], tmp_path / 'typed', tool_refs=[])
    request = request.model_copy(update={'limits_mode': 'agent_work_v1'})
    snapshot = await runner.admission.resolve('caller-a', clients()['caller-a'], request)
    attempt, _ = await store.admit('caller-a', request.model_dump(mode='json'), snapshot, 2)
    runner.agent_executor.runtime = NativeClaudeWorker()
    client = Mock()
    monkeypatch.setattr('kotoyose.native_claude.ClaudeSDKClient', client)
    original_validate = runner.agent_executor._validate_start
    runner.agent_executor._validate_start = AsyncMock(side_effect=AgentStartRejected(
        'grant_expired_before_start', 'fixture grant expired', 409))
    errors.clear()
    await runner._execute(attempt, asyncio.Event())
    assert finish_calls[-1] == 'not_started'
    runner.agent_executor._validate_start.assert_awaited_once()
    client.assert_not_called()
    assert len(errors) == 1 and type(errors[0]) is AgentStartRejected
    assert (errors[0].code, errors[0].status, str(errors[0])) == (
        'grant_expired_before_start', 409, 'fixture grant expired')
    row = await store.attempt('caller-a', attempt['id'])
    assert (row['status'], row['reason']) == ('failed', 'grant_expired_before_start')
    assert (row['limits_state'], row['limits_active_ms'], row['limits_started_at']) == ('ended', 0, None)
    assert row['limits_finished_at'] is not None
    assert (row['limits_output_bytes'], row['limits_turns']) == (0, 0)
    response = await runner.describe_limits('caller-a', attempt['id'])
    observed = response['execution_time']
    assert (observed['state'], observed['active_ms'], observed['started_at']) == ('ended', 0, None)
    assert observed['finished_at'] is not None
    assert response['resource_usage'] == {'output_bytes': 0, 'turns': 0}

    runner.agent_executor._validate_start = original_validate
    runtime = SimpleNamespace(execute=AsyncMock(side_effect=ExecutionError('request_conflict', 'fixture runtime failure', 409)))
    runner.agent_executor.runtime = runtime
    request, _, _ = await _resources(runner, clients()['caller-a'], tmp_path / 'entered', tool_refs=[])
    request = request.model_copy(update={'limits_mode': 'agent_work_v1'})
    snapshot = await runner.admission.resolve('caller-a', clients()['caller-a'], request)
    attempt, _ = await store.admit('caller-a', request.model_dump(mode='json'), snapshot, 2)
    errors.clear()
    await runner._execute(attempt, asyncio.Event())
    assert finish_calls[-1] == 'unknown'
    runtime.execute.assert_awaited_once()
    assert len(errors) == 1 and type(errors[0]) is ExecutionError
    assert (errors[0].code, errors[0].status, str(errors[0])) == ('request_conflict', 409, 'fixture runtime failure')
    row = await store.attempt('caller-a', attempt['id'])
    assert (row['status'], row['reason']) == ('failed', 'request_conflict')
    assert (row['limits_state'], row['limits_active_ms'], row['limits_started_at'], row['limits_finished_at']) == (
        'unknown', 0, None, None)
    response = await runner.describe_limits('caller-a', attempt['id'])
    observed = response['execution_time']
    assert (observed['state'], observed['active_ms'], observed['started_at'], observed['finished_at']) == (
        'unknown', 0, None, None)
