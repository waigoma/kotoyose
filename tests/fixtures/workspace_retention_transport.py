"""実 Kotoyose の workspace GC を TaskHub adapter から検証する専用 fixture。"""

from pathlib import Path

from kotoyose.workspace_gc import WorkspaceCollector
from tests.fixtures.checkpoint_transport import app
from tests.fixtures.clients import clients, set_client


@app.post('/workspace-gc')
async def workspace_gc():
    """fixture が管理する登録 path だけを、実際の Collector で削除する。"""
    service = app.state.execution_service
    collector = WorkspaceCollector(service.store.workspace_retention, clients())
    return {'deleted': await collector.reconcile() + await collector.gc()}


@app.get('/workspace-gc-observation')
async def workspace_gc_observation(workspace_ref: str):
    """Runtime metadata とディレクトリ実体の両方を照合する。"""
    service = app.state.execution_service
    snapshot = await service.store.workspace_retention.get('caller-a', workspace_ref)
    path = Path(await service.store.pool.fetchval('SELECT path FROM kotoyose.execution_workspaces WHERE id=$1', snapshot['workspace_identity']))
    if not path.is_relative_to(app.state.fixture_root):
        raise ValueError('Workspace is outside fixture root')
    return {'state': snapshot['state'], 'exists': path.exists()}
