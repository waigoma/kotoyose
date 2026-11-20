"""TaskHub の checkpoint adapter と実 Kotoyose / Postgres / filesystem をつなぐ fixture。"""

import hashlib
from pathlib import Path
from uuid import uuid4

from tests.fixtures.client_tool_transport import app
from tests.test_execution_agent import _resources
from tests.fixtures.clients import clients, set_client


@app.post('/checkpoint-fixture')
async def checkpoint_fixture():
    """provider を起動せず、保存可能な通常ファイルを持つ停止済み Attempt を用意する。"""
    service = app.state.execution_service
    root = app.state.fixture_root / str(uuid4())
    request, _, _ = await _resources(service, clients()['caller-a'], root, tool_refs=[])
    snapshot = await service.admission.resolve('caller-a', clients()['caller-a'], request)
    content = b'checkpoint transport\n'
    (Path(snapshot['workspace_path']) / 'report.txt').write_bytes(content)
    attempt, _ = await service.store.admit('caller-a', request.model_dump(mode='json'), snapshot, 20)
    await service.store.finish(attempt['id'], 'succeeded', 'fixture', 'confirmed', 0, {})
    observed = await service.describe('caller-a', attempt['id'])
    return {'capture': {'schemaVersion': '1', 'requestId': str(uuid4()), 'workspaceRef': observed['workspace_ref'],
                        'sourceAttemptId': attempt['id'], 'sourceRevision': None,
                        'files': [{'path': 'report.txt', 'digest': hashlib.sha256(content).hexdigest(), 'size': len(content), 'mode': '0644', 'origin': 'generated'}]},
            'restorePath': str(app.state.fixture_root / ('restored-' + str(uuid4())))}


@app.get('/checkpoint-observation')
async def checkpoint_observation(workspace_ref: str):
    """登録された fixture workspace の回復 bytes だけを検査する。"""
    row = await app.state.execution_service.store.workspace('caller-a', workspace_ref)
    path = Path(row['path'])
    if not path.is_relative_to(app.state.fixture_root):
        raise ValueError('Fixture path is outside its root')
    return {'content': (path / 'report.txt').read_text(), 'generation': row['generation'], 'writer': row['writer_attempt']}


@app.post('/checkpoint-partial-fixture')
async def checkpoint_partial_fixture():
    """mkdir 後の中断を再現し、未登録の部分復元を実 DB と filesystem に残す。"""
    from uuid import NAMESPACE_URL, uuid5
    from kotoyose.checkpoint_contract import CheckpointCapture, CheckpointRestore
    from kotoyose.checkpoint_restore import CheckpointRestorer
    from kotoyose.execution_resources import content_digest
    fixture = await checkpoint_fixture()
    source = fixture['capture']
    service = app.state.execution_service
    checkpoint = await service.store.checkpoints.capture('caller-a', CheckpointCapture(
        schema_version='1', request_id=source['requestId'], workspace_ref=source['workspaceRef'],
        source_attempt_id=source['sourceAttemptId'], source_revision=None, files=source['files'],
    ))
    request = CheckpointRestore(schema_version='1', request_id=uuid4(), caller_workspace_ref=f'partial:{uuid4()}', path=fixture['restorePath'], expected_digest=checkpoint['artifact']['digest'])
    ref = checkpoint['checkpoint_ref']
    identity = str(uuid5(NAMESPACE_URL, f'kotoyose-restore:caller-a:{request.request_id}'))
    path = Path(request.path)
    async with service.store.pool.acquire() as conn:
        await CheckpointRestorer(service.store)._prepare(conn, identity, 'caller-a', ref,
            content_digest({'checkpoint_ref': ref, **request.model_dump(mode='json')}), request, path, checkpoint['artifact']['ref'])
        path.mkdir(mode=0o700)
        (path / 'partial.txt').write_text('partial fixture')
        info = path.stat()
        await conn.execute('UPDATE kotoyose.execution_checkpoint_restores SET device=$2,inode=$3 WHERE id=$1', identity, info.st_dev, info.st_ino)
    return {'checkpointRef': ref, 'restoreRequestId': str(request.request_id), 'path': str(path), 'callerWorkspaceRef': request.caller_workspace_ref}
