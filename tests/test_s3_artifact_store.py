"""専用 MinIO と実 Native HTTP / Docker を使う保存 backend 適合試験。"""

import asyncio
from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

import pytest

pytest.importorskip("boto3", reason="S3 adapter tests require the s3 extra")
from botocore.exceptions import EndpointConnectionError

from kotoyose.artifact_blob_store import ArtifactBlob, ArtifactBlobRegistry, ArtifactStorageError
from kotoyose.artifact_storage_config import S3ArtifactStorageConfig
from kotoyose.artifact_retention import ArtifactRetentionChange
from kotoyose.s3_artifact_store import S3ArtifactStore
from tests.test_execution_service import _execution, native  # noqa: F401


CONTENT = b"evidence from private object storage"
BLOB = ArtifactBlob.from_content("artifact:attempt:evidence:digest", CONTENT, "text/plain")


@pytest.fixture
def storage(monkeypatch):
    endpoint = os.environ.get("KOTOYOSE_S3_TEST_ENDPOINT")
    if not endpoint:
        pytest.skip("KOTOYOSE_S3_TEST_ENDPOINT must name an isolated test object store")
    # 実 bucket を作って消すため、test 専用の明示 credential 以外は使わない。
    if not os.environ.get("KOTOYOSE_S3_TEST_ACCESS_KEY") or not os.environ.get("KOTOYOSE_S3_TEST_SECRET_KEY"):
        pytest.fail("Explicit S3 test credentials are required")
    config = S3ArtifactStorageConfig(
        kind="s3", endpoint=endpoint, bucket=f"kotoyose-artifact-test-{uuid4().hex}",
        access_key_env="KOTOYOSE_S3_TEST_ACCESS_KEY", secret_key_env="KOTOYOSE_S3_TEST_SECRET_KEY",
        allow_insecure_http=endpoint.startswith("http://"), request_timeout_seconds=3,
    )
    result = S3ArtifactStore(config)
    result.client.create_bucket(Bucket=config.bucket)
    try:
        yield result
    finally:
        for item in result.client.list_objects_v2(Bucket=config.bucket).get("Contents", []):
            result.client.delete_object(Bucket=config.bucket, Key=item["Key"])
        result.client.delete_bucket(Bucket=config.bucket)


@pytest.mark.asyncio
async def test_real_minio_concurrent_conditional_writes_and_reopen(storage):
    await asyncio.gather(*(storage.put(BLOB, CONTENT) for _ in range(8)))
    reopened = S3ArtifactStore(storage.config)
    assert reopened.identity == storage.identity
    assert await reopened.read(BLOB) == CONTENT
    assert storage.client.list_objects_v2(Bucket=storage.config.bucket)["KeyCount"] == 1


@pytest.mark.asyncio
async def test_real_minio_applied_upload_with_lost_response_is_read_reconciled(storage, monkeypatch):
    put = storage.client.put_object
    calls = 0

    def lose_response(**kwargs):
        nonlocal calls
        calls += 1
        put(**kwargs)
        raise EndpointConnectionError(endpoint_url="http://isolated-test")

    monkeypatch.setattr(storage.client, "put_object", lose_response)
    await storage.put(BLOB, CONTENT)
    assert calls == 1
    assert await storage.read(BLOB) == CONTENT


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["bytes", "size", "media_type", "deleted"])
async def test_real_minio_corrupted_metadata_or_content_is_never_returned(storage, mutation):
    await storage.put(BLOB, CONTENT)
    key = f"{storage.config.prefix}/{BLOB.key}"
    if mutation == "deleted":
        storage.client.delete_object(Bucket=storage.config.bucket, Key=key)
    else:
        content = b"x" * len(CONTENT) if mutation == "bytes" else CONTENT + b"!" if mutation == "size" else CONTENT
        storage.client.put_object(Bucket=storage.config.bucket, Key=key, Body=content,
                                  ContentType="text/html" if mutation == "media_type" else BLOB.media_type)
    with pytest.raises(ArtifactStorageError):
        await storage.read(BLOB)


