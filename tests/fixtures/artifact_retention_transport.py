"""TaskHub の retention Port と実 HTTP / Postgres の GC を照合する fixture。"""

from uuid import uuid4

from tests.fixtures.client_tool_transport import app
from tests.test_execution_agent import _resources
from tests.fixtures.clients import clients, set_client


@app.post('/artifact-fixture')
async def artifact_fixture():
    """provider を起動せず、保存完了した output と正規 Attempt を作る。"""
    service = app.state.execution_service
    policy = clients()['caller-a']
    request, _, _ = await _resources(service, policy, app.state.fixture_root / str(uuid4()))
    snapshot = await service.admission.resolve('caller-a', policy, request)
    attempt, _ = await service.store.admit('caller-a', request.model_dump(mode='json'), snapshot, 20)
    await service.store.mark_running(attempt['id'])
    await service.store.finish(attempt['id'], 'succeeded', 'fixture', 'confirmed', 0, {'output': b'retention fixture'})
    result = await service.store.attempt('caller-a', attempt['id'])
    return {'attemptId': attempt['id'], 'artifactRef': result['artifacts'][0]['ref']}


@app.post('/artifact-gc')
async def artifact_gc():
    """テストだけが明示的に周期を進める。公開 API に GC の直接実行口を追加しない。"""
    return {'deleted': await app.state.execution_service.store.artifact_retention.gc()}
