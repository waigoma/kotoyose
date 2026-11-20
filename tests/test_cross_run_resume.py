"""新 Run の Grant を一つの停止済み前 Attempt へ束縛し、同じ workspace を保つ。"""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from kotoyose.execution_resources import ExecutionGrant
from tests.test_execution_agent import store  # noqa: F401
from tests.test_execution_service import DATABASE_URL, IMAGE_ID, _terminal, native  # noqa: F401
from tests.test_safe_resume import _post, _source


pytestmark = pytest.mark.skipif(not DATABASE_URL or not IMAGE_ID, reason="Dedicated Native test PG and image required")


async def _cross_run(h, predecessor):
    grant = await h.store.resource(h.principal, h.body['execution']['execution_grant_ref'])
    new_run = 'run:human-recovery:' + str(uuid4())
    changes = {'caller_run_ref': new_run, 'version': grant.version + 1}
    if predecessor is not None:
        changes['resume_predecessor_ref'] = predecessor
    ref = await h.store.put_resource(h.principal, grant.model_copy(update=changes))
    h.body['execution'].update(caller_run_ref=new_run, execution_grant_ref=ref)
    return new_run


@pytest.mark.parametrize('modes,mode', [(('native', 'durable_state'), 'native'), (('durable_state',), 'durable_state')])
async def test_stopped_cross_run_uses_fresh_grant_and_same_workspace(native, monkeypatch, modes, mode):
    h = await _source(native, monkeypatch, modes=modes)
    (h.workspace/'partial.txt').write_text('unfinished source\n')
    old = await h.store.attempt(h.principal, h.old_id)
    new_run = await _cross_run(h, h.old_id)
    replies = await asyncio.gather(_post(h), _post(h))
    assert all(response.status_code == 202 for response in replies), [r.text for r in replies]
    assert replies[0].json()['attempt_id'] == replies[1].json()['attempt_id']
    receipt = replies[0].json()
    assert receipt['resume_mode'] == mode
    assert receipt['resume_manifest']['caller_run_ref'] == new_run
    terminal = await _terminal(h.client, receipt['attempt_id'])
    assert terminal['caller_run_ref'] == new_run
    assert (h.workspace/'partial.txt').read_text() == 'unfinished source\n'
    previous = await h.store.attempt(h.principal, h.old_id)
    assert (previous['status'], previous['reason'], previous['snapshot'], previous['request']) == (
        old['status'], old['reason'], old['snapshot'], old['request'])
    h.body['execution']['idempotency_key'] = str(uuid4())
    denied = await _post(h)
    assert denied.status_code == 409
    assert denied.json()['error']['reason_code'] == 'already_resumed'


@pytest.mark.parametrize('binding,reason', [(None, 'run_mismatch'), ('another-attempt', 'grant_mismatch')])
async def test_cross_run_requires_exact_predecessor_in_new_grant(native, monkeypatch, binding, reason):
    h = await _source(native, monkeypatch)
    await _cross_run(h, binding)
    response = await _post(h)
    assert response.status_code == 409, response.text
    assert response.json()['error']['reason_code'] == reason
    assert (await h.store.attempt(h.principal, h.old_id))['resumed_by'] is None


def test_legacy_grant_keeps_its_wire_shape():
    grant = ExecutionGrant.model_validate({
        'schema_version': '1', 'kind': 'grant', 'grant_id': 'fixture-grant', 'version': 1,
        'caller_run_ref': 'run:fixture', 'workspace_ref': 'workspace:fixture', 'isolation': 'sandbox',
        'network': 'none', 'policy_ref': 'policy:fixture', 'not_before': '2026-01-01T00:00:00Z',
        'expires_at': '2099-01-01T00:00:00Z', 'max_seconds': 60, 'delegation': 'none',
    })
    assert 'resume_predecessor_ref' not in grant.model_dump(mode='json')


async def test_cross_run_missing_session_uses_durable_state_without_erasing_old_ref(native, monkeypatch):
    h = await _source(native, monkeypatch, modes=('native', 'durable_state'))
    old = await h.store.attempt(h.principal, h.old_id)
    assert old['provider_session_ref']
    h.runtime.can_resume_session = AsyncMock(return_value=False)
    await _cross_run(h, h.old_id)
    response = await _post(h)
    assert response.status_code == 202, response.text
    assert response.json()['resume_mode'] == 'durable_state'
    await _terminal(h.client, response.json()['attempt_id'])
    assert (await h.store.attempt(h.principal, h.old_id))['provider_session_ref'] == old['provider_session_ref']
    assert h.runtime.jobs[-1].provider_session_ref is None
