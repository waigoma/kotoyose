from datetime import datetime, timedelta, timezone

import pytest
from tests.codex_runtime_fixture import codex_rootfs  # noqa: F401
from tests.test_native_codex_protocol_worker import worker  # noqa: F401

from kotoyose.agent_deadlines import validate_agent_start
from kotoyose.execution_resources import ExecutionError


def test_expiring_grant_never_shortens_hard():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    snapshot = {
        'limits_mode': 'agent_work_v1',
        'timeout': {'hard_seconds': 10, 'grace_seconds': 2},
        'grant': {'expires_at': (now + timedelta(seconds=12)).isoformat(), 'max_seconds': 10},
        'budget': {'deadline': (now + timedelta(seconds=20)).isoformat()},
    }
    validate_agent_start(snapshot, now)
    snapshot['grant']['expires_at'] = (now + timedelta(seconds=11)).isoformat()
    with pytest.raises(ExecutionError) as exc:
        validate_agent_start(snapshot, now)
    assert exc.value.code == 'grant_expired_before_start'


def test_native_required_never_becomes_durable_state():
    from types import SimpleNamespace
    from kotoyose.execution_resume import build_resume_manifest
    from kotoyose.execution_resources import content_digest
    from kotoyose.runtime.resume import ResumeSupport

    old = {'kind': 'agent', 'provider': 'claude'}
    previous = {'id': 'old', 'snapshot': old, 'request': {'caller_run_ref': 'run'},
                'workspace_id': 'workspace', 'generation': 0}
    snapshot = {**old, 'workspace_id': 'workspace', 'generation': 1, 'grant': {}}
    request = SimpleNamespace(execution=SimpleNamespace(kind='agent', caller_run_ref='run'),
        predecessor_snapshot_digest=content_digest(old), required_resume_mode='native')
    with pytest.raises(ExecutionError) as exc:
        build_resume_manifest('caller', previous, [], request, snapshot, ResumeSupport(('durable_state',), 'fixture'))
    assert exc.value.code == 'resume_unsafe'
    assert exc.value.reason_code == 'adapter_unsupported'


async def test_revoked_grant_never_starts(tmp_path):
    import asyncio
    import os
    from tests.fixtures.loop_limits import isolated_limits
    from tests.test_execution_store import _admit_ready

    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, service, _):
        _, refs, request, snapshot = await _admit_ready(store, tmp_path)
        request['limits_mode'] = 'agent_work_v1'
        snapshot.update(kind='agent', limits_mode='agent_work_v1')
        attempt, _ = await store.admit('caller-a', request, snapshot, 1)
        await store.revoke_grant('caller-a', refs['grant'])
        await service._execute(attempt, asyncio.Event())
        row = await store.attempt('caller-a', attempt['id'])
        assert row['stop_confirmation'] == 'confirmed'
        assert row['limits_started_at'] is None and row['limits_active_ms'] == 0
        assert not await store.operations(attempt['id']) or all(
            op['kind'] != 'provider_process' for op in await store.operations(attempt['id']))


async def test_insufficient_budget_is_proven_unstarted(tmp_path):
    import asyncio
    import os
    from tests.fixtures.loop_limits import isolated_limits
    from tests.test_execution_store import _admit_ready

    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, service, _):
        _, refs, request, snapshot = await _admit_ready(store, tmp_path)
        grant = await store.resource('caller-a', refs['grant'])
        now = datetime.now(timezone.utc)
        request['limits_mode'] = 'agent_work_v1'
        snapshot.update(kind='agent', limits_mode='agent_work_v1', grant=grant.model_dump(mode='json'),
            timeout={'hard_seconds': 10, 'grace_seconds': 1},
            budget={'deadline': (now + timedelta(seconds=5)).isoformat()},
            deadline=(now + timedelta(seconds=5)).isoformat())
        attempt, _ = await store.admit('caller-a', request, snapshot, 1)
        await service._execute(attempt, asyncio.Event())
        row = await store.attempt('caller-a', attempt['id'])
        assert (row['status'], row['reason'], row['stop_confirmation']) == ('failed', 'grant_expired_before_start', 'confirmed')
        assert row['limits_started_at'] is None and row['limits_active_ms'] == 0
        assert (row['limits_output_bytes'], row['limits_turns']) == (0, 0)


