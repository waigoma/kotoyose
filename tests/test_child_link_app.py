"""Rust link app against the Python host and the actual IPC runner."""

import asyncio
import base64
from contextlib import asynccontextmanager
import ctypes
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
from signal import SIGHUP, SIGKILL, SIGSTOP, SIGTERM
import shutil
import subprocess
import sys
import tarfile
from textwrap import dedent
from urllib.parse import urlsplit
from zipfile import ZipFile

import httpx
import pytest

from kotoyose import child_update
from kotoyose.child_catalog import component_rows
from kotoyose.child_client import enroll_child
from kotoyose.child_lock import child_lock
from kotoyose.child_protocol import current_package_version, decode_frame, encode_frame
from kotoyose.cli_install_spec import platform_key
from kotoyose.provider_data_runtime import LANDLOCK_PLATFORMS
from kotoyose.child_relay import ChildStream, ChildStreamError, fetch_child_login_status, fetch_child_quota
from kotoyose.child_server import ChildHub, _server_context
from kotoyose.config import HarnessConfig
from kotoyose.runtime.base import RuntimeEvent, RuntimeResult, RuntimeTurnError
from kotoyose.runtime.child import ChildRuntimeSession
from kotoyose.session import SDKConnectionError
from tests.test_child_k1 import _retarget_fingerprint
from tests.test_child_k3 import _host_config, _policy

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux PTY, subreaper and /proc conformance")


@pytest.fixture(scope="module")
def binary():
    cargo = shutil.which("cargo")
    if cargo is None:
        candidate = Path.home() / ".cargo" / "bin" / "cargo"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            cargo = str(candidate)
    if cargo is None:
        pytest.skip("cargo is not installed")
    target = ROOT / "child-app" / "target"
    result = subprocess.run(
        [cargo, "build", "--locked", "--release", "--target-dir", str(target)],
        cwd=ROOT / "child-app", capture_output=True, text=True, timeout=240,
        env={**os.environ, "KOTOYOSE_CHILD_BUILD_ID": "a" * 40},
    )
    assert result.returncode == 0, result.stderr
    return target / "release" / "kotoyose-child-app"


@pytest.fixture
async def host(tmp_path):
    hub = ChildHub(tmp_path / "host", heartbeat_interval=0.15)
    await hub.start("127.0.0.1", 0)
    try:
        yield hub
    finally:
        await hub.stop()


@pytest.fixture
def child_env(tmp_path, monkeypatch):
    os_home = tmp_path / "os-home"
    os_home.mkdir()
    monkeypatch.setenv("HOME", str(os_home))
    home = tmp_path / "child"
    python = tmp_path / "venv" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    stub = ROOT / "tests" / "fixtures" / "child_runner_stub.py"
    command = shlex.join([sys.executable, "-I", str(stub)])
    python.write_text(f'#!/bin/sh\nexport KOTOYOSE_STUB_PYTHON="$0"\nexec {command} "$@"\n')
    python.chmod(0o700)
    return home, {**os.environ, "KOTOYOSE_CHILD_HOME": str(home), "KOTOYOSE_CHILD_PYTHON": str(python)}


async def eventually(predicate, *, timeout=20):
    async with asyncio.timeout(timeout):
        while not (value := predicate()):
            await asyncio.sleep(0.02)
        return value


async def invoke(binary, env, *args):
    proc = await asyncio.create_subprocess_exec(
        binary, *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), 12)
        return proc.returncode, stdout.decode(), stderr.decode()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@asynccontextmanager
async def running(binary, env, *args, capture_url=False):
    proc = await asyncio.create_subprocess_exec(
        binary, *(args if capture_url else args or ("run",)), env=env,
        stdout=asyncio.subprocess.PIPE if capture_url else None, stderr=asyncio.subprocess.PIPE,
    )
    lines = []

    async def drain():
        async for line in proc.stderr:
            lines.append(line.decode().strip())

    reader = asyncio.create_task(drain())
    try:
        yield proc, lines
    finally:
        if proc.returncode is None:
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), 8)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            await reader


def issue(host):
    return host.issue_enrollment_code(f"127.0.0.1:{host.port}")


def pids(path):
    return [int(value) for value in path.read_text().split()]


def reaped(*process_ids):
    return all(not Path(f"/proc/{pid}").exists() for pid in process_ids)


def child_turn(config, cwd, conversation):
    return ChildRuntimeSession(
        name="dev", provider="claude", alias="dev", config=config, preset=config.models["dev"],
        cwd=str(cwd), conversation_key=conversation,
    )


async def read_settings_url(proc):
    url = urlsplit((await asyncio.wait_for(proc.stdout.readline(), 5)).decode().strip())
    assert url.scheme == "http" and url.hostname == "127.0.0.1"
    assert url.path == "/" and not url.query and len(url.fragment) == 43
    return url


@pytest.mark.parametrize("program", ["kotoyose-child-app", "kotoyose-child-background"])
async def test_no_arguments_starts_unregistered_child_on_enrollment_page(binary, child_env, program):
    home, env = child_env
    async with running(binary.with_name(program), env, capture_url=True) as (proc, lines):
        url = await read_settings_url(proc)
        async with httpx.AsyncClient(base_url=f"http://{url.netloc}", trust_env=False) as client:
            origin = {"Origin": str(client.base_url).rstrip("/")}
            session = await client.post("/api/session", headers=origin, json={"code": url.fragment})
            assert session.status_code == 200
            token = session.json()["token"]
            assert (await client.post("/api/session", headers=origin, json={"code": url.fragment})).status_code == 401
            status = await client.get("/api/status", headers={"Authorization": f"Bearer {token}"})
            assert status.json()["link"]["connection"] == "unregistered"
        assert proc.returncode is None
        assert not any("Usage:" in line for line in lines)
        logs = (home / "logs" / "child.log").read_text()
        assert "child is not enrolled" in logs
        assert url.fragment not in logs and token not in logs
        assert not (home / "starts").exists()


async def test_second_start_signals_existing_child(binary, host, child_env):
    home, env = child_env
    await enroll_child(issue(host), home, name="dev")
    async with running(binary, env, "run", capture_url=True) as (proc, _):
        live = await eventually(lambda: host.live_for("dev"))
        rc, stdout, stderr = await invoke(binary, env)
        assert rc == 0, stderr
        assert not stdout
        first = await read_settings_url(proc)
        rc, _, stderr = await invoke(binary, env)
        assert rc == 0, stderr
        second = await read_settings_url(proc)
        assert first.netloc == second.netloc and first.fragment != second.fragment
        assert proc.returncode is None and host.live_for("dev") is live


async def test_ui_starts_settings_and_link_for_an_enrolled_child(binary, host, child_env):
    home, env = child_env
    await enroll_child(issue(host), home, name="dev")
    async with running(binary, env, "ui", capture_url=True) as (proc, _):
        url = await read_settings_url(proc)
        await eventually(lambda: host.live_for("dev"))
        async with httpx.AsyncClient(base_url=f"http://{url.netloc}", trust_env=False) as client:
            page = await client.get("/")
            assert page.status_code == 200 and "/console.css" in page.text
            assert "default-src 'none'" in page.headers["Content-Security-Policy"]
            style = await client.get("/console.css")
            assert style.status_code == 200 and "--font-ui" in style.text
            origin = {"Origin": str(client.base_url).rstrip("/")}
            session = await client.post("/api/session", headers=origin, json={"code": url.fragment})
            assert session.status_code == 200 and session.json()["token"]
        assert not (home / "starts").exists()


