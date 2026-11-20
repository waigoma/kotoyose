from datetime import datetime, timezone
import shutil

import pytest

from kotoyose.agent_work_clock import AgentWorkClock
from tests.fixtures.clients import clients, set_client

_needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="Native execution restart needs bubblewrap on the host")


class FakeTime:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


async def test_checkpoint_failures_do_not_change_attempt_result(tmp_path, monkeypatch, caplog):
    import asyncio
    import os
    from kotoyose.native_claude import NativeAgentResult
    from tests.fixtures.loop_limits import isolated_limits
    from tests.test_execution_agent import _resources, _service_for_admission

    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, _, _):
        service, worker = await _service_for_admission(store, tmp_path)
        monkeypatch.setenv('KOTOYOSE_AGENT_PROVIDER_TOKEN', 'test-token')
        policy = clients()['caller-a']
        request, _, _ = await _resources(service, policy, tmp_path / 'workspace', tool_refs=[])
        request = request.model_copy(update={'limits_mode': 'agent_work_v1'})
        snapshot = await service.admission.resolve('caller-a', policy, request)
        attempt, _ = await store.admit('caller-a', request.model_dump(mode='json'), snapshot, 2)
        original = store.limits.save_time
        calls, trackers = [], []

        async def flaky(principal, attempt_id, sample):
            calls.append(sample.state)
            if len(calls) == 1 or sample.state == 'ended':
                raise OSError('fixture checkpoint failure')
            await original(principal, attempt_id, sample)
        monkeypatch.setattr(store.limits, 'save_time', flaky)

        class Runtime:
            async def execute(self, job, cancel, execute_tool, *, on_process_started):
                await on_process_started(os.getpid())
                tracker = service.agent_executor.work_clocks[attempt['id']]
                trackers.append(tracker)
                assert tracker._task is not None and not tracker._task.done()
                await asyncio.sleep(1.1)
                job.on_runtime_stopped()
                return NativeAgentResult('succeeded', 'completed', b'ok', 'confirmed',
                    {'source': 'unknown', 'cost': None, 'input_tokens': None, 'output_tokens': None})
        service.agent_executor.runtime = Runtime()
        await service._execute(attempt, asyncio.Event())
        row = await store.attempt('caller-a', attempt['id'])
        assert (row['status'], row['stop_confirmation']) == ('succeeded', 'confirmed')
        operations = await store.operations(attempt['id'])
        assert any(item['kind'] == 'provider_process' and item['result']['pid'] == os.getpid() for item in operations)
        assert calls[0] == 'running' and 'running' in calls[1:] and calls[-1] == 'ended'
        assert sum('work clock checkpoint failed: OSError' in record.message for record in caplog.records) == 1
        assert trackers[0]._task.done() and attempt['id'] not in service.agent_executor.work_clocks


async def test_overlapping_approval_bridge_waits_exclude_union(tmp_path, monkeypatch):
    import asyncio
    import os
    from uuid import uuid4
    from kotoyose.client_tool_bridge import ClientToolCallOutcome
    from tests.fixtures.loop_limits import isolated_limits
    from tests.test_execution_agent import _resources, _service_for_admission

    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, _, _):
        service, _ = await _service_for_admission(store, tmp_path)
        policy = clients()['caller-a']
        request, _, _ = await _resources(service, policy, tmp_path / 'workspace', tool_refs=['builtin:request-approval@1'])
        ids = [str(uuid4()), str(uuid4())]
        request = type(request).model_validate({**request.model_dump(mode='json'), 'client_key': 'client:fixture',
            'approval_bindings': [{'approval_id': value, 'subject_digest': 'a' * 64, 'wait_seconds': 5} for value in ids]})
        snapshot = await service.admission.resolve('caller-a', policy, request)
        attempt, _ = await store.admit('caller-a', request.model_dump(mode='json'), snapshot, 2)
        await store.mark_running(attempt['id'])
        entered = [asyncio.Event(), asyncio.Event()]
        releases = [asyncio.Event(), asyncio.Event()]
        calls = []
        async def wait(*args):
            index = len(calls)
            calls.append(args[2])
            entered[index].set()
            await releases[index].wait()
            return ClientToolCallOutcome('succeeded')
        monkeypatch.setattr(store.client_tools, 'wait', wait)
        now = FakeTime()
        clock = AgentWorkClock(monotonic=now)
        clock.start()
        now.advance(2)
        deadline = datetime.fromisoformat(snapshot['deadline'])
        def call(value):
            return store.client_tools.call('caller-a', attempt['id'], 'builtin:request-approval@1',
                {'approval_id': value, 'subject_digest': 'a' * 64}, deadline, approval_clock=clock)
        first = asyncio.create_task(call(ids[0]))
        await asyncio.wait_for(entered[0].wait(), 2)
        now.advance(4)
        second = asyncio.create_task(call(ids[1]))
        await asyncio.wait_for(entered[1].wait(), 2)
        now.advance(3)
        releases[0].set()
        await first
        now.advance(2)
        releases[1].set()
        await second
        now.advance(1)
        assert clock.stop().active_ms == 3000
        assert len(set(calls)) == 2


