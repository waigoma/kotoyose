"""Coordinator の喪失、受付拒否、readiness と終了期限の回帰試験。"""

import asyncio
from contextlib import suppress
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from kotoyose.app import app
from kotoyose.config import HarnessConfig
from kotoyose.execution_resources import NativeExecutionConfig
from kotoyose.execution_service import ExecutionService
from kotoyose.execution_shutdown import CloseTiming


class LeaseConnection:
    """接続生存と lock 所有を独立して制御する、DB を使わない session。"""

    def __init__(self):
        self.held = True
        self.stalled = False
        self.queries = []
        self.terminate = Mock()
        self.execute = AsyncMock()

    async def fetchval(self, query):
        self.queries.append(query)
        if "pg_try_advisory_lock" in query:
            return True
        if self.stalled:
            await asyncio.Event().wait()
        if query == "SELECT 1":
            return 1
        return self.held


def make_service(root):
    """外部 worker と DB のみを差し替えた実 ExecutionService を作る。"""
    import kotoyose.config as config_module
    from kotoyose.config import Client
    current = config_module.get_config()
    config_module._config = current.model_copy(update={
        "clients": {
            "caller": Client(
                workspace_roots=[root],
                max_run_seconds=30, max_memory_bytes=64 * 1024 * 1024,
                max_processes=16, max_cpus=1, max_concurrent_runs=1,
            ),
        },
    })
    config = NativeExecutionConfig.model_validate({
        "schema_version": "1", "image_id": "sha256:" + "a" * 64,
    })
    lease = LeaseConnection()
    store = SimpleNamespace(
        artifact_retention=SimpleNamespace(), workspace_retention=SimpleNamespace(),
        initialize=AsyncMock(), active=AsyncMock(return_value=[]),
        replay=AsyncMock(return_value=None), admit=AsyncMock(), confirm_stop=AsyncMock(),
        pool=SimpleNamespace(acquire=AsyncMock(return_value=lease), release=AsyncMock(),
                             fetchrow=AsyncMock(return_value=None), fetch=AsyncMock(return_value=[])),
    )
    worker = SimpleNamespace(probe=AsyncMock(return_value=True), execute=AsyncMock(), stop=AsyncMock())
    return ExecutionService(config, store, worker), lease


@pytest.fixture
async def coordinator(tmp_path, monkeypatch):
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(models={}))
    monkeypatch.setenv("KOTOYOSE_LEASE_TEST_TOKEN", "fixture-only")
    service, lease = make_service(tmp_path)
    # 修正前にも同じ試験を走らせ、heartbeat の待機間隔だけを短縮する。
    sleep = asyncio.sleep
    tick = asyncio.Event()
    service._test_heartbeat_tick = tick

    async def fast_sleep(delay):
        await tick.wait()
        await sleep(min(delay, 0.001))

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    exit_called = asyncio.Event()
    if hasattr(service, "_coordinator"):
        monkeypatch.setattr("kotoyose.coordinator_lease.exit_for_restart", exit_called.set)
    from tests.fixtures.api_keys import fixture_key_store
    monkeypatch.setattr(app.state, "api_key_store", await fixture_key_store(caller="fixture-only"))
    monkeypatch.setattr(app.state, "execution_service", service, raising=False)
    await service.start()
    try:
        yield service, lease, exit_called
    finally:
        await asyncio.wait_for(service.close(), timeout=1)
        service._watchdog.cancel()
        with suppress(asyncio.CancelledError):
            await service._watchdog


async def request(path, *, method="GET", body=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, json=body, headers={"Authorization": "Bearer kys_fixture-only"})


async def wait_for_loss(service):
    async with asyncio.timeout(6):
        while service._accepting:
            await asyncio.sleep(0.001)


async def test_ready_healthy_has_no_database_or_worker_side_effects(coordinator):
    """N3: readiness の繰り返し取得は lock 再取得も worker 起動も行わない。"""
    service, lease, _ = coordinator
    before = list(lease.queries)
    for _ in range(3):
        response = await request("/ready")
        assert response.status_code == 200
        assert response.json() == {"execution_ready": True, "reason": "ready"}
    assert (await request("/health")).status_code == 200
    assert lease.queries == before
    service.worker.execute.assert_not_awaited()
    service.store.pool.acquire.assert_awaited_once()


