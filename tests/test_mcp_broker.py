"""実 HTTP と PostgreSQL で外部効果の状態遷移、停止、照合の境界を検証する。"""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from kotoyose.app import app
from kotoyose.execution_resources import ExecutionError
from kotoyose.execution_service import ExecutionService
from kotoyose.execution_snapshot import resolved_attempt_snapshot
from kotoyose.mcp_contract import McpPinRequest
from kotoyose.native_claude import NativeAgentResult, _validate_job
from tests.fixtures.api_keys import fixture_key_store
from tests.test_execution_agent import _config, _NoopWorker, _resources
from tests.test_execution_store import store  # noqa: F401
from tests.test_mcp_transport import remote  # noqa: F401
from tests.fixtures.clients import clients, set_client


async def admitted(store, remote, root, *, pin_request=None):
    server, _, state = remote
    config = _config(root)
    config.mcp_servers['fixture'] = server
    clients()['caller-a'].mcp_servers.append('fixture')
    service = ExecutionService(config, store, _NoopWorker())
    policy = clients()['caller-a']
    binding = await service.admission.mcp_resolver.pin(
        policy,
        pin_request or McpPinRequest(
            schema_version='1', server_id='fixture', tools={'append': 'fixture_append'},
        ),
    )
    ref = await service.register_resource('caller-a', policy, binding)
    request, _, _ = await _resources(service, policy, root / 'workspace', tool_refs=[])
    grant = await store.resource('caller-a', request.execution_grant_ref)
    grant_ref = await store.put_resource('caller-a', grant.model_copy(update={'version': 2, 'resource_refs': [*grant.resource_refs, ref]}))
    request = request.model_copy(update={'execution_grant_ref': grant_ref, 'mcp_refs': [ref]})
    snapshot = await service.admission.resolve('caller-a', policy, request)
    attempt, _ = await store.admit('caller-a', request.model_dump(mode='json'), snapshot, 1)
    await store.mark_running(attempt['id'])
    return service, attempt, ref + '#fixture_append', state


@pytest.mark.asyncio
async def test_journal_is_committed_before_actual_post_and_only_pinned_alias_reaches_provider(store, remote, tmp_path, monkeypatch):
    service, attempt, tool, state = await admitted(store, remote, tmp_path)
    monkeypatch.setenv('KOTOYOSE_AGENT_PROVIDER_TOKEN', 'provider-fixture-only')
    async def before_call(body):
        identity = body['params']['_meta']['kotoyose_operation_id']
        row = await store.pool.fetchrow('SELECT * FROM kotoyose.execution_mcp_calls WHERE id=$1', identity)
        assert row['state'] == 'sent' and row['tool_ref'] == tool
        assert json.loads(row['arguments']) == {'value': 'one'}
        assert await store.pool.fetchval('SELECT state FROM kotoyose.execution_operations WHERE id=$1', identity) == 'sent'
    state.before_call = before_call
    async def execute(job, cancel, execute_tool, *, on_process_started):
        _validate_job(job)
        assert [item['name'] for item in job.client_tool_specs] == ['fixture_append']
        assert job.client_tool_specs[0]['effect'] == 'write'
        assert 'mcp-fixture-only' not in repr(job) and remote[0].endpoint not in repr(job)
        outcome = await job.client_tool_call(tool, {'value': 'one'})
        assert outcome.state == 'succeeded' and outcome.result['provenance'] == 'external_untrusted'
        return NativeAgentResult(status='succeeded', reason='completed', content=b'done', stop_confirmation='confirmed', usage={})
    service.agent_executor.runtime = SimpleNamespace(execute=execute)
    result = await service.agent_executor.execute(attempt, asyncio.Event())
    assert result.status == 'succeeded' and len(state.calls) == 1
    calls = await store.mcp_calls.list_calls('caller-a', attempt['id'], None, 100)
    assert calls[0]['state'] == 'confirmed' and calls[0]['disposition'] == 'applied'
    assert 'arguments' not in calls[0] and 'result' not in calls[0]
    assert tool.split('#')[0] in resolved_attempt_snapshot(await store.attempt('caller-a', attempt['id'])).resource_refs.values()


