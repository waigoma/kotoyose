"""Codex child の wire、ローカル CLI 選択、SDK 起動条件の回帰テスト。"""

import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai_codex import ApprovalMode, Sandbox
from openai_codex.generated.v2_all import ConfigReadResponse
from pydantic import ValidationError
from structlog.testing import capture_logs

from kotoyose import child_client, child_codex, child_turn, session
from kotoyose.chat_policy import ChatPolicyViolation, validate_cwd
from kotoyose.child_client import available_adapters, child_session
from kotoyose.child_codex import CODEX_CHILD_DRIVER, ChildCodexLaunch
from kotoyose.child_identity import load_policy
from kotoyose.child_process_tree import TURN_PIDS, kill_pids, new_turn_pids, tracking_type
from kotoyose.child_protocol import CODEX_TURNS_FEATURE, Frame
from kotoyose.config import HarnessConfig, get_config
from kotoyose.registry import registry
from kotoyose.runtime import codex
from kotoyose.runtime.base import RuntimeEvent, RuntimeProviderSpec, RuntimeResult, RuntimeTurnError
from kotoyose.runtime.child import build_turn_payload
from tests.test_child_k1 import _wait_child
from tests.test_child_k3 import _enroll, _host_config, _policy, hub  # noqa: F401


def _config(root: Path, kind="subscription"):
    connection = {"provider": "codex", "kind": kind, "data_policy": "no_training", "on": "child:dev"}
    if kind != "subscription":
        connection["secret"] = "env:HOST_ONLY_SECRET"
    if kind == "endpoint":
        connection["endpoint"] = "https://example.invalid/v1"
    return HarnessConfig.model_validate({
        "connections": {"child-codex": connection},
        "routes": {"r": {"driver": CODEX_CHILD_DRIVER, "model": "gpt-5", "connection": "child-codex"}},
        "models": {"dev": {
            "route": "r", "on": "child:dev", "allow_cwd_override": True,
            "cwd_allowed_roots": [str(root)], "mcp_refs": ["files"],
        }},
    })


@pytest.mark.parametrize("kind", ["api_key", "endpoint"])
def test_codex_child_requires_subscription(tmp_path, kind):
    with pytest.raises(ValidationError, match="api_key and endpoint connections cannot be used"):
        _config(tmp_path, kind)


