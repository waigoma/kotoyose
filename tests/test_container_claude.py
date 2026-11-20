"""Claude coding を container 経路で実行する境界を、fake container / fake SDK / fake broker で検証する。

課金 provider・実 Docker は使わない。KOTOYOSE_DOCKER_IT=1 の統合テストだけが実 Docker を使う。
wrapper → launcher → docker の連鎖は、argv を記録するだけの fake docker で実プロセスとして確かめる。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolResultBlock
from claude_agent_sdk import __version__ as _SDK_VERSION

from kotoyose import agent_container_launcher as launcher
from kotoyose import container_claude as cc
from kotoyose.codex_coding_policy import HOST_MODE, HOST_TOOL_REF
from kotoyose.execution_resources import AgentContainerConfig
from kotoyose.native_claude import NativeAgentJob, NativeClaudeWorker
from kotoyose.native_sessions import ScopedSessionStore
from kotoyose.provider_egress import EgressRoute

IMAGE_ID = "sha256:" + "b" * 64
CAPABILITY_ENV = "ANTHROPIC_API_KEY"
API_KEY = "dummy-api-key-value-0123456789"


class _Process:
    def __init__(self) -> None:
        self.pid = 4321
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


class FakeSdkClient:
    """SDK client の代わり。CLI の起動設定を記録し、用意した message 列を返す。"""

    instances: list["FakeSdkClient"] = []
    messages: list = []
    hang = False
    wrapper_text = ""

    def __init__(self, *, options) -> None:
        self.options = options
        self._transport = SimpleNamespace(_process=_Process())
        self.queried: list = []
        self.disconnected = False
        self.cwd_existed = False
        FakeSdkClient.instances.append(self)

    async def connect(self) -> None:
        FakeSdkClient.wrapper_text = Path(self.options.cli_path).read_text()
        self.cwd_existed = Path(self.options.cwd).is_dir()

    async def query(self, prompt) -> None:
        self.queried.append(prompt)

    async def receive_response(self):
        if FakeSdkClient.hang:
            await asyncio.Future()
        for message in FakeSdkClient.messages:
            yield message

    async def disconnect(self) -> None:
        self.disconnected = True
        self._transport._process.returncode = 0


class FakeContainer:
    """AgentContainer の呼び出しを記録する。remove の結果は試験ごとに変える。"""

    def __init__(self) -> None:
        self.created: list = []
        self.removed: list[tuple[str, int]] = []
        self.remove_result = "confirmed"
        self.instance = "kotoyose"
        self.docker_binary = "docker"

    async def create(self, spec) -> str:
        self.created.append(spec)
        return "c" * 64

    async def remove(self, job_id: str, grace_seconds: int) -> str:
        self.removed.append((job_id, grace_seconds))
        return self.remove_result


class FakeEgress:
    """broker の代わり。spec と socket の位置を記録し、close の失敗を再現できる。"""

    instances: list["FakeEgress"] = []
    close_fails = False

    def __init__(self, spec, socket_path, connect=None) -> None:
        self.spec = spec
        self.socket_path = Path(socket_path)
        self.closed = False
        FakeEgress.instances.append(self)

    async def start(self) -> Path:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        return self.socket_path

    async def close(self) -> None:
        if FakeEgress.close_fails:
            raise OSError("close failed")
        self.closed = True


class FakeConnectEgress:
    """CONNECT pass-through broker の代わり。許可先と close の失敗を記録・再現する。"""

    instances: list["FakeConnectEgress"] = []
    close_fails = False

    def __init__(
        self,
        socket_path,
        *,
        allowed_hosts,
        allowed_ports=(443,),
        run_id,
        idle_timeout_seconds=300.0,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.allowed_hosts = frozenset(allowed_hosts)
        self.allowed_ports = frozenset(allowed_ports)
        self.run_id = run_id
        self.idle_timeout_seconds = idle_timeout_seconds
        self.closed = False
        FakeConnectEgress.instances.append(self)

    async def start(self) -> Path:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        return self.socket_path

    async def close(self) -> None:
        if FakeConnectEgress.close_fails:
            raise OSError("close failed")
        self.closed = True


def _result_message(*, error: bool = False, subtype: str = "success") -> ResultMessage:
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=error, num_turns=1,
                         session_id="session", total_cost_usd=0.25, usage={"input_tokens": 3, "output_tokens": 4})


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeSdkClient.instances.clear()
    FakeSdkClient.messages = [AssistantMessage(content=[TextBlock(text="done")], model="claude-test"),
                              _result_message()]
    FakeSdkClient.hang = False
    FakeSdkClient.wrapper_text = ""
    FakeEgress.instances.clear()
    FakeEgress.close_fails = False
    FakeConnectEgress.instances.clear()
    FakeConnectEgress.close_fails = False
    yield


@pytest.fixture
def env(tmp_path, monkeypatch):
    cli = tmp_path / "claude-cli"
    cli.write_text("claude binary fixture\n")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    homes = tmp_path / "homes"
    homes.mkdir()
    sockets = tmp_path / "sockets"
    sockets.mkdir()
    monkeypatch.setattr(cc, "ProviderEgress", FakeEgress)
    monkeypatch.setattr(cc, "PrintCliEgress", FakeConnectEgress)
    monkeypatch.setattr(cc, "_socket_dir", lambda root: Path(tempfile.mkdtemp(prefix="eg-", dir=sockets)))
    monkeypatch.setattr("kotoyose.container_claude.ClaudeSDKClient", FakeSdkClient)
    container = FakeContainer()
    settings = AgentContainerConfig(agent_root="/opt/agent-root", home_root=str(homes))
    return SimpleNamespace(cli=cli, workspace=workspace, homes=homes, sockets=sockets,
                           container=container, settings=settings, tmp_path=tmp_path)


def _job(env, **overrides) -> NativeAgentJob:
    stat = env.workspace.stat()
    values = dict(
        attempt_id="attempt-1", model="claude-test", cli_binary=env.cli, credential_name="ANTHROPIC_API_KEY",
        credential_value=API_KEY, instructions="Fix the bug.", prompt="fix it", output_contract="text",
        max_turns=4, timeout_seconds=5.0, grace_seconds=0.05, output_limit_bytes=65536,
        tool_refs=(HOST_TOOL_REF,), codex_execution_mode=HOST_MODE, workspace=env.workspace,
        workspace_device=stat.st_dev, workspace_inode=stat.st_ino, generation=7,
    )
    values.update(overrides)
    return NativeAgentJob(**values)


def _worker(env, *, settings="default", session_store=None) -> NativeClaudeWorker:
    runtime = (env.container, env.settings if settings == "default" else settings, IMAGE_ID)
    return NativeClaudeWorker(Path("/fixture"), session_store=session_store, container_runtime=runtime)


async def _run(worker, job, cancel=None, on_started=None):
    return await worker.execute(job, cancel or asyncio.Event(), AsyncMock(),
                                on_process_started=on_started or AsyncMock())


def _options():
    return FakeSdkClient.instances[0].options


# --- SDK options: built-in tools, bypass, host cwd and in-container workdir -----------------------

async def test_options_enable_builtin_tools_and_deny_web_tools(env):
    result = await _run(_worker(env), _job(env))
    assert result.status == "succeeded"
    options = _options()
    assert options.tools is None
    assert options.allowed_tools == []
    assert set(options.disallowed_tools) == {"WebFetch", "WebSearch"}
    assert options.permission_mode == "bypassPermissions"
    assert options.mcp_servers == {} and options.strict_mcp_config is True
    assert options.setting_sources == [] and options.skills == [] and options.plugins == []
    assert options.hooks is None and options.agents is None and options.settings is None
    assert options.max_turns == 4 and options.model == "claude-test"


async def test_host_cwd_is_an_existing_dir_and_the_container_workdir_is_workspace(env):
    """SDK の cwd は host の path で存在が必要なため /workspace にできない。container 内の cwd は launcher が決める。"""
    result = await _run(_worker(env), _job(env))
    assert result.status == "succeeded"
    options = _options()
    assert FakeSdkClient.instances[0].cwd_existed and Path(options.cwd) != env.workspace
    assert options.env["KOTOYOSE_AGENT_WORKDIR"] == "/workspace"


async def test_cli_path_is_a_wrapper_that_execs_the_launcher(env):
    await _run(_worker(env), _job(env))
    assert FakeSdkClient.wrapper_text.startswith("#!/bin/sh\nexec ")
    assert str(Path(cc._LAUNCHER_PATH)) in FakeSdkClient.wrapper_text
    assert '"$@"' in FakeSdkClient.wrapper_text


# --- container spec mounts ------------------------------------------------------------------------

async def test_container_spec_binds_only_the_reviewed_mounts(env):
    result = await _run(_worker(env), _job(env))
    assert result.status == "succeeded"
    spec = env.container.created[0]
    assert spec.job_id == "attempt-1-g7" and spec.image_id == IMAGE_ID
    assert spec.workspace == env.workspace
    assert spec.home.parent == env.homes
    assert spec.agent_root is None
    assert set(spec.ro_files) == {"/opt/agent/bin/claude", "/opt/agent/egress_relay.py"}
    assert spec.ro_files["/opt/agent/bin/claude"] == env.cli.resolve()
    assert spec.ro_files["/opt/agent/egress_relay.py"] == cc._RELAY_PATH
    assert spec.rw_dirs == {} and spec.ro_dirs == {}
    assert spec.env == {
        "CLAUDE_CONFIG_DIR": "/home/agent/.claude",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:18080",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
    }
    assert spec.wall_seconds == 66
    assert spec.memory_bytes == env.settings.workspace_memory_bytes


async def test_socket_dir_is_the_only_egress_mount_and_holds_the_socket(env):
    await _run(_worker(env), _job(env))
    spec = env.container.created[0]
    assert spec.egress_dir == FakeEgress.instances[0].socket_path.parent
    assert FakeEgress.instances[0].socket_path.name == "egress.sock"


# --- capability and credential boundary -----------------------------------------------------------

async def test_capability_reaches_only_the_launcher_env_and_never_the_container_spec(env):
    result = await _run(_worker(env), _job(env))
    assert result.status == "succeeded"
    options = _options()
    capability = options.env[CAPABILITY_ENV]
    assert capability and capability != API_KEY and len(capability) >= 32
    assert options.env["KOTOYOSE_AGENT_SECRET_ENV"] == CAPABILITY_ENV
    # 実 credential は launcher にも container にも渡さない。
    assert API_KEY not in json.dumps(options.env, default=str)
    spec = env.container.created[0]
    assert capability not in json.dumps(spec.env) and capability not in repr(spec)
    assert capability not in options.env["KOTOYOSE_AGENT_ENV"]
    assert capability not in FakeSdkClient.wrapper_text
    assert capability not in " ".join(str(v) for v in spec.ro_files.values())


async def test_launcher_env_builds_the_relay_argv_without_leaking_the_capability(env):
    await _run(_worker(env), _job(env))
    options = _options()
    argv = launcher.build_exec_argv(options.env, ["-p", "--output-format", "stream-json"])
    assert argv == [
        "docker", "exec", "-i",
        "--workdir", "/workspace",
        "-e", "ANTHROPIC_BASE_URL=http://127.0.0.1:18080",
        "-e", "CLAUDE_AGENT_SDK_VERSION=" + _SDK_VERSION,
        "-e", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
        "-e", "CLAUDE_CODE_ENTRYPOINT=sdk-py",
        "-e", "CLAUDE_CODE_SDK_READS_SESSION_STATE=1",
        "-e", "CLAUDE_CONFIG_DIR=/home/agent/.claude",
        "-e", "DISABLE_AUTOUPDATER=1",
        "-e", "DISABLE_TELEMETRY=1",
        "-e", "HOME=/home/agent",
        "-e", CAPABILITY_ENV,
        "kotoyose-agent-attempt-1-g7",
        "python3", "/opt/agent/egress_relay.py", "/run/egress/egress.sock", "--",
        "/opt/agent/bin/claude", "-p", "--output-format", "stream-json",
    ]
    assert options.env[CAPABILITY_ENV] not in "\0".join(argv)


async def test_wrapper_chain_hands_the_capability_to_docker_by_name_only(env, tmp_path):
    """wrapper → launcher → docker を実プロセスで通す。docker の代わりに argv と env を記録する fake を使う。"""
    await _run(_worker(env), _job(env))
    options = _options()
    # 実行後に launch dir は削除される。connect 時に読んだ wrapper の内容をそのまま再現する。
    wrapper = tmp_path / "claude-launch"
    wrapper.write_text(FakeSdkClient.wrapper_text)
    wrapper.chmod(0o700)
    record = tmp_path / "docker-record.txt"
    fake_docker = tmp_path / "fake-docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > '{record}'\n"
        f"printf 'ENV=%s\\n' \"$ANTHROPIC_API_KEY\" >> '{record}'\n"
    )
    fake_docker.chmod(0o700)
    child_env = {"PATH": os.environ.get("PATH", ""), **options.env, "KOTOYOSE_DOCKER_BINARY": str(fake_docker)}
    completed = subprocess.run([str(wrapper), "-p"], env=child_env, capture_output=True, timeout=30, check=False)
    assert completed.returncode == 0, completed.stderr
    lines = record.read_text().splitlines()
    capability = options.env[CAPABILITY_ENV]
    assert lines[-1] == f"ENV={capability}"
    assert "-e" in lines and CAPABILITY_ENV in lines
    assert capability not in lines[:-1]
    assert "/opt/agent/bin/claude" in lines and "python3" in lines


# --- broker spec: routes, upstream, injection -----------------------------------------------------

def test_default_upstream_is_the_public_anthropic_endpoint():
    upstream = cc._upstream(None)
    assert (upstream.host, upstream.port, upstream.tls, upstream.base_path, upstream.allow_private) == (
        "api.anthropic.com", 443, True, "", False)


@pytest.mark.parametrize("base_url, expected", [
    ("http://10.1.2.3:20128/anthropic", ("10.1.2.3", 20128, False, "/anthropic", True)),
    ("http://127.0.0.1:8080/", ("127.0.0.1", 8080, False, "", True)),
    ("https://10.0.0.5/gw/", ("10.0.0.5", 443, True, "/gw", True)),
    ("https://gateway.example.com/base/", ("gateway.example.com", 443, True, "/base", True)),
    ("https://gateway.example.com:8443", ("gateway.example.com", 8443, True, "", True)),
])
def test_base_url_maps_scheme_port_path_and_private_policy(base_url, expected):
    upstream = cc._upstream(base_url)
    assert (upstream.host, upstream.port, upstream.tls, upstream.base_path, upstream.allow_private) == expected


@pytest.mark.parametrize("base_url", [
    "ftp://gateway.example.com", "https://user:pw@gateway.example.com", "https://gw.example.com/?q=1",
    "https://gw.example.com/#frag", "https://gw example.com", "https://[::1]:443/",
])
def test_invalid_base_url_is_rejected(base_url):
    with pytest.raises(ValueError):
        cc._upstream(base_url)


def test_credential_name_selects_the_upstream_header():
    assert cc._injected_headers("ANTHROPIC_API_KEY", "k-1") == {"x-api-key": "k-1"}
    assert cc._injected_headers("ANTHROPIC_AUTH_TOKEN", "t-1") == {"authorization": "Bearer t-1"}
    assert cc._injected_headers("CLAUDE_CODE_OAUTH_TOKEN", "o-1") == {"authorization": "Bearer o-1"}


async def test_broker_spec_routes_only_messages_and_uses_the_capability_header(env):
    await _run(_worker(env), _job(env))
    spec = FakeEgress.instances[0].spec
    assert spec.routes == (EgressRoute("POST", "/v1/messages"), EgressRoute("POST", "/v1/messages/count_tokens"))
    assert spec.capability_header == "x-api-key"
    assert spec.capability == _options().env[CAPABILITY_ENV]
    assert dict(spec.inject_headers) == {"x-api-key": API_KEY}
    assert spec.pass_headers == frozenset({"accept", "anthropic-beta", "anthropic-version", "content-type", "user-agent"})
    assert spec.upstream.host == "api.anthropic.com" and spec.upstream.port == 443


async def test_base_url_job_points_the_broker_at_the_router(env):
    job = _job(env, base_url="http://10.9.8.7:20128/v1-router", credential_name="ANTHROPIC_AUTH_TOKEN")
    result = await _run(_worker(env), job)
    assert result.status == "succeeded"
    spec = FakeEgress.instances[0].spec
    assert (spec.upstream.host, spec.upstream.port, spec.upstream.tls) == ("10.9.8.7", 20128, False)
    assert spec.upstream.base_path == "/v1-router" and spec.upstream.allow_private is True
    assert dict(spec.inject_headers) == {"authorization": f"Bearer {API_KEY}"}
    assert _options().env["ANTHROPIC_AUTH_TOKEN"] != API_KEY and _options().env["ANTHROPIC_API_KEY"] == ""
    # CLI は常に container 内の relay を見る。router の場所は broker だけが知る。
    assert "http://127.0.0.1:18080" in _options().env["KOTOYOSE_AGENT_ENV"]
    assert "10.9.8.7" not in json.dumps(_options().env)


# --- lifecycle, stop proof and cleanup ------------------------------------------------------------

async def test_process_start_reports_container_identity_and_not_scope(env):
    started = AsyncMock()
    result = await _run(_worker(env), _job(env), on_started=started)
    assert result.status == "succeeded"
    assert started.await_args.args == (4321,)
    assert started.await_args.kwargs == {"container_identity": {
        "name": "kotoyose-agent-attempt-1-g7", "id": "c" * 64, "instance": "kotoyose"}}


async def test_success_is_confirmed_only_after_the_container_is_removed(env):
    result = await _run(_worker(env), _job(env))
    assert (result.status, result.reason, result.stop_confirmation) == ("succeeded", "completed", "confirmed")
    assert result.content == b"done"
    assert result.usage == {"source": "reported", "cost": 0.25, "input_tokens": 3, "output_tokens": 4}
    assert env.container.removed == [("attempt-1-g7", 0)]
    assert result.launch_outcome == "started"
    assert FakeSdkClient.instances[0].disconnected is True
    assert FakeEgress.instances[0].closed is True


@pytest.mark.parametrize("failure", ["remove", "egress"])
async def test_unconfirmed_stop_turns_success_into_worker_lost(env, failure):
    if failure == "remove":
        env.container.remove_result = "unconfirmed"
    else:
        FakeEgress.close_fails = True
    result = await _run(_worker(env), _job(env))
    assert (result.status, result.reason, result.stop_confirmation) == ("worker_lost", "worker_lost", "unconfirmed")


async def test_cancel_removes_the_container_and_reports_cancelled(env):
    FakeSdkClient.hang = True
    cancel = asyncio.Event()

    async def started(pid, **_identity):
        asyncio.get_running_loop().call_later(0.2, cancel.set)

    result = await asyncio.wait_for(_run(_worker(env), _job(env), cancel=cancel, on_started=started), 5)
    assert (result.status, result.reason, result.stop_confirmation) == ("cancelled", "cancelled", "confirmed")
    assert env.container.removed == [("attempt-1-g7", 0)]


async def test_deadline_removes_the_container_and_reports_timeout(env):
    FakeSdkClient.hang = True
    result = await asyncio.wait_for(_run(_worker(env), _job(env, timeout_seconds=0.3)), 5)
    assert (result.status, result.reason, result.stop_confirmation) == ("timeout", "timeout", "confirmed")
    assert env.container.removed == [("attempt-1-g7", 0)]


async def test_cancel_before_launch_never_creates_a_container(env):
    cancel = asyncio.Event()
    cancel.set()
    result = await _run(_worker(env), _job(env), cancel=cancel)
    assert (result.status, result.stop_confirmation, result.launch_outcome) == ("cancelled", "confirmed", "not_started")
    assert env.container.created == [] and env.container.removed == [] and FakeSdkClient.instances == []


async def test_missing_agent_container_config_fails_closed_before_docker(env):
    result = await _run(_worker(env, settings=None), _job(env))
    assert (result.status, result.reason) == ("failed", "invalid_job")
    assert result.launch_outcome == "not_started"
    assert env.container.created == [] and FakeSdkClient.instances == [] and FakeEgress.instances == []


@pytest.mark.parametrize("overrides", [
    {"tool_refs": ("builtin:sandbox-exec@1",)},
    {"resume_mode": "native"},
    {"credential_value": ""},
    {"output_contract": "yaml"},
    {"base_url": "ftp://gateway.example.com"},
    {"cli_binary": Path("relative/claude")},
])
async def test_invalid_coding_inputs_fail_before_any_side_effect(env, overrides):
    result = await _run(_worker(env), _job(env, **overrides))
    assert (result.status, result.reason) == ("failed", "invalid_job")
    assert env.container.created == [] and FakeSdkClient.instances == []


async def test_native_resume_is_refused_fail_closed_if_a_ref_ever_reaches_container_coding(env):
    result = await _run(_worker(env, session_store=object()), _job(env, resume_mode="native",
                                                                  provider_session_ref="s-1"))
    assert (result.status, result.reason) == ("failed", "invalid_job")
    assert env.container.created == []


async def test_temp_home_and_socket_dir_are_removed_after_confirmed_stop(env):
    result = await _run(_worker(env), _job(env))
    assert result.status == "succeeded"
    assert not any(env.homes.iterdir())
    assert not any(env.sockets.iterdir())


async def test_temp_home_is_kept_when_the_container_is_not_confirmed_removed(env):
    env.container.remove_result = "unconfirmed"
    result = await _run(_worker(env), _job(env))
    assert result.stop_confirmation == "unconfirmed"
    assert len(list(env.homes.iterdir())) == 1


async def test_json_contract_requires_a_single_object(env):
    FakeSdkClient.messages = [AssistantMessage(content=[TextBlock(text="not json")], model="claude-test"),
                              _result_message()]
    result = await _run(_worker(env), _job(env, output_contract="json"))
    assert (result.status, result.reason) == ("failed", "invalid_json_output")


async def test_provider_error_result_is_not_success(env):
    FakeSdkClient.messages = [_result_message(error=True, subtype="error_during_execution")]
    result = await _run(_worker(env), _job(env))
    assert (result.status, result.reason, result.stop_confirmation) == ("failed", "provider_error", "confirmed")


async def test_container_coding_does_not_retain_a_session_so_agent_execution_falls_back_to_durable_state(env):
    class Store:
        async def load(self, key):
            raise AssertionError("session store must not be used for container coding")

    result = await _run(_worker(env, session_store=Store()), _job(env, session_scope="a" * 64))
    assert result.status == "succeeded"
    options = _options()
    assert options.session_store is None and options.resume is None
    assert options.env["CLAUDE_CONFIG_DIR"] == "/home/agent/.claude"
    # ref が無いので Hub は native ではなく durable_state の再開 (新しい Run) を選ぶ。
    assert result.provider_session_id is None


async def test_usage_limit_reports_resume_unavailable_in_the_standard_vocabulary(env):
    FakeSdkClient.messages = [_result_message(error=True, subtype="error_rate_limit")]
    result = await _run(_worker(env, session_store=object()), _job(env, limits_mode=True))
    if result.provider_limit is not None:
        assert result.provider_limit.resume == "unavailable"
        assert result.provider_limit.unavailable_reason == "adapter_unsupported"


@pytest.mark.parametrize("name, cli_env, header, injected", [
    ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "authorization", "authorization"),
    ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY", "x-api-key", "x-api-key"),
])
async def test_cli_env_and_broker_header_follow_the_real_credential_kind(env, name, cli_env, header, injected):
    result = await _run(_worker(env), _job(env, credential_name=name))
    assert result.status == "succeeded"
    assert FakeConnectEgress.instances == []
    options = _options()
    capability = options.env[cli_env]
    assert capability and capability != API_KEY
    assert options.env["KOTOYOSE_AGENT_SECRET_ENV"] == cli_env
    # 他の認証変数は空。host の環境にある実 credential が CLI へ届かない。
    assert all(options.env[other] == "" for other in cc._AUTH_KINDS if other != cli_env)
    assert API_KEY not in json.dumps(options.env, default=str)
    spec = FakeEgress.instances[0].spec
    assert spec.capability_header == header and spec.capability == capability
    expected = f"Bearer {API_KEY}" if injected == "authorization" else API_KEY
    assert dict(spec.inject_headers) == {injected: expected}
    # CLI の OAuth beta / version header は上流へ通る。
    assert {"anthropic-beta", "anthropic-version"} <= spec.pass_headers
    argv = launcher.build_exec_argv(options.env, ["-p"])
    assert argv[argv.index(cli_env) - 1] == "-e" and capability not in "\0".join(argv)


async def test_subscription_oauth_uses_connect_passthrough_egress(env):
    result = await _run(_worker(env), _job(env, credential_name="CLAUDE_CODE_OAUTH_TOKEN"))
    assert (result.status, result.reason, result.stop_confirmation) == ("succeeded", "completed", "confirmed")
    assert FakeEgress.instances == []
    assert len(FakeConnectEgress.instances) == 1
    egress = FakeConnectEgress.instances[0]
    assert egress.closed is True
    assert egress.allowed_hosts == frozenset(cc._CLAUDE_CONNECT_HOSTS)
    assert egress.allowed_ports == frozenset({443})
    assert egress.run_id == "attempt-1"

    container_spec = env.container.created[0]
    assert "ANTHROPIC_BASE_URL" not in container_spec.env
    assert container_spec.env["HTTPS_PROXY"] == "http://127.0.0.1:18080"
    assert container_spec.env["HTTP_PROXY"] == "http://127.0.0.1:18080"
    assert container_spec.env["NO_PROXY"] == ""
    assert API_KEY not in json.dumps(container_spec.env) and API_KEY not in repr(container_spec)

    options = _options()
    assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == API_KEY
    assert options.env["KOTOYOSE_AGENT_SECRET_ENV"] == "CLAUDE_CODE_OAUTH_TOKEN"
    assert all(options.env[other] == "" for other in cc._AUTH_KINDS if other != "CLAUDE_CODE_OAUTH_TOKEN")
    plain = json.loads(options.env["KOTOYOSE_AGENT_ENV"])
    assert "ANTHROPIC_BASE_URL" not in plain
    assert plain["HTTPS_PROXY"] == "http://127.0.0.1:18080"
    assert plain["HTTP_PROXY"] == "http://127.0.0.1:18080"
    assert plain["NO_PROXY"] == ""
    assert API_KEY not in options.env["KOTOYOSE_AGENT_ENV"]
    assert API_KEY not in FakeSdkClient.wrapper_text

    argv = launcher.build_exec_argv(options.env, ["-p"])
    assert argv[argv.index("CLAUDE_CODE_OAUTH_TOKEN") - 1] == "-e"
    assert API_KEY not in "\0".join(argv)


async def test_subscription_oauth_with_custom_base_url_keeps_provider_egress(env):
    job = _job(env, credential_name="CLAUDE_CODE_OAUTH_TOKEN", base_url="http://10.9.8.7:20128/v1-router")
    result = await _run(_worker(env), job)
    assert result.status == "succeeded"
    assert FakeConnectEgress.instances == []
    assert len(FakeEgress.instances) == 1
    spec = FakeEgress.instances[0].spec
    assert spec.capability_header == "authorization"
    assert dict(spec.inject_headers) == {"authorization": f"Bearer {API_KEY}"}
    assert "ANTHROPIC_BASE_URL" in env.container.created[0].env
    assert _options().env["CLAUDE_CODE_OAUTH_TOKEN"] != API_KEY


async def test_connect_egress_close_failure_marks_worker_lost(env):
    FakeConnectEgress.close_fails = True
    result = await _run(_worker(env), _job(env, credential_name="CLAUDE_CODE_OAUTH_TOKEN"))
    assert (result.status, result.reason, result.stop_confirmation) == ("worker_lost", "worker_lost", "unconfirmed")


async def test_worker_without_container_runtime_fails_closed_without_launch(tmp_path, monkeypatch):
    launch = AsyncMock(side_effect=AssertionError("must not launch"))
    monkeypatch.setattr("kotoyose.container_claude.ClaudeSDKClient", launch)
    result = await NativeClaudeWorker().execute(
        replace(_minimal_job(tmp_path), codex_execution_mode=HOST_MODE, tool_refs=(HOST_TOOL_REF,)),
        asyncio.Event(), AsyncMock(), on_process_started=AsyncMock())
    assert (result.status, result.reason) == ("failed", "invalid_job")
    launch.assert_not_called()


def _minimal_job(tmp_path: Path) -> NativeAgentJob:
    return NativeAgentJob(
        attempt_id="a", model="m", cli_binary=tmp_path / "claude", credential_name="ANTHROPIC_API_KEY",
        credential_value="k", instructions="", prompt="", output_contract="text", max_turns=1,
        timeout_seconds=1.0, grace_seconds=0.0, output_limit_bytes=1, tool_refs=(),
    )


# --- gated real Docker: the image runs the reviewed CLI binary ------------------------------------

_DOCKER_IMAGE = "sha256:f97cd896d8d896dc21211ad9f6fe62ad21050b3c132b7c3a4bd2a46f02269713"


@pytest.mark.skipif(os.environ.get("KOTOYOSE_DOCKER_IT") != "1", reason="set KOTOYOSE_DOCKER_IT=1 to run the Docker test")
async def test_real_container_runs_the_claude_binary_and_is_removed(tmp_path):
    from kotoyose.agent_container import AgentContainer, AgentContainerSpec

    cli_link = Path(os.path.expanduser("~/.local/bin/claude"))
    if not cli_link.exists() or shutil.which("docker") is None:
        pytest.skip("claude binary or docker is unavailable")
    cli = cli_link.resolve(strict=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    container = AgentContainer(instance="kotoyose-test")
    job_id = "it-claude-" + os.urandom(4).hex()
    spec = AgentContainerSpec(
        job_id=job_id, image_id=_DOCKER_IMAGE, workspace=workspace, home=home, agent_root=None,
        egress_dir=None, wall_seconds=120, memory_bytes=512 * 1024 * 1024, cpus=1, pids_limit=256,
        env={"CLAUDE_CONFIG_DIR": "/home/agent/.claude", "DISABLE_AUTOUPDATER": "1"},
        ro_files={"/opt/agent/bin/claude": cli},
    )
    try:
        await container.create(spec)
        rc, stdout, _ = await container.run(job_id, ["/opt/agent/bin/claude", "--version"], timeout=60)
        assert rc == 0 and b"Claude" in stdout
    finally:
        confirmation = await container.remove(job_id, 0)
    assert confirmation == "confirmed"
    assert await container.exists(job_id) is False


def test_launcher_constant_matches_the_relay_target_used_by_this_module():
    assert launcher.RELAY_PROGRAM == cc._CONTAINER_RELAY


def test_container_coding_admission_does_not_require_systemd_or_bubblewrap(monkeypatch):
    from kotoyose import codex_provider_scope as scope

    requirements = SimpleNamespace(os=None, applications=[], gpu="none", workspace_locality="any", native="optional")
    assert scope.admit_provider_executor(requirements).worker_id == scope.PROVIDER_WORKER_ID
    assert scope.admit_provider_executor(requirements, require_host_scope=False).worker_id == scope.PROVIDER_WORKER_ID


def test_pinned_claude_coding_snapshot_is_accepted_without_codex_policy_pins():
    from kotoyose.codex_provider_scope import CodexProviderExecutor, PROVIDER_WORKER_ID
    from kotoyose.execution_service import ExecutionService

    snapshot = {"codex_execution_mode": HOST_MODE, "kind": "agent", "backend": "native", "provider": "claude",
                "worker_id": PROVIDER_WORKER_ID, "tool_refs": [HOST_TOOL_REF], "image_id": None,
                "worker_capabilities": CodexProviderExecutor.public_snapshot()}
    assert ExecutionService._worker_for(SimpleNamespace(), snapshot) is None


def test_emit_sdk_tool_result_forwards_to_activity_event(env):
    """_emit_sdk_tool_result は progress_event と activity_event の両方に tool_result を送る。"""
    from kotoyose.native_claude import _ALLOWED_TOOL, _emit_sdk_tool_result

    progress = []
    activity = []
    job = _job(env, progress_event=progress.append, activity_event=activity.append)

    _emit_sdk_tool_result(
        job,
        ToolResultBlock(tool_use_id="tool-ok", content=f"token={API_KEY}", is_error=False),
        {"tool-ok": "Bash"},
    )
    _emit_sdk_tool_result(
        job,
        ToolResultBlock(tool_use_id="tool-err", content="permission denied", is_error=True),
        {"tool-err": "Read"},
    )
    # _ALLOWED_TOOL は sandbox callback 側で記録するため重複送出しない。
    _emit_sdk_tool_result(
        job,
        ToolResultBlock(tool_use_id="tool-sandbox", content="ok", is_error=False),
        {"tool-sandbox": _ALLOWED_TOOL},
    )

    assert [(ev.kind, ev.tool_name, ev.summary) for ev in activity] == [
        ("tool_result", "Bash", "completed"),
        ("tool_result", "Read", "failed"),
    ]
    assert len(progress) == 2
    assert progress[0].tool_name == "Bash"
    assert API_KEY not in repr(progress)
    assert API_KEY not in repr(activity)
