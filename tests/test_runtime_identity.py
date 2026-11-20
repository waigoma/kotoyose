"""Authenticated runtime identity and pre-mutation fencing; no worker/provider needed."""
import os
from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import UUID, uuid4

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
import pytest

from kotoyose.execution_resources import ExecutionError
from kotoyose.routes.executions import router
from kotoyose.runtime_identity import initialize_runtime_identity, require_expected_identity

from tests.fixtures.api_keys import fixture_key_store

STORE = "28d097b3-85f2-4469-871f-4b030eae0882"


class Service:
    def __init__(self):
        self.store = SimpleNamespace(runtime_store_id=STORE)
        self.config = SimpleNamespace()
        self.mutations = 0

    async def register_resource(self, principal, policy, resource):
        self.mutations += 1
        return "resource:fixture"


def test_header_pair_is_required_and_uninitialized_identity_fails_closed():
    service = Service()
    with pytest.raises(ExecutionError) as missing:
        require_expected_identity(service, "caller-a", {"x-kotoyose-runtime-store-id": STORE})
    assert missing.value.status == 400
    service.store.runtime_store_id = None
    with pytest.raises(ExecutionError):
        require_expected_identity(service, "caller-a", {"x-kotoyose-runtime-store-id": STORE, "x-kotoyose-caller-namespace": "caller-a"})


async def test_identity_auth_aliases_callers_and_mismatch_before_mutation(monkeypatch):
    from kotoyose.config import Client, HarnessConfig

    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(clients={
        "caller-a": Client(),
        "caller-b": Client(),
    }))
    service = Service()
    app = FastAPI()
    app.state.execution_service = service
    app.include_router(router)

    @app.exception_handler(ExecutionError)
    async def error(request: Request, exc: ExecutionError):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code}})

    monkeypatch.setattr(app.state, "api_key_store", await fixture_key_store(**{'caller-a': 'caller-a-token', 'caller-b': 'caller-b-token'}), raising=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://internal.example") as client:
        assert (await client.get("/v1/runtime-identity")).status_code == 401
        first = await client.get("/v1/runtime-identity", headers={"Authorization": "Bearer kys_caller-a-token"})
        assert first.json() == {"schema_version": "1", "runtime_store_id": STORE, "caller_namespace": "caller-a"}
        alias = await client.get("https://public.example/v1/runtime-identity", headers={"Authorization": "Bearer kys_caller-a-token"})
        assert alias.json() == first.json()
        other = await client.get("/v1/runtime-identity", headers={"Authorization": "Bearer kys_caller-b-token"})
        assert other.json()["caller_namespace"] == "caller-b"
        body = {"resource": {"schema_version": "1", "kind": "command_spec", "executable": "/bin/true"}}
        pinned = {"Authorization": "Bearer kys_caller-a-token", "X-Kotoyose-Runtime-Store-Id": STORE, "X-Kotoyose-Caller-Namespace": "caller-a"}
        assert (await client.post("/v1/resources", headers=pinned, json=body)).status_code == 201
        assert service.mutations == 1
        for wrong in [{"X-Kotoyose-Runtime-Store-Id": str(uuid4())}, {"X-Kotoyose-Caller-Namespace": "caller-b"}]:
            response = await client.post("/v1/resources", headers={**pinned, **wrong}, json=body)
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "request_conflict"
            assert service.mutations == 1
        # Existing unpinned clients continue to work, but no identity guarantee is claimed.
        assert (await client.post("/v1/resources", headers={"Authorization": "Bearer kys_caller-a-token"}, json=body)).status_code == 201


async def test_durable_store_identity_survives_restart_and_differs_between_databases():
    url = os.environ.get("KOTOYOSE_RUNTIME_IDENTITY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("KOTOYOSE_RUNTIME_IDENTITY_TEST_DATABASE_URL is not set")
    assert urlparse(url).path.lstrip("/").startswith("ahs_execution_test"), "refusing a non-test database"
    pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
    other_db = "ahs_execution_test_identity_" + uuid4().hex
    try:
        await pool.execute("CREATE SCHEMA IF NOT EXISTS kotoyose")
        first = await initialize_runtime_identity(pool)
        assert str(UUID(first)) == first
        assert await initialize_runtime_identity(pool) == first
        await pool.execute(f'CREATE DATABASE "{other_db}"')
        second_url = urlparse(url)._replace(path="/" + other_db).geturl()
        second = await asyncpg.create_pool(second_url, min_size=1, max_size=2)
        try:
            await second.execute("CREATE SCHEMA kotoyose")
            assert await initialize_runtime_identity(second) != first
        finally:
            await second.close()
        await pool.close()
        pool = await asyncpg.create_pool(url, min_size=1, max_size=2)
        assert await initialize_runtime_identity(pool) == first
    finally:
        await pool.execute(f'DROP DATABASE IF EXISTS "{other_db}"')
        await pool.close()
