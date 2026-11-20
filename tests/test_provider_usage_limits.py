from datetime import datetime, timezone, timedelta

from kotoyose.provider_usage_limits import normalize_provider_usage_limit
from tests.test_native_codex import provider, attempt_counter_fixture  # noqa: F401
from tests.codex_runtime_fixture import codex_rootfs  # noqa: F401

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_claude_rejected_five_hour_has_reset():
    result = normalize_provider_usage_limit('claude', {'status': 'rejected', 'rate_limit_type': 'five_hour', 'resets_at': 1767229200}, NOW)
    assert result.limit_window == 'five_hour'
    assert result.reset_at == NOW + timedelta(hours=1)
    assert result.detected_at == NOW
    assert result.resume == 'unavailable' and result.unavailable_reason == 'no_session'


def test_claude_warning_is_advisory():
    assert normalize_provider_usage_limit('claude', {'status': 'allowed_warning'}, NOW) is None


def test_codex_quota_error_is_typed():
    result = normalize_provider_usage_limit('codex', {'code': 'usageLimitExceeded'}, NOW)
    assert result and result.limit_window == 'unknown'


def test_broker_429_without_reset_is_rate_wait():
    result = normalize_provider_usage_limit('codex', {'http_status': 429}, NOW)
    assert result and result.limit_window == 'rate' and result.reset_at is None


def test_retry_after_is_normalized_without_header_leak():
    for value in ('60', 'Thu, 01 Jan 2026 00:01:00 GMT', 'Thursday, 01-Jan-26 00:01:00 GMT', 'Thu Jan  1 00:01:00 2026'):
        result = normalize_provider_usage_limit('codex', {'http_status': 429, 'retry_after': value, 'body': 'private'}, NOW)
        assert result.reset_at == NOW + timedelta(seconds=60)
        assert 'private' not in result.model_dump_json() and 'retry_after' not in result.model_dump_json()
    assert normalize_provider_usage_limit('codex', {'http_status': 429, 'retry_after': 'Sunday, 01-Jan-68 00:01:00 GMT'}, NOW).reset_at == datetime(2068, 1, 1, 0, 1, tzinfo=timezone.utc)
    assert normalize_provider_usage_limit('codex', {'http_status': 429, 'retry_after': 'Saturday, 01-Jan-77 00:01:00 GMT'}, NOW).reset_at == datetime(1977, 1, 1, 0, 1, tzinfo=timezone.utc)
    for value in ('Sun Jan  1 00:01:00 0068', 'Sun, 01 Jan 0068 00:01:00 GMT'):
        assert normalize_provider_usage_limit('codex', {'http_status': 429, 'retry_after': value}, NOW).reset_at == datetime(68, 1, 1, 0, 1, tzinfo=timezone.utc)
    for value in ('-1', '1.5', 'bogus', 'Sat Jan  1 00:01:00 0000', 'Sat, 01 Jan 0000 00:01:00 GMT', 'Thu, 01 Jan 2026 00:01:00 GMT garbage', 'Thu, 01 Jan 2026 00:01 GMT'):
        assert normalize_provider_usage_limit('codex', {'http_status': 429, 'retry_after': value}, NOW).reset_at is None


def test_unknown_provider_error_remains_fault():
    assert normalize_provider_usage_limit('codex', {'code': 'internalServerError'}, NOW) is None


def test_authentication_is_not_quota():
    for status in (401, 403):
        assert normalize_provider_usage_limit('codex', {'http_status': status}, NOW) is None