@pytest.mark.skipif(not os.environ.get("KOTOYOSE_DOCKER_TEST_IMAGE"), reason="Native conformance requires a Docker test image ID")
@pytest.mark.asyncio
async def test_native_command_http_serves_s3_bytes_through_caller_authorization(storage, native):
    client, workspace, service, isolation = native
    service.store.artifact_storage = ArtifactBlobRegistry("minio", {"minio": storage})
    body = await _execution(client, workspace, ["sh", "-c", "printf hello; printf evidence > evidence.bin"],
                            isolation, artifact_spec=True)
    receipt = await client.post("/v1/executions", json=body)
    assert receipt.status_code == 202, receipt.text
    attempt_id = receipt.json()["attempt_id"]
    for _ in range(600):
        attempt = (await client.get(f"/v1/attempts/{attempt_id}")).json()
        if attempt["finished_at"]:
            break
        await asyncio.sleep(0.05)
    assert attempt["status"] == "succeeded", attempt
    assert attempt["stop_confirmation"] == "confirmed"
    assert len(attempt["artifacts"]) == 3
    assert all("storage" not in name for artifact in attempt["artifacts"] for name in artifact)
    evidence = next(artifact for artifact in attempt["artifacts"] if artifact["name"] == "evidence")
    downloaded = await client.get(f"/v1/artifacts/{evidence['ref']}")
    assert downloaded.status_code == 200 and downloaded.content == b"evidence"
    assert downloaded.headers["etag"] == f'"{evidence["digest"]}"'
    assert downloaded.headers["content-type"] == "application/octet-stream"
    assert downloaded.headers["cache-control"] == "private, no-store"
    denied = await client.get(f"/v1/artifacts/{evidence['ref']}", headers={"Authorization": "Bearer wrong"})
    assert denied.status_code == 401
    assert await service.store.pool.fetchval(
        "SELECT count(*) FROM kotoyose.execution_artifacts WHERE attempt_id=$1 AND content IS NULL AND storage_backend='minio'", attempt_id,
    ) == 3
    service.store.artifact_retention.storage = service.store.artifact_storage
    for artifact in attempt['artifacts']:
        await service.store.artifact_retention.change('test-caller', artifact['ref'], ArtifactRetentionChange(
            schema_version='1', request_id=f'retention:{artifact["ref"]}', expected_version=0,
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1), pins=[], proof_ref='caller:test:expired',
        ))
    assert await service.store.artifact_retention.gc() == 3
    assert storage.client.list_objects_v2(Bucket=storage.config.bucket).get('KeyCount', 0) == 0
    assert (await client.get(f'/v1/artifacts/{evidence["ref"]}')).status_code == 410
    assert (await client.get(f'/v1/artifacts/{evidence["ref"]}/retention')).json()['state'] == 'tombstone'


@pytest.mark.skipif(not os.environ.get("KOTOYOSE_DOCKER_TEST_IMAGE"), reason="Native conformance requires a Docker test image ID")
@pytest.mark.asyncio
async def test_native_storage_failure_finishes_failed_without_success_evidence(storage, native, monkeypatch):
    client, workspace, service, isolation = native
    service.store.artifact_storage = ArtifactBlobRegistry("minio", {"minio": storage})

    async def unavailable(blob, content):
        raise ArtifactStorageError("test storage failure")

    monkeypatch.setattr(storage, "put", unavailable)
    body = await _execution(client, workspace, ["sh", "-c", "printf hello"], isolation)
    receipt = await client.post("/v1/executions", json=body)
    assert receipt.status_code == 202, receipt.text
    attempt_id = receipt.json()["attempt_id"]
    for _ in range(600):
        attempt = (await client.get(f"/v1/attempts/{attempt_id}")).json()
        if attempt["finished_at"]:
            break
        await asyncio.sleep(0.05)
    assert attempt["status"] == "failed" and attempt["reason"] == "artifact_storage_error"
    assert attempt["stop_confirmation"] == "confirmed" and attempt["artifacts"] == []
    events = (await client.get(f"/v1/attempts/{attempt_id}/events")).json()["events"]
    assert not any(event["type"] == "succeeded" for event in events)