async def test_unconfirmed_finish_and_waiting_limits_read_remain_unknown(tmp_path, monkeypatch):
    import asyncio
    import os
    from kotoyose import agent_work_clock
    from kotoyose.native_claude import NativeAgentResult
    from tests.fixtures.loop_limits import isolated_limits
    from tests.test_execution_agent import _resources, _service_for_admission

    now = FakeTime()
    monkeypatch.setattr(agent_work_clock, 'AgentWorkClock', lambda: AgentWorkClock(monotonic=now))
    periodic_started, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    hook_called, cleanup_release = asyncio.Event(), asyncio.Event()
    async def periodic(self):
        periodic_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelling.set()
            await release.wait()
            raise
    monkeypatch.setattr(agent_work_clock.WorkClockTracker, '_periodic', periodic)
    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, _, _):
        service, worker = await _service_for_admission(store, tmp_path)
        monkeypatch.setenv('KOTOYOSE_AGENT_PROVIDER_TOKEN', 'test-token')
        policy = clients()['caller-a']
        request, _, _ = await _resources(service, policy, tmp_path / 'workspace', tool_refs=[])
        request = request.model_copy(update={'limits_mode': 'agent_work_v1'})
        snapshot = await service.admission.resolve('caller-a', policy, request)
        attempt, _ = await store.admit('caller-a', request.model_dump(mode='json'), snapshot, 2)
        original = store.limits.save_time
        fail_final = False
        final_failed = asyncio.Event()
        trackers = []
        async def save(principal, attempt_id, sample):
            if fail_final and sample.state == 'unknown':
                final_failed.set()
                raise OSError('fixture final save failure')
            await original(principal, attempt_id, sample)
        monkeypatch.setattr(store.limits, 'save_time', save)
        class Runtime:
            async def execute(self, job, cancel, execute_tool, *, on_process_started):
                await on_process_started(os.getpid())
                tracker = service.agent_executor.work_clocks[attempt['id']]
                trackers.append(tracker)
                await periodic_started.wait()
                now.advance(3)
                await tracker.checkpoint()
                job.on_runtime_stopped()
                hook_called.set()
                await cleanup_release.wait()
                now.advance(60)
                return NativeAgentResult('succeeded', 'completed', b'ok', 'unconfirmed',
                    {'source': 'unknown', 'cost': None, 'input_tokens': None, 'output_tokens': None})
        service.agent_executor.runtime = Runtime()
        execution = asyncio.create_task(service._execute(attempt, asyncio.Event()))
        try:
            await asyncio.wait_for(hook_called.wait(), 2)
            now.advance(3600)
            after_hook = (await service.describe_limits('caller-a', attempt['id']))['execution_time']
            cleanup_release.set()
            await asyncio.wait_for(cancelling.wait(), 2)
            during_finish = (await service.describe_limits('caller-a', attempt['id']))['execution_time']
            fail_final = True
            assert (after_hook['state'], after_hook['active_ms'], after_hook['finished_at']) == ('unknown', 3000, None)
            assert (during_finish['state'], during_finish['active_ms'], during_finish['finished_at']) == ('unknown', 3000, None)
        finally:
            cleanup_release.set()
            release.set()
            await asyncio.wait_for(execution, 3)
        row = await store.attempt('caller-a', attempt['id'])
        assert row['finished_at'] is not None and row['stop_confirmation'] == 'unconfirmed'
        reread = (await service.describe_limits('caller-a', attempt['id']))['execution_time']
        assert (reread['state'], reread['active_ms'], reread['finished_at']) == ('unknown', 3000, None)
        assert final_failed.is_set()
        assert trackers[0]._task.done() and attempt['id'] not in service.agent_executor.work_clocks