@pytest.mark.parametrize("pool", ["absent", None])
async def test_store_without_pool_has_no_coordinator_and_is_not_native_ready(tmp_path, monkeypatch, pool):
    """pool のない store は構築でき、native 無効と同じ readiness を返す。"""
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(models={}))
    template, _ = make_service(tmp_path)
    store = template.store
    if pool == "absent":
        del store.pool
    else:
        store.pool = None
    service = ExecutionService(template.config, store, template.worker)
    assert service._coordinator is None
    assert service._lease is None and service._watchdog is None
    monkeypatch.setattr(app.state, "execution_service", service, raising=False)
    response = await request("/ready")
    assert response.status_code == 503
    assert response.json() == {"execution_ready": False, "reason": "native_execution_disabled"}
    await service.close()


async def test_db_backed_service_before_start_has_not_created_a_coordinator(tmp_path, monkeypatch):
    """DB 構成でも start 前は lease を作らず、starting として受付不可を返す。"""
    monkeypatch.setattr("kotoyose.config._config", HarnessConfig(models={}))
    service, _ = make_service(tmp_path)
    assert service._coordinator is None
    service.store.pool.acquire.assert_not_awaited()
    monkeypatch.setattr(app.state, "execution_service", service, raising=False)
    response = await request("/ready")
    assert response.status_code == 503
    assert response.json() == {"execution_ready": False, "reason": "coordinator_starting"}
    await service.close()


async def test_normal_shutdown_preserves_worker_and_settle_waits(coordinator, monkeypatch):
    """通常終了は全体期限なしで 90 秒待ち、取消後も 30 秒の停止確認時間を残す。"""
    service, lease, _ = coordinator
    wait = asyncio.wait
    timeouts = []

    async def scaled_wait(tasks, *, timeout=None, **options):
        timeouts.append(timeout)
        return await wait(tasks, timeout=None if timeout is None else timeout / 1000, **options)

    async def worker():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            settled = asyncio.Event()
            asyncio.get_running_loop().call_later(0.02, settled.set)
            await settled.wait()
            return "stopped"

    task = asyncio.create_task(worker())
    service._tasks["normal-shutdown-worker"] = task
    monkeypatch.setattr(asyncio, "wait", scaled_wait)
    await service.close()
    assert timeouts == [None, 90, 30]
    assert task.done() and not task.cancelled()
    assert task.result() == "stopped"
    lease.execute.assert_awaited_once()
    service.store.pool.release.assert_awaited_once()


async def test_heartbeat_timeout_closes_admission_and_close_is_bounded(coordinator, monkeypatch):
    """F2: 応答しない heartbeat と DB cleanup でも取消通知と停止期限を守る。"""
    service, lease, exit_called = coordinator
    cancel = asyncio.Event()
    service._cancels["unconfirmed-worker"] = cancel

    async def stuck(*args, **kwargs):
        await asyncio.Event().wait()

    service.store.pool.fetchrow.side_effect = stuck
    lease.execute.side_effect = stuck
    service.store.pool.release.side_effect = stuck
    timing = CloseTiming(0.025, 0.01, 0.05)
    monkeypatch.setattr("kotoyose.coordinator_lease.LEASE_LOST_CLOSE_TIMING", timing)
    lease.stalled = True
    service._test_heartbeat_tick.set()
    await wait_for_loss(service)
    assert cancel.is_set()
    # 喪失経路の並行呼出しも同じ cleanup に収束し、各呼出元の期限を守る。
    await asyncio.wait_for(service.close(timing), timeout=0.3)
    await asyncio.wait_for(exit_called.wait(), timeout=0.3)
    service.store.confirm_stop.assert_not_awaited()
    # exit helper を差し替えたこの試験だけ、期限後の pending cleanup を回収する。
    lease.execute.side_effect = None
    service.store.pool.release.side_effect = None
    service._close_task.cancel()
    await asyncio.gather(service._close_task, return_exceptions=True)


async def test_released_lock_is_lost_even_with_a_live_connection(coordinator):
    """F3: SELECT 1 が成功しても所有を失った session は受付を閉じる。"""
    service, lease, exit_called = coordinator
    lease.held = False
    assert await lease.fetchval("SELECT 1") == 1
    service._test_heartbeat_tick.set()
    async with asyncio.timeout(0.5):
        while service._accepting:
            await asyncio.sleep(0.001)
    assert not service._accepting
    await asyncio.wait_for(exit_called.wait(), timeout=0.5)
    assert sum("pg_try_advisory_lock" in sql for sql in lease.queries) == 1


