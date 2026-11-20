"""圧縮 mode の拒否、SDK 観測の欠測、機密 metadata の除外を検証する。"""

import asyncio
from unittest.mock import patch

import pytest
from claude_agent_sdk import SystemMessage

from kotoyose.execution_resources import ExecutionError
from kotoyose.execution_service import ExecutionService
from kotoyose.native_claude import NativeClaudeWorker
from kotoyose.native_compression import NativeCompressionObserver, context_usage_projection
from tests.test_execution_agent import _config, _NoopWorker, _resources
from tests.test_execution_store import store  # noqa: F401
from tests.test_native_claude import _Client, _job, _result_message, _successful_tool
from tests.fixtures.clients import clients, set_client


def sample():
    return {'totalTokens': 50, 'maxTokens': 100, 'rawMaxTokens': 120, 'percentage': 50,
            'isAutoCompactEnabled': True, 'memoryFiles': [{'path': '/private/memory', 'content': 'SECRET'}],
            'model': 'provider-model', 'mcpTools': [{'token': 'SECRET'}]}


@pytest.mark.parametrize('replacement', [{'totalTokens': True}, {'totalTokens': -1}, {'maxTokens': 0}, {'percentage': float('nan')}, {'isAutoCompactEnabled': 'true'}])
def test_invalid_observation_cannot_be_reported_as_measured_usage(replacement):
    with pytest.raises(ValueError):
        context_usage_projection({**sample(), **replacement})


def test_context_projection_excludes_runtime_files_and_preserves_real_zero():
    assert 'SECRET' not in repr(context_usage_projection(sample()))
    assert context_usage_projection({**sample(), 'totalTokens': 0, 'percentage': 0})['total_tokens'] == 0


@pytest.mark.asyncio
async def test_sdk_boundary_and_context_are_reported_without_summary_or_grant_changes(tmp_path):
    class Client(_Client):
        messages = [SystemMessage(subtype='compact_boundary', data={'compact_metadata': {'trigger': 'auto', 'pre_tokens': 99}, 'summary': 'SECRET'}), _result_message()]
        async def get_context_usage(self):
            return sample()
    events = []
    async def emit(kind, payload):
        events.append((kind, payload))
    async def started(pid):
        assert pid > 0
    job = _job(tmp_path, compression_event=emit)
    with patch('kotoyose.native_claude.ClaudeSDKClient', Client):
        result = await NativeClaudeWorker().execute(job, asyncio.Event(), _successful_tool, on_process_started=started)
    assert result.status == 'succeeded'
    assert events[0] == ('compression_boundary', {'mode': 'runtime_managed', 'source': 'provider_runtime', 'trigger': 'auto', 'pre_tokens': 99, 'coverage': 'partial'})
    assert events[1][0] == 'context_usage' and events[1][1]['state'] == 'observed'
    assert 'SECRET' not in repr(events)
    assert Client.instances[-1].queried == [job.prompt]
    assert Client.instances[-1].options.system_prompt == job.instructions


@pytest.mark.asyncio
async def test_missing_or_expired_sample_stays_unavailable_and_boundary_stream_is_bounded():
    events = []
    async def emit(kind, payload):
        events.append((kind, payload))
    observer = NativeCompressionObserver(emit)
    await observer.sample(object(), asyncio.get_running_loop().time() + 10)
    await observer.sample(object(), asyncio.get_running_loop().time() - 1)
    assert all(payload['state'] == 'unavailable' and 'total_tokens' not in payload for _, payload in events)
    await observer.boundary({'compact_metadata': {'trigger': {}, 'pre_tokens': None}})
    for _ in range(140):
        await observer.boundary({'compact_metadata': {'trigger': 'unknown', 'pre_tokens': 'invalid'}})
    assert len([kind for kind, _ in events if kind == 'compression_boundary']) == 128
    assert len([kind for kind, _ in events if kind == 'compression_observation_limited']) == 1
    assert events[2][1]['trigger'] == 'unobserved' and events[2][1]['pre_tokens'] is None


@pytest.mark.asyncio
async def test_observation_timeout_is_bounded_and_task_cancellation_propagates(monkeypatch):
    class SlowClient:
        async def get_context_usage(self):
            await asyncio.sleep(10)
    class CancelledClient:
        async def get_context_usage(self):
            raise asyncio.CancelledError
    events = []
    async def emit(kind, payload):
        events.append(payload)
    monkeypatch.setattr('kotoyose.native_compression.CONTEXT_SAMPLE_SECONDS', 0.01)
    observer = NativeCompressionObserver(emit)
    async with asyncio.timeout(1):
        await observer.sample(SlowClient(), asyncio.get_running_loop().time() + 10)
    assert events[0]['state'] == 'unavailable'
    with pytest.raises(asyncio.CancelledError):
        await observer.sample(CancelledClient(), asyncio.get_running_loop().time() + 10)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_caller_controlled_profile_is_denied_before_attempt_and_runtime_policy_is_pinned(store, tmp_path):
    config = _config(tmp_path)
    service = ExecutionService(config, store, _NoopWorker())
    policy = clients()['caller-a']
    request, _, _ = await _resources(service, policy, tmp_path / 'workspace', tool_refs=[])
    config.agent_compression_mode = 'caller_controlled'
    with pytest.raises(ExecutionError) as denied:
        await service.admission.resolve('caller-a', policy, request)
    assert denied.value.code == 'unsupported_capability'
    assert await store.pool.fetchval('SELECT count(*) FROM kotoyose.execution_attempts') == 0
    config.agent_compression_mode = 'runtime_managed'
    snapshot = await service.admission.resolve('caller-a', policy, request)
    assert snapshot['compression_policy']['mode'] == 'runtime_managed'
    assert service.manifest.capabilities['context_observation'].status == 'conditional'
    assert service.manifest.capabilities['manual_compression'].status == 'unsupported'
    attempt, _ = await store.admit('caller-a', request.model_dump(mode='json'), snapshot, 1)
    assert (await service.describe('caller-a', attempt['id']))['compression']['observation']['state'] == 'unobserved'
    await store.append_event(attempt['id'], 'context_usage', context_usage_projection(sample()))
    observed = (await service.describe('caller-a', attempt['id']))['compression']
    assert observed['observation']['state'] == 'observed' and observed['observed_at'] is not None
