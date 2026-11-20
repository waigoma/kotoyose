"""Offline expiry-template regressions. No provider, Docker, or database connection."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from kotoyose.compatibility_config import CompatibilityProfile, NativeAliasSettings, load_compatibility_config
from kotoyose.compatibility_execution import CompatibilityExecution
from kotoyose.config import ModelPreset
from kotoyose.execution_resources import ExecutionError, content_digest
from kotoyose.models import ChatCompletionRequest

NOW = datetime(2026, 10, 10, 23, 59, 55, tzinfo=timezone.utc)


def values(**extra):
    return dict(runtime_profile='profile:test', workspace_path='/tmp/expiry-test',
                caller_workspace_ref='workspace:test', policy_ref='policy:test',
                instructions='Classify supplied text only.',
                timeout=dict(schema_version='1', kind='timeout_policy', soft_seconds=5,
                             hard_seconds=20, grace_seconds=1), **extra)


@pytest.mark.parametrize('ttl', [1, 20, 86400])
def test_relative_schema_and_hard_timeout(ttl):
    p = CompatibilityProfile(**values(expires_after_seconds=ttl))
    assert p.execution_deadline(NOW) == NOW + timedelta(seconds=min(ttl, 20))
    assert 'expires_at' not in p.model_dump(mode='json')
    assert p.model_dump(mode='json')['expires_after_seconds'] == ttl


@pytest.mark.parametrize('ttl', [0, -1, 86401, True, False, 1.0, 1.5, '20'])
def test_relative_integer_bounds_are_strict(ttl):
    with pytest.raises(ValidationError):
        CompatibilityProfile(**values(expires_after_seconds=ttl))


@pytest.mark.parametrize('expiry', [{}, {'expires_at': None},
    {'expires_after_seconds': None}, {'expires_at': NOW, 'expires_after_seconds': 20},
    {'expires_at': NOW.replace(tzinfo=None)}, {'expires_at': NOW, 'ttl': 20}])
def test_invalid_or_ambiguous_expiry_rejected(expiry):
    with pytest.raises(ValidationError):
        CompatibilityProfile(**values(**expiry))


def test_absolute_mode_preserves_legacy_canonical_serialization():
    # Fixed exact baseline fixture, including its nullable runtime_profile and all defaults.
    p = CompatibilityProfile(**values(expires_at=NOW))
    expected = {'runtime_profile': 'profile:test', 'workspace_path': '/tmp/expiry-test',
        'caller_workspace_ref': 'workspace:test', 'policy_ref': 'policy:test', 'policy_version': 1,
        'expires_at': '2026-10-10T23:59:55Z', 'instructions': 'Classify supplied text only.',
        'accept_system_instructions': False, 'tool_refs': [], 'max_turns': 10,
        'timeout': {'schema_version': '1', 'kind': 'timeout_policy', 'soft_seconds': 5.0,
                    'hard_seconds': 20.0, 'grace_seconds': 1.0},
        'memory_bytes': 536870912, 'pids_limit': 128, 'cpus': 1.0, 'output_limit_bytes': 1048576}
    assert p.model_dump(mode='json') == expected
    assert content_digest(p.model_dump(mode='json')) == content_digest(expected)
    p.runtime_profile = None
    assert p.model_dump(mode='json')['runtime_profile'] is None
    alias = NativeAliasSettings(**values(expires_at=NOW), callers=['caller'])
    assert alias.profile().model_dump(mode='json') == expected
    assert alias.model_dump(mode='json') == dict(expected, callers=['caller'])


def test_native_yaml_and_json_modes_round_trip(tmp_path, monkeypatch):
    import json
    alias = NativeAliasSettings(**values(expires_after_seconds=20), callers=['caller'])
    dumped = alias.model_dump(mode='json')
    assert 'expires_at' not in dumped
    monkeypatch.delenv('KOTOYOSE_COMPATIBILITY_CONFIG', raising=False)
    config = load_compatibility_config({'alias': ModelPreset(execution='native', native=alias)})
    assert config.profile('caller', 'alias').expires_after_seconds == 20
    path = tmp_path / 'compat.json'
    path.write_text(json.dumps({'schema_version': '1', 'callers': {'caller': {'alias': alias.profile().model_dump(mode='json')}}}))
    monkeypatch.setenv('KOTOYOSE_COMPATIBILITY_CONFIG', str(path))
    assert load_compatibility_config({}).profile('caller', 'alias').model_dump(mode='json') == alias.profile().model_dump(mode='json')


class Clock(datetime):
    current = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.current


class MemoryPool:
    """Exercises the real CompatibilityRequestStore lookup/create path without PostgreSQL."""
    def __init__(self):
        self.rows = {}
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            yield self

    async def execute(self, query, *args):
        if 'INSERT INTO kotoyose.compatibility_requests' in query:
            principal, key, digest, execution = args
            self.rows[principal, key] = {'request_digest': digest, 'execution': execution}

    async def fetchrow(self, query, principal, key):
        return self.rows.get((principal, key))


@pytest.fixture
def kotoyose(monkeypatch):
    import kotoyose.compatibility_execution as module
    Clock.current = NOW
    monkeypatch.setattr(module, 'datetime', Clock)
    profile = CompatibilityProfile(**values(expires_after_seconds=12))
    resources = {}
    pool = MemoryPool()
    async def register(principal, policy, resource):
        ref = 'resource:' + str(len(resources))
        resources[ref] = resource
        return ref
    service = SimpleNamespace(store=SimpleNamespace(pool=pool),
        config=SimpleNamespace(input_artifact_limits=None),
        register_resource=AsyncMock(side_effect=register),
        register_workspace=AsyncMock(return_value='workspace:test'),
        dispatch=AsyncMock(return_value='attempt:test'))
    # No images in these text-only classifier requests; bypass only decoding.
    monkeypatch.setattr(module, 'validate_chat_images', lambda *a, **k: {})
    def adapter():
        result = CompatibilityExecution(service, None)
        result.profile = lambda *a: profile
        return result
    request = ChatCompletionRequest(model='alias', messages=[{'role':'user','content':'classify'}])
    return SimpleNamespace(profile=profile, resources=resources, service=service,
                           pool=pool, adapter=adapter, request=request)


def grant_for(h, execution):
    return h.resources[execution.execution_grant_ref]


@pytest.mark.asyncio
async def test_new_request_after_midnight_gets_finite_new_window(kotoyose):
    h = kotoyose
    adapter = h.adapter()
    await adapter.dispatch('caller', None, h.request, request_key='one')
    first = h.service.dispatch.call_args.args[2]
    original = grant_for(h, first)
    assert original.expires_at == NOW + timedelta(seconds=12)
    assert h.resources[first.budgets_ref].deadline == original.expires_at
    Clock.current = NOW + timedelta(days=1)
    await adapter.dispatch('caller', None, h.request, request_key='two')
    second = h.service.dispatch.call_args.args[2]
    assert grant_for(h, second).expires_at == Clock.current + timedelta(seconds=12)
    assert grant_for(h, first) == original
    assert grant_for(h, second).network == 'none'
    assert grant_for(h, second).delegation == 'none'
    assert grant_for(h, second).workspace_ref == 'workspace:test'
    assert grant_for(h, second).runtime_profiles == ['profile:test']


@pytest.mark.asyncio
async def test_replay_after_restart_and_expiry_never_refreshes_grant(kotoyose):
    h = kotoyose
    await h.adapter().dispatch('caller', None, h.request, request_key='same')
    first = h.service.dispatch.call_args.args[2]
    count = len(h.resources)
    Clock.current = NOW + timedelta(days=2)
    await h.adapter().dispatch('caller', None, h.request, request_key='same')
    assert h.service.dispatch.call_args.args[2] == first
    assert len(h.resources) == count
    assert grant_for(h, first).expires_at < Clock.current
    assert h.service.register_workspace.await_count == 1


@pytest.mark.asyncio
async def test_lost_dispatch_and_concurrent_reconnect_keep_first_window(kotoyose):
    h = kotoyose
    h.service.dispatch.side_effect = ExecutionError('transport_failure', 'lost', 503)
    with pytest.raises(ExecutionError):
        await h.adapter().dispatch('caller', None, h.request, request_key='same')
    first = h.service.dispatch.call_args.args[2]
    count = len(h.resources)
    Clock.current += timedelta(seconds=10)
    h.service.dispatch.side_effect = None
    await asyncio.gather(*(h.adapter().dispatch('caller', None, h.request, request_key='same') for _ in range(6)))
    assert all(call.args[2] == first for call in h.service.dispatch.call_args_list)
    assert len(h.resources) == count
    assert h.service.register_workspace.await_count == 1


@pytest.mark.asyncio
async def test_changed_relative_policy_conflicts_without_new_resources(kotoyose):
    h = kotoyose
    await h.adapter().dispatch('caller', None, h.request, request_key='same')
    count = len(h.resources)
    h.profile.expires_after_seconds = 13
    with pytest.raises(ExecutionError, match='different input or policy'):
        await h.adapter().dispatch('caller', None, h.request, request_key='same')
    assert len(h.resources) == count


@pytest.mark.asyncio
async def test_expired_absolute_policy_stops_before_workspace(kotoyose):
    h = kotoyose
    h.profile.expires_after_seconds = None
    h.profile.expires_at = NOW
    with pytest.raises(ExecutionError, match='expired'):
        await h.adapter().dispatch('caller', None, h.request, request_key='new')
    h.service.register_workspace.assert_not_awaited()
    h.service.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_prepared_request_rejected_by_real_admission(kotoyose, tmp_path, monkeypatch):
    import kotoyose.execution_admission as module
    h = kotoyose
    await h.adapter().dispatch('caller', None, h.request, request_key='prepared')
    execution = h.service.dispatch.call_args.args[2]
    Clock.current += timedelta(seconds=13)
    await h.adapter().dispatch('caller', None, h.request, request_key='prepared')
    replay = h.service.dispatch.call_args.args[2]
    monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(module, 'agent_profile_allowed', lambda *a, **k: True)
    monkeypatch.setattr('kotoyose.execution_resources.agent_profile_allowed', lambda *a, **k: True)
    path = tmp_path / 'workspace'
    path.mkdir()
    info = path.stat()
    store = SimpleNamespace(workspace=AsyncMock(return_value={'path': str(path),
        'device': info.st_dev, 'inode': info.st_ino}))
    admission = module.ExecutionAdmission(store, SimpleNamespace(capabilities={
        name: SimpleNamespace(status='supported') for name in ('native_execution', 'sandbox')}), 'image')
    admission._effective_request = lambda request: (request, None)
    admission._select_worker = lambda *a, **k: SimpleNamespace(workspace_locality='local')
    admission._get = AsyncMock(side_effect=lambda principal, ref, cls: h.resources[ref])
    policy = SimpleNamespace(places=['isolated'], workspace_roots=[tmp_path])
    admitted = replay.model_copy(update={'model': 'profile:test', 'runtime_profile': None})
    with pytest.raises(ExecutionError, match='outside its validity interval'):
        await admission.resolve('caller', policy, admitted)
    assert replay == execution
    assert h.service.register_workspace.await_count == 1
