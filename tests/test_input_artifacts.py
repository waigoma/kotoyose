"""Native image input upload の principal、digest、保存上限を検証する。"""

import asyncio
from datetime import datetime, timedelta, timezone
import base64
import os
from types import SimpleNamespace
from pathlib import Path

import asyncpg
import httpx
import pytest
import pytest_asyncio

from kotoyose.app import app
from kotoyose.execution_resources import NativeExecutionConfig
from kotoyose.execution_store import ExecutionStore
from kotoyose.artifact_blob_store import ArtifactBlob, ArtifactBlobRegistry, ArtifactStorageError
from kotoyose.input_artifacts import InputArtifactLimits


DATABASE_URL = os.environ.get("KOTOYOSE_INPUT_ARTIFACT_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="Input artifact integration requires an explicitly isolated Postgres test DB",
)

PNG = b"\x89PNG\r\n\x1a\ninput-proof"


@pytest_asyncio.fixture
async def input_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert DATABASE_URL is not None
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4, command_timeout=10)
    database = await pool.fetchval("SELECT current_database()")
    assert database.startswith("ahs_execution_test"), "Refusing a non-test database"
    await pool.execute("DROP SCHEMA IF EXISTS kotoyose CASCADE; CREATE SCHEMA kotoyose")
    store = ExecutionStore(pool)
    await store.initialize()
    from kotoyose.config import HarnessConfig
    from tests.fixtures.api_keys import fixture_key_store

    clients = {"caller-a": {
            "workspace_roots": [str(tmp_path)],
            "max_run_seconds": 30, "max_memory_bytes": 64 * 1024 * 1024,
            "max_processes": 8, "max_cpus": 1, "max_concurrent_runs": 1,
        }}
    clients["another-caller"] = {}
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(clients=clients))
    monkeypatch.setattr(app.state, "api_key_store", await fixture_key_store(**{'caller-a': 'input-token', 'another-caller': 'another-caller'}))
    config = NativeExecutionConfig.model_validate({
        "schema_version": "1", "image_id": "sha256:" + "a" * 64,
        "input_artifact_limits": {"allow_retention_pin": True},
    })
    service = SimpleNamespace(config=config, store=store)
    previous = getattr(app.state, "execution_service", None)
    app.state.execution_service = service
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer kys_input-token"},
        ) as client:
            yield client, store, config
    finally:
        app.state.execution_service = previous
        await pool.execute("DROP SCHEMA IF EXISTS kotoyose CASCADE")
        await pool.close()


def _upload(content: bytes = PNG, *, media_type: str = "image/png", retention_pin: bool = True) -> dict:
    return {
        "schema_version": "1", "media_type": media_type,
        "data_base64": base64.b64encode(content).decode("ascii"),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "retention_pin": retention_pin,
    }