@_needs_bwrap
async def test_restart_marks_unmeasured_tail(tmp_path, monkeypatch):
    import asyncio
    import os
    from unittest.mock import AsyncMock
    from tests.fixtures.loop_limits import isolated_limits
    from tests.test_execution_agent import _service_for_admission
    from tests.test_execution_store import _admit_ready
    from kotoyose.agent_work_clock import WorkClockTracker
    from kotoyose.execution_limits import project_limits

    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, _, _):
        _, _, request, snapshot = await _admit_ready(store, tmp_path)
        request['limits_mode'] = 'agent_work_v1'
        snapshot['kind'] = 'agent'
        attempt, _ = await store.admit('caller-a', request, snapshot, 1)
        await store.mark_running(attempt['id'])
        tracker = WorkClockTracker(store.limits, 'caller-a', attempt['id'])
        now = FakeTime()
        tracker.clock = AgentWorkClock(monotonic=now)
        await tracker.start()
        now.advance(3)
        await tracker.checkpoint()
        tracker._task.cancel()
        await asyncio.gather(tracker._task, return_exceptions=True)
        service, _ = await _service_for_admission(store, tmp_path)
        monkeypatch.setenv('KOTOYOSE_AGENT_CALLER_TOKEN', 'fixture-token')
        stop = AsyncMock(return_value='confirmed')
        monkeypatch.setattr(service, '_stop_attempt', stop)
        await service.start()
        try:
            stop.assert_awaited_once()
            now.advance(100)
            await tracker.checkpoint()
            observed = project_limits(await store.attempt('caller-a', attempt['id'])).execution_time
            assert observed.state == 'unknown' and observed.active_ms == 3000
            assert observed.finished_at is None
        finally:
            await service.close()


async def test_command_has_no_agent_time(tmp_path):
    import os
    from tests.fixtures.loop_limits import isolated_limits
    from tests.test_execution_store import _admit_ready

    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, service, _):
        _, _, request, snapshot = await _admit_ready(store, tmp_path)
        request['limits_mode'] = 'agent_work_v1'
        snapshot['kind'] = 'command'
        attempt, _ = await store.admit('caller-a', request, snapshot, 1)
        await store.mark_running(attempt['id'])
        observed = (await service.describe_limits('caller-a', attempt['id']))['execution_time']
        assert observed['state'] == 'not_started' and observed['active_ms'] == 0


async def test_blocking_approval_keeps_existing_wait_semantics(tmp_path, monkeypatch):
    import asyncio
    import os
    from uuid import uuid4
    from tests.fixtures.loop_limits import isolated_limits
    from tests.test_execution_agent import _resources, _service_for_admission

    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, _, _):
        service, _ = await _service_for_admission(store, tmp_path)
        policy = clients()['caller-a']
        request, _, _ = await _resources(service, policy, tmp_path / 'workspace', tool_refs=['builtin:request-approval@1'])
        approval_id = str(uuid4())
        request = type(request).model_validate({**request.model_dump(mode='json'), 'limits_mode': 'agent_work_v1',
            'client_key': 'client:fixture', 'approval_bindings': [{'approval_id': approval_id,
                'subject_digest': 'a' * 64, 'wait_seconds': 1}]})
        snapshot = await service.admission.resolve('caller-a', policy, request)
        assert snapshot['limits_mode'] == 'agent_work_v1'
        attempt, _ = await store.admit('caller-a', request.model_dump(mode='json'), snapshot, 2)
        await store.mark_running(attempt['id'])
        clock = AgentWorkClock()
        clock.start()
        loop = asyncio.get_running_loop()
        start = loop.time()
        outcome = await store.client_tools.call('caller-a', attempt['id'], 'builtin:request-approval@1',
            {'approval_id': approval_id, 'subject_digest': 'a' * 64},
            datetime.fromisoformat(snapshot['deadline']), approval_clock=clock)
        elapsed = loop.time() - start
        assert 0.8 <= elapsed < 3
        assert outcome.result['outcome'] == 'approval_required'
        assert outcome.required_action['approval_id'] == approval_id
        assert clock.stop().active_ms < 500