def test_legacy_resume_request_digest_is_unchanged():
    from kotoyose.execution_contract import AgentExecution, ResumeRequest
    from kotoyose.execution_resources import content_digest
    from kotoyose.execution_resume import resume_request_digest

    request = ResumeRequest(schema_version='1', predecessor_snapshot_digest='a' * 64, execution=AgentExecution(
        schema_version='1', kind='agent', caller_run_ref='run', idempotency_key='key', workspace_ref='workspace',
        execution_grant_ref='grant', budgets_ref='budget', agent_definition_ref='definition', runtime_profile='profile',
        inputs=[{'kind': 'task_request', 'provenance': 'project', 'content_ref': 'input'}],
    ))
    payload = request.model_dump(mode='json')
    assert 'required_resume_mode' not in payload and 'limits_mode' not in payload['execution']
    assert resume_request_digest('old', request) == content_digest({'predecessor_attempt_id': 'old', **payload})


async def test_claude_preparation_does_not_consume_work_timeout(tmp_path, monkeypatch):
    from kotoyose.agent_work_clock import AgentWorkClock
    from kotoyose.agent_deadlines import AgentStartRejected
    from kotoyose.native_claude import NativeClaudeWorker
    from tests.test_native_claude import _Client, _job, _result_message, _successful_tool
    from unittest.mock import AsyncMock, Mock
    import asyncio

    class SlowConnect(_Client):
        messages = [_result_message()]
        wait_for_messages = False

        async def connect(self):
            await asyncio.sleep(0.15)

    monkeypatch.setattr('kotoyose.native_claude.ClaudeSDKClient', SlowConnect)
    clock = AgentWorkClock()
    hard = 0.1
    async def started(_pid):
        clock.start()
    async def validate():
        pass
    job = _job(tmp_path, timeout_seconds=hard, limits_mode='agent_work_v1', validate_start=validate,
               work_remaining=lambda: hard - clock.sample().active_ms / 1000)
    result = await NativeClaudeWorker().execute(job, asyncio.Event(), _successful_tool, on_process_started=started)
    assert result.status == 'succeeded'
    assert clock.sample().started_at is not None


    client = Mock()
    monkeypatch.setattr('kotoyose.native_claude.ClaudeSDKClient', client)
    async def reject():
        raise AgentStartRejected('grant_expired_before_start', 'insufficient time')
    job = _job(tmp_path, limits_mode='agent_work_v1', validate_start=reject)
    with pytest.raises(AgentStartRejected):
        await NativeClaudeWorker().execute(job, asyncio.Event(), _successful_tool, on_process_started=lambda _: None)
    client.assert_not_called()

    class FailsAfterLaunchAttempt(_Client):
        async def connect(self):
            await asyncio.sleep(0)
            raise AgentStartRejected('grant_expired_before_start', 'fixture')
    monkeypatch.setattr('kotoyose.native_claude.ClaudeSDKClient', FailsAfterLaunchAttempt)
    async def pending_action(_worker, invocation, _owner):
        invocation.required_action = {'fixture': 'pending'}
        await asyncio.Future()
    with monkeypatch.context() as patch:
        patch.setattr(NativeClaudeWorker, '_watchdog', pending_action)
        result = await NativeClaudeWorker().execute(_job(tmp_path, limits_mode='agent_work_v1'), asyncio.Event(),
                                                     _successful_tool, on_process_started=AsyncMock())
    assert (result.status, result.reason, result.launch_outcome) == ('failed', 'sdk_error', 'unknown')
    assert result.required_action is None
    monkeypatch.setattr('kotoyose.native_claude.ClaudeSDKClient', _Client)
    result = await NativeClaudeWorker().execute(_job(tmp_path, limits_mode='agent_work_v1'), asyncio.Event(),
        _successful_tool, on_process_started=AsyncMock(
            side_effect=AgentStartRejected('grant_expired_before_start', 'fixture')))
    assert (result.status, result.reason, result.launch_outcome) == ('failed', 'sdk_error', 'unknown')
    monkeypatch.setattr(_Client, 'messages', [_result_message()])
    def late_stop():
        raise AgentStartRejected('grant_expired_before_start', 'fixture')
    result = await NativeClaudeWorker().execute(_job(tmp_path, limits_mode='agent_work_v1', on_runtime_stopped=late_stop),
        asyncio.Event(), _successful_tool, on_process_started=AsyncMock())
    assert (result.status, result.reason, result.launch_outcome) == ('failed', 'sdk_error', 'unknown')