@pytest.mark.parametrize("sdk", [False, True])
@pytest.mark.parametrize("bundled, system", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("choice", ["absent", "bundled", "system"])
def test_codex_advertises_only_with_sdk_and_selected_binary(tmp_path, monkeypatch, sdk, bundled, system, choice):
    def distribution(name):
        assert name == "openai-codex"
        if not sdk:
            raise child_codex.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(child_codex.metadata, "distribution", distribution)
    monkeypatch.setattr(child_codex, "_bundled_binary", lambda: "/wheel/codex" if bundled else None)
    monkeypatch.setattr(child_codex.shutil, "which", lambda _: "/system/codex" if system else None)
    monkeypatch.setattr(child_codex, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(child_client, "sdk_importable", lambda _row: sdk)
    monkeypatch.setattr(
        "kotoyose.child_components.bundled_cli",
        lambda _row: Path("/wheel/codex") if bundled else None,
    )
    monkeypatch.setattr(
        "kotoyose.child_components.system_cli",
        lambda _row: Path("/system/codex") if system else None,
    )
    if choice != "absent":
        (tmp_path / "app.json").write_text(json.dumps({"codex_cli": choice}))
    selected_bundled = bundled and choice != "system"
    selected_system = system and choice == "system"
    assert available_adapters([], tmp_path) == []
    assert available_adapters([CODEX_CHILD_DRIVER], tmp_path) == (
        [CODEX_CHILD_DRIVER] if sdk and (selected_bundled or selected_system) else []
    )
    if selected_bundled or selected_system:
        launch = child_codex.resolve_child_codex(tmp_path)
        assert launch.bundled is selected_bundled
        assert launch.argv == (("/wheel/codex",) if selected_bundled else ("/system/codex",))
    else:
        with pytest.raises(FileNotFoundError):
            child_codex.resolve_child_codex(tmp_path)


def test_windows_codex_npm_shim_is_resolved_without_a_shell(tmp_path, monkeypatch):
    shim = tmp_path / "codex.cmd"
    runner = tmp_path / "node.exe"
    script = tmp_path / "node_modules/@openai/codex/bin/codex.js"
    script.parent.mkdir(parents=True)
    script.touch()
    runner.touch()
    shim.write_text('@ECHO off\nSET "_prog=%dp0%\\node.exe"\n"%_prog%" "%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*')
    (tmp_path / "app.json").write_text('{"codex_cli":"system"}')
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(child_codex, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(child_codex.shutil, "which", {str(shim): str(shim), str(runner): str(runner)}.get)
    monkeypatch.setattr(child_codex, "Path", lambda value: Path(value.replace("\\", "/")))
    launch = child_codex.resolve_child_codex(tmp_path)
    assert launch.argv == (str(runner), str(tmp_path) + r"\node_modules\@openai\codex\bin\codex.js")
    script.unlink()
    with pytest.raises(FileNotFoundError):
        child_codex.resolve_child_codex(tmp_path)


@pytest.mark.parametrize("use_cwd", [False, True])
async def test_child_codex_streams_and_keeps_warm_runtime_without_host_auth(hub, tmp_path, monkeypatch, use_cwd):
    config = _config(tmp_path)
    preset = config.models["dev"]
    connection = config.connections["child-codex"]
    assert connection.secret is None
    connection.secret = "env:HOST_ONLY_SECRET"
    connection.home = "HOST_ONLY_HOME"
    connection.auth_file = tmp_path / "HOST_ONLY_AUTH_FILE"
    preset.codex_home = "HOST_ONLY_CODEX_HOME"
    preset.codex = {"sandbox": "read-only", "codex_bin": "HOST_ONLY_CLI", "config": {"token": "HOST_ONLY_TOKEN"}}
    config.routes["r"].model_provider = {"base_url": "https://host-only.invalid", "token": "HOST_ONLY_PROVIDER_TOKEN"}
    resolution = AsyncMock(side_effect=AssertionError("host attempted to resolve credentials"))
    monkeypatch.setattr(session, "resolve_preset_secrets_with_versions_async", resolution)
    home = tmp_path / "child"
    workspace = tmp_path / "repo/project"
    workspace.mkdir(parents=True)
    cwd = str(workspace) if use_cwd else None
    await _enroll(hub, home, workspace.parent, adapters=[CODEX_CHILD_DRIVER])
    monkeypatch.setattr(child_codex, "_bundled_binary", lambda: "/wheel/codex")
    builds = []

    class Runtime:
        async def stream(self, prompt):
            yield prompt
            yield RuntimeEvent(kind="tool", tool_name="mcp__files__read")
            yield RuntimeResult(text=prompt, runtime_session_id="thread-1", provider="codex")

    def builder(effective, cwd, _session_id, _headers, *, child_launch):
        assert child_launch == ChildCodexLaunch(("/wheel/codex",), bundled=True)
        assert effective.codex == {"sandbox": "read-only", "approval_mode": "deny_all"}
        assert effective.codex_home is None
        local = get_config()
        assert local.connections["child-codex"].home == "default"
        assert local.connections["child-codex"].auth_file is None
        assert local.connections["child-codex"].secret is None
        assert local.mcps["files"].server["command"] == "child-local-mcp"
        builds.append(cwd)
        return Runtime()

    monkeypatch.setitem(registry.runtime_providers, "codex", RuntimeProviderSpec(builder, "codex"))
    monkeypatch.setattr(registry, "_child_builder_overrides", {**registry._child_builder_overrides})
    registry.override_child_builder("codex", lambda _alias, preset, cwd, **options: builder(preset, cwd, None, None, **options))
    monkeypatch.setattr(session, "sessions", {})
    received = []
    async with child_session(home, heartbeat_interval=0.05, received_turns=received):
        await _wait_child(hub, "dev", online=True)
        assert hub.hello_payloads[-1]["adapters"] == [CODEX_CHILD_DRIVER]
        assert CODEX_TURNS_FEATURE in hub.hello_payloads[-1]["features"]
        client, _ = await session.get_or_resume_client("k8-test", "dev", cwd, config_snapshot=config)
        first = [item async for item in client.stream("first")]
        second = await client.ask("second")
        assert len(builds) == 1
        assert builds[0] == str(workspace) if use_cwd else Path(builds[0]).is_dir()
        assert first[0] == "first"
        assert first[1].tool_name == "mcp__files__read"
        assert first[2].runtime_session_id == second.runtime_session_id == "thread-1"
        raw = json.dumps(received)
        assert "HOST_ONLY" not in raw
        assert "host-only.invalid" not in raw
    resolution.assert_not_awaited()


async def test_codex_refuses_a_same_version_child_without_the_feature(hub, tmp_path, monkeypatch):
    home = tmp_path / "child"
    await _enroll(hub, home, tmp_path, adapters=[CODEX_CHILD_DRIVER, "claude.agent-sdk"])
    monkeypatch.setattr(child_codex, "_bundled_binary", lambda: "/wheel/codex")
    monkeypatch.setattr(session, "sessions", {})
    config = _config(tmp_path)
    claude = HarnessConfig.model_validate(_host_config(tmp_path))
    async with child_session(home, heartbeat_interval=0.05):
        client, _ = await session.get_or_resume_client("codex-reconnect", "dev", config_snapshot=config)

    encode = child_client.encode_frame
    received = []

    def old_hello(stream, kind, payload):
        if kind == "hello":
            payload = {key: value for key, value in payload.items() if key != "features"}
        return encode(stream, kind, payload)

    monkeypatch.setattr(child_client, "encode_frame", old_hello)
    async with child_session(home, heartbeat_interval=0.05, received_turns=received):
        assert hub.hello_payloads[-1]["features"] is None
        assert hub.hello_payloads[-1]["adapters"] == [CODEX_CHILD_DRIVER, "claude.agent-sdk"]
        with pytest.raises(session.SDKConnectionError, match="does not advertise Codex turn support"):
            await session.get_or_resume_client("codex-reconnect", "dev", config_snapshot=config)
        with pytest.raises(session.SDKConnectionError, match="does not advertise Codex turn support"):
            await client.ask("never sent")
        legacy, _ = await session.get_or_resume_client("claude-reconnect", "dev", config_snapshot=claude)
        assert legacy.provider == "claude"
        assert received == []


@pytest.mark.parametrize(("cap", "expected"), [
    (None, ("read-only", "read-only", "workspace-write", "workspace-write")),
    ("read-only", ("read-only", "read-only", "read-only", "read-only")),
    ("workspace-write", ("read-only", "read-only", "workspace-write", "workspace-write")),
    ("danger-full-access", ("read-only", "read-only", "workspace-write", "danger-full-access")),
])
@pytest.mark.parametrize("requested", [None, "read-only", "workspace-write", "danger-full-access"])
def test_child_clamps_sandbox_and_drops_host_approval_options(tmp_path, monkeypatch, cap, expected, requested):
    home = tmp_path / "child"
    _policy(home, tmp_path, adapters=[CODEX_CHILD_DRIVER])
    policy_file = home / "policy.json"
    if cap is not None:
        policy_file.write_text(json.dumps({**json.loads(policy_file.read_text()), "codex_sandbox_max": cap}))
    assert load_policy(policy_file).codex_sandbox_max == (cap or "workspace-write")
    monkeypatch.setattr(child_codex, "_bundled_binary", lambda: "/wheel/codex")
    config = _config(tmp_path)
    payload = build_turn_payload(
        alias="dev", config=config, preset=config.models["dev"], conversation_key=None, cwd=str(tmp_path), prompt="test",
    )
    # Simulate an older or modified host sending options that the new host strips.
    payload["preset"]["codex"] = {
        "sandbox": requested, "timeout": 12, "approval_mode": "auto_review", "dangerously_bypass_approvals_and_sandbox": True,
    }
    assert child_codex.codex_turn_options(payload["preset"]["codex"]) == {
        "timeout": 12, **({"sandbox": requested} if requested else {}),
    }
    prepared = child_turn._prepare(home, payload)
    try:
        effective = prepared["config"].preset_for("dev")
        mode = expected[[None, "read-only", "workspace-write", "danger-full-access"].index(requested)]
        assert effective.codex == {"sandbox": mode, "timeout": 12, "approval_mode": "deny_all"}
    finally:
        prepared["cleanup"]()


@pytest.mark.parametrize("invalid", [None, "full-access", True, [], {}])
def test_child_rejects_invalid_codex_policy_limit(tmp_path, invalid):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"workspace_roots": [], "adapters": [], "mcp": {}, "codex_sandbox_max": invalid}))
    with pytest.raises(ValueError, match="codex_sandbox_max"):
        load_policy(path)


