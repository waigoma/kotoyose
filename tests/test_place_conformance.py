"""Adapter × Place conformance tests for env scrubbing, filesystem boundaries, and egress."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from openai_codex import Sandbox
import pytest

from kotoyose import container_claude as cc
from kotoyose import process_env
from kotoyose.agent_container import AgentContainer
from kotoyose.config import Connection, HarnessConfig, ModelPreset
from kotoyose.container_print import build_print_cli_container_plan
from kotoyose.execution_resources import AgentContainerConfig
from kotoyose.jail_place import JailPlace, build_connect_proxy_env
from kotoyose.place_conformance import (
    ADAPTER_PLACE_MATRIX,
    HOST_CONFORMANT_DRIVERS,
    can_claim_host_place,
)
from kotoyose.print_cli_agent import PrintCliJob
from kotoyose.print_cli_egress import PrintCliEgress
from kotoyose.print_cli_profiles import AGY_PROFILE, build_print_cli_env
from kotoyose.runtime.claude_auth import build_auth_env
from kotoyose.runtime.claude_options import build_claude_options
from kotoyose.runtime.codex import _map_sandbox

_IMAGE_ID = "sha256:" + ("c" * 64)

_SENTINEL_SECRETS = {
    "DATABASE_URL": "postgresql://kotoyose:secret@localhost/kotoyose",
    "KOTOYOSE_TOKEN": "sentinel-legacy-bearer-token",
    "OPENAI_API_KEY": "sk\x2dsentinel-openai-key",
    "ANTHROPIC_API_KEY": "sk\x2dant-sentinel-anthropic-key",
    "SENTINEL_SECRET": "sentinel-secret-value",
}


@pytest.fixture
def _scrubbed_sentinel_env(monkeypatch):
    """Populate synthetic secrets, scrub os.environ, and restore cleanly on teardown."""
    snapshot = dict(os.environ)
    process_env._reset_for_tests(restore_environ=False)
    for key, value in _SENTINEL_SECRETS.items():
        monkeypatch.setenv(key, value)
    process_env.scrub(["SENTINEL_SECRET"])
    try:
        yield dict(_SENTINEL_SECRETS)
    finally:
        process_env._reset_for_tests(restore_environ=False)
        for key in list(os.environ.keys()):
            if key not in snapshot:
                os.environ.pop(key, None)
        for key, value in snapshot.items():
            os.environ[key] = value


def test_place_conformance_matrix_restricts_host_to_claude_sdk() -> None:
    """Only claude.agent-sdk has verified host tool disablement; others require jail."""
    assert HOST_CONFORMANT_DRIVERS == frozenset({"claude.agent-sdk"})
    assert can_claim_host_place("claude.agent-sdk") is True
    for driver in ("codex.app-server", "cli.print:agy", "cursor.sdk", "unknown"):
        assert can_claim_host_place(driver) is False

    assert ADAPTER_PLACE_MATRIX["claude.agent-sdk"].places == frozenset({"host", "jail"})
    assert ADAPTER_PLACE_MATRIX["codex.app-server"].places == frozenset({"jail"})
    assert ADAPTER_PLACE_MATRIX["cli.print:agy"].places == frozenset({"jail"})
    assert ADAPTER_PLACE_MATRIX["cursor.sdk"].places == frozenset()


def test_conformance_probe_env_dump_scrubs_secrets_across_adapters_and_places(
    _scrubbed_sentinel_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """Probe 1 (env_dump): scrubbed Kotoyose secrets never leak into host, jail, or child envs."""
    secrets = _scrubbed_sentinel_env
    workspace = tmp_path / "workspace"
    login_home = tmp_path / "login-home"
    egress_dir = tmp_path / "egress"
    settings_dir = tmp_path / "settings"
    for directory in (workspace, login_home, egress_dir, settings_dir):
        directory.mkdir()
    fake_cli = tmp_path / "fake-agy"
    fake_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    # 1. Host claude.agent-sdk (build_claude_options + build_auth_env with subscription config_dir)
    cfg = HarnessConfig(
        connections={
            "claude-sub": Connection(
                provider="claude",
                kind="subscription", data_policy="no_training",
                config_dir=str(login_home),
            )
        },
        models={
            "claude-host-none": ModelPreset(
                provider="claude",
                model="claude-sonnet-4-5",
                cwd=str(workspace),
                tools=[],
                setting_sources=[],
            )
        },
    )
    preset = cfg.preset_for("claude-host-none")
    claude_options = build_claude_options(
        cfg,
        preset,
        model_name="claude-host-none",
        cwd_override=None,
        request_headers={},
        env=build_auth_env(preset, cfg, lambda: "synthetic-oauth-token"),
        session_store=None,
    )
    for key, value in secrets.items():
        assert value not in repr(claude_options.env)
        if key != "ANTHROPIC_API_KEY":
            assert key not in claude_options.env
    assert claude_options.env.get("ANTHROPIC_API_KEY", "") == ""

    # 2. Host cli.print:agy env
    agy_host_env = build_print_cli_env(AGY_PROFILE, login_home)
    for key, value in secrets.items():
        assert key not in agy_host_env
        assert value not in repr(agy_host_env)

    # 3. Jail container_claude env + launcher env (OAuth CONNECT mode)
    container_claude_env = cc._container_env(connect_egress=True)
    launcher_env = cc._launcher_env(
        job_id="att-conf-1",
        secret_value="synthetic-oauth-capability",
        docker_binary="docker",
        credential_name="CLAUDE_CODE_OAUTH_TOKEN",
        connect_egress=True,
    )
    for env_map in (container_claude_env, launcher_env):
        serialized = repr(env_map)
        for key, value in secrets.items():
            assert value not in serialized
            if key != "ANTHROPIC_API_KEY":
                assert key not in env_map
        # Host ANTHROPIC_API_KEY is scrubbed; launcher clears _AUTH_KINDS to "".
        assert launcher_env.get("ANTHROPIC_API_KEY", "") == ""

    # 4. Jail container_codex spec env & AgentContainer argv
    settings = AgentContainerConfig(
        agent_root="/opt/agent-root",
        home_root=str(tmp_path),
        workspace_memory_bytes=2 * 1024**3,
        cpus=1.0,
        pids_limit=128,
    )
    place = JailPlace(container=None, settings=settings, image_id=_IMAGE_ID)
    codex_bin = tmp_path / "codex"
    codex_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    relay_file = tmp_path / "relay.py"
    relay_file.write_text("# relay\n", encoding="utf-8")
    codex_spec = place.build_spec(
        job_id="codex-conf-1",
        workspace=workspace,
        home=login_home,
        egress_dir=egress_dir,
        timeout_seconds=30.0,
        grace_seconds=2.0,
        env={"CODEX_HOME": "/home/agent/.codex"},
        ro_files={"/opt/agent/bin/codex": codex_bin, "/opt/agent/egress_relay.py": relay_file},
        workspace_readonly=True,
    )
    for key, value in secrets.items():
        assert key not in codex_spec.env
        assert value not in repr(codex_spec.env)

    # 5. Jail container_print plan env
    print_job = PrintCliJob(
        profile_id=AGY_PROFILE.id,
        cli_binary=fake_cli,
        model="gemini-2.5-pro",
        workspace=workspace,
        login_home=login_home,
        surface="read",
        instructions="Check conformance.",
        prompt="hello",
        output_contract="text",
        timeout_seconds=30.0,
        grace_seconds=2.0,
        output_limit_bytes=65536,
        memory_bytes=1024**3,
        pids_limit=64,
        cpus=1.0,
    )
    print_spec, _argv, print_exec_env, _cli_args = build_print_cli_container_plan(
        print_job,
        AGY_PROFILE,
        settings,
        image_id=_IMAGE_ID,
        egress_dir=egress_dir,
        settings_dir=settings_dir,
        print_timeout_seconds=20.0,
        job_id="print-conf-1",
    )
    for env_map in (print_spec.env, print_exec_env):
        serialized = repr(env_map)
        for key, value in secrets.items():
            assert key not in env_map
            assert value not in serialized

    # 6. Real local child process spawned with build_minimal_env()
    proc = subprocess.run(
        [sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"],
        env=process_env.build_minimal_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    dumped = json.loads(proc.stdout)
    dumped_serialized = repr(dumped)
    for key, value in secrets.items():
        assert key not in dumped
        assert value not in dumped_serialized


def test_conformance_probe_host_file_read_and_jail_mounts(tmp_path: Path) -> None:
    """Probe 2 (host_file_read): only claude disables host tools; jail locks root & workspace."""
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    egress_dir = tmp_path / "egress"
    cli = tmp_path / "cli"
    for directory in (workspace, home, egress_dir):
        directory.mkdir()
    cli.write_text("#!/bin/sh\n", encoding="utf-8")

    # Claude on host with tools=[] and setting_sources=[] disables all built-in host tools.
    cfg = HarnessConfig(
        models={
            "claude-host-none": ModelPreset(
                provider="claude",
                model="claude-sonnet-4-5",
                cwd=str(workspace),
                tools=[],
                setting_sources=[],
            )
        }
    )
    claude_options = build_claude_options(
        cfg,
        cfg.preset_for("claude-host-none"),
        model_name="claude-host-none",
        cwd_override=None,
        request_headers={},
        env={},
        session_store=None,
    )
    assert claude_options.tools == []
    assert claude_options.setting_sources == []
    assert can_claim_host_place("claude.agent-sdk") is True

    # Codex read-only still retains host file-read / read-only command capability.
    assert _map_sandbox("read-only", {}) == Sandbox.read_only
    assert can_claim_host_place("codex.app-server") is False

    # agy has no `none` surface and `--mode plan` still reads host files.
    assert "none" not in AGY_PROFILE.surfaces
    assert AGY_PROFILE.surfaces["read"].extra_args == ("--mode", "plan")
    assert can_claim_host_place("cli.print:agy") is False

    # JailPlace + AgentContainer creates a read-only root without host / or host ~.
    settings = AgentContainerConfig(
        agent_root="/opt/agent-root",
        home_root=str(tmp_path),
        workspace_memory_bytes=2 * 1024**3,
        cpus=1.0,
        pids_limit=128,
    )
    place = JailPlace(container=None, settings=settings, image_id=_IMAGE_ID)
    ro_spec = place.build_spec(
        job_id="jail-ro-1",
        workspace=workspace,
        home=home,
        egress_dir=egress_dir,
        timeout_seconds=30.0,
        grace_seconds=2.0,
        env={},
        ro_files={"/opt/agent/bin/cli": cli},
        workspace_readonly=True,
    )
    argv_ro = AgentContainer(instance="kotoyose")._create_argv(ro_spec)
    assert "--read-only" in argv_ro
    assert "--cap-drop" in argv_ro and argv_ro[argv_ro.index("--cap-drop") + 1] == "ALL"
    assert "--security-opt" in argv_ro and argv_ro[argv_ro.index("--security-opt") + 1] == "no-new-privileges"
    assert f"type=bind,src={workspace},dst=/workspace,readonly" in argv_ro
    assert f"type=bind,src={home},dst=/home/agent" in argv_ro
    mount_sources = [
        item.split("src=", 1)[1].split(",", 1)[0]
        for item in argv_ro
        if item.startswith("type=bind,src=")
    ]
    assert "/" not in mount_sources
    assert str(Path.home()) not in mount_sources

    rw_spec = place.build_spec(
        job_id="jail-rw-1",
        workspace=workspace,
        home=home,
        egress_dir=egress_dir,
        timeout_seconds=30.0,
        grace_seconds=2.0,
        env={},
        ro_files={"/opt/agent/bin/cli": cli},
        workspace_readonly=False,
    )
    argv_rw = AgentContainer(instance="kotoyose")._create_argv(rw_spec)
    assert "--read-only" in argv_rw
    assert f"type=bind,src={workspace},dst=/workspace" in argv_rw


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_name", "allowed_hosts"),
    [
        ("claude", cc._CLAUDE_CONNECT_HOSTS),
        ("agy", AGY_PROFILE.egress_hosts),
    ],
)
async def test_conformance_probe_egress_connection_blocks_disallowed_targets(
    tmp_path: Path,
    adapter_name: str,
    allowed_hosts: tuple[str, ...],
) -> None:
    """Probe 3 (egress_connection): jail uses --network none and broker blocks disallowed targets."""
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    egress_dir = tmp_path / "egress"
    cli = tmp_path / "cli"
    for directory in (workspace, home, egress_dir):
        directory.mkdir()
    cli.write_text("#!/bin/sh\n", encoding="utf-8")

    settings = AgentContainerConfig(
        agent_root="/opt/agent-root",
        home_root=str(tmp_path),
        workspace_memory_bytes=2 * 1024**3,
        cpus=1.0,
        pids_limit=128,
    )
    spec = JailPlace(container=None, settings=settings, image_id=_IMAGE_ID).build_spec(
        job_id=f"egress-{adapter_name}",
        workspace=workspace,
        home=home,
        egress_dir=egress_dir,
        timeout_seconds=30.0,
        grace_seconds=2.0,
        env={},
        ro_files={"/opt/agent/bin/cli": cli},
    )
    argv = AgentContainer(instance="kotoyose")._create_argv(spec)
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"

    proxy_env = build_connect_proxy_env({"LANG": "C.UTF-8"})
    assert proxy_env["HTTPS_PROXY"] == "http://127.0.0.1:18080"
    assert proxy_env["HTTP_PROXY"] == "http://127.0.0.1:18080"
    assert proxy_env["NO_PROXY"] == ""

    async def private_resolver(_host: str, port: int) -> list[tuple[str, int]]:
        return [("127.0.0.1", port)]

    allowed_host = allowed_hosts[0]
    with tempfile.TemporaryDirectory(prefix="pc-", dir="/tmp") as sock_dir:
        sock_path = Path(sock_dir) / "egress.sock"
        broker = PrintCliEgress(
            sock_path,
            allowed_hosts=allowed_hosts,
            allowed_ports=(443,),
            run_id=f"conformance-{adapter_name}",
            allow_private=False,
            resolver=private_resolver,
        )
        await broker.start()
        try:
            for connect_request in (
                b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n",
                b"CONNECT 127.0.0.1:443 HTTP/1.1\r\nHost: 127.0.0.1:443\r\n\r\n",
                f"CONNECT {allowed_host}:8443 HTTP/1.1\r\nHost: {allowed_host}:8443\r\n\r\n".encode("ascii"),
                f"CONNECT {allowed_host}:443 HTTP/1.1\r\nHost: {allowed_host}:443\r\n\r\n".encode("ascii"),
            ):
                reader, writer = await asyncio.open_unix_connection(str(sock_path))
                try:
                    writer.write(connect_request)
                    await writer.drain()
                    response = await asyncio.wait_for(reader.read(4096), timeout=2.0)
                finally:
                    writer.close()
                    await writer.wait_closed()
                assert response.startswith(b"HTTP/1.1 403 Forbidden\r\n"), connect_request
        finally:
            await broker.close()