async def test_linux_source_has_no_offer_and_preserves_local_guards(binary, host, child_env, tmp_path):
    from tests.test_child_update import staged

    home, env = child_env
    dist = tmp_path / "dist"
    platform = platform_key()
    metadata = staged(dist, platform=platform, build="b" * 40)
    host.update_dir = tmp_path / "updates"
    child_update.publish(dist, host.update_dir, metadata["version"], platform)
    await enroll_child(issue(host), home, name="dev")
    async with running(binary, env, "ui", capture_url=True) as (proc, _):
        url = await read_settings_url(proc)
        async with httpx.AsyncClient(base_url=f"http://{url.netloc}", trust_env=False) as client:
            origin = {"Origin": str(client.base_url).rstrip("/")}
            session = await client.post("/api/session", headers=origin, json={"code": url.fragment})
            headers = {**origin, "Authorization": f"Bearer {session.json()['token']}"}
            for _ in range(100):
                status = (await client.get("/api/status", headers=headers)).json()
                if status["link"]["connection"] == "connected":
                    break
                await asyncio.sleep(0.02)
            assert status["link"]["connection"] == "connected"
            assert host.list_children()[0]["update_offer"] is None
            assert child_update.FEATURE not in host.hello_payloads[-1]["features"]
            assert status["build_id"] == "a" * 40
            assert status["update"]["offer"] is None
            assert not status["can_update"] and not status["restart_needed"]
            request = {"action": "download", "sha256": metadata["sha256"], "defer": False}
            assert (await client.post("/api/update", headers=origin, json=request)).status_code == 401
            assert (await client.post("/api/update", headers={**headers, "Origin": "http://localhost"}, json=request)).status_code == 403
            assert (await client.post("/api/update", headers={**headers, "Host": "localhost"}, json=request)).status_code == 403
            assert (await client.post("/api/update", headers=headers, json=request)).status_code == 409
            assert (await client.post("/api/action", headers=headers, json={"restart": True})).status_code == 409
        assert not list(home.glob("update-*.tar.gz"))


@asynccontextmanager
async def update_settings(url):
    async with httpx.AsyncClient(base_url=f"http://{url.netloc}", trust_env=False) as client:
        origin = {"Origin": str(client.base_url).rstrip("/")}
        token = (await client.post("/api/session", headers=origin, json={"code": url.fragment})).json()["token"]
        client.headers.update({**origin, "Authorization": f"Bearer {token}"})
        yield client


def packaged_child(folder, binary, env, *, different_build=False, fail_pip=False, provider_pip=""):
    folder.mkdir()
    (folder / "bin" / "parts").mkdir(parents=True)
    (folder / "bin" / "parts" / "kotoyose-fixture.whl").touch()
    (folder / "bin" / "parts" / "requirements.txt").touch()
    data = binary.read_bytes()
    marker = re.search(rb"KOTOYOSE_CHILD_BUILD:([a-f0-9]{40}(?:-dirty-[a-f0-9]{12})?)\x00", data)
    assert marker is not None
    build = marker[1]
    if different_build:
        new_build = (b"f" if build.startswith(b"0") else b"0") + build[1:]
        data = data[:marker.start(1)] + new_build + data[marker.end(1):]
        build = new_build
    app = folder / "bin" / "kotoyose-child-app"
    app.write_bytes(data)
    app.chmod(0o700)
    python = folder / "python" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text(
        f'#!/bin/sh\nif [ "${{4:-}}" = pip ]; then\n{provider_pip}\nexit {int(fail_pip)}\nfi\n'
        f'exec {shlex.quote(env["KOTOYOSE_CHILD_PYTHON"])} "$@"\n'
    )
    python.chmod(0o700)
    shutil.copy2(ROOT / "child-app" / "installer" / "install-linux.sh", folder / "install.sh")
    return build.decode()


