"""Remote Worker の mTLS identity、冪等性、Docker 縦断を検証する。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import ssl
import subprocess
import uuid
from pathlib import Path

import pytest

from kotoyose.execution_jobs import CommandJob, WorkerResult
from kotoyose.local_worker import DockerLocalWorker
from kotoyose.remote_worker_client import RemoteWorkerClient
from kotoyose.remote_worker_contract import RemoteCommandResult, RemoteWorkerRef
from kotoyose.remote_worker_identity import certificate_file_fingerprint, certificate_fingerprint
from kotoyose.remote_worker_journal import RemoteWorkerJournal
from kotoyose.remote_worker_server import RemoteWorkerServer


def _run(*args: str) -> None:
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture()
def pki(tmp_path: Path) -> dict[str, Path | str]:
    """一時 CA と server/gateway 証明書を生成する。"""
    _run(
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(tmp_path / "ca.key"), "-out", str(tmp_path / "ca.crt"),
        "-subj", "/CN=kotoyose-test-ca", "-days", "1",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
    )
    for name, cn, extension in (
        ("server", "localhost", "subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth"),
        ("server-other", "localhost", "subjectAltName=DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth"),
        ("server-ip", "wrong-host", "subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth"),
        ("gateway", "gateway", "extendedKeyUsage=clientAuth"),
        ("other", "other-gateway", "extendedKeyUsage=clientAuth"),
    ):
        _run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(tmp_path / f"{name}.key"),
             "-out", str(tmp_path / f"{name}.csr"), "-subj", f"/CN={cn}")
        extension_file = tmp_path / f"{name}.ext"
        extension_file.write_text(extension, encoding="ascii")
        _run("openssl", "x509", "-req", "-in", str(tmp_path / f"{name}.csr"),
             "-CA", str(tmp_path / "ca.crt"), "-CAkey", str(tmp_path / "ca.key"), "-CAcreateserial",
             "-out", str(tmp_path / f"{name}.crt"), "-days", "1", "-extfile", str(extension_file))
    der = ssl.PEM_cert_to_DER_cert((tmp_path / "gateway.crt").read_text(encoding="ascii"))
    return {"root": tmp_path, "fingerprint": hashlib.sha256(der).hexdigest()}


class _FakeWorker:
    async def probe(self) -> bool:
        return True

    async def execute(self, job: CommandJob, cancel: asyncio.Event) -> WorkerResult:
        return WorkerResult("succeeded", 0, b"fake-output", b"", "confirmed", "command_completed")

    async def stop(self, attempt_id: str, generation: int, grace_seconds: float) -> str:
        return "confirmed"


def _server(pki: dict[str, Path | str], tmp_path: Path, worker) -> tuple[RemoteWorkerServer, RemoteWorkerClient, RemoteWorkerRef]:
    root = tmp_path / "worker-root"
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    journal = RemoteWorkerJournal(tmp_path / "worker.sqlite")
    fingerprint = str(pki["fingerprint"])
    ref = RemoteWorkerRef(
        worker_id="worker-1", generation=1, principal="gateway", certificate_fingerprint=fingerprint,
        server_certificate_fingerprint=certificate_file_fingerprint(Path(pki["root"]) / "server.crt"),
    )
    server = RemoteWorkerServer(
        journal=journal,
        worker=worker,
        workspace_root=root,
        ca_file=Path(pki["root"]) / "ca.crt",
        certificate_file=Path(pki["root"]) / "server.crt",
        key_file=Path(pki["root"]) / "server.key",
    )
    server.enroll_worker(ref)
    server.register_workspace("workspace-1", workspace, 1)
    client_journal = RemoteWorkerJournal(tmp_path / "gateway.sqlite")
    client = RemoteWorkerClient(
        base_url=f"https://127.0.0.1:{server.address[1]}", worker=ref,
        ca_file=Path(pki["root"]) / "ca.crt", certificate_file=Path(pki["root"]) / "gateway.crt",
        key_file=Path(pki["root"]) / "gateway.key", journal=client_journal,
        workspace_ref="workspace-1", workspace_generation=1,
    )
    return server, client, ref


async def _remove_container(worker: DockerLocalWorker, job: CommandJob) -> None:
    """縦断試験が作成した停止確認済み container を後始末する。"""
    if await worker.stop(job.attempt_id, job.generation, 0) != "confirmed":
        return
    process = await asyncio.create_subprocess_exec(
        worker.docker_binary, "rm", worker._container_name(job),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await process.communicate()


def test_journal_rejects_reused_operation_with_different_digest(tmp_path):
    journal = RemoteWorkerJournal(tmp_path / "journal.sqlite")
    ref = RemoteWorkerRef(
        worker_id="worker-1", generation=1, principal="gateway", certificate_fingerprint="a" * 64,
        server_certificate_fingerprint="b" * 64,
    )
    journal.enroll(ref)
    journal.prepare_operation("operation-1", "attempt-1", 1, "b" * 64, ref)
    with pytest.raises(ValueError, match="digest"):
        journal.prepare_operation("operation-1", "attempt-1", 1, "c" * 64, ref)
    journal.close()


def test_workspace_writer_claim_is_fenced_until_confirmed(tmp_path):
    journal = RemoteWorkerJournal(tmp_path / "claims.sqlite")
    ref = RemoteWorkerRef(
        worker_id="worker-1", generation=1, principal="gateway", certificate_fingerprint="a" * 64,
        server_certificate_fingerprint="b" * 64,
    )
    journal.enroll(ref)
    journal.prepare_operation("op-1", "attempt-1", 1, "c" * 64, ref, "workspace-1", 1)
    with pytest.raises(ValueError, match="claimed"):
        journal.prepare_operation("op-2", "attempt-2", 1, "d" * 64, ref, "workspace-1", 1)
    journal.save_result(RemoteCommandResult(
        operation_id="op-1", status="worker_lost", exit_code=None, stdout="", stderr="",
        stop_confirmation="unconfirmed", reason="partition",
    ))
    with pytest.raises(ValueError, match="claimed"):
        journal.prepare_operation("op-2", "attempt-2", 1, "d" * 64, ref, "workspace-1", 1)
    journal.save_result(RemoteCommandResult(
        operation_id="op-1", status="worker_lost", exit_code=None, stdout="", stderr="",
        stop_confirmation="confirmed", reason="reconciled",
    ))
    journal.prepare_operation("op-2", "attempt-2", 1, "d" * 64, ref, "workspace-1", 1)
    journal.close()


@pytest.mark.asyncio
async def test_mtls_journal_and_response_loss_reconnect(pki, tmp_path):
    """実 TLS peer certificate と同一 operation の GET 回復を確認する。"""
    server, client, _ = _server(pki, tmp_path, _FakeWorker())
    try:
        server.start()
        assert await client.probe()
        job = CommandJob("attempt-fake", 1, tmp_path / "unused", ("true",))
        server.drop_next_response("remote-attempt-fake-1")
        result = await client.execute(job, asyncio.Event())
        assert result.status == "succeeded"
        assert result.stdout == b"fake-output"
        assert client.journal.operation("remote-attempt-fake-1").state == "confirmed"
    finally:
        server.close()
        server.journal.close()
        client.journal.close()


@pytest.mark.asyncio
async def test_shared_workspace_challenge_is_read_on_both_sides(pki, tmp_path):
    server, client, _ = _server(pki, tmp_path, _FakeWorker())
    workspace = tmp_path / "worker-root" / "workspace"
    challenge = workspace / ".kotoyose-binding"
    challenge.write_bytes(os.urandom(32))
    # 再登録では challenge identity も durable に固定されるため、最初の登録を作り直す。
    old_server = server
    server.journal.close()
    old_server.close()
    server = RemoteWorkerServer(
        journal=RemoteWorkerJournal(tmp_path / "binding-server.sqlite"), worker=_FakeWorker(),
        workspace_root=tmp_path / "worker-root", ca_file=Path(pki["root"]) / "ca.crt",
        certificate_file=Path(pki["root"]) / "server.crt", key_file=Path(pki["root"]) / "server.key",
    )
    fp = str(pki["fingerprint"])
    ref = RemoteWorkerRef(
        worker_id="worker-1", generation=1, principal="gateway", certificate_fingerprint=fp,
        server_certificate_fingerprint=certificate_file_fingerprint(Path(pki["root"]) / "server.crt"),
    )
    server.enroll_worker(ref)
    server.register_workspace("workspace-1", workspace, 1, ".kotoyose-binding")
    client.journal.close()
    client = RemoteWorkerClient(
        base_url=f"https://127.0.0.1:{server.address[1]}", worker=ref,
        ca_file=Path(pki["root"]) / "ca.crt", certificate_file=Path(pki["root"]) / "gateway.crt",
        key_file=Path(pki["root"]) / "gateway.key", journal=RemoteWorkerJournal(tmp_path / "binding-client.sqlite"),
        workspace_ref="workspace-1", workspace_generation=1,
    )
    try:
        server.start()
        assert await client.verify_shared_binding(workspace, "workspace-1", 1, ".kotoyose-binding")
        challenge.write_bytes(b"changed")
        assert not await client.verify_shared_binding(workspace, "workspace-1", 1, ".kotoyose-binding")
    finally:
        server.close()
        server.journal.close()
        client.journal.close()


@pytest.mark.asyncio
async def test_wrong_certificate_is_rejected_without_operation(pki, tmp_path):
    server, client, ref = _server(pki, tmp_path, _FakeWorker())
    other = RemoteWorkerClient(
        base_url=f"https://127.0.0.1:{server.address[1]}", worker=ref,
        ca_file=Path(pki["root"]) / "ca.crt", certificate_file=Path(pki["root"]) / "other.crt",
        key_file=Path(pki["root"]) / "other.key", journal=RemoteWorkerJournal(tmp_path / "other.sqlite"),
        workspace_ref="workspace-1", workspace_generation=1,
    )
    try:
        server.start()
        assert not await other.probe()
        result = await other.execute(CommandJob("attempt-other", 1, tmp_path / "unused", ("true",)), asyncio.Event())
        assert result.status == "worker_lost"
        assert server.journal.operation("remote-attempt-other-1") is None
    finally:
        server.close()
        server.journal.close()
        client.journal.close()
        other.journal.close()


@pytest.mark.asyncio
async def test_same_ca_wrong_server_certificate_is_pinned(pki, tmp_path):
    """hostname が一致していても enrollment 済み server fingerprint 以外は拒否する。"""
    server, client, ref = _server(pki, tmp_path, _FakeWorker())
    wrong_journal = RemoteWorkerJournal(tmp_path / "wrong-server.sqlite")
    wrong_server = RemoteWorkerServer(
        journal=wrong_journal,
        worker=_FakeWorker(),
        workspace_root=tmp_path / "worker-root",
        ca_file=Path(pki["root"]) / "ca.crt",
        certificate_file=Path(pki["root"]) / "server-other.crt",
        key_file=Path(pki["root"]) / "server-other.key",
    )
    wrong_ref = ref.model_copy(update={
        "server_certificate_fingerprint": certificate_file_fingerprint(Path(pki["root"]) / "server-other.crt"),
    })
    wrong_server.enroll_worker(wrong_ref)
    wrong_server.register_workspace("workspace-1", tmp_path / "worker-root" / "workspace", 1)
    wrong_server.start()
    client.endpoint = type(client.endpoint)("127.0.0.1", wrong_server.address[1])
    try:
        assert not await client.probe()
    finally:
        wrong_server.close()
        wrong_server.journal.close()
        server.close()
        server.journal.close()
        client.journal.close()


def test_server_hostname_is_verified_by_tls(pki, tmp_path):
    root = tmp_path / "worker-root"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    journal = RemoteWorkerJournal(tmp_path / "hostname.sqlite")
    client_journal = RemoteWorkerJournal(tmp_path / "hostname-client.sqlite")
    gateway_fp = str(pki["fingerprint"])
    server_fp = certificate_file_fingerprint(Path(pki["root"]) / "server-ip.crt")
    ref = RemoteWorkerRef(
        worker_id="worker-hostname", generation=1, principal="gateway", certificate_fingerprint=gateway_fp,
        server_certificate_fingerprint=server_fp,
    )
    server = RemoteWorkerServer(
        journal=journal, worker=_FakeWorker(), workspace_root=root,
        ca_file=Path(pki["root"]) / "ca.crt", certificate_file=Path(pki["root"]) / "server-ip.crt",
        key_file=Path(pki["root"]) / "server-ip.key",
    )
    server.enroll_worker(ref)
    server.register_workspace("workspace-hostname", workspace, 1)
    server.start()
    client = RemoteWorkerClient(
        base_url=f"https://localhost:{server.address[1]}", worker=ref,
        ca_file=Path(pki["root"]) / "ca.crt", certificate_file=Path(pki["root"]) / "gateway.crt",
        key_file=Path(pki["root"]) / "gateway.key", journal=client_journal,
        workspace_ref="workspace-hostname", workspace_generation=1,
    )
    try:
        with pytest.raises(ssl.SSLCertVerificationError):
            client._request("GET", "/v1/remote/health", None)
    finally:
        server.close()
        journal.close()
        client_journal.close()


def test_rotation_revokes_previous_worker_generation(pki, tmp_path):
    server, client, ref = _server(pki, tmp_path, _FakeWorker())
    try:
        rotated = RemoteWorkerRef(
            worker_id=ref.worker_id, generation=2, principal=ref.principal,
            certificate_fingerprint="b" * 64,
            server_certificate_fingerprint=ref.server_certificate_fingerprint,
        )
        server.enroll_worker(rotated)
        assert server.journal.active_worker(ref.worker_id, ref.generation, ref.certificate_fingerprint) is None
        assert server.journal.active_worker(rotated.worker_id, rotated.generation, rotated.certificate_fingerprint) == rotated
        server.journal.prepare_operation("old-op", "attempt-old", 1, "c" * 64, ref, "workspace-1", 1)
        assert server.journal.operation_for_worker("old-op", rotated) is not None
        assert server.journal.prepare_operation("old-op", "attempt-old", 1, "c" * 64, rotated, "workspace-1", 1).state == "prepared"
    finally:
        server.close()
        server.journal.close()
        client.journal.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("KOTOYOSE_REMOTE_WORKER_TESTS"), reason="set KOTOYOSE_REMOTE_WORKER_TESTS=1")
async def test_real_https_docker_command_and_workspace_write(pki, tmp_path):
    image = os.environ.get("KOTOYOSE_DOCKER_TEST_IMAGE")
    if not image:
        pytest.fail("KOTOYOSE_DOCKER_TEST_IMAGE is required for remote worker integration")
    server, client, _ = _server(pki, tmp_path, DockerLocalWorker(image))
    workspace = tmp_path / "worker-root" / "workspace"
    try:
        server.start()
        attempt_id = f"attempt-real-{uuid.uuid4().hex[:10]}"
        job = CommandJob(
            attempt_id, 1, workspace,
            ("/bin/sh", "-c", "printf remote-proof > /workspace/result"),
            timeout_seconds=20,
        )
        result = await client.execute(job, asyncio.Event())
        assert result.status == "succeeded", result.reason
        assert result.stop_confirmation == "confirmed"
        assert (workspace / "result").read_text(encoding="ascii") == "remote-proof"
    finally:
        server.close()
        server.journal.close()
        client.journal.close()
        await _remove_container(server.worker, job)


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("KOTOYOSE_REMOTE_WORKER_TESTS"), reason="set KOTOYOSE_REMOTE_WORKER_TESTS=1")
async def test_restart_reconciles_existing_docker_without_redispatch(pki, tmp_path):
    image = os.environ.get("KOTOYOSE_DOCKER_TEST_IMAGE")
    if not image:
        pytest.fail("KOTOYOSE_DOCKER_TEST_IMAGE is required for remote worker integration")
    server, client, ref = _server(pki, tmp_path, DockerLocalWorker(image))
    restarted = None
    workspace = tmp_path / "worker-root" / "workspace"
    attempt = f"attempt-restart-{uuid.uuid4().hex[:10]}"
    job = CommandJob(attempt, 1, workspace, ("/bin/sh", "-c", "sleep 30"), timeout_seconds=30)
    try:
        # 起動応答を受け取れない状態を journal に記録し、container は別途一度だけ作る。
        worker = server.worker
        assert await worker._create_container(job, workspace, worker._container_name(job))
        process = await worker._spawn("start", worker._container_name(job), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await process.communicate()
        await asyncio.sleep(0.2)
        server.journal.prepare_operation("remote-attempt-restart-1", attempt, 1, "c" * 64, ref)
        assert server.journal.mark_running("remote-attempt-restart-1")
        server.close()
        restarted, restarted_client, _ = _server(pki, tmp_path, DockerLocalWorker(image))
        # start() の recovery は Docker identity を inspect して停止し、再 dispatch はしない。
        restarted.start()
        state = restarted.journal.operation("remote-attempt-restart-1")
        assert state is not None and state.state == "confirmed"
        assert state.result is not None and state.result.status == "worker_lost"
        assert await restarted.worker.stop(attempt, 1, 0) == "confirmed"
        restarted.close()
        restarted.journal.close()
        restarted_client.journal.close()
    finally:
        if server._thread is not None:
            server.close()
        server.journal.close()
        client.journal.close()
        if restarted is not None:
            restarted.close()
            restarted.journal.close()
        await _remove_container(server.worker, job)


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("KOTOYOSE_REMOTE_WORKER_TESTS"), reason="set KOTOYOSE_REMOTE_WORKER_TESTS=1")
async def test_cancel_remote_execution_confirms_container_stop(pki, tmp_path):
    image = os.environ.get("KOTOYOSE_DOCKER_TEST_IMAGE")
    if not image:
        pytest.fail("KOTOYOSE_DOCKER_TEST_IMAGE is required for remote worker integration")
    server, client, _ = _server(pki, tmp_path, DockerLocalWorker(image))
    workspace = tmp_path / "worker-root" / "workspace"
    cancel = asyncio.Event()
    job = CommandJob(f"attempt-cancel-{uuid.uuid4().hex[:10]}", 1, workspace, ("/bin/sh", "-c", "sleep 30"), timeout_seconds=30)
    try:
        server.start()
        task = asyncio.create_task(client.execute(job, cancel))
        await asyncio.sleep(0.5)
        cancel.set()
        result = await asyncio.wait_for(task, timeout=15)
        assert result.status == "cancelled", result.reason
        assert result.stop_confirmation == "confirmed"
    finally:
        server.close()
        server.journal.close()
        client.journal.close()
        await _remove_container(server.worker, job)