def command_body(key="new-key"):
    return {"execution": {
        "schema_version": "1", "kind": "command", "idempotency_key": key,
        "caller_run_ref": "run-1", "workspace_ref": "workspace-1",
        "execution_grant_ref": "grant-1", "budgets_ref": "budget-1",
        "command_spec_ref": "command-1", "argv_ref": "argv-1",
        "cwd_ref": "cwd-1", "timeout_policy_ref": "timeout-1",
    }}


async def test_lost_dispatch_rejects_new_work_but_preserves_replay(coordinator):
    """F7: 未受理だけを安全な 503 とし、受理済み key は同じ Attempt を返す。"""
    service, lease, _ = coordinator
    lease.held = False
    # 修正前も接続障害で喪失状態へ入れ、拒否理由の欠落を直接確認する。
    lease.fetchval = AsyncMock(side_effect=ConnectionError("private connection detail"))
    service._test_heartbeat_tick.set()
    await wait_for_loss(service)
    path = "/v1/executions"

    def body(key):
        return command_body(key)

    rejected = await request(path, method="POST", body=body("new-key"))
    assert rejected.status_code == 503
    error = rejected.json()["error"]
    assert error["code"] == error["type"] == "provider_failure"
    assert error["reason_code"] == "coordinator_lease_lost"
    assert error["safe_to_retry"] is True and error["retryable"] is True
    service.store.admit.assert_not_awaited()
    service.worker.execute.assert_not_awaited()
    assert not service._tasks
    service.store.replay.return_value = {"id": "already-admitted"}
    service.describe = AsyncMock(return_value={"resume_manifest": {}, "resume_manifest_digest": "fixture",
                                               "resume_mode": "native"})
    replay = await request(path, method="POST", body=body("old-key"))
    assert replay.status_code == 202
    assert replay.json()["attempt_id"] == "already-admitted"


async def test_loss_during_resolution_preserves_a_concurrently_admitted_key(coordinator):
    """F7: 解決の待機中に別要求が同じ key を受理した場合も安全な replay を返す。"""
    service, _, _ = coordinator

    async def resolve(*args):
        service._coordinator.state = "lost"
        service._close_admission()
        return {}

    service.admission.resolve = resolve
    service._prepare_remote = AsyncMock()
    service.store.replay.side_effect = [None, {"id": "concurrently-admitted"}]
    response = await request("/v1/executions", method="POST", body=command_body())
    assert response.status_code == 202
    assert response.json()["attempt_id"] == "concurrently-admitted"
    service.store.admit.assert_not_awaited()
    service.worker.execute.assert_not_awaited()


async def test_lost_dispatch_with_unavailable_replay_does_not_claim_safe_retry(coordinator):
    """F7: DB 不通で受理済みか照合できなければ、未受理と断定しない。"""
    service, _, _ = coordinator
    service._coordinator.state = "lost"
    service._close_admission()
    service.store.replay.side_effect = ConnectionError("private connection detail")
    response = await request("/v1/executions", method="POST", body=command_body())
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["reason_code"] == "coordinator_lease_lost"
    assert error["safe_to_retry"] is False
    service.store.admit.assert_not_awaited()
    service.worker.execute.assert_not_awaited()


@pytest.mark.parametrize("state, reason", [
    ("disabled", "native_execution_disabled"),
    ("starting", "coordinator_starting"),
    ("unverified", "coordinator_lease_unverified"),
    ("closed", "coordinator_admission_closed"),
    ("lost", "coordinator_lease_lost"),
    ("stopping", "coordinator_stopping"),
    ("stale", "coordinator_heartbeat_stale"),
    ("dead_watchdog", "coordinator_watchdog_dead"),
])
async def test_ready_reports_each_non_ready_state(coordinator, monkeypatch, state, reason):
    """F8/F9: 受付 flag が true でも古い heartbeat や死んだ監視を healthy にしない。"""
    service, _, _ = coordinator
    lease = getattr(service, "_coordinator", SimpleNamespace())
    if state == "disabled":
        monkeypatch.setattr(app.state, "execution_service", None)
    elif state in {"starting", "lost", "stopping"}:
        monkeypatch.setattr(lease, "state", state, raising=False)
    elif state == "unverified":
        monkeypatch.setattr(lease, "last_success", None, raising=False)
    elif state == "closed":
        service._accepting = False
    elif state == "stale":
        monkeypatch.setattr(lease, "last_success", time.monotonic() - 60, raising=False)
    else:
        watchdog = getattr(lease, "watchdog", service._watchdog)
        watchdog.cancel()
        with suppress(asyncio.CancelledError):
            await watchdog
    response = await request("/ready")
    assert response.status_code == 503
    assert response.json() == {"execution_ready": False, "reason": reason}
    assert response.headers["cache-control"] == "no-store"
    assert (await request("/health")).status_code == 200