@pytest.mark.asyncio
async def test_http_upload_is_immutable_principal_scoped_and_readable(input_runtime):
    client, store, _ = input_runtime
    payload = _upload()
    first = await client.post("/v1/input-artifacts", json=payload)
    assert first.status_code == 201, first.text
    artifact = first.json()["artifact"]
    assert artifact["ref"].startswith("artifact:input:")
    downloaded = await client.get("/v1/artifacts/" + artifact["ref"])
    assert downloaded.status_code == 200
    assert downloaded.content == PNG
    assert downloaded.headers["content-type"] == "image/png"
    assert downloaded.headers["etag"] == f'"{artifact["digest"]}"'
    replay = await client.post("/v1/input-artifacts", json=payload)
    assert replay.status_code == 201 and replay.json()["artifact"]["ref"] == artifact["ref"]
    changed_expiry = dict(payload)
    changed_expiry["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    changed = await client.post("/v1/input-artifacts", json=changed_expiry)
    assert changed.status_code == 201
    assert changed.json()["artifact"]["ref"] != artifact["ref"]
    assert await store.pool.fetchval(
        "SELECT count(*) FROM kotoyose.execution_input_artifacts WHERE state='active'"
    ) == 2
    denied = await client.get(
        "/v1/artifacts/" + artifact["ref"], headers={"Authorization": "Bearer kys_another-caller"},
    )
    assert denied.status_code == 401


@pytest.mark.asyncio
async def test_upload_rejects_signature_and_explicit_limits(input_runtime):
    client, _, config = input_runtime
    bad_signature = await client.post("/v1/input-artifacts", json=_upload(b"not-an-image"))
    assert bad_signature.status_code == 400, bad_signature.text
    config.input_artifact_limits = InputArtifactLimits(max_bytes=12, max_count=1, max_total_bytes=12)
    unauthorized_pin = await client.post("/v1/input-artifacts", json=_upload(retention_pin=True))
    assert unauthorized_pin.status_code == 400, unauthorized_pin.text
    config.input_artifact_limits.allow_retention_pin = True
    oversized = await client.post("/v1/input-artifacts", json=_upload(retention_pin=False))
    assert oversized.status_code == 400, oversized.text


@pytest.mark.asyncio
async def test_input_artifact_store_is_not_an_attempt_artifact(input_runtime):
    client, store, _ = input_runtime
    response = await client.post("/v1/input-artifacts", json=_upload())
    ref = response.json()["artifact"]["ref"]
    row = await store.artifact("caller-a", ref)
    assert row["content"] == PNG
    assert row["attempt_id"] is None
    assert await store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 0


@pytest.mark.asyncio
async def test_expired_input_is_not_readable(input_runtime):
    """期限を過ぎた input ref は immutable metadata が残っていても読ませない。"""
    client, store, _ = input_runtime
    response = await client.post("/v1/input-artifacts", json=_upload())
    assert response.status_code == 201, response.text
    ref = response.json()["artifact"]["ref"]
    await store.pool.execute(
        "UPDATE kotoyose.execution_input_artifacts SET expires_at=now()-interval '1 second' WHERE ref=$1", ref,
    )
    downloaded = await client.get("/v1/artifacts/" + ref)
    assert downloaded.status_code == 410


@pytest.mark.asyncio
async def test_expired_input_gc_tombstones_without_deleting_identity(input_runtime):
    """GC は bytes だけを消し、期限切れ ref の tombstone identity を残す。"""
    client, store, _ = input_runtime
    response = await client.post("/v1/input-artifacts", json=_upload(retention_pin=False))
    assert response.status_code == 201, response.text
    ref = response.json()["artifact"]["ref"]
    await store.pool.execute(
        "UPDATE kotoyose.execution_input_artifacts SET expires_at=now()-interval '1 second' WHERE ref=$1", ref,
    )
    assert await store.gc_expired_input_artifacts() == 1
    row = await store.pool.fetchrow(
        "SELECT ref,state,content FROM kotoyose.execution_input_artifacts WHERE ref=$1", ref,
    )
    assert row["ref"] == ref and row["state"] == "tombstone" and row["content"] is None
    assert (await client.get("/v1/artifacts/" + ref)).status_code == 410


@pytest.mark.asyncio
async def test_admin_retention_pin_blocks_expiry_gc(input_runtime):
    """管理者が許可した retention pin は期限切れでも GC 対象にしない。"""
    client, store, _ = input_runtime
    response = await client.post("/v1/input-artifacts", json=_upload(retention_pin=True))
    assert response.status_code == 201, response.text
    ref = response.json()["artifact"]["ref"]
    await store.pool.execute(
        "UPDATE kotoyose.execution_input_artifacts SET expires_at=now()-interval '1 second' WHERE ref=$1", ref,
    )
    assert await store.gc_expired_input_artifacts() == 0
    assert await store.pool.fetchval(
        "SELECT state FROM kotoyose.execution_input_artifacts WHERE ref=$1", ref,
    ) == "active"


@pytest.mark.asyncio
async def test_input_quota_is_serialized_for_concurrent_uploads(input_runtime):
    """同一 Caller の count / bytes quota は advisory lock 内で一件だけ通す。"""
    client, store, config = input_runtime
    config.input_artifact_limits = InputArtifactLimits(
        max_bytes=64, max_count=1, max_total_bytes=64,
    )
    responses = await asyncio.gather(
        *(client.post("/v1/input-artifacts", json=_upload(PNG + suffix, retention_pin=False))
          for suffix in (b"-a", b"-b"))
    )
    assert sorted(response.status_code for response in responses) == [201, 400]
    assert await store.pool.fetchval(
        "SELECT count(*) FROM kotoyose.execution_input_artifacts WHERE state='active'"
    ) == 1


@pytest.mark.asyncio
async def test_tampered_input_bytes_are_never_returned(input_runtime):
    """保存済み digest と異なる bytes は HTTP readback で evidence にならない。"""
    client, store, _ = input_runtime
    response = await client.post("/v1/input-artifacts", json=_upload())
    assert response.status_code == 201, response.text
    ref = response.json()["artifact"]["ref"]
    await store.pool.execute(
        "UPDATE kotoyose.execution_input_artifacts SET content=$1 WHERE ref=$2", b"tampered", ref,
    )
    downloaded = await client.get("/v1/artifacts/" + ref)
    assert downloaded.status_code == 503
    assert await store.pool.fetchval(
        "SELECT count(*) FROM kotoyose.execution_input_artifacts WHERE ref=$1", ref,
    ) == 1


@pytest.mark.asyncio
async def test_provider_failure_does_not_leave_input_metadata(input_runtime):
    """外部 backend の保存失敗時に、既存 input と未確定 upload を安全に保持する。"""
    client, store, _ = input_runtime
    existing = await client.post("/v1/input-artifacts", json=_upload())
    assert existing.status_code == 201, existing.text
    existing_ref = existing.json()["artifact"]["ref"]

    class BrokenBackend:
        identity = "broken-v1"

        async def put(self, blob: ArtifactBlob, content: bytes) -> None:
            raise ArtifactStorageError("provider unavailable")

        async def read(self, blob: ArtifactBlob) -> bytes:
            raise ArtifactStorageError("provider unavailable")

    store.artifact_storage = ArtifactBlobRegistry("broken", {"broken": BrokenBackend()})
    response = await client.post("/v1/input-artifacts", json=_upload(PNG + b"-second"))
    assert response.status_code == 503, response.text
    assert await store.pool.fetchval(
        "SELECT count(*) FROM kotoyose.execution_input_artifacts WHERE state='active'"
    ) == 1
    assert await store.pool.fetchval(
        "SELECT count(*) FROM kotoyose.execution_input_artifacts WHERE state='pending'"
    ) == 1
    retained = await client.get("/v1/artifacts/" + existing_ref)
    assert retained.status_code == 200 and retained.content == PNG

    class RecoveringBackend(BrokenBackend):
        async def read(self, blob: ArtifactBlob) -> bytes:
            return blob.verify(PNG + b"-second")

    store.artifact_storage = ArtifactBlobRegistry("broken", {"broken": RecoveringBackend()})
    assert await store.reconcile_input_artifacts() == 0
    assert await store.pool.fetchval(
        "SELECT count(*) FROM kotoyose.execution_input_artifacts WHERE state='active'"
    ) == 2
