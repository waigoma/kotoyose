"""Codex coding を container 経路で実行する境界を、fake container と実 pipe の疑似 app-server で検証する。

課金 provider・実 Docker は使わない。KOTOYOSE_DOCKER_IT=1 の統合テストは test_agent_container.py 側にある。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kotoyose import container_codex, native_codex
from kotoyose.codex_subscription import CAPABILITY_ENV
from kotoyose.container_codex import _container_job_id
from kotoyose.agent_container import JOB_ID_RE
from kotoyose.execution_resources import AgentContainerConfig
from kotoyose.native_codex import NativeCodexWorker
from tests.test_codex_host_coding import job

PROXY_ENV = {"CODEX_HOME": "/home/agent/.codex", "HTTP_PROXY": "http://127.0.0.1:18080",
             "HTTPS_PROXY": "http://127.0.0.1:18080", "NO_PROXY": ""}
IMAGE_ID = "sha256:" + "b" * 64
IMAGE_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
             "LC_ALL": "C.UTF-8", "COREPACK_HOME": "/opt/corepack"}
LIMITS = {"version": 1, "total_bytes": 2000000, "total_connections": "disabled",
          "request_bytes": 16777216, "response_bytes": 33554432}

# 疑似 app-server。thread/start の params を mode 指定のファイルへ書き、turn を正常終了させる。
_FAKE_APP_SERVER = '''
import json, sys, time
mode, record = sys.argv[1], sys.argv[2]
THREAD = "00000000-0000-4000-8000-000000000001"
def emit(value):
    sys.stdout.write(json.dumps(value) + "\\n")
    sys.stdout.flush()
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        emit({"id": message["id"], "result": {"userAgent": "fake"}})
    elif method == "thread/start":
        with open(record, "w") as handle:
            json.dump(message["params"], handle)
        emit({"id": message["id"], "result": {
            "thread": {"id": THREAD, "path": "/home/agent/.codex/sessions/" + THREAD + ".jsonl"},
            "model": message["params"]["model"], "modelProvider": message["params"]["modelProvider"],
            "cwd": "/workspace", "approvalPolicy": "never", "sandbox": {"type": "readOnly", "networkAccess": False}}})
    elif method == "thread/settings/update":
        emit({"id": message["id"], "result": {}})
        emit({"method": "thread/settings/updated", "params": {"threadId": THREAD, "threadSettings": {
            "cwd": "/workspace", "approvalPolicy": "never",
            "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "restricted"}}}})
    elif method == "turn/start":
        emit({"id": message["id"], "result": {"turn": {"id": "turn"}}})
        if mode == "hang":
            time.sleep(600)
        emit({"method": "item/completed", "params": {"threadId": THREAD, "turnId": "turn", "item": {
            "type": "agentMessage", "id": "answer", "text": "done", "phase": "final_answer"}}})
        emit({"method": "turn/completed", "params": {"threadId": THREAD, "turnId": "turn",
                                                       "turn": {"id": "turn", "status": "completed"}}})
'''


class FakeContainer:
    """AgentContainer の呼び出しを記録する。exec は実 pipe の疑似 app-server を起動する。"""

    def __init__(self, script: Path, mode: str, record: Path) -> None:
        self.script = script
        self.mode = mode
        self.record = record
        self.created: list = []
        self.execs: list[dict] = []
        self.removed: list[tuple[str, int]] = []
        self.procs: list = []
        self.image_calls: list[str] = []
        self.remove_result = "confirmed"
        self.instance = "kotoyose"

    async def image_env(self, image_id: str) -> dict[str, str]:
        self.image_calls.append(image_id)
        return dict(IMAGE_ENV)

    async def create(self, spec) -> str:
        self.created.append(spec)
        return "c" * 64

    async def exec(self, job_id, argv, *, env=None, secret_env=None, stdin=True, workdir=None, limit=None):
        self.execs.append({"job_id": job_id, "argv": list(argv), "env": dict(env or {}),
                           "secret_env": dict(secret_env or {}), "limit": limit})
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(self.script), self.mode, str(self.record),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", ""), **(secret_env or {})}, limit=limit or 65536,
        )
        self.procs.append(process)
        return process

    async def remove(self, job_id: str, grace_seconds: int) -> str:
        self.removed.append((job_id, grace_seconds))
        # container の削除は中の process を全て止める。疑似 process も同じく止める。
        for process in self.procs:
            if process.returncode is None:
                process.kill()
        return self.remove_result


@pytest.fixture
def env(tmp_path, monkeypatch):
    """worker・container・broker・設定を揃えた実行環境。broker は egress だけ fake にする。"""
    script = tmp_path / "fake-app-server.py"
    script.write_text(_FAKE_APP_SERVER)
    record = tmp_path / "thread-start.json"
    agent_root = tmp_path / "agent"
    (agent_root / "bin").mkdir(parents=True)
    (agent_root / "bin" / "codex").write_text("reviewed codex placeholder\n")
    (agent_root / "egress_relay.py").write_text("placeholder\n")
    homes = tmp_path / "homes"
    homes.mkdir()
    cli = tmp_path / "codex-cli"
    cli.write_text("codex binary fixture\n")
    workspace = tmp_path / "workspace"
    (workspace / ".codex").mkdir(parents=True)
    (workspace / ".agents").mkdir()
    egress_dir = tmp_path / "egress"
    egress_dir.mkdir()
    state = SimpleNamespace(broker_start=None, broker_close="confirmed", closed=[], egress_dir=egress_dir)

    class Broker:
        def __init__(self, socket_path, **kwargs):
            assert socket_path == egress_dir / "egress.sock"
            state.broker_options = kwargs

        async def start(self):
            if state.broker_start is not None:
                await state.broker_start(self)

        async def close(self):
            state.closed.append(True)
            if state.broker_close != "confirmed":
                raise OSError("close failed")

    def unexpected_broker(*args, **kwargs):
        pytest.fail("subscription jail must not start an HTTP-terminating broker")

    monkeypatch.setattr(native_codex, "CodexEgress", unexpected_broker)
    monkeypatch.setattr(container_codex, "PrintCliEgress", Broker)
    monkeypatch.setattr(container_codex, "create_egress_socket_dir", lambda root: egress_dir)
    codex_home = tmp_path / "connection-codex-home"
    codex_home.mkdir()
    container = FakeContainer(script, "ok", record)
    settings = AgentContainerConfig(agent_root=str(agent_root), home_root=str(homes))
    return SimpleNamespace(container=container, settings=settings, tmp_path=tmp_path, cli=cli,
                           workspace=workspace, homes=homes, record=record, state=state, egress_dir=egress_dir)


def _worker(env, *, sessions: Path | None = None, settings="default") -> NativeCodexWorker:
    runtime = (env.container, env.settings if settings == "default" else settings, IMAGE_ID)
    return NativeCodexWorker(Path("/fixture"), session_root=sessions, container_runtime=runtime)


def _job(env, **overrides):
    values = dict(cli_binary=env.cli, grace_seconds=.05, timeout_seconds=5,
                  codex_auth_file=env.tmp_path / "connection-codex-home" / "auth.json")
    values.update(overrides)
    return job(env.tmp_path, **values)


async def _run(worker, job_value, cancel=None, on_started=None):
    return await worker.execute(job_value, cancel or asyncio.Event(), AsyncMock(),
                                on_process_started=on_started or AsyncMock())


def test_container_job_id_is_stable_and_safe():
    assert JOB_ID_RE.fullmatch(_container_job_id(SimpleNamespace(attempt_id="attempt-1", generation=7)))
    weird = _container_job_id(SimpleNamespace(attempt_id="a/b c", generation=1))
    assert weird.startswith("job-") and JOB_ID_RE.fullmatch(weird)


async def test_create_spec_binds_only_the_reviewed_mounts(env, tmp_path):
    sessions = tmp_path / "sessions"
    scope = "a" * 64
    result = await _run(_worker(env, sessions=sessions), _job(env, session_scope=scope))
    assert result.status == "succeeded"
    spec = env.container.created[0]
    assert spec.job_id == "attempt-1-g7" and spec.image_id == IMAGE_ID
    assert spec.workspace == env.workspace and spec.home.parent == env.homes
    assert spec.egress_dir == env.egress_dir
    assert spec.agent_root is None
    assert set(spec.ro_files) == {"/opt/agent/bin/codex", "/opt/agent/egress_relay.py"}
    assert spec.ro_files["/opt/agent/bin/codex"] == env.cli.resolve()
    assert Path(spec.rw_dirs["/home/agent/.codex/sessions"]).resolve() == (sessions / scope).resolve()
    assert spec.rw_dirs["/home/agent/.codex"] == tmp_path / "connection-codex-home"
    assert set(spec.rw_dirs) == {"/home/agent/.codex", "/home/agent/.codex/sessions"}
    assert spec.workspace_readonly is False
    assert spec.ro_dirs == {"/workspace/.codex": env.workspace / ".codex",
                            "/workspace/.agents": env.workspace / ".agents"}
    assert spec.env == PROXY_ENV
    assert spec.wall_seconds == 66
    assert spec.memory_bytes == env.settings.workspace_memory_bytes


async def test_read_room_mounts_workspace_readonly_without_prepared_entries(env):
    """閲覧 clone に .codex/.agents が無くても部屋は立つ。/workspace は ro、home は rw の別ディレクトリ。"""
    from kotoyose.codex_coding_policy import READ_MODE, READ_SANDBOX_POLICY, READ_TOOL_POLICY_DIGEST, READ_TOOL_REF

    for name in (".codex", ".agents"):
        (env.workspace / name).rmdir()
    result = await _run(_worker(env), _job(
        env, tool_refs=(READ_TOOL_REF,), codex_execution_mode=READ_MODE,
        codex_tool_policy_digest=READ_TOOL_POLICY_DIGEST, codex_sandbox_policy=READ_SANDBOX_POLICY,
        execution_environment="isolated", codex_sandbox="read-only",
    ))
    assert result.status == "succeeded"
    spec = env.container.created[0]
    assert spec.workspace_readonly is True
    assert spec.workspace == env.workspace
    assert spec.home.parent == env.homes and env.workspace not in spec.home.parents
    assert spec.ro_dirs == {}
    params = json.loads(env.record.read_text())
    assert params["sandbox"] == "read-only"
    assert params["cwd"] == "/workspace"


async def test_exec_starts_connect_relay_without_credentials_in_env_argv_or_spec(env):
    result = await _run(_worker(env), _job(env))
    assert result.status == "succeeded"
    exec_call = env.container.execs[0]
    argv = exec_call["argv"]
    assert argv[:4] == ["python3", "/opt/agent/egress_relay.py", "/run/egress/egress.sock", "--"]
    assert argv[4] == "/opt/agent/bin/codex" and argv[5:7] == ["app-server", "--listen"]
    assert "--config" in argv and 'model_catalog_json="/run/egress/models.json"' in argv
    assert 'model_provider="openai"' in argv
    assert 'cli_auth_credentials_store="file"' in argv
    assert 'sandbox_mode="read-only"' in argv
    assert f'model="{_job(env).model}"' in argv
    assert all(CAPABILITY_ENV not in arg and "auth.json" not in arg for arg in argv)
    assert exec_call["secret_env"] == {}
    assert exec_call["env"] == PROXY_ENV
    assert env.container.created[0].env == PROXY_ENV
    params = json.loads(env.record.read_text())
    assert params["modelProvider"] == "openai"
    assert "model_providers.kotoyose_native" not in params["config"]
    assert env.state.broker_options["allowed_hosts"] == ("chatgpt.com", "auth.openai.com")
    assert env.state.broker_options["allowed_ports"] == (443,)


async def test_config_clears_inherited_env_and_uses_only_the_image_env(env):
    result = await _run(_worker(env), _job(env))
    assert result.status == "succeeded"
    params = json.loads(env.record.read_text())
    config = params["config"]
    assert config["shell_environment_policy.inherit"] == "none"
    assert config["shell_environment_policy.set"] == {**IMAGE_ENV, "HOME": "/home/agent", "LANG": "C.UTF-8"}
    assert config["sandbox_workspace_write.network_access"] is False
    assert params["cwd"] == "/workspace"
    assert env.container.image_calls == [IMAGE_ID]
    assert CAPABILITY_ENV not in json.dumps(params)


async def test_success_is_confirmed_only_after_the_container_is_removed(env):
    result = await _run(_worker(env), _job(env))
    assert (result.status, result.reason, result.stop_confirmation) == ("succeeded", "completed", "confirmed")
    assert result.content == b"done"
    assert env.container.removed == [("attempt-1-g7", 0)]
    assert result.launch_outcome == "started"


@pytest.mark.parametrize("failure", ["remove", "egress"])
async def test_unconfirmed_stop_turns_success_into_worker_lost(env, failure):
    if failure == "remove":
        env.container.remove_result = "unconfirmed"
    else:
        env.state.broker_close = "unconfirmed"
    result = await _run(_worker(env), _job(env))
    assert (result.status, result.reason, result.stop_confirmation) == ("worker_lost", "worker_lost", "unconfirmed")


async def test_cancel_removes_the_container_and_reports_cancelled(env):
    env.container.mode = "hang"
    cancel = asyncio.Event()

    async def started(pid, **_identity):
        asyncio.get_running_loop().call_later(.2, cancel.set)

    result = await asyncio.wait_for(_run(_worker(env), _job(env), cancel=cancel, on_started=started), 5)
    assert (result.status, result.reason, result.stop_confirmation) == ("cancelled", "cancelled", "confirmed")
    assert env.container.removed == [("attempt-1-g7", 0)]


async def test_deadline_removes_the_container_and_reports_timeout(env):
    env.container.mode = "hang"
    result = await asyncio.wait_for(_run(_worker(env), _job(env, timeout_seconds=.3)), 5)
    assert (result.status, result.reason, result.stop_confirmation) == ("timeout", "timeout", "confirmed")
    assert env.container.removed == [("attempt-1-g7", 0)]


async def test_cancel_during_broker_start_never_creates_a_container(env):
    entered = asyncio.Event()

    async def blocked(broker):
        entered.set()
        await asyncio.Future()

    env.state.broker_start = blocked
    cancel = asyncio.Event()
    task = asyncio.create_task(_run(_worker(env), _job(env), cancel=cancel))
    await entered.wait()
    cancel.set()
    result = await asyncio.wait_for(task, 2)
    assert (result.status, result.stop_confirmation, result.launch_outcome) == ("cancelled", "confirmed", "not_started")
    assert env.container.created == [] and env.container.removed == [] and env.container.execs == []


async def test_api_key_is_still_rejected_by_subscription_only_room(env):
    result = await _run(_worker(env), _job(
        env, credential_name="OPENAI_API_KEY", credential_value="fake-api-key",
        codex_auth_file=None, codex_auth_identity=None,
    ))
    assert (result.status, result.reason) == ("failed", "invalid_job")
    assert env.container.created == [] and env.container.execs == []
    assert not hasattr(env.state, "broker_options")


async def test_everyday_codex_home_is_never_bound_into_the_jail(env, monkeypatch):
    home = env.tmp_path / "user-home"
    (home / ".codex").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    result = await _run(_worker(env), _job(env, codex_auth_file=home / ".codex" / "auth.json"))
    assert result.status == "failed"
    assert env.container.created == [] and env.container.execs == []


async def test_missing_agent_container_config_fails_closed_before_docker(env):
    result = await _run(_worker(env, settings=None), _job(env))
    assert (result.status, result.reason) == ("failed", "invalid_job")
    assert env.container.image_calls == [] and env.container.created == [] and env.container.execs == []


async def test_missing_container_runtime_fails_closed(tmp_path, monkeypatch):
    launch = AsyncMock(side_effect=AssertionError("must not launch"))
    monkeypatch.setattr(native_codex.asyncio, "create_subprocess_exec", launch)
    result = await NativeCodexWorker().execute(job(tmp_path), asyncio.Event(), AsyncMock(), on_process_started=AsyncMock())
    assert (result.status, result.reason) == ("failed", "invalid_job")
    launch.assert_not_awaited()


async def test_missing_protected_entry_is_not_mounted_and_does_not_fail(env):
    (env.workspace / ".agents").rmdir()
    result = await _run(_worker(env), _job(env, codex_protected_workspace=None))
    assert (result.status, result.reason) == ("succeeded", "completed")
    assert set(env.container.created[0].ro_dirs) == {"/workspace/.codex"}


async def test_temp_home_is_removed_and_session_directory_is_kept(env, tmp_path):
    sessions = tmp_path / "sessions"
    scope = "c" * 64
    result = await _run(_worker(env, sessions=sessions), _job(env, session_scope=scope))
    assert result.status == "succeeded"
    assert not any(env.homes.iterdir())
    assert (sessions.resolve() / scope).is_dir()
    assert (tmp_path / "connection-codex-home").is_dir()
    assert not env.egress_dir.exists()


@pytest.mark.parametrize("limits", [None, LIMITS, {**LIMITS, "total_bytes": "disabled"}])
async def test_pinned_egress_totals_reach_connect_broker(env, limits):
    result = await _run(_worker(env), _job(env, codex_egress_limits=limits))
    assert result.status == "succeeded"
    options = env.state.broker_options
    expected_bytes = (1048576 if limits is None else limits["total_bytes"])
    assert options["max_total_bytes"] == (None if expected_bytes == "disabled" else expected_bytes)
    assert options["max_total_tunnels"] == (32 if limits is None else None)
    assert options["max_tunnels"] == 16
    assert 0 < options["wall_timeout_seconds"] <= 5
    assert result.codex_egress_metrics is None


async def test_process_start_reports_container_identity(env):
    """host coding の所有の証拠は scope ではなく部屋の名前と ID。execution_agent はこれを要求する。"""
    started = AsyncMock()
    result = await _run(_worker(env), _job(env), on_started=started)
    assert result.status == "succeeded"
    kwargs = started.await_args.kwargs
    assert "scope_identity" not in kwargs
    identity = kwargs["container_identity"]
    assert identity == {"name": "kotoyose-agent-attempt-1-g7", "id": "c" * 64, "instance": "kotoyose"}
