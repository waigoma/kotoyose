"""保持未決定・pin・停止未確認・削除応答消失からの誤 GC を検出する。"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from kotoyose.app import app
from kotoyose.artifact_blob_store import ArtifactBlobRegistry, ArtifactStorageError
from kotoyose.artifact_retention import ArtifactRetentionChange, OutputArtifactLimits
from kotoyose.execution_resources import ExecutionError, NativeExecutionConfig
from kotoyose.execution_store import ExecutionStore
from kotoyose.local_artifact_store import LocalArtifactStore
from tests.test_execution_store import _admit_ready, store  # noqa: F401
from kotoyose.config import Client
from tests.fixtures.clients import set_client


def policy(*, version=0, request='change-1', pins=(), acknowledgement=None):
    return ArtifactRetentionChange(
        schema_version='1', request_id=request, expected_version=version,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1), pins=list(pins),
        proof_ref='caller:retention-decision:1', acknowledged_required_action_ref=acknowledgement,
    )


async def artifact(store, tmp_path, *, stop='confirmed', required_action=None):
    _, _, request, snapshot = await _admit_ready(store, tmp_path)
    attempt, _ = await store.admit('caller-a', request, snapshot, 1)
    await store.finish(attempt['id'], 'approval_required' if required_action else 'succeeded', 'fixture', stop, 0,
                       {'stdout': b'retention-evidence'}, required_action=required_action)
    result = await store.attempt('caller-a', attempt['id'])
    return result, result['artifacts'][0]['ref']


def test_retention_change_rejects_ambiguous_dates_duplicate_pins_and_negative_versions():
    raw = policy().model_dump(mode='json')
    for replacement in ({'expires_at': '2026-01-01T00:00:00'}, {'pins': ['same', 'same']}, {'expected_version': -1}):
        with pytest.raises(ValidationError):
            ArtifactRetentionChange.model_validate({**raw, **replacement})
    assert policy(pins=['z', 'a']).pins == ['a', 'z']
    for limits in ({'gc_interval_seconds': 0}, {'max_pins': 0}, {'gc_batch_size': 1001}, {'default_lifetime_seconds': 2, 'max_lifetime_seconds': 1}):
        with pytest.raises(ValidationError):
            OutputArtifactLimits.model_validate(limits)


@pytest.mark.asyncio
async def test_administrator_limits_apply_to_new_changes_but_do_not_invalidate_receipt_replay(store, tmp_path):
    _, ref = await artifact(store, tmp_path)
    change = policy(pins=['audit:1', 'checkpoint:1'])
    receipt = await store.artifact_retention.change('caller-a', ref, change)
    store.artifact_retention.limits = OutputArtifactLimits(max_pins=1, default_lifetime_seconds=60, max_lifetime_seconds=60)
    assert await store.artifact_retention.change('caller-a', ref, change) == receipt
    with pytest.raises(ExecutionError) as pins:
        await store.artifact_retention.change('caller-a', ref, policy(version=1, request='too-many', pins=['one', 'two']))
    assert pins.value.status == 403
    future = policy(version=1, request='too-long').model_copy(update={'expires_at': datetime.now(timezone.utc) + timedelta(seconds=120)})
    with pytest.raises(ExecutionError) as lifetime:
        await store.artifact_retention.change('caller-a', ref, future)
    assert lifetime.value.status == 403


@pytest.mark.asyncio
async def test_pending_policy_pin_cas_gc_tombstone_and_principal_scope(store, tmp_path):
    attempt, ref = await artifact(store, tmp_path)
    assert (await store.artifact_retention.get('caller-a', ref))['pins'] == ['caller-policy-pending']
    await store.pool.execute("UPDATE kotoyose.execution_artifact_retention SET expires_at=now()-interval '1 day'")
    assert await store.artifact_retention.gc() == 0
    request = policy()
    receipt = await store.artifact_retention.change('caller-a', ref, request)
    assert receipt['version'] == 1 and receipt['state'] == 'active'
    assert await store.artifact_retention.change('caller-a', ref, request) == receipt
    assert await store.artifact_retention.gc() == 1
    assert await store.artifact_retention.gc() == 0
    tombstone = await store.artifact_retention.get('caller-a', ref)
    assert tombstone['state'] == 'tombstone' and tombstone['deleted_at']
    assert (await store.attempt('caller-a', attempt['id']))['artifacts'][0]['availability'] == 'tombstone'
    assert (await store.attempt('caller-a', attempt['id']))['artifacts'][0]['size'] == len(b'retention-evidence')
    with pytest.raises(ExecutionError) as deleted:
        await store.artifact('caller-a', ref)
    assert deleted.value.status == 410
    for method in (store.artifact_retention.get, store.artifact):
        with pytest.raises(ExecutionError) as foreign:
            await method('caller-b', ref)
        assert foreign.value.status == 404
    # 旧 receipt は受理の証拠として replay するが、現在の tombstone は復活しない。
    assert await store.artifact_retention.change('caller-a', ref, request) == receipt
    with pytest.raises(ExecutionError) as resurrection:
        await store.artifact_retention.change('caller-a', ref, policy(version=1, request='resurrection', pins=['keep']))
    assert resurrection.value.status == 410


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['pin', 'process', 'side_effect', 'operation', 'required_action'])
async def test_all_mechanical_and_caller_holds_dominate_expiry(store, tmp_path, reason):
    action = {'kind': 'approval', 'call_id': 'approval-call'} if reason == 'required_action' else None
    attempt, ref = await artifact(store, tmp_path, stop='unconfirmed' if reason == 'process' else 'confirmed', required_action=action)
    if reason == 'side_effect':
        await store.pool.execute("UPDATE kotoyose.execution_attempts SET side_effect_resolution='unresolved' WHERE id=$1", attempt['id'])
    if reason == 'operation':
        await store.pool.execute("INSERT INTO kotoyose.execution_operations(id,attempt_id,kind,state) VALUES('unknown-operation',$1,'fixture','unknown')", attempt['id'])
    await store.artifact_retention.change('caller-a', ref, policy(pins=['budget:held'] if reason == 'pin' else []))
    assert await store.artifact_retention.gc() == 0
    assert (await store.artifact('caller-a', ref))['content'] == b'retention-evidence'
    if reason == 'required_action':
        with pytest.raises(ExecutionError):
            await store.artifact_retention.change('caller-a', ref, policy(version=1, request='wrong-action', acknowledgement='other'))
        await store.artifact_retention.change('caller-a', ref, policy(version=1, request='action-resolved', acknowledgement='approval-call'))
        assert await store.artifact_retention.gc() == 1


@pytest.mark.asyncio
async def test_concurrent_policy_changes_and_replay_never_overwrite_a_newer_pin(store, tmp_path):
    _, ref = await artifact(store, tmp_path)
    changes = [policy(request=f'writer-{n}', pins=[f'pin:{n}']) for n in range(2)]
    results = await asyncio.gather(*(store.artifact_retention.change('caller-a', ref, change) for change in changes), return_exceptions=True)
    assert sum(isinstance(value, dict) for value in results) == 1
    failure = next(value for value in results if isinstance(value, ExecutionError))
    assert failure.code == 'request_conflict'
    index = next(index for index, result in enumerate(results) if isinstance(result, dict))
    first = results[index]
    second = await store.artifact_retention.change('caller-a', ref, policy(version=1, request='next', pins=['audit:kept']))
    assert await store.artifact_retention.change('caller-a', ref, changes[index]) == first
    assert await store.artifact_retention.get('caller-a', ref) == second
    assert await store.artifact_retention.gc() == 0
    with pytest.raises(ExecutionError) as contradiction:
        await store.artifact_retention.change('caller-a', ref, policy(request=changes[index].request_id, pins=['changed']))
    assert contradiction.value.code == 'request_conflict'


@pytest.mark.asyncio
async def test_lost_backend_delete_response_remains_deleting_until_reconciliation(store, tmp_path):
    class LostResponse(LocalArtifactStore):
        lose_response = True

        async def delete(self, blob):
            await super().delete(blob)
            if self.lose_response:
                raise ArtifactStorageError('delete response lost')

    backend = LostResponse(tmp_path / 'artifacts')
    external = ExecutionStore(store.pool, ArtifactBlobRegistry('disk', {'disk': backend}))
    _, ref = await artifact(external, tmp_path)
    await external.artifact_retention.change('caller-a', ref, policy())
    assert await external.artifact_retention.gc() == 0
    assert (await external.artifact_retention.get('caller-a', ref))['state'] == 'deleting'
    with pytest.raises(ExecutionError) as unavailable:
        await external.artifact('caller-a', ref)
    assert unavailable.value.status == 503
    with pytest.raises(ExecutionError):
        await external.artifact_retention.change('caller-a', ref, policy(version=1, request='late-pin', pins=['keep']))
    backend.lose_response = False
    assert await external.artifact_retention.reconcile() == 1
    assert (await external.artifact_retention.get('caller-a', ref))['state'] == 'tombstone'


@pytest.mark.asyncio
async def test_changed_backend_identity_cannot_be_used_to_confirm_deletion(store, tmp_path):
    backend = LocalArtifactStore(tmp_path / 'artifacts')
    external = ExecutionStore(store.pool, ArtifactBlobRegistry('disk', {'disk': backend}))
    _, ref = await artifact(external, tmp_path)
    await external.artifact_retention.change('caller-a', ref, policy())
    external.artifact_retention.storage = ArtifactBlobRegistry('disk', {'disk': LocalArtifactStore(tmp_path / 'other')})
    assert await external.artifact_retention.gc() == 0
    assert await external.artifact_retention.reconcile() == 0
    assert (await external.artifact_retention.get('caller-a', ref))['state'] == 'deleting'
    external.artifact_retention.storage = external.artifact_storage
    assert await external.artifact_retention.reconcile() == 1


@pytest.mark.asyncio
async def test_retention_http_requires_caller_authorization_and_returns_deleted_metadata(store, tmp_path, monkeypatch):
    _, ref = await artifact(store, tmp_path)
    config = NativeExecutionConfig.model_validate({'schema_version': '1', 'image_id': 'sha256:' + 'a' * 64})
    set_client('caller-a', Client(workspace_roots=[tmp_path],
        max_run_seconds=30, max_memory_bytes=67108864, max_processes=8, max_cpus=1, max_concurrent_runs=1))
    from tests.fixtures.api_keys import fixture_key_store

    monkeypatch.setattr(app.state, "api_key_store", await fixture_key_store(**{"caller-a": "retention-fixture-token"}))
    previous = getattr(app.state, 'execution_service', None)
    app.state.execution_service = SimpleNamespace(config=config, store=store)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get(f'/v1/artifacts/{ref}/retention')).status_code == 401
            headers = {'Authorization': 'Bearer kys_retention-fixture-token'}
            raw = policy().model_dump(mode='json')
            first = await client.put(f'/v1/artifacts/{ref}/retention', json=raw, headers=headers)
            assert first.status_code == 200, first.text
            assert (await client.put(f'/v1/artifacts/{ref}/retention', json=raw, headers=headers)).json() == first.json()
            assert await store.artifact_retention.gc() == 1
            assert (await client.get(f'/v1/artifacts/{ref}', headers=headers)).status_code == 410
            assert (await client.get(f'/v1/artifacts/{ref}/retention', headers=headers)).json()['state'] == 'tombstone'
    finally:
        app.state.execution_service = previous