def restore_from_fixture_index(index, home):
    index.mkdir()
    wheel = index / "claude_agent_sdk-9.9.0-py3-none-any.whl"
    metadata = "claude_agent_sdk-9.9.0.dist-info"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(f"{metadata}/METADATA", "Metadata-Version: 2.1\nName: claude-agent-sdk\nVersion: 9.9.0\n")
        archive.writestr(f"{metadata}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(f"{metadata}/RECORD", "")
    requirement = "claude-agent-sdk==9.9.0 --hash=sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
    pip = dedent(f"""\
        set -eu
        for arg do requirements=$arg; done
        case "$requirements" in
            */requirements.txt|*.whl) ;;
            */requirements-claude.txt)
                case " $* " in
                    *" --require-hashes "*" --index-url https://pypi.org/simple "*) ;;
                    *) exit 7 ;;
                esac
                test "$(cat "$requirements")" = {shlex.quote(requirement)}
                echo claude >> {shlex.quote(str(home / "provider-install-calls"))}
                while [ ! -f {shlex.quote(str(home / "allow-provider-install"))} ]; do sleep 0.05; done
                site="${{0%/bin/python3}}/lib/python3.13/site-packages"
                {shlex.quote(sys.executable)} -I -m zipfile -e {shlex.quote(str(wheel))} "$site"
                echo 9.9.0 > {shlex.quote(str(home / "fake-sdk-claude"))}
                ;;
            *) exit 7 ;;
        esac
    """)
    return requirement, pip


@pytest.mark.parametrize("outcome", ["updated", "repair_failed", "link_lost"])
async def test_linux_applies_verified_package_or_keeps_old_running_build(binary, host, child_env, tmp_path, outcome, monkeypatch):
    from tests.test_child_update import staged

    home, env = child_env
    version = current_package_version()
    first = tmp_path / f"kotoyose-child-{version}"
    old_build = packaged_child(first, binary, env)
    code, _, error = await invoke("/bin/sh", env, str(first / "install.sh"))
    assert code == 0, error
    assert not home.exists()
    package_root = Path(env["HOME"]) / ".local" / "share" / "kotoyose-child-app"
    launcher = Path(env["HOME"]) / ".local" / "bin" / "kotoyose-child-app"
    pointer = package_root / "current"
    previous = pointer.resolve()
    assert previous.name == f"{version}-{old_build}"

    folder = tmp_path / "next"
    provider_pip = ""
    if outcome == "updated":
        (previous / "python/lib/python3.13/site-packages/claude_agent_sdk-9.8.0.dist-info").mkdir(parents=True)
        requirement, provider_pip = restore_from_fixture_index(tmp_path / "wheel-index", home)
    new_build = packaged_child(folder, binary, env, different_build=True, fail_pip=outcome == "repair_failed", provider_pip=provider_pip)
    if outcome == "updated":
        (folder / "bin/parts/requirements-claude.txt").write_text(requirement + "\n")
    archive = tmp_path / "archive.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.USTAR_FORMAT) as stream:
        stream.add(folder, arcname=f"kotoyose-child-{version}")
    dist = tmp_path / "dist"
    platform = platform_key()
    metadata = staged(dist, archive.read_bytes(), platform=platform, version=version, build=new_build)
    host.update_dir = tmp_path / "updates"
    child_update.publish(dist, host.update_dir, version, platform, allow_dirty=True)
    await enroll_child(issue(host), home, name="dev")
    if outcome == "updated":
        _policy(home, tmp_path, adapters=["claude.agent-sdk", "codex.app-server"], mcp={})
        (home / "fake-sdk-codex").write_text("1")
    stale_extract = previous.parent / ".install-stale"
    stale_extract.mkdir()

    if outcome == "link_lost":
        async def lose_link(_publication, send):
            with archive.open("rb") as source:
                data = base64.b64encode(source.read(32)).decode()
            await send("app_update_chunk", {"offset": 0, "data": data})
            await host.live_for("dev").ws.close()

        monkeypatch.setattr(child_update.Publication, "stream", lose_link)

    async with running(launcher, env, "ui", capture_url=True) as (proc, _):
        async with update_settings(await read_settings_url(proc)) as client:
            async with asyncio.timeout(8):
                while True:
                    status = (await client.get("/api/status")).json()
                    if status["update"]["offer"] is not None:
                        break
                    await asyncio.sleep(0.02)
            assert status["update"]["offer"] == metadata
            assert status["can_update"] and status["build_id"] == old_build
            assert not status["restart_needed"]
            assert not stale_extract.exists()
            request = {
                "action": "download", "sha256": metadata["sha256"], "defer": False,
            }
            if outcome == "updated":
                async with client.stream(
                    "POST", "/api/update", json=request, headers={"Accept": "application/x-ndjson"},
                ) as progress:
                    assert progress.status_code == 202
                    views = [json.loads(line) async for line in progress.aiter_lines()]
                received = [view["received"] for view in views if view["phase"] == "downloading"]
                assert any(0 < count < metadata["size"] for count in received)
                assert received == sorted(received) and received[-1] == metadata["size"]
                assert list(dict.fromkeys(view["phase"] for view in views)) == [
                    "requested", "downloading", "verifying", "waiting", "installing",
                ]
                assert views[-1]["expected_shutdown"] and views[-1]["offer"] == metadata
                acknowledged = await client.post("/api/update", json={"action": "acknowledge"})
                assert acknowledged.status_code == 204
            else:
                started = await client.post("/api/update", json=request)
                assert started.status_code == 202
                error = "disconnect" if outcome == "link_lost" else "setup"
                async with asyncio.timeout(8):
                    while True:
                        status = (await client.get("/api/status")).json()
                        if status["update"]["phase"] == "idle" and status["update"]["error"] == error:
                            break
                        await asyncio.sleep(0.02)
                assert status["build_id"] == old_build and pointer.resolve() == previous
                assert not (previous.parent / f"{version}-{new_build}").exists()
                assert not list(home.glob("update-download-*"))
                return

        async with update_settings(await read_settings_url(proc)) as client:
            status = (await client.get("/api/status")).json()
            assert status["build_id"] == new_build and not status["restart_needed"]
            calls = home / "provider-install-calls"
            await eventually(calls.is_file)
            marker = pointer / "python/bin/claude.needs-reinstall"
            assert marker.is_file()
            components = (await client.get("/api/components")).json()
            assert components["auto_repair"] == {"claude": "installing"}
            link = await eventually(lambda: live if (live := host.live_for("dev")) and live.build_id == new_build else None)
            assert host.hello_payloads[-1]["adapters"] == ["codex.app-server"]
            stream_id = host.allocate_stream()
            queue = link.mux.queue(stream_id)
            try:
                assert await host.send_frame("dev", stream_id, "turn", {"route": {"driver": "claude.agent-sdk"}})
                refused = await asyncio.wait_for(queue.get(), 3)
                assert refused.kind == "error" and "needs reinstalling" in refused.payload["message"]
            finally:
                link.mux.discard(stream_id)
            heartbeat = link.last_mono
            await eventually(lambda: link.last_mono > heartbeat, timeout=3)
            await host.disconnect("dev")
            reconnected = await eventually(lambda: live if (live := host.live_for("dev")) and live is not link else None)
            assert host.hello_payloads[-1]["adapters"] == ["codex.app-server"]
            assert marker.is_file()
            (home / "allow-provider-install").touch()
            await eventually(lambda: (
                (live := host.live_for("dev")) and live is not reconnected
                and host.hello_payloads[-1]["adapters"] == ["claude.agent-sdk", "codex.app-server"]
            ))
            assert not marker.exists()
            components = (await client.get("/api/components")).json()
            assert components["auto_repair"] == {"claude": "done"}
            assert components["error"] is None
            assert (pointer / "python/lib/python3.13/site-packages/claude_agent_sdk-9.9.0.dist-info/METADATA").is_file()
            assert calls.read_text().splitlines() == ["claude"]
        assert proc.returncode is None
        assert pointer.resolve().name == f"{version}-{new_build}"
        assert previous.is_dir()
        assert not (pointer / ".kotoyose-child-installing").exists()
        assert not list(home.glob("update-download-*"))


@pytest.mark.parametrize("manager_available", [False, True])
async def test_linux_install_optionally_enables_a_user_service(binary, child_env, tmp_path, manager_available):
    home, env = child_env
    config = tmp_path / "user-config"
    mock_bin = tmp_path / "mock-bin"
    mock_bin.mkdir()
    calls = tmp_path / "systemctl-calls"
    systemctl = mock_bin / "systemctl"
    systemctl.write_text(
        f'#!/bin/sh\nprintf "%s\n" "$*" >> {shlex.quote(str(calls))}\n'
        'case "$2" in\n'
        f'  show-environment) exit {int(not manager_available)} ;;\n'
        '  is-enabled) echo not-found; exit 4 ;;\n'
        '  daemon-reload|enable) exit 0 ;;\n'
        '  *) exit 2 ;;\n'
        'esac\n'
    )
    systemctl.chmod(0o700)
    env = {**env, "XDG_CONFIG_HOME": str(config), "PATH": f"{mock_bin}:{env['PATH']}"}
    package = tmp_path / f"kotoyose-child-{current_package_version()}"
    packaged_child(package, binary, env)
    code, output, error = await invoke("/bin/sh", env, str(package / "install.sh"), "--systemd")
    assert code == 0, error
    assert not home.exists()
    unit = config / "systemd/user/kotoyose-child.service"
    if manager_available:
        assert calls.read_text().splitlines() == [
            "--user show-environment", "--user is-enabled kotoyose-child.service",
            "--user daemon-reload", "--user enable kotoyose-child.service",
        ]
        assert unit.read_text().startswith("# managed-by kotoyose\n")
        assert "ExecStart=%h/.local/share/kotoyose-child-app/current/bin/kotoyose-child-app run" in unit.read_text()
        assert "systemctl --user start kotoyose-child.service" in output
    else:
        assert calls.read_text().splitlines() == ["--user show-environment"]
        assert not unit.exists()
        assert "~/.local/bin/kotoyose-child-app run" in output


async def test_restart_uses_the_build_installed_in_current(binary, child_env, tmp_path):
    home, env = child_env
    version = current_package_version()
    releases = Path(env["HOME"]) / ".local" / "share" / "kotoyose-child-app" / "versions"
    releases.mkdir(parents=True)
    first = tmp_path / "first"
    second = tmp_path / "second"
    old_build = packaged_child(first, binary, env)
    new_build = packaged_child(second, binary, env, different_build=True)
    for tree, build in [(first, old_build), (second, new_build)]:
        (tree / ".kotoyose-child-install").write_text(build)
        (tree / "python/bin/kotoyose-ready").touch()
        tree.rename(releases / f"{version}-{build}")
    current = releases.parent / "current"
    current.symlink_to(releases / f"{version}-{old_build}")
    app = current / "bin" / "kotoyose-child-app"

    async with running(app, env, "ui", capture_url=True) as (proc, _):
        async with update_settings(await read_settings_url(proc)) as client:
            assert not (await client.get("/api/status")).json()["restart_needed"]
            replacement = current.with_name("next")
            replacement.symlink_to(releases / f"{version}-{new_build}")
            replacement.replace(current)
            assert (await client.get("/api/status")).json()["restart_needed"]
            result = await client.post("/api/action", json={"restart": True})
            assert result.status_code == 200
        async with update_settings(await read_settings_url(proc)) as client:
            after = (await client.get("/api/status")).json()
            assert after["build_id"] == new_build
            assert not after["restart_needed"]
        assert proc.returncode is None


async def test_workspace_policy_save_advertises_new_policy_on_reconnect(binary, host, child_env, tmp_path):
    home, env = child_env
    await enroll_child(issue(host), home, name="dev")
    workspace = tmp_path / "work"
    workspace.mkdir()
    async with running(binary, env, "ui", capture_url=True) as (proc, _):
        url = await read_settings_url(proc)
        await eventually(lambda: host.live_for("dev"))
        assert host.list_children()[0]["workspace_policy"] == {
            "workspace_roots": [], "codex_sandbox_max": "workspace-write",
        }
        hello_count = host.hello_count
        async with httpx.AsyncClient(base_url=f"http://{url.netloc}", trust_env=False) as client:
            origin = {"Origin": str(client.base_url).rstrip("/")}
            session = await client.post("/api/session", headers=origin, json={"code": url.fragment})
            client.headers.update({**origin, "Authorization": f"Bearer {session.json()['token']}"})
            current = (await client.get("/api/policy")).json()
            summary = {"workspace_roots": [str(workspace)], "codex_sandbox_max": "read-only"}
            saved = await client.post("/api/policy", json={
                "revision": current["revision"],
                "policy": {**current["policy"], **summary, "workspace_roots": [f' "{workspace}" ']},
            })
            assert saved.status_code == 200, saved.text
            assert not (await client.get("/api/status")).json()["restart_needed"]
        await eventually(lambda: host.hello_count > hello_count and host.live_for("dev"))
        assert host.list_children()[0]["workspace_policy"] == summary


async def test_codex_hello_requires_sdk_and_selected_cli(binary, host, child_env, tmp_path):
    home, env = child_env
    system_path = tmp_path / "system-cli"
    system_path.mkdir()
    env["PATH"] = str(system_path)
    driver = "codex.app-server"
    _policy(home, tmp_path, adapters=[driver])
    status, _, stderr = await invoke(binary, env, "enroll", issue(host), "--name", "dev")
    assert status == 0, stderr
    sites = tmp_path / "venv/lib/python3/site-packages"
    package = sites / "codex_cli_bin"
    (package / "bin").mkdir(parents=True)
    (package / "codex-package.json").write_text("{}")
    wheel_cli = package / "bin/codex"
    system_cli = system_path / "codex"

    async def advertised(expected):
        async with running(binary, env):
            await eventually(lambda: host.live_for("dev"))
            assert host.hello_payloads[-1]["adapters"] == expected
            assert not (home / "starts").exists()
        await eventually(lambda: host.live_for("dev") is None)

    wheel_cli.touch(mode=0o700)
    await advertised([])
    (sites / "openai_codex-0.170.0.dist-info").mkdir()
    await advertised([driver])
    (home / "app.json").write_text('{"codex_cli":"system"}')
    await advertised([])
    system_cli.touch(mode=0o700)
    await advertised([driver])
    _policy(home, tmp_path, adapters=[])
    await advertised([])


async def test_local_llm_turn_through_rust_and_python_runner(binary, host, child_env):
    from kotoyose.child_local_llm import LOCAL_LLM_UI_PORT_FILE
    from tests.test_child_local_llm import _chat_endpoint, _client, _config, _write_policy

    home, env = child_env
    env["KOTOYOSE_CHILD_PYTHON"] = sys.executable
    await enroll_child(issue(host), home, name="dev")
    async with _chat_endpoint() as (url, requests, _):
        _write_policy(home, {"desk": {"url": url, "models": ["tiny"]}})
        async with running(binary, env, "run", capture_url=True) as (proc, _):
            live = await eventually(lambda: host.live_for("dev"))
            assert live.local_llm == {"desk": ("tiny",)}
            assert url not in json.dumps(host.list_children())
            client = await _client(HarnessConfig.model_validate(_config()))
            result = await asyncio.wait_for(client.ask("text from the host"), 10)
            assert result.text == "hello"
            rc, _, stderr = await invoke(binary, env, "ui")
            assert rc == 0, stderr
            settings = await read_settings_url(proc)
            assert (home / LOCAL_LLM_UI_PORT_FILE).read_text() == str(settings.port)
            _write_policy(home, {"desk": {"url": f"http://{settings.netloc}/v1", "models": ["tiny"]}})
            with pytest.raises(RuntimeTurnError, match="settings UI or control-plane port"):
                await asyncio.wait_for(client.ask("stale catalog"), 10)
        assert not (home / LOCAL_LLM_UI_PORT_FILE).exists()
    [(line, headers, body)] = requests
    assert line == "POST /v1/chat/completions HTTP/1.1"
    assert "authorization" not in headers
    assert body["messages"] == [{"role": "user", "content": "text from the host"}]


async def test_enroll_and_stream_through_on_demand_runner(binary, host, child_env, tmp_path, monkeypatch):
    home, env = child_env
    _policy(home, tmp_path)
    policy = home / "policy.json"
    policy.write_text(json.dumps({**json.loads(policy.read_text()), "logins": ["claude"]}))
    (home / "app.json").write_text('{"runner_idle_seconds": 1}')
    fake_cli = home / "fake-cli"
    fake_cli.write_text(f"#!{sys.executable}\n" + dedent("""
        import json
        import sys

        if sys.argv[1:] == ["auth", "login", "--claudeai"]:
            assert sys.stdin.isatty()
            print("https://claude.ai/authorize?state=fake", flush=True)
            assert input() == "fake-code"
        elif sys.argv[1:] == ["auth", "status", "--json"]:
            print(json.dumps({"loggedIn": True}))
        elif sys.argv[1:] == ["-p", "/usage", "--output-format", "json"]:
            print(json.dumps({"result": "Current session: 24% used · resets 11:05pm (UTC)"}))
        else:
            raise SystemExit(2)
    """))
    fake_cli.chmod(0o700)
    config = HarnessConfig.model_validate(_host_config(tmp_path))
    asset = Path(config.skill_definitions["demo"].plugin) / "skills/demo/large-fixture.bin"
    asset.write_bytes(b"\xff" * (800 * 1024))

    code = issue(host)
    wrong_pin = _retarget_fingerprint(code, "0" * 64)
    async with running(binary, env, "enroll", wrong_pin, "--name", "dev") as (proc, lines):
        await eventually(lambda: any("fingerprint mismatch" in line and "retrying in 5.0s" in line for line in lines))
        assert proc.returncode is None
        assert host.enrollment_messages == 0
        assert not (home / "enrollment.pending.json").exists()
    status, stdout, stderr = await invoke(binary, env, "enroll", code, "--name", "dev")
    assert status == 0, stderr
    assert "enrolled dev" in stdout
    assert host.enrollment_messages == 1
    assert not (home / "enrollment.pending.json").exists()

    async with running(binary, env) as (proc, lines):
        live = await eventually(lambda: host.live_for("dev"))
        stamp = live.last_mono
        await eventually(lambda: live.last_mono > stamp + 0.3)
        assert host.list_children()[0]["build_id"] is None
        assert host.hello_payloads[-1] == {
            "protocol": 2, "version": current_package_version(), "adapters": ["claude.agent-sdk"],
            "features": [
                "codex-turns", "login-status", "provider-data/1", "workspace-policy/1",
                "prompt-prefix-skills/1", "session-collect/1", "skill-folders/1", "repository-skills/1",
                "local-llm/1",
                *(["provider-data-mcp/1"] if platform_key() in LANDLOCK_PLATFORMS else []),
                *(["sdk-skills/1", "host-http-mcp/1"] if platform_key().startswith("linux-") else []),
            ],
            "mcp": [{"name": "files", "description": "workspace files"}],
            "quota_providers": ["claude"],
            "local_llm": {},
        }
        status, stdout, _ = await invoke(binary, env, "status")
        assert status == 0 and "instance lock: held" in stdout
        with pytest.raises(ValueError, match="child is already running"), child_lock(home):
            pytest.fail("the Python CLI lock must exclude the Rust app")
        assert not (home / "starts").exists()
        rss = next(line for line in Path(f"/proc/{proc.pid}/status").read_text().splitlines() if line.startswith("VmRSS:"))
        print(f"release child-app idle, connected without runner: {rss.split(':', 1)[1].strip()}")
        assert "connected to host" in "\n".join(lines)
        assert Path(f"/proc/{proc.pid}/task/{proc.pid}/children").read_text() == ""

        async with asyncio.timeout(30):
            warm = child_turn(config, tmp_path, "warm")
            shown = [item async for item in warm.stream("first")]
            assert shown[:2] == ["こん", "にちは"]
            assert isinstance(shown[2], RuntimeEvent) and shown[2].tool_name == "fixture"
            assert isinstance(shown[3], RuntimeResult) and shown[3].text == "こんにちは"
            assert (home / "skill-fixture-read").exists()
            first_runner, = pids(home / "starts")

            blocked = child_turn(config, tmp_path, "cancel")
            stream = blocked.stream("cancel")
            assert await anext(stream) == "partial"
            await eventually(lambda: (home / "cancel.grandchild").is_file())
            await stream.aclose()
            await eventually(lambda: (home / "cancelled").exists())
            await eventually(lambda: reaped(*pids(home / "cancel.pid"), *pids(home / "cancel.grandchild")))
            assert (await warm.ask("second")).text == "こんにちは"
            assert pids(home / "starts") == [first_runner]
            # The successful turns reused one runtime while cancellation used its own.
            assert pids(home / "builds") == [first_runner, first_runner]
            release_id = host.allocate_stream()
            (home / "released").unlink()
            await host.send_frame("dev", release_id, "release", {"conversation_key": "warm"})
            await eventually(lambda: (home / "released").exists())

            login = ChildStream("dev")
            try:
                await login.send("login", {
                    "provider": "claude", "flow": "paste_code", "command": ["auth", "login", "--claudeai"],
                })
                assert (await login.receive("login_output")).payload["url"] == "https://claude.ai/authorize?state=fake"
                await login.send("login_input", {"code": "fake-code"})
                assert (await login.receive("login_exit")).payload["success"] is True
            finally:
                await login.close("login_cancel")

            malformed = ChildStream("dev")
            try:
                await malformed.send("quota", {"provider": "claude", "command": ["a" * 129]})
                with pytest.raises(ChildStreamError, match="invalid runner operation"):
                    await malformed.receive("quota_result")
            finally:
                await malformed.close("quota_cancel")
            assert (await fetch_child_quota("dev", "claude", None, 3))["quota"][0]["used_percent"] == 24
            assert await fetch_child_login_status("dev", "claude", None, 3) == "logged_in"

        await eventually(lambda: reaped(first_runner) and not list(home.glob(".runner-*")))
        assert host.live_for("dev") is live
        assert (home / "released").exists()
        assert any("runner stopped" in line for line in lines)

        tree = child_turn(config, tmp_path, "tree").stream("tree")
        async with asyncio.timeout(30):
            assert await anext(tree) == "partial"
            await eventually(lambda: (home / "tree.grandchild").is_file())
            runner = pids(home / "starts")[-1]
            child, = pids(home / "tree.pid")
            grandchild, = pids(home / "tree.grandchild")
            assert runner != first_runner
            assert not reaped(runner, child, grandchild)
            await host.disconnect("dev")
            with pytest.raises(SDKConnectionError, match="offline"):
                await anext(tree)
            await eventually(lambda: reaped(runner, child, grandchild))
            await eventually(lambda: host.hello_count == 2 and host.live_for("dev") is not None)
            assert len(pids(home / "starts")) == 2

        monkeypatch.setattr("kotoyose.child_server.current_package_version", lambda: "0.0.0-host")
        await host.disconnect("dev")
        await eventually(lambda: host.hello_count == 3 and host.live_for("dev") is not None)
        with pytest.raises(RuntimeTurnError, match="package version mismatch"):
            async with asyncio.timeout(30):
                await child_turn(config, tmp_path, "mismatch").ask("mismatch")
        assert proc.returncode is None


async def test_python_enrollment_runner_failure_and_zero_idle(binary, host, child_env, tmp_path):
    home, env = child_env
    _policy(home, tmp_path)
    (home / "app.json").write_text('{"runner_idle_seconds": 0}')
    await enroll_child(issue(host), home, name="dev")
    config = HarnessConfig.model_validate(_host_config(tmp_path))

    async with running(binary, env) as (proc, lines):
        live = await eventually(lambda: host.live_for("dev"))
        with pytest.raises(RuntimeTurnError, match="child runner failed") as caught:
            async with asyncio.timeout(30):
                await child_turn(config, tmp_path, None).ask("crash")
        assert caught.value.partial_result.text == "partial"
        assert caught.value.retryable is False
        runner, = pids(home / "starts")
        await eventually(lambda: (home / "crash.grandchild").is_file())
        await eventually(lambda: reaped(runner, *pids(home / "crash.pid"), *pids(home / "crash.grandchild")))
        assert host.live_for("dev") is live

        async with asyncio.timeout(30):
            assert (await child_turn(config, tmp_path, "normal").ask("new")).text == "こんにちは"
            next_runner = pids(home / "starts")[-1]
            assert next_runner != runner
            await eventually(lambda: reaped(next_runner))
        assert sum("runner stopped" in line for line in lines) == 2
        assert host.hello_count == 1
        await host.revoke("dev")
        assert await asyncio.wait_for(proc.wait(), 5) == 1
        await eventually(lambda: any("child revoked" in line for line in lines))


async def test_enrollment_recovery_uses_the_staged_identity(binary, host, child_env, monkeypatch):
    home, env = child_env
    real_on_enroll = host._on_enroll

    async def lose_confirmation(ws, payload):
        send = ws.send

        async def without_confirmation(message):
            if decode_frame(message).kind == "enroll_ok":
                await ws.close()
            else:
                await send(message)

        monkeypatch.setattr(ws, "send", without_confirmation)
        await real_on_enroll(ws, payload)

    monkeypatch.setattr(host, "_on_enroll", lose_confirmation)
    pending = home / "enrollment.pending.json"
    async with running(binary, env, "enroll", issue(host), "--name", "dev") as (_proc, lines):
        await eventually(lambda: any("enrollment not confirmed" in line for line in lines))
        staged = pending.read_bytes()
        assert not (home / "key.pem").exists()
        assert host.enrollment_messages == 1

    port = host.port
    await host.stop()
    # Keep the same pinned host and registry while the TLS context is missing the registered leaf.
    host._ssl = _server_context(host.identity.certificate_file, host.identity.key_file)
    await host.start("127.0.0.1", port)
    async with running(binary, env) as (proc, lines):
        await eventually(lambda: any("pending enrollment could not be confirmed" in line for line in lines))
        assert pending.read_bytes() == staged and proc.returncode is None
        assert host.hello_count == 0
        host._ssl.load_verify_locations(cadata=json.loads(staged)["cert"])

        await eventually(lambda: not pending.exists() and host.live_for("dev"))
        assert host.enrollment_messages == 1
        assert (home / "key.pem").read_text() == json.loads(staged)["key"]
        assert not (home / "starts").exists()


@pytest.mark.parametrize(("reason", "keep_pending"), [
    ("unknown child", False),
    ("child revoked", False),
    ("package version mismatch: child 1 != host 2", True),
])
async def test_only_authenticated_identity_rejection_can_stop_recovery(
    binary, host, child_env, monkeypatch, reason, keep_pending,
):
    home, env = child_env
    await enroll_child(issue(host), home, name="dev")
    pending = home / "enrollment.pending.json"
    key = (home / "key.pem").read_text()
    pending.write_text(json.dumps({
        "host": json.loads((home / "host.json").read_text()),
        "cert": (home / "cert.pem").read_text(),
        "key": key,
        "accepted": False,
    }))
    staged = pending.read_bytes()

    async def reject(ws, _mux, _payload):
        assert host._peer_fingerprint(ws) == host.registry.get("dev")["fingerprint"]
        await ws.send(encode_frame(0, "reject", {"reason": reason}))
        await ws.close()

    monkeypatch.setattr(host, "_on_hello", reject)
    status, _, stderr = await invoke(binary, env, "run")
    assert status == 1 and "connection stopped" in stderr and reason in stderr
    if keep_pending:
        assert pending.read_bytes() == staged
    else:
        assert not pending.exists()
    assert (home / "key.pem").read_text() == key


@pytest.mark.parametrize(("filename", "corrupt"), [
    ("policy.json", b"{"),
    ("host.json", b"{"),
    ("key.pem", None),
    ("enrollment.pending.json", b"{"),
])
async def test_retries_unreadable_state_until_it_is_repaired(binary, host, child_env, filename, corrupt):
    home, env = child_env
    await enroll_child(issue(host), home, name="dev")
    path = home / filename
    original = path.read_bytes() if path.exists() else None
    if corrupt is None:
        path.unlink()
    else:
        path.write_bytes(corrupt)

    async with running(binary, env) as (proc, lines):
        await eventually(lambda: any(
            ("pending enrollment" if filename == "enrollment.pending.json" else filename) in line
            and "retrying in 5.0s" in line for line in lines
        ))
        assert proc.returncode is None and host.hello_count == 0
        if corrupt is not None:
            assert path.read_bytes() == corrupt
        if original is None:
            path.unlink()
        else:
            path.write_bytes(original)
        await eventually(lambda: host.live_for("dev"))


async def test_invalid_host_text_is_counted_and_reconnects(binary, host, child_env):
    home, env = child_env
    await enroll_child(issue(host), home, name="dev")
    async with running(binary, {**env, "RUST_LOG": "debug"}) as (proc, lines):
        live = await eventually(lambda: host.live_for("dev"))
        for count, raw in enumerate(("{", '{"stream":-1,"kind":"turn"}'), start=1):
            await live.ws.send(raw)
            await eventually(lambda: any(f"invalid host text frame count={count}" in line for line in lines))
            await eventually(lambda: any(f"invalid host frame envelope (total: {count})" in line for line in lines))
            previous = live
            live = await eventually(lambda: (current := host.live_for("dev")) and current is not previous and current)
        assert proc.returncode is None and host.hello_count == 3


@pytest.mark.parametrize("stop", [SIGHUP, SIGTERM, "quit"])
async def test_stopping_the_link_reaps_runner_tree(binary, host, child_env, tmp_path, stop):
    home, env = child_env
    _policy(home, tmp_path)
    await enroll_child(issue(host), home, name="dev")
    config = HarnessConfig.model_validate(_host_config(tmp_path))

    async with running(binary, env) as (proc, _lines):
        await eventually(lambda: host.live_for("dev"))
        stream = child_turn(config, tmp_path, "signal").stream("tree")
        try:
            assert await asyncio.wait_for(anext(stream), 8) == "partial"
            await eventually(lambda: (home / "tree.grandchild").is_file())
            tree = pids(home / "starts") + pids(home / "tree.pid") + pids(home / "tree.grandchild")
            if stop == "quit":
                status, _, stderr = await invoke(binary, env, "quit")
                assert status == 0, stderr
            else:
                proc.send_signal(stop)
            assert await asyncio.wait_for(proc.wait(), 7) == 0
            assert reaped(*tree)
            await eventually(lambda: host.live_for("dev") is None)
            with pytest.raises(SDKConnectionError, match="offline"):
                await asyncio.wait_for(anext(stream), 5)
        finally:
            await stream.aclose()


async def test_runner_receives_linux_parent_death_signal(binary, host, child_env, tmp_path):
    home, env = child_env
    _policy(home, tmp_path)
    await enroll_child(issue(host), home, name="dev")
    config = HarnessConfig.model_validate(_host_config(tmp_path))

    async with running(binary, env) as (proc, _lines):
        await eventually(lambda: host.live_for("dev"))
        assert (await child_turn(config, tmp_path, "normal").ask("done")).text == "こんにちは"
        runner, = pids(home / "starts")

        def runner_state():
            return Path(f"/proc/{runner}/stat").read_text().rsplit(") ", 1)[1].split()[0]

        # Adopt the runner when the link exits so this test can reap it.
        libc = ctypes.CDLL(None)
        previous = ctypes.c_int()
        pr_set_child_subreaper, pr_get_child_subreaper = 36, 37
        assert libc.prctl(pr_get_child_subreaper, ctypes.byref(previous), 0, 0, 0) == 0
        assert libc.prctl(pr_set_child_subreaper, 1, 0, 0, 0) == 0
        try:
            # A stopped runner cannot exit in response to stdin EOF.
            os.kill(runner, SIGSTOP)
            try:
                await eventually(lambda: runner_state() == "T")
            finally:
                proc.kill()
                await proc.wait()
            try:
                await eventually(lambda: runner_state() == "Z", timeout=3)
            finally:
                if runner_state() != "Z":
                    os.kill(runner, SIGKILL)
                os.waitpid(runner, 0)
        finally:
            assert libc.prctl(pr_set_child_subreaper, previous.value, 0, 0, 0) == 0


async def test_cancel_timeout_kills_runner_and_ends_other_work(binary, host, child_env, tmp_path):
    home, env = child_env
    _policy(home, tmp_path)
    await enroll_child(issue(host), home, name="dev")
    config = HarnessConfig.model_validate(_host_config(tmp_path))

    async with running(binary, env) as (_proc, lines):
        live = await eventually(lambda: host.live_for("dev"))
        # 5 秒の取消期限と Runner の起動 2 回 (CI では import が遅い) を含むので、全体の上限は広く取る。
        async with asyncio.timeout(60):
            stuck = child_turn(config, tmp_path, "stuck").stream("stuck")
            assert await anext(stuck) == "stuck partial"
            await stuck.aclose()
            await eventually(lambda: (home / "cancelled").exists())
            other = child_turn(config, tmp_path, "other").stream("tree")
            assert await anext(other) == "partial"
            first, = pids(home / "starts")
            await eventually(lambda: (home / "tree.grandchild").is_file())
            with pytest.raises(RuntimeTurnError, match="child runner failed"):
                await anext(other)
            await eventually(lambda: reaped(first, *pids(home / "tree.pid"), *pids(home / "tree.grandchild")))
            assert any("runner cancellation timed out" in line for line in lines)
            assert host.live_for("dev") is live
            assert (await child_turn(config, tmp_path, "after").ask("new")).text == "こんにちは"


async def test_components_stream_packages_and_refresh_hello(binary, host, child_env, tmp_path):
    home, env = child_env
    _policy(home, tmp_path, adapters=["claude.agent-sdk", "codex.app-server"])
    (home / "fake-components").touch()
    installed = tmp_path / "installed location" / "1.2.0"
    bin_dir = installed / "bin"
    parts = bin_dir / "parts"
    parts.mkdir(parents=True)
    (parts / "requirements-claude.txt").write_text("claude-agent-sdk==1 --hash=sha256:fake\n")
    (parts / "requirements-codex.txt").write_text(
        "openai-codex==1 --hash=sha256:fake\nopenai-codex-cli-bin==1 --hash=sha256:fake\n"
    )
    app = bin_dir / binary.name
    shutil.copy2(binary, app)
    python = installed / "python" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    shutil.copy2(env["KOTOYOSE_CHILD_PYTHON"], python)
    python.with_name("kotoyose-ready").touch()
    site_packages = installed / "python" / "lib" / "python3.13" / "site-packages"
    site_packages.mkdir(parents=True)
    await enroll_child(issue(host), home, name="dev")

    async with running(app, env, "ui", capture_url=True) as (proc, _):
        url = await read_settings_url(proc)
        await eventually(lambda: host.live_for("dev"))
        assert host.hello_payloads[-1]["adapters"] == []
        async with httpx.AsyncClient(base_url=f"http://{url.netloc}", trust_env=False) as client:
            origin = {"Origin": str(client.base_url).rstrip("/")}
            assert (await client.get("/api/components")).status_code == 401
            session = await client.post("/api/session", headers=origin, json={"code": url.fragment})
            client.headers.update({**origin, "Authorization": f"Bearer {session.json()['token']}"})

            async def status(provider="claude", *, check=False):
                response = await client.get("/api/components/check" if check else "/api/components")
                response.raise_for_status()
                data = response.json()
                assert data["can_manage_packages"]
                assert [row["id"] for row in data["providers"]] == [row["id"] for row in component_rows()]
                rows = [row for row in data["installed"] if row["id"] == provider]
                assert rows, f"no installed row for {provider}: {data}"
                return rows[0]

            def probes():
                return (home / "component-probes").read_text().splitlines()

            async def reconnect():
                original = host.hello_count
                await host.disconnect("dev")
                await eventually(lambda: host.hello_count > original and host.live_for("dev"))

            async def operation(change, progress=None, *, interrupt=False, provider="claude", succeeds=True):
                original = host.hello_count
                policy = (await client.get("/api/policy")).json()
                (home / "continue-pip").unlink(missing_ok=True)
                async with client.stream("POST", "/api/components", json={
                    "provider": provider, "change": change, "interrupt": interrupt,
                }) as response:
                    response.raise_for_status()
                    assert response.headers["Content-Type"].startswith("application/x-ndjson")
                    lines = response.aiter_lines()
                    if progress:
                        assert json.loads(await anext(lines)) == {"event": "progress", "message": progress}
                        if change == "install":
                            before = probes()
                            for url in ("/api/components", "/api/components/check"):
                                busy = (await client.get(url)).json()
                                assert busy["installed"] == [] and "in progress" in busy["error"]
                            assert (await client.get("/api/policy")).status_code == 409
                            assert (await client.post("/api/policy", json={
                                "revision": policy["revision"], "policy": policy["policy"],
                            })).status_code == 409
                            assert probes() == before
                        (home / "continue-pip").touch()
                    events = [json.loads(line) async for line in lines]
                    assert [event["event"] for event in events].count("result") == 1
                    assert events[-1]["event"] == "result" and events[-1]["ok"] is succeeds
                await eventually(lambda: host.hello_count > original and host.live_for("dev"))
                return events

            initial = await status()
            assert initial["packages"]["claude-agent-sdk"] is None
            assert initial["system_cli"] is True and initial["system_version"] is None
            assert (await status(check=True))["system_version"] == "Claude fixture 9.9"
            assert initial["choice"] == "bundled"
            await reconnect()
            assert probes().count("availability") == 1
            await operation("install", "Downloading the fake SDK")
            assert host.hello_payloads[-1]["adapters"] == ["claude.agent-sdk"]
            assert (await status())["packages"]["claude-agent-sdk"] == "1"
            good = probes().count("availability")
            (home / "fail-probe").touch()
            (site_packages / "external-change").mkdir()
            await reconnect()
            assert probes().count("availability") == good + 1
            assert host.hello_payloads[-1]["adapters"] == ["claude.agent-sdk"]
            (home / "fail-probe").unlink()
            (home / "app.json").write_text("{}")
            await reconnect()
            assert probes().count("availability") == good + 2
            config = HarnessConfig.model_validate(_host_config(tmp_path))
            turn = child_turn(config, tmp_path, "blocked").stream("stuck")
            try:
                assert await anext(turn) == "stuck partial"
                refused = await client.post("/api/components", json={"provider": "claude", "change": "install"})
                assert refused.status_code == 409
                assert (await status())["packages"]["claude-agent-sdk"] == "1"
                await operation("install", "Downloading the fake SDK", interrupt=True)
                assert (await status())["packages"]["claude-agent-sdk"] == "2"
            finally:
                await turn.aclose()

            (home / "no-bundled-cli").touch()
            await operation({"use_cli": "system"})
            selected = await status()
            assert selected["choice"] == "system" and not selected["bundled_cli"]
            assert host.hello_payloads[-1]["adapters"] == ["claude.agent-sdk"]
            settings = (await client.get("/api/settings")).json()
            saved = await client.post("/api/settings", json={"revision": settings["revision"], "runner_idle_seconds": 123})
            assert saved.status_code == 200
            assert json.loads((home / "app.json").read_text()) == {"runner_idle_seconds": 123, "claude_cli": "system"}
            await operation({"use_cli": "bundled"})
            assert (await status())["choice"] == "bundled"
            assert host.hello_payloads[-1]["adapters"] == []
            await operation("remove", "Removing the fake SDK")
            assert (await status())["packages"]["claude-agent-sdk"] is None

            (home / "fail-pip").touch()
            events = await operation("install", "Downloading the fake SDK", succeeds=False)
            assert events[0] == {"event": "progress", "message": "Could not replace '<runtime>'"}
            assert "Repair" in events[-1]["message"]
            failed = (await client.get("/api/components")).json()
            assert failed["needs_reinstall"] == ["claude"]
            (home / "fail-pip").unlink()
            (home / "no-bundled-cli").unlink()
            (home / "flood-pip").touch()
            await operation("install", "Downloading the fake SDK")
            assert (await client.get("/api/components")).json()["needs_reinstall"] == []
            (home / "flood-pip").unlink()

            await operation("install", "Downloading the fake SDK", provider="codex")
            codex = await status("codex")
            assert codex["packages"] == {"openai-codex": "1", "openai-codex-cli-bin": "1"}
            assert (await status("codex", check=True))["system_version"] == "Codex fixture 9.9"
            assert codex["bundled_cli"] and codex["turns_supported"]
            assert host.hello_payloads[-1]["adapters"] == ["claude.agent-sdk", "codex.app-server"]
            await operation({"use_cli": "system"}, provider="codex")
            assert (await status("codex"))["choice"] == "system"
            await operation("remove", "Removing the fake SDK", provider="codex")
            assert (await status("codex"))["packages"] == {"openai-codex": None, "openai-codex-cli-bin": None}


async def test_settings_local_login_uses_the_pinned_cli_without_a_host(binary, child_env, tmp_path):
    home, env = child_env
    home.mkdir(mode=0o700)
    (home / "app.json").write_text('{"claude_cli":"system"}')
    tools = tmp_path / "fake-bin"
    tools.mkdir()
    calls = tmp_path / "calls"
    rejected = tmp_path / "rejected"
    release_probe = tmp_path / "release-probe"
    cli = tools / "claude"
    cli.write_text(
        f"#!{sys.executable}\n" + dedent(f'''
        import json
        import os
        from pathlib import Path
        import sys
        import time

        calls = Path({str(calls)!r})
        args = sys.argv[1:]
        with calls.open("a") as output:
            output.write(json.dumps(args) + "\\n")
        if args == ["auth", "login", "--claudeai"]:
            assert all(os.isatty(fd) for fd in (0, 1, 2))
            print("unrelated CLI output", flush=True)
            print("https://claude.ai/authorize?state=fake", flush=True)
            while input() != "fake-code":
                Path({str(rejected)!r}).touch()
                print("private rejection detail", flush=True)
            sys.exit(0)
        if args == ["auth", "status", "--json"]:
            while not Path({str(release_probe)!r}).exists():
                time.sleep(0.01)
            print(json.dumps({{"loggedIn": len(calls.read_text().splitlines()) == 3, "account": "private"}}))
            sys.exit(0)
        sys.exit(99)
        '''),
    )
    cli.chmod(0o700)
    env = {**env, "PATH": str(tools), "KOTOYOSE_CHILD_PYTHON": sys.executable}
    async with running(binary, env, "ui", capture_url=True) as (proc, _):
        url = await read_settings_url(proc)
        async with httpx.AsyncClient(base_url=f"http://{url.netloc}", trust_env=False) as client:
            origin = {"Origin": str(client.base_url).rstrip("/")}
            session = await client.post("/api/session", headers=origin, json={"code": url.fragment})
            headers = {**origin, "Authorization": f"Bearer {session.json()['token']}"}
            endpoint = "/api/login/claude"
            status_endpoint = "/api/login/status/claude"
            assert (await client.post(endpoint, headers=origin, json={})).status_code == 401
            assert (await client.delete(endpoint, headers=origin)).status_code == 401
            assert (await client.delete(endpoint, headers={**headers, "Origin": "http://localhost"})).status_code == 403
            assert (await client.post(endpoint, headers=headers, json={"argv": ["--version"]})).status_code == 422
            assert (await client.post("/api/login/unknown", headers=headers, json={})).status_code == 404

            check = asyncio.create_task(client.post(status_endpoint, headers=headers, json={}))
            await eventually(calls.exists)
            duplicates = [asyncio.create_task(client.post(status_endpoint, headers=headers, json={})) for _ in range(3)]
            starting = asyncio.create_task(client.post(endpoint, headers=headers, json={}))
            await asyncio.sleep(0.2)
            assert not starting.done() and not any(request.done() for request in duplicates)
            assert len(calls.read_text().splitlines()) == 1
            release_probe.touch()
            assert [result.json() for result in await asyncio.gather(check, *duplicates)] == [{"state": "logged_out"}] * 4
            assert (await starting).json() == {"state": "running"}

            async def progress(predicate):
                async with asyncio.timeout(10):
                    while True:
                        response = await client.get(endpoint, headers=headers)
                        view = response.json()
                        if predicate(view):
                            return view
                        await asyncio.sleep(0.02)

            prompt = await progress(lambda view: view.get("needs_code") or view["state"] != "running")
            assert prompt == {"state": "running", "url": "https://claude.ai/authorize?state=fake", "needs_code": True}
            duplicate = await client.post(endpoint, headers=headers, json={})
            assert duplicate.json() == prompt
            assert (await client.post(status_endpoint, headers=headers, json={})).json() == prompt
            assert (await client.post(f"{endpoint}/code", headers=headers, json={"code": "wrong-code"})).status_code == 200
            await eventually(rejected.exists)
            assert await progress(lambda view: view.get("needs_code")) == prompt
            result = await client.post(f"{endpoint}/code", headers=headers, json={"code": "fake-code"})
            assert result.status_code == 200
            assert await progress(lambda view: view["state"] != "running") == {"state": "logged_in"}

            assert (await client.post(endpoint, headers=headers, json={})).json() == {"state": "running"}
            assert await progress(lambda view: view.get("needs_code")) == prompt
            assert (await client.delete(endpoint, headers=headers)).json() == {"state": "cancelled"}
            assert (await client.get(endpoint, headers=headers)).json() == {"state": "cancelled"}

    assert [json.loads(line) for line in calls.read_text().splitlines()] == [
        ["auth", "status", "--json"], ["auth", "login", "--claudeai"], ["auth", "status", "--json"],
        ["auth", "login", "--claudeai"],
    ]


async def test_malformed_login_helper_is_reaped_before_the_next_login(binary, child_env, tmp_path):
    _home, env = child_env
    started = tmp_path / "helper-pids"
    cleaning = tmp_path / "cleaning"
    stdin_closed = tmp_path / "stdin-closed"
    helper = tmp_path / "fake-python"
    helper.write_text(f"#!{sys.executable}\n" + dedent(f"""
        import os
        from pathlib import Path
        import signal
        import sys
        import time

        started = Path({str(started)!r})
        if started.exists():
            print('{{"state":"logged_out"}}', flush=True)
            sys.exit(0)

        def terminate(signal, frame):
            Path({str(cleaning)!r}).touch()
            if not sys.stdin.read():
                Path({str(stdin_closed)!r}).touch()
            time.sleep(60)

        signal.signal(signal.SIGTERM, terminate)
        started.write_text(str(os.getpid()))
        print("not json", flush=True)
        time.sleep(60)
        """))
    helper.chmod(0o700)
    env["KOTOYOSE_CHILD_PYTHON"] = str(helper)
    async with running(binary, env, "ui", capture_url=True) as (proc, _):
        url = await read_settings_url(proc)
        async with httpx.AsyncClient(base_url=f"http://{url.netloc}", trust_env=False) as client:
            origin = {"Origin": str(client.base_url).rstrip("/")}
            session = await client.post("/api/session", headers=origin, json={"code": url.fragment})
            headers = {**origin, "Authorization": f"Bearer {session.json()['token']}"}
            endpoint = "/api/login/claude"
            assert (await client.post(endpoint, headers=headers, json={})).json() == {"state": "running"}
            await eventually(cleaning.exists)
            await eventually(stdin_closed.exists)
            pid = int(started.read_text())
            assert not reaped(pid)
            assert (await client.post(endpoint, headers=headers, json={})).json() == {"state": "running"}
            assert (await client.post("/api/login/status/claude", headers=headers, json={})).json() == {"state": "running"}

            async with asyncio.timeout(5):
                while (await client.get(endpoint, headers=headers)).json() != {"state": "failed"}:
                    await asyncio.sleep(0.02)
            assert reaped(pid)
            assert (await client.post(endpoint, headers=headers, json={})).json() == {"state": "running"}
            async with asyncio.timeout(5):
                while (await client.get(endpoint, headers=headers)).json() != {"state": "logged_out"}:
                    await asyncio.sleep(0.02)


async def test_runner_idle_shutdown_preserves_local_login(binary, host, child_env, tmp_path):
    home, env = child_env
    _policy(home, tmp_path)
    await enroll_child(issue(host), home, name="dev")
    (home / "app.json").write_text('{"runner_idle_seconds":1}')
    helper = tmp_path / "fake-python"
    helper.write_text(f"#!{sys.executable}\n" + dedent(f"""
        import os
        import sys

        if sys.argv[1:4] != ["-I", "-m", "kotoyose.child_local_login"]:
            os.execv({env["KOTOYOSE_CHILD_PYTHON"]!r}, [{env["KOTOYOSE_CHILD_PYTHON"]!r}, *sys.argv[1:]])
        print('{{"state":"running","url":"https://claude.ai/authorize","needs_code":true}}', flush=True)
        sys.stdin.read()
        """))
    helper.chmod(0o700)
    env["KOTOYOSE_CHILD_PYTHON"] = str(helper)
    config = HarnessConfig.model_validate(_host_config(tmp_path))

    async with running(binary, env, "ui", capture_url=True) as (proc, _):
        url = await read_settings_url(proc)
        await eventually(lambda: host.live_for("dev"))
        async with httpx.AsyncClient(base_url=f"http://{url.netloc}", trust_env=False) as client:
            origin = {"Origin": str(client.base_url).rstrip("/")}
            session = await client.post("/api/session", headers=origin, json={"code": url.fragment})
            client.headers.update({**origin, "Authorization": f"Bearer {session.json()['token']}"})
            endpoint = "/api/login/claude"
            assert (await client.post(endpoint, json={})).json() == {"state": "running"}
            assert (await child_turn(config, tmp_path, "warm").ask("done")).text == "こんにちは"
            runner, = pids(home / "starts")
            await eventually(lambda: reaped(runner))
            assert (await client.get(endpoint)).json() == {
                "state": "running", "url": "https://claude.ai/authorize", "needs_code": True,
            }
            assert (await client.delete(endpoint)).json() == {"state": "cancelled"}


async def test_host_mcp_approval_and_http_call_through_rust(binary, host, child_env, tmp_path, monkeypatch):
    from tests.test_child_host_mcp import fake_http_mcp, relay_config

    home, env = child_env
    _policy(home, tmp_path)
    async with fake_http_mcp() as upstream:
        cfg = relay_config(tmp_path, upstream.url, request_headers=False)
        cfg.mcps["remote"].server["headers"] = {"Authorization": "rust-host-only-secret"}
        monkeypatch.setattr("kotoyose.config._config", cfg)
        await enroll_child(issue(host), home, name="dev")
        async with running(binary, env, "ui", capture_url=True) as (proc, _):
            url = await read_settings_url(proc)
            async with httpx.AsyncClient(base_url=f"http://{url.netloc}", trust_env=False) as client:
                assert (await client.get("/api/host-mcp")).status_code == 401
                origin = {"Origin": str(client.base_url).rstrip("/")}
                response = await client.post("/api/session", headers=origin, json={"code": url.fragment})
                response.raise_for_status()
                credentials = {**origin, "Authorization": f"Bearer {response.json()['token']}"}
                async with asyncio.timeout(5):
                    while True:
                        view = (await client.get("/api/host-mcp", headers=credentials)).json()
                        if view["catalog"]["offers"]:
                            break
                        await asyncio.sleep(0.02)
                offer, = view["catalog"]["offers"]
                assert offer["name"] == "remote"
                assert "rust-host-only-secret" not in json.dumps(view)
                assert upstream.url not in json.dumps(view)
                runtime = child_turn(cfg, tmp_path, "rust-mcp")
                with pytest.raises(RuntimeTurnError, match="requires child owner approval"):
                    await runtime.ask("host-mcp")

                edit = {"fingerprint": view["catalog"]["fingerprint"], "name": offer["name"],
                        "version": offer["version"], "approve": True}
                rejected = await client.post("/api/host-mcp", headers=credentials, json={**edit, "version": "0" * 64})
                assert rejected.status_code == 409
                assert (await client.post("/api/host-mcp", headers=credentials, json=edit)).status_code == 200
                assert (home / "host-mcp-approvals.json").stat().st_mode & 0o077 == 0
                await eventually(lambda: host.live_for("dev").mcp_approvals.get("remote") == offer["version"])
                result = await asyncio.wait_for(runtime.ask("host-mcp"), 8)
                assert result.text == "rust-relay"
                assert upstream.calls == ["rust-relay"]
                assert all(headers["authorization"] == "rust-host-only-secret" for headers, _ in upstream.requests)
                assert (await client.post("/api/host-mcp", headers=credentials, json={**edit, "approve": False})).status_code == 200
                await eventually(lambda: not host.live_for("dev").mcp_approvals)
                with pytest.raises(RuntimeTurnError, match="requires child owner approval"):
                    await runtime.ask("host-mcp")
                assert upstream.calls == ["rust-relay"]
                assert (await client.post("/api/host-mcp", headers=credentials, json=edit)).status_code == 200
                await eventually(lambda: host.live_for("dev").mcp_approvals.get("remote") == offer["version"])
                pending = asyncio.create_task(runtime.ask("host-mcp-block"))
                await asyncio.wait_for(upstream.started.wait(), 5)
                await host.disconnect("dev")
                with pytest.raises(RuntimeTurnError, match="result is unknown"):
                    await asyncio.wait_for(pending, 5)
                await asyncio.wait_for(upstream.cancelled.wait(), 5)
                marker = home / "relay-result"
                await eventually(marker.exists)
                assert "result is unknown" in marker.read_text()
                assert upstream.calls == ["rust-relay", "block"]