def test_child_rechecks_cwd_and_inbound_connection(tmp_path, monkeypatch):
    home = tmp_path / "child"
    workspace = tmp_path / "workspace"
    alias_root = workspace / "repo"
    alias_root.mkdir(parents=True)
    outside = workspace / "other"
    outside.mkdir()
    escape = alias_root / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(child_codex, "_bundled_binary", lambda: "/wheel/codex")
    _policy(home, workspace, adapters=[CODEX_CHILD_DRIVER])
    config = _config(alias_root)
    preset = config.models["dev"]
    route = config.routes["r"].model_dump(mode="json")
    connection = config.connections["child-codex"].model_dump(mode="json")
    payload = {"alias": "dev", "preset": preset.model_dump(mode="json"), "route": route, "connection": connection}
    with pytest.raises(ChatPolicyViolation, match="許可ルート外"):
        validate_cwd(preset, "dev", str(outside))
    validate_cwd(preset, "dev", str(escape))
    with pytest.raises(child_turn.ChildTurnError, match="outside the alias"):
        child_turn._prepare(home, {**payload, "cwd": str(escape)})
    with pytest.raises(child_turn.ChildTurnError, match="outside workspace_roots"):
        child_turn._prepare(home, {**payload, "cwd": str(tmp_path)})
    connection.update(home="host-only", auth_file="host-only", secret="host-only", token="host-only", endpoint="host-only")
    payload["preset"]["codex"] = {"config": {"mcp_servers": {"bad": {"command": "host-only"}}}}
    prepared = child_turn._prepare(home, {**payload, "cwd": str(alias_root)})
    try:
        assert "host-only" not in prepared["config"].model_dump_json()
        assert set(prepared["config"].mcps) == {"files"}
    finally:
        prepared["cleanup"]()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process group and /proc assertions")
