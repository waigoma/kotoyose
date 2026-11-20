"""Tests for isolated per-Run container launch of print-mode CLIs (`container_print`)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import textwrap

import pytest

from kotoyose.agent_container import AgentContainer, AgentContainerSpec
from kotoyose.agent_deadlines import AgentStartRejected
from kotoyose.container_print import build_print_cli_container_plan
from kotoyose.execution_resources import AgentContainerConfig
from kotoyose.print_cli_agent import _HOME_SEMAPHORES, PrintCliJob, PrintCliWorker, compose_print_cli_prompt
from kotoyose.print_cli_profiles import AGY_PROFILE, get_print_cli_profile


def _write_fake_cli(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_ac_config(root: Path) -> AgentContainerConfig:
    sock_root = Path("/tmp")
    agent_root = root / "agent-root"
    home_root = root / "home-root"
    agent_root.mkdir(parents=True, exist_ok=True)
    home_root.mkdir(parents=True, exist_ok=True)
    return AgentContainerConfig(
        agent_root=str(agent_root),
        home_root=str(home_root),
        workspace_memory_bytes=256 * 1024 * 1024,
        cpus=2.0,
        pids_limit=64,
        min_free_disk_bytes=0,
        max_containers=4,
        egress_socket_root=str(sock_root),
    )


def _normalize_settings_arg(args: list[str]) -> tuple[list[str], str | None]:
    normalized = list(args)
    settings_path: str | None = None
    if "--settings" in normalized:
        idx = normalized.index("--settings")
        settings_path = normalized[idx + 1]
        normalized[idx + 1] = "<SETTINGS_FILE>"
    return normalized, settings_path


def test_launch_spec_binds_network_env_and_argv_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak-secret-value")
    monkeypatch.setenv("GEMINI_API_KEY", "gk-secret-value")
    monkeypatch.setenv("SENTINEL_SECRET", "top-secret-sentinel")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("TERM", "xterm-256color")

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / ".codex").mkdir()
    (ws / ".agents").mkdir()
    login_home = tmp_path / "login-home"
    login_home.mkdir()
    fake_agy = _write_fake_cli(tmp_path / "bin" / "agy", "print('ok')\n")
    egress_dir = tmp_path / "egress"
    egress_dir.mkdir()
    settings_dir = tmp_path / "cfg"
    settings_dir.mkdir()
    host_settings_dir = tmp_path / "host-cfg"
    host_settings_dir.mkdir()

    ac_cfg = _make_ac_config(tmp_path)
    image_id = "sha256:" + ("b" * 64)
    job = PrintCliJob(
        profile_id="agy",
        cli_binary=fake_agy,
        model="gemini-4-hydrogen-high",
        workspace=ws,
        login_home=login_home,
        surface="write",
        instructions="Follow rules.",
        prompt="Edit a file.",
        output_contract="text",
        timeout_seconds=20.0,
        grace_seconds=2.0,
        output_limit_bytes=32768,
        memory_bytes=128 * 1024 * 1024,
        pids_limit=32,
        cpus=1.5,
        add_dirs=(ws / "extra",),
        execution_environment="isolated",
        image_id=image_id,
        attempt_id="att-spec-1",
    )

    spec, argv, exec_env, cli_args = build_print_cli_container_plan(
        job,
        AGY_PROFILE,
        ac_cfg,
        image_id=image_id,
        egress_dir=egress_dir,
        settings_dir=settings_dir,
        print_timeout_seconds=17.0,
        job_id="spec-1",
    )

    # Binds on AgentContainerSpec
    assert spec.image_id == image_id
    assert spec.workspace == ws
    assert spec.workspace_readonly is False
    assert spec.home == login_home
    assert spec.ro_files["/opt/agent/bin/agy"] == fake_agy.resolve()
    assert spec.ro_files["/opt/agent/egress_relay.py"].name == "egress_relay.py"
    assert "/opt/agent/agy-write-settings.json" in spec.ro_files
    assert spec.ro_dirs == {
        "/workspace/.codex": ws / ".codex",
        "/workspace/.agents": ws / ".agents",
    }

    # Rendered docker create argv enforces --network none, ro binary/settings/protected, rw home/workspace
    docker_argv = AgentContainer(instance="test")._create_argv(spec)
    net_idx = docker_argv.index("--network")
    assert docker_argv[net_idx + 1] == "none"
    assert "--read-only" in docker_argv
    assert ["--env", "HOME=/home/agent"] == [
        docker_argv[i : i + 2] for i in range(len(docker_argv) - 1) if docker_argv[i : i + 2] == ["--env", "HOME=/home/agent"]
    ][0]
    mounts = [docker_argv[i + 1] for i in range(len(docker_argv) - 1) if docker_argv[i] == "--mount"]
    assert any(f"src={fake_agy.resolve()},dst=/opt/agent/bin/agy,readonly" in m for m in mounts)
    assert any("dst=/opt/agent/agy-write-settings.json,readonly" in m for m in mounts)
    assert any(f"src={login_home},dst=/home/agent" in m and ",readonly" not in m for m in mounts)
    assert any(f"src={ws},dst=/workspace" in m and ",readonly" not in m for m in mounts)
    assert any(f"src={ws / '.codex'},dst=/workspace/.codex,readonly" in m for m in mounts)
    assert any(f"src={ws / '.agents'},dst=/workspace/.agents,readonly" in m for m in mounts)

    read_spec, *_ = build_print_cli_container_plan(
        replace(job, surface="read"),
        AGY_PROFILE,
        ac_cfg,
        image_id=image_id,
        egress_dir=egress_dir,
        settings_dir=settings_dir,
        print_timeout_seconds=17.0,
        job_id="spec-read",
    )
    assert read_spec.workspace_readonly is True
    read_argv = AgentContainer(instance="test")._create_argv(read_spec)
    assert f"type=bind,src={ws},dst=/workspace,readonly" in read_argv

    # Env: allowlist + HOME=/home/agent + proxy vars only; sentinel secrets absent
    expected_allow = {k: os.environ[k] for k in AGY_PROFILE.env_allowlist if k in os.environ}
    assert exec_env == {
        **expected_allow,
        "HOME": "/home/agent",
        "HTTPS_PROXY": "http://127.0.0.1:18080",
        "HTTP_PROXY": "http://127.0.0.1:18080",
        "NO_PROXY": "",
    }
    for secret_key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "SENTINEL_SECRET"):
        assert secret_key not in exec_env
        assert secret_key not in spec.env

    # Argv parity with host path (modulo binary path and mounted settings path)
    assert argv[:4] == ["python3", "/opt/agent/egress_relay.py", "/run/egress/egress.sock", "--"]
    assert argv[4] == "/opt/agent/bin/agy"
    assert argv[5:] == cli_args

    host_prompt = compose_print_cli_prompt(job.instructions, job.prompt, output_contract=job.output_contract)
    host_settings = AGY_PROFILE.write_settings_file(job.surface, host_settings_dir)
    host_args = AGY_PROFILE.render_argv(
        model=job.model,
        prompt=host_prompt,
        timeout_seconds=17.0,
        surface=job.surface,
        settings_path=host_settings,
        add_dirs=job.add_dirs,
    )
    norm_container, container_settings = _normalize_settings_arg(cli_args)
    norm_host, host_settings_arg = _normalize_settings_arg(host_args)
    assert norm_container == norm_host
    assert container_settings == "/opt/agent/agy-write-settings.json"
    assert host_settings_arg == str(host_settings)
    assert json.loads(spec.ro_files["/opt/agent/agy-write-settings.json"].read_text()) == json.loads(
        host_settings.read_text()
    )


@pytest.mark.parametrize(("surface", "flags", "workspace_suffix"), [
    ("read", ["--mode", "ask", "--trust"], ",readonly"),
    ("write", ["--force", "--trust"], ""),
])
def test_cursor_container_plan_mounts_versioned_bundle(tmp_path, surface, flags, workspace_suffix):
    workspace = tmp_path / "workspace"
    home = tmp_path / "login-home"
    egress = tmp_path / "egress"
    for directory in (workspace, home, egress):
        directory.mkdir()
    binary = _write_fake_cli(tmp_path / "versions" / "v1" / "cursor-agent", "pass")
    (binary.parent / "node").touch()
    link = tmp_path / "agent"
    link.symlink_to(binary)
    image_id = "sha256:" + "c" * 64
    profile = get_print_cli_profile("cursor")
    job = PrintCliJob(
        profile_id=profile.id, cli_binary=link, model="auto", workspace=workspace,
        login_home=home, surface=surface, instructions="", prompt="--force {model}",
        output_contract="text", timeout_seconds=10, grace_seconds=1, output_limit_bytes=32768,
        memory_bytes=128 * 1024 * 1024, pids_limit=32, cpus=1,
        execution_environment="isolated", image_id=image_id,
    )
    spec, argv, env, cli_args = build_print_cli_container_plan(
        job, profile, _make_ac_config(tmp_path), image_id=image_id, egress_dir=egress,
        settings_dir=tmp_path, print_timeout_seconds=8, job_id="cursor-plan",
    )
    create_argv = AgentContainer(instance="test")._create_argv(spec)
    assert f"type=bind,src={binary.parent},dst=/opt/agent/runtime,readonly" in create_argv
    assert f"type=bind,src={home},dst=/home/agent" in create_argv
    assert f"type=bind,src={workspace},dst=/workspace{workspace_suffix}" in create_argv
    assert set(spec.ro_files) == {"/opt/agent/egress_relay.py"}
    assert env["HOME"] == "/home/agent"
    assert env["CURSOR_CONFIG_DIR"] == "/home/agent/.cursor"
    assert env["NODE_USE_ENV_PROXY"] == "1"
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://127.0.0.1:18080"
    assert profile.egress_hosts == (
        "api2.cursor.sh", "agentn.global.api5.cursor.sh", "authenticate.cursor.sh", "prod.authentication.cursor.sh", "authentication.cursor.sh",
    )
    assert profile.egress_ports == (443,)
    assert cli_args == [
        "--print", "--output-format", "stream-json", "--model", "auto", *flags, "--",
        compose_print_cli_prompt("", job.prompt, output_contract="text"),
    ]
    assert argv[4:] == ["/opt/agent/runtime/cursor-agent", *cli_args]


def test_bundle_version_changes_launch_digest_with_identical_executable(tmp_path):
    v1 = _write_fake_cli(tmp_path / "versions" / "v1" / "cursor-agent", "pass")
    v2 = _write_fake_cli(tmp_path / "versions" / "v2" / "cursor-agent", "pass")
    link = tmp_path / "agent"
    link.symlink_to(v1)
    cursor = get_print_cli_profile("cursor")
    pinned = link.resolve(strict=True)
    digest = cursor.launch_digest(pinned, placement="isolated")
    link.unlink()
    link.symlink_to(v2)
    assert cursor.launch_digest(link.resolve(strict=True), placement="isolated") != digest
    assert cursor.launch_digest(pinned, placement="isolated") == digest
    assert AGY_PROFILE.launch_digest(v1, placement="isolated") == AGY_PROFILE.launch_digest(v2, placement="isolated")


class _FakeAgentContainer:
    """In-process fake container running egress_relay + CLI on the host with remapped container paths."""

    def __init__(self, *, run_real_relay: bool = True) -> None:
        self.instance = "fake-instance"
        self.run_real_relay = run_real_relay
        self.specs: list[AgentContainerSpec] = []
        self.removed: list[tuple[str, int]] = []
        self.exec_envs: list[dict[str, str]] = []

    async def create(self, spec: AgentContainerSpec) -> str:
        self.specs.append(spec)
        return f"cid-{spec.job_id}"

    async def exec(self, job_id: str, argv: list[str], *, env: dict[str, str], limit: int):
        self.exec_envs.append(dict(env))
        spec = next(s for s in self.specs if s.job_id == job_id)
        if not self.run_real_relay:
            # Fast sleep process for concurrency tests
            return await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import time; time.sleep(0.5)",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=limit,
            )
        # Map container paths back to host files created in spec
        host_relay = str(spec.ro_files["/opt/agent/egress_relay.py"])
        host_sock = str(spec.egress_dir / "egress.sock")
        cli_path = Path(argv[4])
        if argv[4] in spec.ro_files:
            host_cli = str(spec.ro_files[argv[4]])
        else:
            host_cli = str(spec.ro_dirs[str(cli_path.parent)] / cli_path.name)
        remapped_cli_args = [
            str(spec.ro_files[arg]) if arg in spec.ro_files else arg
            for arg in argv[5:]
        ]
        # Replace HOME=/home/agent only for running host fake script, while passing proxy env through
        proc_env = {
            **env,
            "KOTOYOSE_TEST_ORIG_HOME": env["HOME"],
            "KOTOYOSE_TEST_EGRESS_SOCK_EXISTS": "1" if Path(host_sock).exists() else "0",
        }
        return await asyncio.create_subprocess_exec(
            sys.executable,
            host_relay,
            host_sock,
            "--",
            host_cli,
            *remapped_cli_args,
            cwd=spec.workspace,
            env=proc_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=limit,
        )

    async def remove(self, job_id: str, grace_seconds: int = 0) -> str:
        self.removed.append((job_id, grace_seconds))
        return "confirmed"


async def test_isolated_worker_executes_through_relay_and_preserves_login_home(
    tmp_path: Path,
) -> None:
    _HOME_SEMAPHORES.clear()
    ws = tmp_path / "workspace"
    ws.mkdir()
    login_home = tmp_path / "persistent-login-home"
    (login_home / ".gemini").mkdir(parents=True)
    auth_marker = login_home / ".gemini" / "auth-marker.json"
    auth_marker.write_text('{"marker": "do-not-delete"}\n', encoding="utf-8")

    fake_agy = _write_fake_cli(
        tmp_path / "bin" / "agy",
        """
        import json, os, sys
        assert os.environ["KOTOYOSE_TEST_ORIG_HOME"] == "/home/agent"
        assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:18080"
        assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:18080"
        assert os.environ["NO_PROXY"] == ""
        assert os.environ["KOTOYOSE_TEST_EGRESS_SOCK_EXISTS"] == "1"
        print(json.dumps({"event": "init", "conversation_id": "c-iso", "init": {"model": "gemini-4-hydrogen-high"}}))
        print(json.dumps({"event": "result", "result": {"conversation_id": "c-iso", "status": "SUCCESS", "response": "isolated ok", "num_turns": 1, "usage": {"input_tokens": 5, "output_tokens": 3}}}))
        """,
    )
    ac_cfg = _make_ac_config(tmp_path)
    image_id = "sha256:" + ("c" * 64)
    fake_container = _FakeAgentContainer(run_real_relay=True)
    worker = PrintCliWorker(container_runtime=(fake_container, ac_cfg, None))

    started_calls: list[tuple[int, dict | None]] = []
    events: list[tuple[str, dict]] = []

    async def on_started(pid: int, *, container_identity: dict | None = None) -> None:
        started_calls.append((pid, container_identity))

    async def on_event(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    job = PrintCliJob(
        profile_id="agy",
        cli_binary=fake_agy,
        model="gemini-4-hydrogen-high",
        workspace=ws,
        login_home=login_home,
        surface="write",
        instructions="Follow rules.",
        prompt="Say isolated ok.",
        output_contract="text",
        timeout_seconds=10.0,
        grace_seconds=1.0,
        output_limit_bytes=32768,
        memory_bytes=128 * 1024 * 1024,
        pids_limit=32,
        cpus=1.0,
        execution_environment="isolated",
        image_id=image_id,
        attempt_id="att-iso-1",
    )

    result = await worker.execute(
        job,
        asyncio.Event(),
        on_process_started=on_started,
        on_event=on_event,
    )

    assert result.status == "succeeded"
    assert result.reason == "completed"
    assert result.content == b"isolated ok"
    assert result.stop_confirmation == "confirmed"
    assert len(started_calls) == 1
    assert started_calls[0][1] is not None
    assert started_calls[0][1]["id"].startswith("cid-")
    assert started_calls[0][1]["instance"] == "fake-instance"
    assert len(fake_container.removed) == 1
    assert fake_container.removed[0][1] == 0

    # Persistent login home must remain untouched on disk; temp egress dir must be removed.
    assert login_home.exists()
    assert auth_marker.read_text(encoding="utf-8") == '{"marker": "do-not-delete"}\n'
    assert not fake_container.specs[0].egress_dir.exists()


async def test_cursor_jail_finishes_after_leader_exit_with_helper_holding_pipes(tmp_path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "login-home"
    workspace.mkdir()
    home.mkdir()
    cli = _write_fake_cli(
        tmp_path / "versions" / "v1" / "cursor-agent",
        """
        import json, os, subprocess, sys
        # A separate process group models helpers that hold both pipes after the CLI exits.
        helper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(6)"],
            process_group=0,
        )
        assert os.getsid(helper.pid) == os.getsid(0)
        print(json.dumps({"type": "result", "subtype": "success", "result": "cursor ok"}), flush=True)
        """,
    )
    container = _FakeAgentContainer()
    worker = PrintCliWorker(container_runtime=(container, _make_ac_config(tmp_path), None))
    job = PrintCliJob(
        profile_id="cursor", cli_binary=cli, model="auto", workspace=workspace, login_home=home,
        surface="read", instructions="", prompt="Hello", output_contract="text",
        timeout_seconds=3, grace_seconds=1, output_limit_bytes=32768,
        memory_bytes=128 * 1024 * 1024, pids_limit=32, cpus=1,
        execution_environment="isolated", image_id="sha256:" + "c" * 64, attempt_id="cursor-helper",
    )

    async def noop(*args, **kwargs):
        pass

    result = await worker.execute(job, asyncio.Event(), on_process_started=noop, on_event=noop)
    assert (result.status, result.content, result.stop_confirmation) == ("succeeded", b"cursor ok", "confirmed")
    assert len(container.removed) == 1


async def test_semaphore_shared_across_host_and_isolated_runs(tmp_path: Path) -> None:
    _HOME_SEMAPHORES.clear()
    ws = tmp_path / "workspace"
    ws.mkdir()
    login_home = tmp_path / "shared-login-home"
    login_home.mkdir()
    slow_cli = _write_fake_cli(
        tmp_path / "bin" / "agy",
        """
        import time
        time.sleep(0.5)
        """,
    )
    ac_cfg = _make_ac_config(tmp_path)
    image_id = "sha256:" + ("d" * 64)
    fake_container = _FakeAgentContainer(run_real_relay=False)
    worker = PrintCliWorker(container_runtime=(fake_container, ac_cfg, image_id))

    async def noop_started(*_args, **_kwargs) -> None:
        return None

    async def noop_event(*_args) -> None:
        return None

    base_kwargs = dict(
        profile_id="agy",
        cli_binary=slow_cli,
        model="gemini-4-hydrogen-high",
        workspace=ws,
        login_home=login_home,
        surface="write",
        instructions="Follow rules.",
        prompt="Wait.",
        output_contract="text",
        timeout_seconds=5.0,
        grace_seconds=0.2,
        output_limit_bytes=32768,
        memory_bytes=128 * 1024 * 1024,
        pids_limit=32,
        cpus=1.0,
    )
    cancel1 = asyncio.Event()
    cancel2 = asyncio.Event()
    host_job = PrintCliJob(**base_kwargs, execution_environment="host", attempt_id="att-h1")
    iso_job = PrintCliJob(**base_kwargs, execution_environment="isolated", image_id=image_id, attempt_id="att-i1")
    third_iso = PrintCliJob(**base_kwargs, execution_environment="isolated", image_id=image_id, attempt_id="att-i2")
    third_host = PrintCliJob(**base_kwargs, execution_environment="host", attempt_id="att-h2")

    t1 = asyncio.create_task(
        worker.execute(host_job, cancel1, on_process_started=noop_started, on_event=noop_event)
    )
    t2 = asyncio.create_task(
        worker.execute(iso_job, cancel2, on_process_started=noop_started, on_event=noop_event)
    )
    try:
        # Yield briefly until both slots (max_concurrent_per_home=2) are acquired
        for _ in range(50):
            sem = _HOME_SEMAPHORES.get("agy", login_home, AGY_PROFILE.max_concurrent_per_home)
            if sem.locked():
                break
            await asyncio.sleep(0.01)

        with pytest.raises(AgentStartRejected) as iso_err:
            await worker.execute(third_iso, asyncio.Event(), on_process_started=noop_started, on_event=noop_event)
        assert iso_err.value.code == "capacity_exhausted"
        assert iso_err.value.status == 409

        with pytest.raises(AgentStartRejected) as host_err:
            await worker.execute(third_host, asyncio.Event(), on_process_started=noop_started, on_event=noop_event)
        assert host_err.value.code == "capacity_exhausted"
        assert host_err.value.status == 409
    finally:
        cancel1.set()
        cancel2.set()
        await asyncio.gather(t1, t2, return_exceptions=True)