async def test_resume_resource_counters_are_attempt_local(tmp_path, provider, monkeypatch):
    import os
    from tests.fixtures.loop_limits import isolated_limits
    from tests.test_execution_store import _admit_ready
    from kotoyose.execution_limits import AgentResourceUsage
    async with isolated_limits(os.environ.get('KOTOYOSE_EXECUTION_TEST_DATABASE_URL')) as (store, service, _):
        async with attempt_counter_fixture(provider, tmp_path, monkeypatch) as (execute, ref, calls):
            _, _, request, snapshot = await _admit_ready(store, tmp_path)
            request['limits_mode'] = 'agent_work_v1'
            snapshot['kind'] = 'agent'
            first, _ = await store.admit('caller-a', request, snapshot, 1)
            initial = await service.describe_limits('caller-a', first['id'])
            first_result = await execute('resume-first', first['id'])
            assert (first_result.reason, first_result.output_bytes, first_result.turns, first_result.provider_session_id) == ('provider_usage_limit', 17, 3, ref)
            await store.finish(first['id'], first_result.status, first_result.reason, first_result.stop_confirmation, None, {},
                provider_session_ref=first_result.provider_session_id,
                limits_result=(AgentResourceUsage(output_bytes=first_result.output_bytes, turns=first_result.turns), first_result.provider_limit))
            observed = await service.describe_limits('caller-a', first['id'])
            assert observed['resource_usage'] == {'output_bytes': 17, 'turns': 3}
            assert observed['provider_limit']['limit_window'] == 'unknown'
            assert observed['sequence'] > initial['sequence']
            assert observed['execution_time']['active_ms'] == 0
            predecessor = await store.attempt('caller-a', first['id'])
            assert predecessor['reason'] == 'provider_usage_limit'
            from kotoyose.execution_resources import content_digest
            from kotoyose.execution_resume import operations_digest
            import json
            grant = await store.pool.fetchval("SELECT payload FROM kotoyose.execution_resources WHERE principal=$1 AND ref=$2", 'caller-a', request['execution_grant_ref'])
            generation = predecessor['generation'] + 1
            manifest = {
                'schema_version': '1', 'predecessor_attempt_id': first['id'],
                'predecessor_generation': predecessor['generation'], 'predecessor_status': predecessor['status'],
                'predecessor_sequence': predecessor['sequence'], 'stop_confirmation': 'confirmed',
                'side_effect_resolution': 'resolved', 'operations_digest': operations_digest(await store.operations(first['id'])),
                'predecessor_snapshot_digest': content_digest(predecessor['snapshot']),
                'provider_session_ref': ref, 'provider_session_reason': None, 'provider': 'codex',
                'runtime_profile': None, 'model': None, 'mode': 'native',
                'workspace_ref': f"workspace:{first['workspace_id']}:{generation}", 'workspace_generation': generation,
                'checkpoint_ref': None, 'checkpoint_digest': None, 'execution_grant_ref': request['execution_grant_ref'],
                'grant_version': 1, 'grant_digest': content_digest(json.loads(grant)), 'principal': 'caller-a',
                'caller_run_ref': request['caller_run_ref'], 'resume_request_key': 'resume-1', 'request_digest': 'a' * 64,
            }
            resumed_request = {**request, 'idempotency_key': 'resume-1', 'workspace_ref': manifest['workspace_ref']}
            resumed_snapshot = {**snapshot, 'generation': generation, 'deadline': (NOW + timedelta(days=10000)).isoformat(),
                                'resume_manifest': manifest, 'resume_manifest_digest': content_digest(manifest), 'resume_mode': 'native'}
            second, created = await store.admit('caller-a', resumed_request, resumed_snapshot, 1)
            assert created and second['api_operation'] == 'resume'
            before = await service.describe_limits('caller-a', second['id'])
            assert before['resource_usage'] == {'output_bytes': None, 'turns': None}
            second_result = await execute('resume-next', second['id'])
            assert (second_result.status, second_result.output_bytes, second_result.turns, second_result.provider_session_id) == ('succeeded', 42, 2, ref)
            await store.finish(second['id'], second_result.status, second_result.reason, second_result.stop_confirmation, None, {},
                provider_session_ref=second_result.provider_session_id,
                limits_result=(AgentResourceUsage(output_bytes=second_result.output_bytes, turns=second_result.turns), second_result.provider_limit))
            after = await service.describe_limits('caller-a', second['id'])
            assert after['resource_usage'] == {'output_bytes': 42, 'turns': 2}
            assert (await service.describe_limits('caller-a', first['id']))['resource_usage'] == {'output_bytes': 17, 'turns': 3}
            assert calls == [200, 200, 200, 429, 200, 200]
