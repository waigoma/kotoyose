"""実 HTTP / Postgres / Docker を横断する Native command の適合試験。"""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio

from kotoyose.app import app
from kotoyose.execution_resources import NativeExecutionConfig
from kotoyose.execution_service import ExecutionService
from kotoyose.execution_store import ExecutionStore
from kotoyose.local_worker import DockerLocalWorker


DATABASE_URL = os.environ.get("KOTOYOSE_EXECUTION_SERVICE_TEST_DATABASE_URL")
IMAGE_ID = os.environ.get("KOTOYOSE_DOCKER_TEST_IMAGE")
pytestmark = pytest.mark.skipif(
    not IMAGE_ID,
    reason="Native conformance requires a Docker test image ID",
)


@pytest_asyncio.fixture(params=["sandbox"])
async def native(tmp_path: Path, monkeypatch, request):
    """専用 DB だけを初期化し、公開サービスや provider を呼ばない。"""
    isolation = request.param
    assert DATABASE_URL is not None, "Dedicated test database connection is required"
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=6, command_timeout=10)
    database = await pool.fetchval("SELECT current_database()")
    assert database.startswith("ahs_execution_test_service"), "Refusing to touch a non-test database"
    await pool.execute("DROP SCHEMA IF EXISTS kotoyose CASCADE; CREATE SCHEMA kotoyose")
    workspace = tmp_path / "clone"
    workspace.mkdir()
    from kotoyose.config import HarnessConfig
    from tests.fixtures.api_keys import fixture_key_store

    clients = {"test-caller": {
            "workspace_roots": [str(tmp_path)],
            "max_run_seconds": 30, "max_memory_bytes": 128 * 1024 * 1024, "max_processes": 32,
            "max_cpus": 1, "max_concurrent_runs": 2,
            "places": ["isolated" if isolation == "sandbox" else "host"],
        }}
    clients["other-caller"] = {}
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(clients=clients))
    monkeypatch.setattr(app.state, "api_key_store", await fixture_key_store(**{'test-caller': 'native-conformance-only', 'other-caller': 'other-caller'}))
    config = NativeExecutionConfig.model_validate({
        "schema_version": "1", "image_id": IMAGE_ID,
    })
    service = ExecutionService(config, ExecutionStore(pool), DockerLocalWorker(IMAGE_ID))
    await service.start()
    previous = getattr(app.state, "execution_service", None)
    app.state.execution_service = service
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer kotoyose_native-conformance-only"},
        ) as client:
            yield client, workspace, service, isolation
    finally:
        await service.close()
        app.state.execution_service = previous
        rows = await pool.fetch("SELECT id,generation FROM kotoyose.execution_attempts")
        for row in rows:
            process = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", f"kotoyose-{row['id']}-{row['generation']}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await process.wait()
        await pool.execute("DROP SCHEMA kotoyose CASCADE")
        await pool.close()


