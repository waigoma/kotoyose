"""TaskHub client Tool adapter と実 HTTP / Postgres を接続する loopback 専用 fixture。"""

import os
import shutil
import tempfile
from urllib.parse import unquote, urlparse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
import asyncpg
from fastapi import FastAPI
from kotoyose.app import execution_exception_handler
from kotoyose.execution_resources import ClientToolSpec, ExecutionError
from kotoyose.execution_store import ExecutionStore
from kotoyose.routes.executions import router
from tests.test_execution_agent import _resources, _service_for_admission
from tests.fixtures.clients import clients, set_client

os.environ['KOTOYOSE_AGENT_CALLER_TOKEN'] = 'client-transport-fixture'

@asynccontextmanager
async def lifespan(app):
    """専用 DB と repository 内の一時 workspace だけを初期化する。"""
    database_url = os.environ['KOTOYOSE_CLIENT_TRANSPORT_TEST_DATABASE_URL']
    name = unquote(urlparse(database_url).path.lstrip('/'))
    if not name.startswith('ahs_execution_test_agent_client_transport'):
        raise ValueError('Dedicated client transport DB required')
    runtime_root = Path(__file__).resolve().parents[2] / '.harness' / 'runtime'
    runtime_root.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='client-transport-', dir=runtime_root))
    app.state.fixture_root = root
    pool = await asyncpg.create_pool(database_url)
    if await pool.fetchval('SELECT current_database()') != name:
        raise ValueError('Test DB identity differs')
    await pool.execute('DROP SCHEMA IF EXISTS kotoyose CASCADE; CREATE SCHEMA kotoyose')
    store = ExecutionStore(pool)
    await store.initialize()
    service, _ = await _service_for_admission(store, root)
    app.state.execution_service = service
    try:
        yield
    finally:
        await pool.close()
        shutil.rmtree(root)

app = FastAPI(lifespan=lifespan)
app.include_router(router)
app.add_exception_handler(ExecutionError, execution_exception_handler)

@app.post('/fixture')
async def fixture():
    """実行記録と未配送 Call を作る。provider は起動しない。"""
    service = app.state.execution_service
    root = app.state.fixture_root
    policy = clients()['caller-a']
    tool = await service.register_resource('caller-a', policy, ClientToolSpec(
        schema_version='1', kind='client_tool', name='expression', description='Fixture expression.',
        input_schema={'type':'object','additionalProperties':False,'properties':{'value':{'type':'string'}},'required':['value']},
        effect='set', timeout_seconds=30,
    ))
    request, _, _ = await _resources(service, policy, root / str(uuid4()), tool_refs=[tool])
    request = type(request).model_validate({**request.model_dump(mode='json'), 'client_key': 'client:cross'})
    snapshot = await service.admission.resolve('caller-a', policy, request)
    attempt, _ = await service.store.admit('caller-a', request.model_dump(mode='json'), snapshot, 20)
    await service.store.mark_running(attempt['id'])
    await service.store.client_tools.register_connection('caller-a', attempt['id'], 'client:cross', 0)
    call = await service.store.client_tools.create_call('caller-a', attempt['id'], 'client:cross', 0, tool, {'value':'happy'}, datetime.now(timezone.utc) + timedelta(seconds=30))
    return {'attemptId':attempt['id'], 'callId':call['id']}