async def test_cancel_during_approval_releases_clock_and_tracker(tmp_path, monkeypatch):
    import asyncio
    import os
    from uuid import uuid4
    from kotoyose import agent_work_clock
    from kotoyose.native_claude import NativeAgentResult
    from tests.fixtures.loop_limits import isolated_limits
    from tests.test_execution_agent import _resources, _service_for_admission

    now = FakeTime()
    monkeypatch.setattr(agent_work_clock, 'AgentWorkClock', lambda: AgentWorkClock(monotonic=now))
    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, _, _):
        service, worker = await _service_for_admission(store, tmp_path)
        monkeypatch.setenv('KOTOYOSE_AGENT_PROVIDER_TOKEN', 'test-token')
        policy = clients()['caller-a']
        request, _, _ = await _resources(service, policy, tmp_path / 'workspace', tool_refs=['builtin:request-approval@1'])
        approval_id = str(uuid4())
        request = type(request).model_validate({**request.model_dump(mode='json'), 'limits_mode': 'agent_work_v1',
            'client_key': 'client:fixture', 'approval_bindings': [{'approval_id': approval_id,
                'subject_digest': 'a' * 64, 'wait_seconds': 5}]})
        snapshot = await service.admission.resolve('caller-a', policy, request)
        attempt, _ = await store.admit('caller-a', request.model_dump(mode='json'), snapshot, 2)
        await store.mark_running(attempt['id'])
        entered, trackers = asyncio.Event(), []
        original_wait = store.client_tools.wait
        async def wait(*args, **kwargs):
            entered.set()
            return await original_wait(*args, **kwargs)
        monkeypatch.setattr(store.client_tools, 'wait', wait)
        class Runtime:
            async def execute(self, job, cancel, execute_tool, *, on_process_started):
                await on_process_started(os.getpid())
                trackers.append(service.agent_executor.work_clocks[attempt['id']])
                now.advance(2)
                outcome = await job.client_tool_call('builtin:request-approval@1',
                    {'approval_id': approval_id, 'subject_digest': 'a' * 64})
                assert outcome.state == 'cancelled'
                job.on_runtime_stopped()
                return NativeAgentResult('cancelled', 'cancelled', b'', 'confirmed',
                    {'source': 'unknown', 'cost': None, 'input_tokens': None, 'output_tokens': None})
        service.agent_executor.runtime = Runtime()
        cancel = asyncio.Event()
        execution = asyncio.create_task(service.agent_executor.execute(attempt, cancel, worker=worker))
        await asyncio.wait_for(entered.wait(), 2)
        now.advance(3600)
        cancel.set()
        result = await asyncio.wait_for(execution, 3)
        assert (result.status, result.stop_confirmation) == ('cancelled', 'confirmed')
        assert trackers[0].clock.sample().active_ms == 2000
        assert trackers[0].clock._waits == 0 and trackers[0]._task.done()
        assert attempt['id'] not in service.agent_executor.work_clocks