@pytest.mark.parametrize("action", ["cancel", "disconnect"])
async def test_codex_turn_kills_the_process_group_after_its_leader_exits(tmp_path, monkeypatch, action):
    home = tmp_path / "child"
    _policy(home, tmp_path, adapters=[CODEX_CHILD_DRIVER])
    monkeypatch.setattr(child_codex, "_bundled_binary", lambda: "/wheel/codex")
    config = _config(tmp_path)
    started = asyncio.Event()
    stopped = asyncio.Event()
    processes = []

    def spawn():
        with subprocess.Popen(
            [sys.executable, "-I", "-c", 'import subprocess, sys; '
             'print(subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"], '
             'stdout=subprocess.DEVNULL).pid)'],
            stdout=subprocess.PIPE,
            text=True,
        ) as process:
            stdout, _ = process.communicate(timeout=5)
            processes.append((process.pid, int(stdout)))

    sdk = SimpleNamespace(
        __aenter__=AsyncMock(),
        _client=SimpleNamespace(
            start=lambda: asyncio.to_thread(spawn),
            request=AsyncMock(return_value=ConfigReadResponse.model_validate({"config": {}, "origins": {}})),
        ),
    )

    class Runtime:
        async def stream(self, _prompt):
            await codex.start_child_sdk(sdk, str(tmp_path), {})
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()
            yield RuntimeResult(text="")

    monkeypatch.setitem(registry.runtime_providers, "codex", RuntimeProviderSpec(lambda *_args, **_kwargs: Runtime(), "codex"))
    monkeypatch.setattr(registry, "_child_builder_overrides", {**registry._child_builder_overrides, "codex": lambda *_args, **_kwargs: Runtime()})
    turns = child_turn.ChildTurns(home, AsyncMock(), None)
    turns.start()
    try:
        payload = build_turn_payload(alias="dev", config=config, preset=config.models["dev"], conversation_key="c", cwd=str(tmp_path), prompt="hold")
        task = turns.handle(Frame(1, "turn", payload))
        try:
            await asyncio.wait_for(started.wait(), 5)
        except TimeoutError:
            if task.done():
                task.result()
            raise
        leader, grandchild = processes[0]
        assert not Path(f"/proc/{leader}").exists()
        assert os.getpgid(grandchild) == leader
        if action == "cancel":
            turns.handle(Frame(1, "cancel", {}))
        else:
            turns.begin_shutdown()
        await asyncio.wait_for(stopped.wait(), 2)
        async with asyncio.timeout(2):
            while _process_running(grandchild):
                await asyncio.sleep(0.01)
    finally:
        await turns.finish()
        for _, grandchild in processes:
            if _process_running(grandchild):
                # 確かめた直後に終わることがある (CI で ProcessLookupError が元の結果を隠していた)。
                with contextlib.suppress(ProcessLookupError):
                    os.kill(grandchild, signal.SIGKILL)


