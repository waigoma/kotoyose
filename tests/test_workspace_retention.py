"""期限・世代・停止・回復 bytes・応答消失の組合せを実 DB / filesystem で検証する。"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from kotoyose.app import app
from kotoyose.checkpoint_contract import CheckpointRelease
from kotoyose.execution_resources import ExecutionError
from kotoyose.workspace_gc import WorkspaceCollector
from kotoyose.workspace_retention import WorkspaceRetentionChange
from tests.test_checkpoint_store import source
from tests.test_execution_agent import _config
from tests.test_execution_store import store  # noqa: F401
from tests.fixtures.clients import clients


def policy(ref, **overrides):
    return WorkspaceRetentionChange.model_validate({
        'schema_version': '1', 'request_id': str(uuid4()), 'expected_version': 0,
        'workspace_ref': ref, 'expires_at': datetime.now(timezone.utc) - timedelta(seconds=1),
        'pins': [], 'proof_ref': 'task:terminal', 'recovery': {'mode': 'discard', 'decision_ref': 'human:discard'},
        **overrides,
    })


def collector(store, tmp_path):
    return WorkspaceCollector(store.workspace_retention, clients(_config(tmp_path)))


@pytest.mark.parametrize('change', [
    {'expires_at': '2026-01-01T00:00:00'}, {'pins': ['same', 'same']},
    {'expected_version': False}, {'expected_version': '0'},
    {'recovery': {'mode': 'checkpoint', 'checkpoint_ref': 'c', 'acknowledged_coverage': 'complete'}},
])
def test_policy_rejects_ambiguous_time_duplicate_pins_and_overclaimed_recovery(change):
    with pytest.raises(ValidationError):
        policy('workspace:x:0', **change)


@pytest.mark.asyncio
async def test_initial_pin_explicit_policy_and_immutable_receipt_survive_deletion(store, tmp_path):
    workspace, _, capture = await source(store, tmp_path)
    ref = capture.workspace_ref
    repo = store.workspace_retention
    initial = await repo.get('caller-a', f"workspace:{workspace['id']}:0")
    assert initial['workspace_ref'] == ref and initial['expires_at'] is None
    assert initial['pins'] == ['caller-policy-pending'] and 'path' not in initial
    assert await collector(store, tmp_path).gc() == 0
    request = policy(ref)
    first, replay = await asyncio.gather(repo.change('caller-a', ref, request), repo.change('caller-a', ref, request))
    assert first == replay and first['version'] == 1
    with pytest.raises(ExecutionError):
        await repo.change('caller-a', ref, request.model_copy(update={'pins': ['changed']}))
    with pytest.raises(ExecutionError) as foreign:
        await repo.get('caller-b', ref)
    assert foreign.value.status == 404
    assert await collector(store, tmp_path).gc() == 1
    assert not Path(workspace['path']).exists()
    assert (await repo.get('caller-a', ref))['state'] == 'tombstone'
    assert await repo.change('caller-a', ref, request) == first
    with pytest.raises(ExecutionError):
        await store.workspace('caller-a', ref)
    Path(workspace['path']).mkdir()
    with pytest.raises(ExecutionError):
        await store.register_workspace('caller-a', workspace['caller_ref'], Path(workspace['path']))
    assert await collector(store, tmp_path).reconcile() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', ['pins', 'future', 'generation', 'stop', 'operation', 'approval'])
async def test_gc_keeps_workspace_while_any_condition_is_unmet(store, tmp_path, guard):
    workspace, attempt, capture = await source(store, tmp_path)
    kwargs = {}
    if guard == 'pins':
        kwargs['pins'] = ['benchmark:adopted']
    if guard == 'future':
        kwargs['expires_at'] = datetime.now(timezone.utc) + timedelta(days=1)
    await store.workspace_retention.change('caller-a', capture.workspace_ref, policy(capture.workspace_ref, **kwargs))
    if guard == 'generation':
        await store.pool.execute('UPDATE kotoyose.execution_workspaces SET generation=generation+1 WHERE id=$1', workspace['id'])
    if guard == 'stop':
        await store.pool.execute("UPDATE kotoyose.execution_attempts SET stop_confirmation='unconfirmed' WHERE id=$1", attempt['id'])
    if guard == 'operation':
        await store.pool.execute("INSERT INTO kotoyose.execution_operations(id,attempt_id,kind,state) VALUES($1,$2,'fixture','unknown')", str(uuid4()), attempt['id'])
    if guard == 'approval':
        await store.pool.execute("UPDATE kotoyose.execution_attempts SET required_action='{\"call_id\":\"call:pending\"}'::jsonb WHERE id=$1", attempt['id'])
    assert await collector(store, tmp_path).gc() == 0
    assert Path(workspace['path']).is_dir()
    if guard == 'approval':
        with pytest.raises(ExecutionError):
            await store.workspace_retention.change('caller-a', capture.workspace_ref, policy(capture.workspace_ref, expected_version=1, acknowledged_required_actions=['wrong']))
        await store.workspace_retention.change('caller-a', capture.workspace_ref, policy(capture.workspace_ref, expected_version=1, acknowledged_required_actions=['call:pending']))
        assert await collector(store, tmp_path).gc() == 1


@pytest.mark.asyncio
async def test_failed_delete_is_fenced_reconciled_and_checkpoint_cannot_be_released_mid_delete(store, tmp_path, monkeypatch):
    workspace, _, capture = await source(store, tmp_path)
    checkpoint = await store.checkpoints.capture('caller-a', capture)
    request = policy(capture.workspace_ref, recovery={'mode': 'checkpoint', 'checkpoint_ref': checkpoint['checkpoint_ref'], 'acknowledged_coverage': 'selected_files'})
    await store.workspace_retention.change('caller-a', capture.workspace_ref, request)
    import kotoyose.workspace_gc as implementation
    original = implementation.delete_owned_workspace
    def interrupted(*args):
        raise OSError('fixture disk failure')
    monkeypatch.setattr(implementation, 'delete_owned_workspace', interrupted)
    assert await collector(store, tmp_path).gc() == 0
    assert (await store.workspace_retention.get('caller-a', capture.workspace_ref))['state'] == 'deleting'
    with pytest.raises(ExecutionError):
        await store.checkpoints.release('caller-a', checkpoint['checkpoint_ref'], CheckpointRelease(schema_version='1', request_id=uuid4()))
    with pytest.raises(ExecutionError):
        await store.register_workspace('caller-a', workspace['caller_ref'], Path(workspace['path']))
    with pytest.raises(ExecutionError):
        await store.checkpoints.capture('caller-a', capture.model_copy(update={'request_id': uuid4()}))
    monkeypatch.setattr(implementation, 'delete_owned_workspace', original)
    assert await WorkspaceCollector(store.workspace_retention, {}).reconcile() == 0
    assert Path(workspace['path']).exists()
    saved = await store.artifact('caller-a', checkpoint['artifact']['ref'])
    await store.pool.execute('UPDATE kotoyose.execution_artifacts SET content=$2 WHERE ref=$1', checkpoint['artifact']['ref'], b'corrupt after restart')
    assert await collector(store, tmp_path).reconcile() == 0
    assert Path(workspace['path']).exists()
    await store.pool.execute('UPDATE kotoyose.execution_artifacts SET content=$2 WHERE ref=$1', checkpoint['artifact']['ref'], saved['content'])
    assert await collector(store, tmp_path).reconcile() == 1
    # 回復 bytes は workspace と独立して存在し、削除完了後に限り明示解除できる。
    assert (await store.artifact('caller-a', checkpoint['artifact']['ref']))['content']
    assert (await store.checkpoints.release('caller-a', checkpoint['checkpoint_ref'], CheckpointRelease(schema_version='1', request_id=uuid4())))['state'] == 'released'


@pytest.mark.asyncio
@pytest.mark.parametrize('unavailable', ['released', 'corrupt'])
async def test_released_or_corrupt_recovery_point_blocks_gc_and_discard_requires_new_intent(store, tmp_path, unavailable):
    workspace, _, capture = await source(store, tmp_path)
    checkpoint = await store.checkpoints.capture('caller-a', capture)
    request = policy(capture.workspace_ref, recovery={'mode': 'checkpoint', 'checkpoint_ref': checkpoint['checkpoint_ref'], 'acknowledged_coverage': 'selected_files'})
    await store.workspace_retention.change('caller-a', capture.workspace_ref, request)
    if unavailable == 'released':
        await store.checkpoints.release('caller-a', checkpoint['checkpoint_ref'], CheckpointRelease(schema_version='1', request_id=uuid4()))
    else:
        await store.pool.execute('UPDATE kotoyose.execution_artifacts SET content=$2 WHERE ref=$1', checkpoint['artifact']['ref'], b'corrupt')
    assert await collector(store, tmp_path).gc() == 0
    assert Path(workspace['path']).exists()
    await store.workspace_retention.change('caller-a', capture.workspace_ref, policy(capture.workspace_ref, expected_version=1))
    assert await collector(store, tmp_path).gc() == 1


@pytest.mark.asyncio
async def test_deletion_receipt_loss_converges_from_verified_missing_path(store, tmp_path, monkeypatch):
    workspace, _, capture = await source(store, tmp_path)
    await store.workspace_retention.change('caller-a', capture.workspace_ref, policy(capture.workspace_ref))
    import kotoyose.workspace_gc as implementation
    original = implementation.delete_owned_workspace
    def interrupted(*args):
        original(*args)
        raise OSError('process lost before tombstone commit')
    monkeypatch.setattr(implementation, 'delete_owned_workspace', interrupted)
    assert await collector(store, tmp_path).gc() == 0
    assert not Path(workspace['path']).exists()
    assert (await store.workspace_retention.get('caller-a', capture.workspace_ref))['state'] == 'deleting'
    monkeypatch.setattr(implementation, 'delete_owned_workspace', original)
    assert await collector(store, tmp_path).reconcile() == 1


@pytest.mark.asyncio
async def test_admission_racing_gc_never_starts_writer_on_deleted_generation(store, tmp_path):
    workspace, _, capture = await source(store, tmp_path)
    await store.workspace_retention.change('caller-a', capture.workspace_ref, policy(capture.workspace_ref))
    prior = await store.pool.fetchrow('SELECT request,snapshot FROM kotoyose.execution_attempts LIMIT 1')
    import json
    request, snapshot = json.loads(prior['request']), json.loads(prior['snapshot'])
    request.update(idempotency_key=str(uuid4()), workspace_ref=capture.workspace_ref)
    snapshot['generation'] = 1
    admission, deleted = await asyncio.gather(store.admit('caller-a', request, snapshot, 1), collector(store, tmp_path).gc(), return_exceptions=True)
    assert deleted in (0, 1)
    if isinstance(admission, ExecutionError):
        assert deleted == 1
    else:
        assert isinstance(admission, tuple) and deleted == 0
        assert Path(workspace['path']).exists()


@pytest.mark.asyncio
async def test_http_retention_auth_and_roundtrip(store, tmp_path, monkeypatch):
    _, _, capture = await source(store, tmp_path)
    config = _config(tmp_path)
    from tests.fixtures.api_keys import fixture_key_store

    monkeypatch.setattr(app.state, "api_key_store", await fixture_key_store(**{"caller-a": "workspace-retention-fixture"}))
    previous = getattr(app.state, 'execution_service', None)
    app.state.execution_service = SimpleNamespace(config=config, store=store)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            url = f'/v1/workspaces/{capture.workspace_ref}/retention'
            assert (await client.get(url)).status_code == 401
            headers = {'Authorization': 'Bearer kys_workspace-retention-fixture'}
            assert (await client.get(url, headers=headers)).json()['version'] == 0
            result = await client.put(url, headers=headers, json=policy(capture.workspace_ref).model_dump(mode='json'))
            assert result.status_code == 200, result.text
            assert result.json()['version'] == 1
    finally:
        app.state.execution_service = previous