async def test_runtime_persists_work_and_excludes_approval_and_cleanup(tmp_path, monkeypatch):
    import asyncio
    import os
    from uuid import uuid4
    from kotoyose import agent_work_clock
    from kotoyose.execution_jobs import WorkerResult
    from kotoyose.native_claude import NativeAgentResult
    from tests.fixtures.loop_limits import isolated_limits
    from tests.test_execution_agent import _resources, _service_for_admission

    now = FakeTime()
    monkeypatch.setattr(agent_work_clock, 'AgentWorkClock', lambda: AgentWorkClock(monotonic=now))
    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, _, _):
        service, worker = await _service_for_admission(store, tmp_path)
        monkeypatch.setenv('KOTOYOSE_AGENT_PROVIDER_TOKEN', 'test-token')
        policy = clients()['caller-a']
        request, _, _ = await _resources(service, policy, tmp_path / 'workspace',
            tool_refs=['builtin:sandbox-exec@1', 'builtin:request-approval@1'])
        approval_id = str(uuid4())
        request = type(request).model_validate({**request.model_dump(mode='json'), 'limits_mode': 'agent_work_v1',
            'client_key': 'client:fixture', 'approval_bindings': [{'approval_id': approval_id,
                'subject_digest': 'a' * 64, 'wait_seconds': 1}]})
        snapshot = await service.admission.resolve('caller-a', policy, request)
        attempt, _ = await store.admit('caller-a', request.model_dump(mode='json'), snapshot, 2)
        await store.mark_running(attempt['id'])
        original_wait = store.client_tools.wait

        async def wait(*args, **kwargs):
            now.advance(3600)
            return await original_wait(*args, **kwargs)
        monkeypatch.setattr(store.client_tools, 'wait', wait)

        async def command(*args):
            now.advance(1)
            return WorkerResult('succeeded', 0, b'', b'', 'confirmed', 'completed')
        worker.execute.side_effect = command

        class Runtime:
            async def execute(self, job, cancel, execute_tool, *, on_process_started):
                assert job.limits_mode == 'agent_work_v1'
                now.advance(5)
                await on_process_started(os.getpid())
                now.advance(2)
                await execute_tool(('true',), '.')
                outcome = await job.client_tool_call('builtin:request-approval@1',
                    {'approval_id': approval_id, 'subject_digest': 'a' * 64})
                assert outcome.result['outcome'] == 'approval_required'
                job.on_runtime_stopped()
                frozen = (await service.describe_limits('caller-a', attempt['id']))['execution_time']
                assert (frozen['state'], frozen['active_ms'], frozen['finished_at']) == ('unknown', 3000, None)
                now.advance(60)
                return NativeAgentResult('approval_required', 'approval_required', b'', 'confirmed',
                                         {'source': 'unknown', 'cost': None, 'input_tokens': None, 'output_tokens': None})
        service.agent_executor.runtime = Runtime()
        result = await service.agent_executor.execute(attempt, asyncio.Event(), worker=worker)
        assert result.status == 'approval_required'
        first = await service.describe_limits('caller-a', attempt['id'])
        second = await service.describe_limits('caller-a', attempt['id'])
        assert first['execution_time']['active_ms'] == 3000
        assert second['execution_time'] == first['execution_time']
        assert first['execution_time']['state'] == 'ended'
        assert first['execution_time']['finished_at'] is not None


async def test_running_checkpoint_is_persisted_every_second(tmp_path):
    import asyncio
    import os
    from kotoyose.agent_work_clock import WorkClockTracker
    from tests.fixtures.loop_limits import isolated_limits
    from tests.test_execution_store import _admit_ready

    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, _, _):
        _, _, request, snapshot = await _admit_ready(store, tmp_path)
        request['limits_mode'] = 'agent_work_v1'
        snapshot['kind'] = 'agent'
        attempt, _ = await store.admit('caller-a', request, snapshot, 1)
        await store.mark_running(attempt['id'])
        now = FakeTime()
        tracker = WorkClockTracker(store.limits, 'caller-a', attempt['id'])
        tracker.clock = AgentWorkClock(monotonic=now)
        await tracker.start()
        now.advance(.0019)
        assert tracker.clock.sample().active_ms == 1
        now.advance(.001)
        assert tracker.clock.sample().active_ms == 2
        before = await store.attempt('caller-a', attempt['id'])
        now.advance(2)
        await asyncio.sleep(1.15)
        after = await store.attempt('caller-a', attempt['id'])
        assert after['limits_state'] == 'running'
        assert after['limits_active_ms'] == 2002
        assert after['limits_sequence'] > before['limits_sequence']
        await tracker.finish(confirmed=True)