async def _execution(client, workspace, argv, isolation, *, artifact_spec=False):
    async def resource(kind, **payload):
        response = await client.post("/v1/resources", json={"resource": {"schema_version": "1", "kind": kind, **payload}})
        assert response.status_code == 201, response.text
        return response.json()["resource_ref"]

    registered = await client.post("/v1/workspaces", json={
        "schema_version": "1", "path": str(workspace), "caller_workspace_ref": "test-clone",
    })
    assert registered.status_code == 201, registered.text
    workspace_ref = registered.json()["workspace_ref"]
    assert registered.json()["workspace_generation"] == 0
    command = await resource("command_spec", executable=argv[0])
    args = await resource("argv", values=argv[1:])
    cwd = await resource("cwd", path=".")
    timeout = await resource("timeout_policy", soft_seconds=1, hard_seconds=20, grace_seconds=1)
    artifact = None
    if artifact_spec:
        artifact = await resource(
            "artifact_spec", entries=[{
                "name": "evidence", "path": "evidence.bin", "media_type": "application/octet-stream",
                "required": True, "max_bytes": 1024,
            }], max_total_bytes=1024,
        )
    now = datetime.now(timezone.utc)
    budget = await resource(
        "budget", reservation_ref="test-reservation", mode="observed", deadline=(now + timedelta(minutes=2)).isoformat(),
        memory_bytes=64 * 1024 * 1024, pids_limit=16, cpus=1, output_limit_bytes=8192,
    )
    grant = await resource(
        "grant", grant_id="test-grant", version=1, caller_run_ref="test-run", workspace_ref=workspace_ref,
        isolation=isolation, network="none", command_spec_refs=[command],
        resource_refs=[args, cwd, timeout, budget, *([artifact] if artifact else [])],
        policy_ref="test-policy", not_before=(now - timedelta(seconds=1)).isoformat(),
        expires_at=(now + timedelta(minutes=2)).isoformat(), max_seconds=30, delegation="none",
    )
    return {"execution": {
        "schema_version": "1", "kind": "command", "idempotency_key": str(uuid4()), "caller_run_ref": "test-run",
        "workspace_ref": workspace_ref, "execution_grant_ref": grant, "budgets_ref": budget,
        "command_spec_ref": command, "argv_ref": args, "cwd_ref": cwd, "timeout_policy_ref": timeout,
        "requirements": {"isolation": isolation},
        **({"artifact_spec_ref": artifact} if artifact else {}),
    }}


async def test_cancel_by_key_fences_late_dispatch_without_starting_a_worker(native):
    """受理証がない取消を再送しても、新規 command の実行を伴わない。"""
    client, workspace, service, isolation = native
    body = await _execution(client, workspace, ["sh", "-c", "touch should-not-exist"], isolation)
    key = body["execution"]["idempotency_key"]
    for _ in range(2):
        response = await client.post("/v1/execution-cancellations", json={
            "schema_version": "1", "idempotency_key": key,
        })
        assert response.status_code == 200, response.text
        assert response.json() == {
            "schema_version": "1", "idempotency_key": key, "admission_closed": True,
            "outcome": "not_admitted", "attempt_id": None,
        }
    rejected = await client.post("/v1/executions", json=body)
    assert rejected.status_code == 409, rejected.text
    assert not (workspace / "should-not-exist").exists()
    assert not service._tasks


async def _terminal(client, attempt_id):
    async with asyncio.timeout(35):
        while True:
            result = await client.get(f"/v1/attempts/{attempt_id}")
            assert result.status_code == 200, result.text
            body = result.json()
            if body["finished_at"] is not None:
                return body
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_native_command_real_dispatch_replay_artifact_and_fence(native):
    """同時再送は一度だけ書き込み、終端・artifact・世代更新が一致する。"""
    client, workspace, _, isolation = native
    body = await _execution(client, workspace, ["sh", "-c", "printf once >> output; printf evidence"], isolation)
    responses = await asyncio.gather(*[client.post("/v1/executions", json=body) for _ in range(4)])
    assert all(response.status_code == 202 for response in responses), [response.text for response in responses]
    ids = {response.json()["attempt_id"] for response in responses}
    assert len(ids) == 1
    attempt_id = ids.pop()
    terminal = await _terminal(client, attempt_id)
    assert terminal["status"] == "succeeded", terminal
    assert terminal["stop_confirmation"] == "confirmed"
    assert terminal["side_effect_resolution"] == "resolved"
    reconciled = await client.post(f"/v1/attempts/{attempt_id}/reconcile", json={})
    assert reconciled.status_code == 200
    assert reconciled.json()["sequence"] == terminal["sequence"]
    assert reconciled.json()["status"] == "succeeded"
    assert terminal["workspace_ref"].endswith(":1")
    assert terminal["workspace_generation"] == 1
    assert terminal["usage"]["cost"] is None
    assert (workspace / "output").read_text() == "once"
    artifact = next(item for item in terminal["artifacts"] if item["name"] == "stdout")
    content = await client.get("/v1/artifacts/" + artifact["ref"])
    assert content.content == b"evidence"
    assert hashlib.sha256(content.content).hexdigest() == artifact["digest"]
    replay = await client.post("/v1/executions", json=body)
    assert replay.json()["attempt_id"] == attempt_id
    body["execution"]["caller_run_ref"] = "changed-run"
    assert (await client.post("/v1/executions", json=body)).status_code == 409
    events = (await client.get(f"/v1/attempts/{attempt_id}/events")).json()
    assert [item["sequence"] for item in events["events"]] == list(range(1, events["next_cursor"] + 1))
    assert events["events"][-1]["type"] == "succeeded"
    assert (await client.get(f"/v1/attempts/{attempt_id}/events?after={events['next_cursor']}")).json()["events"] == []
    denied = await client.get("/v1/artifacts/" + artifact["ref"], headers={"Authorization": "Bearer kys_other-caller"})
    assert denied.status_code == 401