def _process_running(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


def test_stopped_turn_cannot_spawn_late_sdk_process():
    pids = new_turn_pids()
    kill_pids(pids)
    token = TURN_PIDS.set(pids)
    class UnexpectedPopen:
        def __init__(self):
            pytest.fail("a cancelled turn spawned a process")

    try:
        with pytest.raises(OSError, match="turn has stopped"):
            tracking_type(UnexpectedPopen)()
    finally:
        TURN_PIDS.reset(token)


@pytest.mark.parametrize("bundled", [False, True])
def test_codex_sdk_passes_policy_mcp_env_outside_argv(monkeypatch, tmp_path, bundled):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "child-login"))
    launch = ChildCodexLaunch(("/fake/node", "/fake/codex.js"), bundled=bundled)
    servers = {
        name: {"command": sys.executable, "args": ["-I", "-c", "import os; print(os.environ['CODEX_HOME'])"],
               "env": {"CODEX_HOME": value}}
        for name, value in {"files.local": "fake-mcp-secret-one", "notes": "fake-mcp-secret-two"}.items()
    }
    runtime = codex.CodexRuntimeSession(
        model="gpt-5", cwd=str(tmp_path), child_launch=launch, codex_home="/host-home", mcp_servers=servers,
    )
    cfg = runtime._sdk_config()
    if bundled:
        assert cfg.codex_bin is None and cfg.launch_args_override is None
        argv = cfg.config_overrides
    else:
        argv = cfg.launch_args_override
        assert argv[:2] == launch.argv
        assert argv[-3:] == ("app-server", "--listen", "stdio://")
    assert "CODEX_HOME" not in cfg.env
    assert set(cfg.env.values()) == {"fake-mcp-secret-one", "fake-mcp-secret-two"}
    assert all(secret not in " ".join(argv) for secret in cfg.env.values())
    mcp_override = next(arg for arg in argv if arg.startswith("mcp_servers="))
    mcp = tomllib.loads(mcp_override)["mcp_servers"]
    assert set(mcp) == set(servers)
    for name, spec in mcp.items():
        env = {**os.environ, **{key: cfg.env[key] for key in spec["env_vars"]}}
        result = subprocess.run(
            [spec["command"], *spec["args"]], env=env, cwd=tmp_path, capture_output=True, text=True, check=True, timeout=5,
        )
        assert result.stdout.strip() == servers[name]["env"]["CODEX_HOME"]