async def test_prestart_failure_records_zero(tmp_path):
    import os
    from tests.fixtures.loop_limits import isolated_limits
    from tests.test_execution_store import _admit_ready

    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, service, _):
        _, _, request, snapshot = await _admit_ready(store, tmp_path)
        request['limits_mode'] = 'agent_work_v1'
        snapshot['kind'] = 'agent'
        snapshot['limits_mode'] = 'agent_work_v1'
        attempt, _ = await store.admit('caller-a', request, snapshot, 1)
        from datetime import timedelta
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        await store.pool.execute("UPDATE kotoyose.execution_resources SET payload=jsonb_set(payload, '{expires_at}', to_jsonb($3::text)) WHERE principal=$1 AND ref=$2", 'caller-a', request['execution_grant_ref'], expired)
        grant = await store.resource('caller-a', request['execution_grant_ref'])
        assert grant.expires_at < datetime.now(timezone.utc)
        import asyncio
        await service._execute(attempt, asyncio.Event())
        measured = await service.describe_limits('caller-a', attempt['id'])
        assert measured['execution_time']['state'] == 'ended'
        assert measured['execution_time']['started_at'] is None and measured['execution_time']['active_ms'] == 0
        assert measured['resource_usage'] == {'output_bytes': 0, 'turns': 0}
        row = await store.attempt('caller-a', attempt['id'])
        assert row['reason'] == 'grant_expired_before_start' and row['stop_confirmation'] == 'confirmed'


@pytest.mark.parametrize('evidence', ['unconfirmed_tool', 'overlapping_tools', 'missing_tool', 'read_error'])
async def test_claude_clock_requires_aggregate_tool_stop_evidence(tmp_path, monkeypatch, evidence):
    import asyncio
    import os
    import asyncpg
    from kotoyose.execution_jobs import WorkerResult
    from kotoyose.native_claude import NativeAgentResult
    from tests.fixtures.loop_limits import isolated_limits
    from tests.test_execution_agent import _resources, _service_for_admission

    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, _, _):
        service, worker = await _service_for_admission(store, tmp_path)
        monkeypatch.setenv('KOTOYOSE_AGENT_PROVIDER_TOKEN', 'test-token')
        request, _, _ = await _resources(service, clients()['caller-a'], tmp_path / 'workspace')
        request = request.model_copy(update={'limits_mode': 'agent_work_v1'})
        snapshot = await service.admission.resolve('caller-a', clients()['caller-a'], request)
        attempt, _ = await store.admit('caller-a', request.model_dump(mode='json'), snapshot, 2)
        await store.mark_running(attempt['id'])
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        async def command(job, cancel):
            nonlocal calls
            calls += 1
            if evidence == 'missing_tool':
                return WorkerResult('succeeded', 0, b'', b'', 'confirmed', 'completed')
            if evidence == 'overlapping_tools' and calls == 2:
                entered.set()
                await release.wait()
                return WorkerResult('succeeded', 0, b'', b'', 'confirmed', 'completed')
            if evidence == 'overlapping_tools':
                await entered.wait()
            return WorkerResult('failed', None, b'', b'', 'unconfirmed', 'unknown')
        worker.execute.side_effect = command
        class Runtime:
            async def execute(self, job, cancel, execute_tool, *, on_process_started):
                await on_process_started(os.getpid())
                if evidence in ('unconfirmed_tool', 'missing_tool'):
                    await execute_tool(('true',), '.')
                elif evidence == 'overlapping_tools':
                    first = asyncio.create_task(execute_tool(('false',), '.'))
                    second = asyncio.create_task(execute_tool(('true',), '.'))
                    await first
                    release.set()
                    await second
                    assert not job.tool_stop_idle.is_set()
                job.on_runtime_stopped()
                return NativeAgentResult('succeeded', 'completed', b'', 'confirmed',
                    {'source': 'unknown', 'cost': None, 'input_tokens': None, 'output_tokens': None})
        service.agent_executor.runtime = Runtime()
        if evidence == 'read_error':
            async def fail_read(attempt_id):
                raise asyncpg.PostgresError('database unavailable')
            monkeypatch.setattr(store, 'operations', fail_read)
        elif evidence == 'missing_tool':
            async def missing_read(attempt_id):
                return []
            monkeypatch.setattr(store, 'operations', missing_read)
        result = await service.agent_executor.execute(attempt, asyncio.Event(), worker=worker)
        assert result.stop_confirmation == 'confirmed'
        observed = (await service.describe_limits('caller-a', attempt['id']))['execution_time']
        assert observed['state'] == 'unknown' and observed['finished_at'] is None


