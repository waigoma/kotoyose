"""Remote Worker を Native HTTP / Postgres の execution lifecycle から検証する。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import ssl
import subprocess
import uuid

import asyncpg
import httpx
import pytest
import pytest_asyncio

from kotoyose.app import app
from kotoyose.execution_resources import NativeExecutionConfig
from kotoyose.execution_service import ExecutionService
from kotoyose.execution_store import ExecutionStore
from kotoyose.local_worker import DockerLocalWorker
from kotoyose.remote_worker_identity import certificate_file_fingerprint
from kotoyose.remote_worker_contract import RemoteWorkerRef
from kotoyose.remote_worker_journal import RemoteWorkerJournal
from kotoyose.remote_worker_server import RemoteWorkerServer
from kotoyose.execution_resources import (
    RemoteWorkerConnection,
    SharedWorkspaceBinding,
)
from kotoyose.execution_resources import ExecutionError


DATABASE_URL = os.environ.get("KOTOYOSE_REMOTE_EXECUTION_TEST_DATABASE_URL")
IMAGE_ID = os.environ.get("KOTOYOSE_DOCKER_TEST_IMAGE")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL or not IMAGE_ID,
    reason="Remote execution integration requires an explicitly isolated test DB and Docker image",
)


def _run(*args: str) -> None:
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _make_pki(root: Path) -> dict[str, Path | str]:
    """試験専用の CA、gateway client、worker server 証明書を作る。"""
    root.mkdir(parents=True, exist_ok=True)
    _run(
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(root / "ca.key"), "-out", str(root / "ca.crt"),
        "-subj", "/CN=kotoyose-remote-test-ca", "-days", "1",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
    )
    for name, common_name, extension in (
        ("server", "localhost", "subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth"),
        ("gateway", "gateway", "extendedKeyUsage=clientAuth"),
    ):
        _run(
            "openssl", "req", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(root / f"{name}.key"), "-out", str(root / f"{name}.csr"),
            "-subj", f"/CN={common_name}",
        )
        ext = root / f"{name}.ext"
        ext.write_text(extension, encoding="ascii")
        _run(
            "openssl", "x509", "-req", "-in", str(root / f"{name}.csr"),
            "-CA", str(root / "ca.crt"), "-CAkey", str(root / "ca.key"),
            "-CAcreateserial", "-out", str(root / f"{name}.crt"), "-days", "1",
            "-extfile", str(ext),
        )
    der = ssl.PEM_cert_to_DER_cert((root / "gateway.crt").read_text(encoding="ascii"))
    return {"root": root, "gateway_fingerprint": hashlib.sha256(der).hexdigest()}


class _NoLocalWorker:
    """remote-only 構成で local Docker を暗黙に使わせない。"""

    async def probe(self) -> bool:
        return False

    async def execute(self, job, cancel):
        raise AssertionError("local worker must not be selected")

    async def stop(self, attempt_id: str, generation: int, grace_seconds: float) -> str:
        return "unconfirmed"


@pytest_asyncio.fixture
async def remote_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """専用 DB/schema と同一 shared workspace を持つ実 mTLS/Docker runtime。"""
    assert DATABASE_URL is not None and IMAGE_ID is not None
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=8, command_timeout=15)
    database = await pool.fetchval("SELECT current_database()")
    assert database.startswith("ahs_execution_test_remote"), "Refusing a non-dedicated remote DB"
    await pool.execute("DROP SCHEMA IF EXISTS kotoyose CASCADE; CREATE SCHEMA kotoyose")

    pki = _make_pki(tmp_path / "pki")
    shared_root = tmp_path / "shared-root"
    workspace = shared_root / "workspace"
    workspace.mkdir(parents=True)
    challenge = os.urandom(32)
    (workspace / ".kotoyose-binding").write_bytes(challenge)
    server_journal = RemoteWorkerJournal(tmp_path / "worker.sqlite")
    worker_ref = RemoteWorkerRef(
        worker_id="remote-1", generation=1, principal="gateway",
        certificate_fingerprint=str(pki["gateway_fingerprint"]),
        server_certificate_fingerprint=certificate_file_fingerprint(Path(pki["root"]) / "server.crt"),
    )
    server = RemoteWorkerServer(
        journal=server_journal, worker=DockerLocalWorker(IMAGE_ID), workspace_root=shared_root,
        ca_file=Path(pki["root"]) / "ca.crt", certificate_file=Path(pki["root"]) / "server.crt",
        key_file=Path(pki["root"]) / "server.key",
    )
    server.enroll_worker(worker_ref)
    server.register_workspace("remote-workspace", workspace, 0, ".kotoyose-binding")
    server.start()
    binding = SharedWorkspaceBinding(
        caller_workspace_ref="caller-workspace", remote_workspace_ref="remote-workspace",
        local_path=workspace, challenge_relative=".kotoyose-binding",
        challenge_digest=hashlib.sha256(challenge).hexdigest(), remote_workspace_generation=0,
    )
    connection = RemoteWorkerConnection(
        worker=worker_ref, endpoint=f"https://127.0.0.1:{server.address[1]}",
        ca_file=Path(pki["root"]) / "ca.crt", certificate_file=Path(pki["root"]) / "gateway.crt",
        key_file=Path(pki["root"]) / "gateway.key", journal_path=tmp_path / "gateway.sqlite",
    )
    from kotoyose.config import HarnessConfig
    from tests.fixtures.api_keys import fixture_key_store

    clients = {"caller-a": {
            "workspace_roots": [str(tmp_path)],
            "max_run_seconds": 30, "max_memory_bytes": 128 * 1024 * 1024, "max_processes": 16,
            "max_cpus": 1, "max_concurrent_runs": 2, "places": ["isolated"],
        }}
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(clients=clients))
    monkeypatch.setattr(app.state, "api_key_store", await fixture_key_store(**{'caller-a': 'remote-test-token'}))
    config = NativeExecutionConfig.model_validate({
        "schema_version": "1", "image_id": IMAGE_ID,
        "worker_capabilities": {"remote-1": {
            "worker_id": "remote-1", "os": "linux", "isolation_modes": ["sandbox"],
            "workspace_locality": "remote", "remote_connection": connection.model_dump(mode="json"),
            "workspace_bindings": [binding.model_dump(mode="json")],
        }},
    })
    service = ExecutionService(config, ExecutionStore(pool), _NoLocalWorker())
    await service.start()
    previous = getattr(app.state, "execution_service", None)
    app.state.execution_service = service
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"Authorization": "Bearer kys_remote-test-token"},
        ) as client:
            yield client, workspace, service, server, worker_ref
    finally:
        app.state.execution_service = previous
        await service.close()
        server.close()
        server_journal.close()
        for row in await pool.fetch("SELECT id, generation FROM kotoyose.execution_attempts"):
            process = await asyncio.create_subprocess_exec(
                "docker", "rm", "-f", f"kotoyose-{row['id']}-{row['generation']}",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await process.wait()
        await pool.execute("DROP SCHEMA IF EXISTS kotoyose CASCADE")
        await pool.close()


async def _resource(client: httpx.AsyncClient, kind: str, **payload: object) -> str:
    response = await client.post("/v1/resources", json={"resource": {"schema_version": "1", "kind": kind, **payload}})
    assert response.status_code == 201, response.text
    return response.json()["resource_ref"]


async def _command(client: httpx.AsyncClient, workspace: Path, argv: list[str], *, long_running: bool = False) -> dict:
    registered = await client.post("/v1/workspaces", json={
        "schema_version": "1", "path": str(workspace), "caller_workspace_ref": "caller-workspace",
    })
    assert registered.status_code == 201, registered.text
    workspace_ref = registered.json()["workspace_ref"]
    actual_argv = ["/bin/sh", "-c", "sleep 30"] if long_running else [
        "/bin/sh", "-c", "printf remote-proof > result; printf remote-output",
    ]
    command = await _resource(client, "command_spec", executable=actual_argv[0])
    args = await _resource(client, "argv", values=actual_argv[1:])
    cwd = await _resource(client, "cwd", path=".")
    timeout = await _resource(client, "timeout_policy", soft_seconds=1, hard_seconds=20, grace_seconds=1)
    now = datetime.now(timezone.utc)
    budget = await _resource(
        client, "budget", reservation_ref="remote-reservation", mode="observed",
        deadline=(now + timedelta(minutes=2)).isoformat(), memory_bytes=64 * 1024 * 1024,
        pids_limit=8, cpus=1, output_limit_bytes=8192,
    )
    grant = await _resource(
        client, "grant", grant_id="remote-grant", version=1, caller_run_ref="remote-run",
        workspace_ref=workspace_ref, isolation="sandbox", network="none",
        command_spec_refs=[command], resource_refs=[args, cwd, timeout, budget],
        policy_ref="remote-policy", not_before=(now - timedelta(seconds=1)).isoformat(),
        expires_at=(now + timedelta(minutes=2)).isoformat(), max_seconds=30, delegation="none",
    )
    return {"execution": {
        "schema_version": "1", "kind": "command", "idempotency_key": str(uuid.uuid4()),
        "caller_run_ref": "remote-run", "workspace_ref": workspace_ref,
        "execution_grant_ref": grant, "budgets_ref": budget, "command_spec_ref": command,
        "argv_ref": args, "cwd_ref": cwd, "timeout_policy_ref": timeout,
        "requirements": {"isolation": "sandbox", "worker_requirements": {"workspace_locality": "remote"}},
    }}


async def _terminal(client: httpx.AsyncClient, attempt_id: str) -> dict:
    async with asyncio.timeout(40):
        while True:
            response = await client.get(f"/v1/attempts/{attempt_id}")
            assert response.status_code == 200, response.text
            body = response.json()
            if body["finished_at"] is not None:
                return body
            await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_remote_http_pg_docker_artifact_events_and_response_loss(remote_runtime):
    """HTTP admission から remote Docker、artifact、event、同一 operation 回復まで通す。"""
    client, workspace, service, server, _ = remote_runtime
    body = await _command(client, workspace, ["/bin/sh", "-c", "ignored"])
    response = await client.post("/v1/executions", json=body)
    assert response.status_code == 202, response.text
    attempt_id = response.json()["attempt_id"]
    # POST の応答後に transport response を落としても、operation は GET 回復される。
    server.drop_next_response(f"remote-{attempt_id}-0")
    terminal = await _terminal(client, attempt_id)
    assert terminal["status"] == "succeeded", terminal
    assert terminal["stop_confirmation"] == "confirmed"
    assert terminal["workspace_generation"] == 1
    assert (workspace / "result").read_text(encoding="ascii") == "remote-proof"
    stdout = next(item for item in terminal["artifacts"] if item["name"] == "stdout")
    artifact = await client.get(f"/v1/artifacts/{stdout['ref']}")
    assert artifact.status_code == 200
    assert artifact.content == b"remote-output"
    assert artifact.headers["etag"] == f'"{hashlib.sha256(artifact.content).hexdigest()}"'
    events = (await client.get(f"/v1/attempts/{attempt_id}/events")).json()
    assert [event["sequence"] for event in events["events"]] == list(range(1, events["next_cursor"] + 1))
    assert events["events"][-1]["type"] == "succeeded"
    operations = service._remote_journals["remote-1"].operation(f"remote-{attempt_id}-0")
    assert operations is not None and operations.state == "confirmed"


@pytest.mark.asyncio
async def test_remote_http_cancel_confirms_stop_and_releases_workspace(remote_runtime):
    """remote worker の cancel は Docker 停止確認後にだけ writer を解放する。"""
    client, workspace, _, server, _ = remote_runtime
    body = await _command(client, workspace, ["/bin/sh", "-c", "ignored"], long_running=True)
    response = await client.post("/v1/executions", json=body)
    assert response.status_code == 202, response.text
    attempt_id = response.json()["attempt_id"]
    async with asyncio.timeout(10):
        while True:
            state = await client.get(f"/v1/attempts/{attempt_id}")
            if state.json()["status"] == "running":
                break
            await asyncio.sleep(0.05)
    cancellation = await client.post(f"/v1/attempts/{attempt_id}/cancel", json={})
    assert cancellation.status_code == 200, cancellation.text
    terminal = await _terminal(client, attempt_id)
    assert terminal["status"] == "cancelled", terminal
    assert terminal["stop_confirmation"] == "confirmed"
    assert terminal["side_effect_resolution"] == "resolved"
    assert (await client.post(f"/v1/attempts/{attempt_id}/reconcile", json={})).status_code == 200
    assert server.journal.operation(f"remote-{attempt_id}-0") is not None


@pytest.mark.asyncio
async def test_remote_gateway_restart_reconciles_same_pg_attempt_without_redispatch(remote_runtime):
    """gateway 再起動後も同じ PG Attempt と operation を読み取り、再 dispatch しない。"""
    client, workspace, service, server, worker_ref = remote_runtime
    body = await _command(client, workspace, ["/bin/sh", "-c", "ignored"], long_running=True)
    response = await client.post("/v1/executions", json=body)
    assert response.status_code == 202, response.text
    attempt_id = response.json()["attempt_id"]
    async with asyncio.timeout(10):
        while (await client.get(f"/v1/attempts/{attempt_id}")).json()["status"] != "running":
            await asyncio.sleep(0.05)
    operation_id = f"remote-{attempt_id}-0"
    # gateway の running は remote dispatch 前にも見えるため、故障対象の実行を待つ。
    async with asyncio.timeout(10):
        while server.journal.operation(operation_id) is None:
            await asyncio.sleep(0.05)
    await asyncio.to_thread(server.close)
    server.start()
    terminal = await _terminal(client, attempt_id)
    assert terminal["status"] == "worker_lost", terminal
    assert terminal["stop_confirmation"] == "confirmed"
    operation = service._remote_journals[worker_ref.worker_id].operation(operation_id)
    assert operation is not None and operation.state == "confirmed"
    assert await service.store.pool.fetchval(
        "SELECT count(*) FROM kotoyose.execution_operations WHERE attempt_id=$1 AND kind='local_execution'",
        attempt_id,
    ) == 1


@pytest.mark.asyncio
async def test_remote_requirements_are_rejected_before_attempt(remote_runtime):
    """unsupported OS/GPU と challenge drift は Attempt 作成前に拒否する。"""
    client, workspace, _, _, _ = remote_runtime
    body = await _command(client, workspace, ["/bin/sh", "-c", "ignored"])
    for requirements in (
        {"isolation": "sandbox", "worker_requirements": {"os": "windows", "workspace_locality": "remote"}},
        {"isolation": "sandbox", "worker_requirements": {"gpu": "any", "workspace_locality": "remote"}},
    ):
        invalid = {"execution": {**body["execution"], "idempotency_key": str(uuid.uuid4()), "requirements": requirements}}
        response = await client.post("/v1/executions", json=invalid)
        assert response.status_code == 501, response.text
    (workspace / ".kotoyose-binding").write_bytes(b"different-challenge")
    invalid = {"execution": {**body["execution"], "idempotency_key": str(uuid.uuid4())}}
    response = await client.post("/v1/executions", json=invalid)
    assert response.status_code == 409, response.text
    count = await remote_runtime[2].store.pool.fetchval("SELECT count(*) FROM kotoyose.execution_attempts")
    assert count == 0


@pytest.mark.asyncio
async def test_remote_attempt_rejects_worker_identity_rotation_before_stop(remote_runtime):
    """保存済み remote identity が現在の enrollment と変われば旧設定で停止しない。"""
    client, workspace, service, _, _ = remote_runtime
    body = await _command(client, workspace, ["/bin/sh", "-c", "printf proof > result"])
    response = await client.post("/v1/executions", json=body)
    assert response.status_code == 202, response.text
    attempt_id = response.json()["attempt_id"]
    attempt = await service.store.attempt("caller-a", attempt_id)
    worker = service.config.worker_capabilities["remote-1"]
    assert worker.remote_connection is not None
    worker.remote_connection.worker.generation += 1
    with pytest.raises(ExecutionError, match="identity or binding changed"):
        service._worker_for(attempt["snapshot"])