def _sdk_with_config(config):
    # ConfigReadResponse includes a Config with extra fields and typed origin metadata.
    response = ConfigReadResponse.model_validate({
        "config": {
            "model": "gpt-5", "approval_policy": "on-request", "sandbox_mode": "danger-full-access", **config,
        },
        "origins": {"model": {"name": {"type": "sessionFlags"}, "version": "test-version"}},
        "layers": None,
    })
    return SimpleNamespace(
        __aenter__=AsyncMock(), _client=SimpleNamespace(start=AsyncMock(), request=AsyncMock(return_value=response)),
        thread_start=AsyncMock(), close=AsyncMock(),
    )


@pytest.mark.parametrize(("case", "allowed"), [
    ("defaulted", True), ("no_table", True), ("empty_table", True),
    # ambient: the fake SDK ignores the enabled=false override, so the re-check still sees it and refuses.
    ("ambient", False), ("disabled_ambient", True), ("disabled_policy", False),
    ("extra_env", False), ("extra_env_vars", False), ("wrong_command", False), ("remote_transport", False), ("relaxed_tools", False),
])
async def test_sdk_checks_effective_mcp_before_starting_a_thread(monkeypatch, case, allowed):
    policy = {"files": {"command": "allowed", "args": [], "disabled_tools": ["write"]}}
    server = {
        "command": "allowed", "args": [], "disabled_tools": ["write"], "enabled": True,
        "cwd": None, "env": None, "env_vars": [], "required": False, "startup_timeout_sec": 10, "tool_timeout_sec": 60,
        # Real Codex reports the approval mode the child configured (verified with Codex 0.170.0).
        "default_tools_approval_mode": "approve",
    }
    servers = {"files": server}
    match case:
        case "ambient" | "disabled_ambient":
            servers["outside-policy"] = {"command": "unexpected", "enabled": case == "ambient"}
        case "disabled_policy":
            server["enabled"] = False
        case "extra_env":
            server["env"] = {"EXTRA": "unexpected"}
        case "extra_env_vars":
            server["env_vars"] = ["EXTRA"]
        case "wrong_command":
            server["command"] = "unexpected"
        case "remote_transport":
            server["url"] = "https://example.invalid/mcp"
        case "relaxed_tools":
            server["disabled_tools"] = []
        case "no_table" | "empty_table":
            servers = policy = {}
    sdk = _sdk_with_config({} if case == "no_table" else {"mcp_servers": servers})
    monkeypatch.setattr(codex, "AsyncCodex", lambda _config: sdk)
    runtime = codex.CodexRuntimeSession(
        model="gpt-5", cwd="/work", config=child_codex.child_codex_turn_options(None, "workspace-write"),
        child_launch=ChildCodexLaunch(("/codex",), bundled=True), mcp_servers=policy,
    )
    if allowed:
        await runtime._ensure_thread()
        sdk.thread_start.assert_awaited_once()
        assert sdk.thread_start.call_args.kwargs["sandbox"] == Sandbox.read_only
        assert sdk.thread_start.call_args.kwargs["approval_mode"] == ApprovalMode.deny_all
    else:
        with pytest.raises(RuntimeTurnError, match="differs from the child policy"):
            await runtime._ensure_thread()
        sdk.thread_start.assert_not_awaited()
        assert sdk.close.await_count == (2 if case == "ambient" else 1)
    sdk._client.request.assert_awaited_with("config/read", {"cwd": "/work"}, response_model=ConfigReadResponse)
    assert sdk._client.request.await_count == (2 if case == "ambient" else 1)