async def test_poll_before_process_notice_preserves_unknown(tmp_path, monkeypatch):
    import asyncio
    import os
    from kotoyose.native_claude import NativeAgentResult
    from tests.fixtures.loop_limits import isolated_limits
    from tests.test_execution_agent import _resources, _service_for_admission

    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, _, _):
        service, worker = await _service_for_admission(store, tmp_path)
        monkeypatch.setenv('KOTOYOSE_AGENT_PROVIDER_TOKEN', 'test-token')
        request, _, _ = await _resources(service, clients()['caller-a'], tmp_path / 'workspace', tool_refs=[])
        request = request.model_copy(update={'limits_mode': 'agent_work_v1'})
        snapshot = await service.admission.resolve('caller-a', clients()['caller-a'], request)
        attempt, _ = await store.admit('caller-a', request.model_dump(mode='json'), snapshot, 2)
        await store.mark_running(attempt['id'])
        class Runtime:
            async def execute(self, job, cancel, execute_tool, *, on_process_started):
                tracker = service.agent_executor.work_clocks[attempt['id']]
                await store.limits.save_time('caller-a', attempt['id'], tracker.clock.sample())
                assert (await store.attempt('caller-a', attempt['id']))['limits_state'] == 'unknown'
                await store.pool.execute("UPDATE kotoyose.execution_attempts SET limits_state='not_started' WHERE id=$1", attempt['id'])
                observed = (await service.describe_limits('caller-a', attempt['id']))['execution_time']
                assert (observed['state'], observed['active_ms']) == ('unknown', 0)
                await on_process_started(os.getpid())
                job.on_runtime_stopped()
                return NativeAgentResult('succeeded', 'completed', b'', 'confirmed',
                    {'source': 'unknown', 'cost': None, 'input_tokens': None, 'output_tokens': None})
        service.agent_executor.runtime = Runtime()
        await service.agent_executor.execute(attempt, asyncio.Event(), worker=worker)
        assert (await service.describe_limits('caller-a', attempt['id']))['execution_time']['state'] == 'ended'


@_needs_bwrap
async def test_restart_recovers_previously_running_not_started_clock(tmp_path, monkeypatch):
    import os
    from unittest.mock import AsyncMock
    from tests.fixtures.loop_limits import isolated_limits
    from tests.test_execution_agent import _service_for_admission
    from tests.test_execution_store import _admit_ready

    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, _, _):
        _, _, request, snapshot = await _admit_ready(store, tmp_path)
        request['limits_mode'] = 'agent_work_v1'
        snapshot['kind'] = 'agent'
        attempt, _ = await store.admit('caller-a', request, snapshot, 1)
        await store.mark_running(attempt['id'])
        await store.pool.execute("UPDATE kotoyose.execution_attempts SET limits_state='not_started' WHERE id=$1", attempt['id'])
        service, _ = await _service_for_admission(store, tmp_path)
        monkeypatch.setenv('KOTOYOSE_AGENT_CALLER_TOKEN', 'fixture-token')
        monkeypatch.setattr(service, '_stop_attempt', AsyncMock(return_value='confirmed'))
        await service.start()
        try:
            observed = (await service.describe_limits('caller-a', attempt['id']))['execution_time']
            assert (observed['state'], observed['active_ms'], observed['finished_at']) == ('unknown', 0, None)
        finally:
            await service.close()