@pytest.mark.parametrize("failure", ["unlock_error", "unresponsive_cleanup", "normal_shutdown_in_progress"])
def test_uvicorn_exits_nonzero_after_loss_within_deadline(tmp_path, failure):
    """F4: 実 uvicorn + main:app が cleanup の失敗時も非ゼロ終了する。"""
    script = r'''
import asyncio
import os
from pathlib import Path
import runpy
import uvicorn
from unittest.mock import AsyncMock
import kotoyose.app as app_module
import kotoyose.config as config_module
import kotoyose.coordinator_lease as lease_module
from kotoyose.config import HarnessConfig
from kotoyose.execution_shutdown import CloseTiming

helpers = runpy.run_path("tests/test_coordinator_lease.py")
config_module._config = HarnessConfig(models={})
os.environ["KOTOYOSE_LEASE_TEST_TOKEN"] = "fixture-only"
service, lease = helpers["make_service"](Path(os.environ["LEASE_TEST_ROOT"]))
lease_module.LEASE_LOST_CLOSE_TIMING = CloseTiming(0.04, 0.02, 0.08)
cleanup_entered = asyncio.Event()

async def stuck(*args, **kwargs):
    if args and "SELECT principal" in args[0]:
        print('{"event": "fixture.cleanup_entered"}', flush=True)
        cleanup_entered.set()
    # cancellation に抵抗する依存でも、親プロセスの終了を妨げないことを確認する。
    while True:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass

async def start():
    await service.start()
    if os.environ["LEASE_TEST_FAILURE"] == "unlock_error":
        lease.execute = AsyncMock(side_effect=RuntimeError("postgresql://DO_NOT_LOG"))
        service.store.pool.release = AsyncMock(side_effect=RuntimeError("postgresql://DO_NOT_LOG"))
    else:
        service._cancels["unknown-worker"] = asyncio.Event()
        service.store.pool.fetchrow = stuck
        lease.execute = stuck
        service.store.pool.release = stuck
        if os.environ["LEASE_TEST_FAILURE"] == "normal_shutdown_in_progress":
            asyncio.create_task(service.close())
            await cleanup_entered.wait()
    lease.fetchval = AsyncMock(side_effect=ConnectionError("postgresql://DO_NOT_LOG"))
    return service

# 本番と同じ uvicorn 起動経路で、外部 DB と会話の起動のみを隔離する。
app_module.init_db = AsyncMock()
app_module.close_db = AsyncMock()
app_module.create_conversations = lambda: None
app_module.start_execution_service = start
sleep = asyncio.sleep
async def fast_sleep(delay):
    await sleep(min(delay, 0.01))
asyncio.sleep = fast_sleep
uvicorn.run("main:app", host="127.0.0.1", port=0, log_level="error")
'''
    env = {**os.environ, "PYTHON_DOTENV_DISABLED": "1", "PYTHONPATH": "src",
           "LEASE_TEST_ROOT": str(tmp_path), "LEASE_TEST_FAILURE": failure}
    process = subprocess.Popen([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        output, _ = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate()
        pytest.fail(f"uvicorn remained alive after lease loss: {output}")
    assert process.returncode == 75, output
    assert output.count('"event": "execution.lease_lost"') == 1
    assert output.count('"event": "execution.exiting_for_restart"') == 1
    assert output.count('"event": "execution.cleanup_timed_out"') == (failure != "unlock_error")
    assert output.count('"event": "fixture.cleanup_entered"') == (failure != "unlock_error")
    assert '"error_type": "ConnectionError"' in output
    assert '"last_heartbeat_age_seconds":' in output
    assert "DO_NOT_LOG" not in output
    events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    lost = next(event for event in events if event["event"] == "execution.lease_lost")
    exiting = next(event for event in events if event["event"] == "execution.exiting_for_restart")
    elapsed = (datetime.fromisoformat(exiting["ts"]) - datetime.fromisoformat(lost["ts"])).total_seconds()
    assert elapsed < 0.4, output
    print(f"{failure}: uvicorn exit={process.returncode}; loss-to-exit={elapsed:.3f}s; structured events verified")