@pytest.mark.parametrize("phase", ["start", "initialize", "config/read", "sdk_change"])
async def test_codex_startup_error_is_generic_and_local_log_has_no_argv(monkeypatch, phase):
    sdk = _sdk_with_config({})
    failure = FileNotFoundError(2, "could not start", "app-server --config FAKE_SECRET")
    match phase:
        case "start":
            sdk._client.start.side_effect = failure
        case "initialize":
            sdk.__aenter__.side_effect = failure
        case "config/read":
            sdk._client.request.side_effect = failure
        case "sdk_change":
            del sdk._client
    monkeypatch.setattr(codex, "AsyncCodex", lambda _config: sdk)
    runtime = codex.CodexRuntimeSession(model="gpt-5", cwd="/work", child_launch=ChildCodexLaunch(("/codex",), bundled=True))
    with capture_logs() as logs, pytest.raises(RuntimeTurnError, match="^Codex app-server startup failed$") as error:
        await runtime._ensure_thread()
    assert "FAKE_SECRET" not in repr(error.value)
    assert "FAKE_SECRET" not in repr(logs)
    assert logs[-1]["event"] == "child.codex.startup_failed"
    if phase == "sdk_change":
        assert logs[0]["event"] == "child.codex.sdk_transport_changed"
    else:
        assert logs[-1]["error_type"] == "FileNotFoundError"
        assert logs[-1]["errno"] == 2
    sdk.thread_start.assert_not_awaited()
    sdk.close.assert_awaited_once()


def test_output_guard_redacts_env_keys_and_never_reads_login_files(tmp_path, monkeypatch):
    # The owner's rule (ADR-0001): Kotoyose never reads or moves official CLI login tokens, so the
    # guard only knows API-key values from the child process environment.
    from kotoyose.child_output import ChildOutputGuard

    monkeypatch.setenv("OPENAI_API_KEY", "fake-env-api-key")
    opened: list[str] = []
    real_open = Path.read_text

    def spy(self, *args, **kwargs):
        opened.append(str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    guard = ChildOutputGuard("codex")
    text = guard.delta("before fake-env-") + guard.delta("api-key after") + guard.flush()
    assert "fake-env-api-key" not in text and "before" in text and "after" in text
    assert not any(name.endswith(("auth.json", ".credentials.json")) for name in opened)




async def test_local_codex_mcp_outside_policy_is_disabled_for_the_turn(monkeypatch):
    # A child whose ~/.codex/config.toml has its own MCP servers must still run Codex turns:
    # those servers are disabled for the session (enabled=false override) instead of refusing the turn.
    server = {"command": "allowed", "args": [], "disabled_tools": [], "enabled": True, "env": None, "env_vars": [], "default_tools_approval_mode": "approve"}
    first = _sdk_with_config({"mcp_servers": {"files": server, "mine": {"command": "local", "enabled": True}}})
    second = _sdk_with_config({"mcp_servers": {"files": server, "mine": {"command": "local", "enabled": False}}})
    configs = []
    sdks = iter([first, second])
    monkeypatch.setattr(codex, "AsyncCodex", lambda config: (configs.append(config), next(sdks))[1])
    runtime = codex.CodexRuntimeSession(
        model="gpt-5", cwd="/work", config=child_codex.child_codex_turn_options(None, "workspace-write"),
        child_launch=ChildCodexLaunch(("/codex",), bundled=True),
        mcp_servers={"files": {"command": "allowed", "args": []}},
    )
    await runtime._ensure_thread()
    first.close.assert_awaited_once()
    second.thread_start.assert_awaited_once()
    overrides = list(configs[1].config_overrides)
    # Bare key (codex -c does not accept quoted keys) and after the policy table (which would reset it).
    assert "mcp_servers.mine.enabled=false" in overrides
    table = next(i for i, item in enumerate(overrides) if item.startswith("mcp_servers={"))
    assert overrides.index("mcp_servers.mine.enabled=false") > table
    assert not any('"mine"' in item for item in configs[0].config_overrides)