@pytest.mark.asyncio
async def test_response_loss_holds_writer_until_exact_human_reconciliation_and_does_not_revive_attempt(store, remote, tmp_path, monkeypatch):
    service, attempt, tool, state = await admitted(store, remote, tmp_path)
    state.mode = 'lost'
    outcome = await service.agent_executor.mcp_broker.call('caller-a', attempt['id'], tool, {'value': 'one'}, asyncio.Event())
    assert outcome.state == 'unknown' and len(state.calls) == 1
    with pytest.raises(ExecutionError):
        await store.prepare_operation(attempt['id'], 'sandbox_command')
    with pytest.raises(ExecutionError):
        await service.agent_executor.mcp_broker.call('caller-a', attempt['id'], tool, {'value': 'two'}, asyncio.Event())
    await store.finish(attempt['id'], 'failed', 'fixture', 'confirmed', 1, {})
    await service.reconcile('caller-a', attempt['id'])
    stopped = await store.attempt('caller-a', attempt['id'])
    assert stopped['stop_confirmation'] == 'confirmed' and stopped['side_effect_resolution'] == 'unresolved'
    assert (await store.workspace('caller-a', attempt['request']['workspace_ref']))['writer_attempt'] == attempt['id']
    call = (await store.mcp_calls.list_calls('caller-a', attempt['id'], None, 1))[0]
    evidence = {'schema_version': '1', 'tool_ref': tool, 'arguments_digest': call['arguments_digest'],
                'generation': attempt['generation'], 'disposition': 'applied', 'result': {'proof': 'remote-record-1'}, 'evidence_ref': 'operator:record:1'}
    monkeypatch.setattr(app.state, 'api_key_store', await fixture_key_store(**{'caller-a': 'caller-fixture'}), raising=False)
    monkeypatch.setattr(app.state, 'execution_service', service, raising=False)
    path = f"/v1/attempts/{attempt['id']}/mcp-calls/{call['id']}/reconciliation"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        assert (await client.post(path, json=evidence)).status_code == 401
        client.headers['Authorization'] = 'Bearer kys_caller-fixture'
        assert (await client.post(path, json={**evidence, 'arguments_digest': 'f' * 64})).status_code == 409
        response = await client.post(path, json=evidence)
        assert response.status_code == 200, response.text
        assert (await client.post(path, json=evidence)).json() == response.json()
        assert (await client.post(path, json={**evidence, 'disposition': 'not_applied'})).status_code == 409
        listing = await client.get(f"/v1/attempts/{attempt['id']}/mcp-calls")
        assert listing.json()['calls'][0]['evidence_ref'] == evidence['evidence_ref']
    final = await store.attempt('caller-a', attempt['id'])
    assert final['status'] == 'failed' and final['finished_at'] == stopped['finished_at']
    assert final['side_effect_resolution'] == 'resolved'
    workspace = await store.pool.fetchrow('SELECT * FROM kotoyose.execution_workspaces WHERE id=$1', attempt['workspace_id'])
    assert workspace['writer_attempt'] is None and workspace['generation'] == attempt['generation'] + 1
    assert len(state.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['metadata', 'caller', 'config', 'cancel', 'grant'])
async def test_revocation_or_changed_surface_cannot_send_tool(store, remote, tmp_path, change):
    service, attempt, tool, state = await admitted(store, remote, tmp_path)
    cancel = asyncio.Event()
    if change == 'metadata':
        state.tool = {**state.tool, 'description': 'Changed'}
    elif change == 'caller':
        clients()['caller-a'].mcp_servers.clear()
    elif change == 'config':
        service.config.mcp_servers['fixture'] = service.config.mcp_servers['fixture'].model_copy(update={'trust_profile_ref': 'operator:changed'})
    elif change == 'cancel':
        cancel.set()
    else:
        await store.revoke_grant('caller-a', attempt['request']['execution_grant_ref'])
    if change == 'grant':
        with pytest.raises(ExecutionError):
            await service.agent_executor.mcp_broker.call('caller-a', attempt['id'], tool, {'value': 'one'}, cancel)
    else:
        result = await service.agent_executor.mcp_broker.call('caller-a', attempt['id'], tool, {'value': 'one'}, cancel)
        assert result.state == 'failed'
        call = (await store.mcp_calls.list_calls('caller-a', attempt['id'], None, 1))[0]
        assert call['disposition'] == 'not_applied'
    assert state.calls == []


@pytest.mark.asyncio
async def test_arguments_and_foreign_callers_are_rejected_and_send_claim_is_once(store, remote, tmp_path):
    service, attempt, tool, state = await admitted(store, remote, tmp_path)
    for arguments in ({}, {'value': 7}, {'value': 'x', 'extra': 'x'}):
        with pytest.raises(ExecutionError):
            await store.mcp_calls.prepare('caller-a', attempt['id'], tool, arguments)
    with pytest.raises(ExecutionError):
        await store.mcp_calls.prepare('caller-b', attempt['id'], tool, {'value': 'one'})
    with pytest.raises(ExecutionError):
        await store.mcp_calls.list_calls('caller-b', attempt['id'], None, 100)
    call = await store.mcp_calls.prepare('caller-a', attempt['id'], tool, {'value': 'one'})
    results = await asyncio.gather(*(store.mcp_calls.mark_sent('caller-a', attempt['id'], call['id']) for _ in range(2)), return_exceptions=True)
    assert len([item for item in results if isinstance(item, ExecutionError)]) == 1
    assert state.calls == []


@pytest.mark.asyncio
async def test_terminalization_resolves_unsent_but_keeps_sent_uncertain(store, remote, tmp_path):
    _, attempt, tool, _ = await admitted(store, remote, tmp_path)
    prepared = await store.mcp_calls.prepare('caller-a', attempt['id'], tool, {'value': 'one'})
    sent = await store.mcp_calls.prepare('caller-a', attempt['id'], tool, {'value': 'two'})
    await store.mcp_calls.mark_sent('caller-a', attempt['id'], sent['id'])
    await store.finish(attempt['id'], 'cancelled', 'fixture', 'confirmed', None, {})
    calls = {row['id']: row for row in await store.mcp_calls.list_calls('caller-a', attempt['id'], None, 100)}
    assert calls[prepared['id']]['disposition'] == 'not_applied'
    assert calls[sent['id']]['state'] == 'unknown'
    with pytest.raises(ExecutionError):
        await store.mcp_calls.mark_sent('caller-a', attempt['id'], prepared['id'])
    with pytest.raises(ExecutionError):
        await store.mcp_calls.finish('caller-a', attempt['id'], sent['id'], 'applied', {})
