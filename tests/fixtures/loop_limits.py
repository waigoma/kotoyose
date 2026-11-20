"""隔離 PostgreSQL と実行しない service を観測契約の試験に提供する。"""

from contextlib import asynccontextmanager
from urllib.parse import urlparse
from uuid import uuid4

from types import SimpleNamespace
from unittest.mock import patch

import asyncpg
import pytest
from fastapi import FastAPI

from kotoyose.app import execution_exception_handler
from kotoyose.config import Client
from tests.fixtures.api_keys import fixture_key_store
from kotoyose.execution_resources import ExecutionError
from kotoyose.execution_service import ExecutionService
from kotoyose.execution_store import ExecutionStore
from kotoyose.routes.executions import router


@asynccontextmanager
async def isolated_limits(database_url):
    """注入された接続先で自分の DB だけを作成し、終了時に削除する。"""
    if not database_url:
        pytest.skip("isolated PostgreSQL test URL is not configured")
    admin = await asyncpg.connect(database_url)
    name = 'ahs_execution_test_limits_' + uuid4().hex
    pool = None
    created = False
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        created = True
        pool = await asyncpg.create_pool(urlparse(database_url)._replace(path='/' + name).geturl())
        await pool.execute('CREATE SCHEMA kotoyose')
        store = ExecutionStore(pool)
        await store.initialize()
        service = ExecutionService.__new__(ExecutionService)
        service.store = store
        service.config = SimpleNamespace()
        app = FastAPI()
        app.state.execution_service = service
        callers = {"caller-a": Client(models=["*"]), "caller-b": Client(models=["*"])}
        app.state.api_key_store = await fixture_key_store(**{name: name for name in callers})
        app.add_exception_handler(ExecutionError, execution_exception_handler)
        app.include_router(router)
        with patch("kotoyose.config.get_clients", return_value=callers):
            yield store, service, app
    finally:
        if pool is not None:
            await pool.close()
        if created:
            await admin.execute(f'DROP DATABASE "{name}"')
        await admin.close()