@pytest.mark.asyncio
async def test_artifact_spec_is_collected_with_binary_http_metadata(native):
    """停止確認後にだけ file artifact を保存し、GET が media type と digest を返す。"""
    client, workspace, _, isolation = native
    body = await _execution(
        client, workspace, ["sh", "-c", "printf '\\000\\001evidence' > evidence.bin"], isolation,
        artifact_spec=True,
    )
    response = await client.post("/v1/executions", json=body)
    assert response.status_code == 202, response.text
    terminal = await _terminal(client, response.json()["attempt_id"])
    assert terminal["status"] == "succeeded", terminal
    evidence = next(item for item in terminal["artifacts"] if item["name"] == "evidence")
    assert evidence["media_type"] == "application/octet-stream"
    assert terminal["artifact_manifest"][0]["digest"] == evidence["digest"]
    downloaded = await client.get("/v1/artifacts/" + evidence["ref"])
    assert downloaded.status_code == 200
    assert downloaded.content == b"\x00\x01evidence"
    assert downloaded.headers["content-type"] == "application/octet-stream"
    assert downloaded.headers["etag"] == '"' + evidence["digest"] + '"'


@pytest.mark.asyncio
async def test_required_artifact_failure_is_output_contract_error(native):
    """required artifact の欠落は停止確認済みでも succeeded にしない。"""
    client, workspace, _, isolation = native
    body = await _execution(client, workspace, ["true"], isolation, artifact_spec=True)
    response = await client.post("/v1/executions", json=body)
    assert response.status_code == 202, response.text
    terminal = await _terminal(client, response.json()["attempt_id"])
    assert terminal["status"] == "failed", terminal
    assert terminal["reason"] == "output_contract_error"
    assert terminal["stop_confirmation"] == "confirmed"
    assert terminal["artifact_manifest"][0]["state"] == "missing"


@pytest.mark.asyncio
async def test_native_cancellation_confirms_child_stop_and_keeps_events(native):
    """無出力を失敗と扱わず、明示取消時には namespace 内の子も停止する。"""
    client, workspace, _, isolation = native
    body = await _execution(client, workspace, ["sh", "-c", "printf started > started; sleep 300 & wait"], isolation)
    response = await client.post("/v1/executions", json=body)
    assert response.status_code == 202, response.text
    attempt_id = response.json()["attempt_id"]
    async with asyncio.timeout(10):
        while not (workspace / "started").exists():
            await asyncio.sleep(0.05)
    cancellation = await client.post(f"/v1/attempts/{attempt_id}/cancel", json={})
    assert cancellation.status_code == 200
    terminal = await _terminal(client, attempt_id)
    assert terminal["status"] == "cancelled", terminal
    assert terminal["stop_confirmation"] == "confirmed"
    assert terminal["reason"] == "user_cancelled"


@pytest.mark.asyncio
async def test_unsupported_requirement_is_rejected_without_dispatch(native):
    """利用不能な要求に別 backend や保証の fallback を行わない。"""
    client, workspace, service, isolation = native
    body = await _execution(client, workspace, ["true"], isolation)
    body["execution"]["requirements"] = (
        {"isolation": "native"} if isolation == "sandbox"
        else {"isolation": "native", "capabilities": ["strict_cost"]}
    )
    response = await client.post("/v1/executions", json=body)
    assert response.status_code == 501
    assert await service.store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts") == 0