@pytest.mark.parametrize('cleanup', ['confirmed', 'unconfirmed'])
async def test_codex_prestart_expiry_waits_for_egress_cleanup(tmp_path, monkeypatch, codex_rootfs, worker, cleanup):
    import asyncio
    from unittest.mock import AsyncMock
    from kotoyose.native_codex import NativeCodexWorker
    from kotoyose import native_codex
    from kotoyose.agent_work_clock import AgentWorkClock
    from tests.test_native_codex import job

    class Egress:
        def __init__(self, *args, **kwargs):
            self.directory = None
            self.failure_reason = None
            self.failure_limit = None
            self.closed = False
        async def start(self):
            raise TimeoutError
        async def close(self, grace):
            self.closed = True
            if cleanup == 'unconfirmed':
                raise RuntimeError('fixture cleanup failed')
            return cleanup
        def redact(self, content):
            return content
    instances = []
    def create(*args, **kwargs):
        obj = Egress(*args, **kwargs)
        instances.append(obj)
        return obj
    real_egress = native_codex.CodexEgress
    monkeypatch.setattr('kotoyose.native_codex.CodexEgress', create)
    from kotoyose.agent_deadlines import AgentStartRejected
    async def expired():
        raise AgentStartRejected('grant_expired_before_start', 'fixture')
    result = await NativeCodexWorker().execute(job(tmp_path, limits_mode='agent_work_v1',
        codex_rootfs=codex_rootfs[0], codex_rootfs_digest=codex_rootfs[1],
        work_remaining=lambda: 10, validate_start=expired), asyncio.Event(), AsyncMock(), on_process_started=AsyncMock())
    assert instances[0].closed
    assert (result.status, result.reason, result.stop_confirmation) == ('failed', 'grant_expired_before_start', cleanup)
    assert (result.output_bytes, result.turns) == (0, 0)

    monkeypatch.setattr('kotoyose.native_codex.CodexEgress', real_egress)
    original_start = real_egress.start
    now = [0.0]
    clock = AgentWorkClock(monotonic=lambda: now[0])
    async def delayed_start(egress):
        now[0] += 300
        await asyncio.sleep(0.65)
        return await original_start(egress)
    monkeypatch.setattr(real_egress, 'start', delayed_start)
    async def started(_pid):
        assert now[0] == 300
        clock.start()
    validate = AsyncMock()
    runtime, rootfs = worker
    active = await runtime.execute(job(tmp_path, limits_mode='agent_work_v1',
        codex_rootfs=rootfs[0], codex_rootfs_digest=rootfs[1], output_contract='json',
        timeout_seconds=0.5, deadline_monotonic=asyncio.get_running_loop().time() + 60,
        work_remaining=lambda: 0.5 - clock.sample().active_ms / 1000, validate_start=validate),
        asyncio.Event(), AsyncMock(), on_process_started=started)
    assert active.status == 'succeeded' and active.stop_confirmation == 'confirmed'
    assert clock.sample().active_ms == 0
    validate.assert_awaited_once()
    attempted = await runtime.execute(job(tmp_path, limits_mode='agent_work_v1',
        codex_rootfs=rootfs[0], codex_rootfs_digest=rootfs[1], timeout_seconds=2,
        deadline_monotonic=asyncio.get_running_loop().time() + 60,
        work_remaining=lambda: 2, validate_start=AsyncMock()), asyncio.Event(), AsyncMock(),
        on_process_started=AsyncMock(side_effect=AgentStartRejected('permission_denied', 'fixture')))
    assert (attempted.status, attempted.reason, attempted.launch_outcome) == ('failed', 'provider_error', 'started')
    not_started = AsyncMock()
    rejected = await runtime.execute(job(tmp_path, limits_mode='agent_work_v1',
        codex_rootfs=rootfs[0], codex_rootfs_digest=rootfs[1],
        timeout_seconds=2, deadline_monotonic=asyncio.get_running_loop().time() + 60,
        work_remaining=lambda: 2, validate_start=expired),
        asyncio.Event(), AsyncMock(), on_process_started=not_started)
    assert (rejected.reason, rejected.stop_confirmation, rejected.output_bytes, rejected.turns) == (
        'grant_expired_before_start', 'confirmed', 0, 0)
    not_started.assert_not_awaited()
    async def uncertain_spawn(*args, **kwargs):
        raise TimeoutError
    monkeypatch.setattr('kotoyose.native_codex.asyncio.create_subprocess_exec', uncertain_spawn)
    ambiguous = await runtime.execute(job(tmp_path, limits_mode='agent_work_v1',
        codex_rootfs=rootfs[0], codex_rootfs_digest=rootfs[1],
        timeout_seconds=2, deadline_monotonic=asyncio.get_running_loop().time() + 60,
        work_remaining=lambda: 2, validate_start=AsyncMock()),
        asyncio.Event(), AsyncMock(), on_process_started=AsyncMock())
    assert (ambiguous.reason, ambiguous.stop_confirmation) == ('timeout', 'unconfirmed')


def test_capabilities_advertise_complete_contract():
    from kotoyose.capabilities import capability_manifest
    assert capability_manifest().capabilities['agent_execution_v1'].status == 'unsupported'
