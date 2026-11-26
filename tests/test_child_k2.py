"""K2 login and quota through the real localhost TLS child link."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import sys
from textwrap import dedent
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi import FastAPI

from kotoyose import child_client, child_ops, cli_login, config, connections_probe
from kotoyose.auth import require_basic_auth
from kotoyose.child_client import ChildSession, child_session, enroll_child
from kotoyose.child_identity import load_policy
from kotoyose.child_pinned import status_spec
from kotoyose.child_protocol import Frame, LOGIN_MESSAGE_LIMIT, QUOTA_WINDOW_LIMIT, decode_frame, encode_frame
from kotoyose.child_relay import (
    ChildFeatureUnsupported, ChildStream, ChildStreamError, fetch_child_login_status, fetch_child_quota,
)
from kotoyose.child_server import ChildHub
from kotoyose.config import HarnessConfig
from kotoyose.connection_state_store import ConnectionStateStore
from kotoyose.routes import admin_children, admin_settings

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Runs fake CLI processes in Linux PTYs")


@pytest.fixture
async def hub(tmp_path):
    host = ChildHub(tmp_path / "host")
    await host.start("127.0.0.1", 0)
    try:
        yield host
    finally:
        await cli_login.shutdown_cli_login_sessions()
        await host.stop()


@pytest.fixture
async def child_home(hub, tmp_path):
    home = tmp_path / "child"
    await enroll_child(hub.issue_enrollment_code(f"127.0.0.1:{hub.port}"), home, name="box")
    return home


def allow_logins(home, *providers):
    path = home / "policy.json"
    policy = json.loads(path.read_text())
    policy["logins"] = list(providers)
    path.write_text(json.dumps(policy))


def allow_adapters(home, monkeypatch, *adapters):
    path = home / "policy.json"
    policy = json.loads(path.read_text())
    path.write_text(json.dumps({**policy, "adapters": adapters}))
    monkeypatch.setattr(child_client, "available_adapters", lambda names, home: list(names))


def child_config(provider="claude", **fields):
    return HarnessConfig(connections={"sub": {
        "provider": provider, "kind": "subscription", "on": "child:box",
        "data_policy": "no_training", **fields,
    }})


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    home = tmp_path / "os-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setattr(cli_login, "resolve_print_cli_binary", fail_host_launch)
    monkeypatch.setattr(connections_probe, "resolve_connection_cli_binary", fail_host_launch)

    def install(source):
        binary = tmp_path / "fake-cli"
        binary.write_text(f"#!{sys.executable}\n" + dedent(source))
        binary.chmod(0o700)
        monkeypatch.setattr(child_ops, "resolve_child_binary", lambda *_: binary)
    return home, install


def fail_host_launch(*_args, **_kwargs):
    pytest.fail("The host tried to resolve a child CLI locally")


@pytest.mark.parametrize(("provider", "output", "exit_code", "state", "auth"), [
    ("anthropic", '{"loggedIn":true,"email":"private@example.invalid"}', 0, "logged_in", "ok"),
    ("claude", '{"loggedIn":false}', 1, "logged_out", "logged_out"),
    ("claude", "invalid output", 0, "unknown", "unknown"),
    ("codex", "Logged in using ChatGPT", 0, "logged_in", "ok"),
    ("codex", "Not logged in", 1, "logged_out", "logged_out"),
    ("codex", "Unexpected CLI error", 1, "unknown", "unknown"),
])
async def test_child_status_without_quota(child_home, fake_cli, monkeypatch, provider, output, exit_code, state, auth):
    home, install = fake_cli
    is_codex = provider == "codex"
    command = ["login", "status"] if is_codex else ["auth", "status", "--json"]
    install(f"""
        from pathlib import Path
        import sys

        assert sys.argv[1:] == {command!r}
        with (Path.home() / "status-runs").open("a") as log:
            log.write("status\\n")
        print({output!r}, file=sys.stderr if {is_codex!r} else sys.stdout)
        sys.exit({exit_code})
    """)
    if is_codex:
        from kotoyose import child_codex

        binary = child_ops.resolve_child_binary(provider, None, child_home)
        monkeypatch.setattr(child_codex, "resolve_child_codex", lambda _: SimpleNamespace(argv=(str(binary),)))

    path = child_home / "policy.json"
    policy = json.loads(path.read_text())
    driver = "codex.app-server" if is_codex else "claude.agent-sdk"
    allow_adapters(child_home, monkeypatch, driver)
    frames = []
    send = ChildSession.send_frame

    async def record(session, stream, kind, payload):
        if stream:
            frames.append((kind, payload))
        await send(session, stream, kind, payload)

    monkeypatch.setattr(ChildSession, "send_frame", record)
    store = ConnectionStateStore()
    await store.upsert(
        "sub", auth="ok", plan="old plan", quota=[{"window": "5h", "used_percent": 24}],
        quota_source="official_rpc", last_error="old error",
    )
    async with child_session(child_home):
        result = await connections_probe.probe_connection("sub", cfg=child_config(provider), store=store)
        assert result["child_state"] == "online"
        assert (result["auth"], result["quota"], result["quota_source"], result["plan"], result["last_error"]) == (
            auth, [], None, None, "child could not determine login status" if auth == "unknown" else None,
        )
        assert frames == [("status_result", {"state": state})]

        allow_logins(child_home, provider)
        frames.clear()
        assert await fetch_child_login_status("box", provider, None, 2) == state
        assert frames == [("status_result", {"state": state})]

        path.write_text(json.dumps({**policy, "adapters": [], "logins": [provider]}))
        with pytest.raises(ChildStreamError, match="status refused by child policy"):
            await fetch_child_login_status("box", provider, None, 2)
        assert (home / "status-runs").read_text().splitlines() == ["status"]


async def test_old_child_without_login_status_feature(hub, child_home, monkeypatch):
    allow_adapters(child_home, monkeypatch, "claude.agent-sdk")
    on_hello = hub._on_hello

    async def old_hello(ws, mux, payload):
        payload.pop("quota_providers")
        await on_hello(ws, mux, {**payload, "features": ["codex-turns"]})

    monkeypatch.setattr(hub, "_on_hello", old_hello)
    monkeypatch.setattr(child_ops.ChildOps, "_status", fail_host_launch)
    sent = []
    send = ChildStream.send

    async def track(stream, kind, payload):
        sent.append(kind)
        await send(stream, kind, payload)

    monkeypatch.setattr(ChildStream, "send", track)
    store = ConnectionStateStore()
    await store.upsert("sub", auth="ok", quota=[{"window": "5h", "used_percent": 24}], last_error="old error")
    async with child_session(child_home):
        with pytest.raises(ChildFeatureUnsupported, match="login-status"):
            ChildStream("box", feature="login-status")
        assert await fetch_child_login_status("box", "claude", None, 2) == "unknown"
        result = await connections_probe.probe_connection("sub", cfg=child_config(), store=store)
        assert (result["child_state"], result["auth"], result["quota"], result["last_error"]) == (
            "online", "unknown", [], "child does not support login-status",
        )
        assert (await store.get("sub"))["last_error"] == result["last_error"]
        assert sent == ["quota"]
        assert hub.live_for("box") is not None


async def test_console_child_presence_without_a_provider_probe(hub, child_home, monkeypatch):
    cfg = child_config()
    monkeypatch.setattr(admin_settings, "get_config", lambda: cfg)
    monkeypatch.setattr("kotoyose.child_relay.fetch_child_quota", fail_host_launch)
    monkeypatch.setattr("kotoyose.child_relay.fetch_child_login_status", fail_host_launch)
    app = FastAPI()
    app.include_router(admin_settings.router)
    app.include_router(admin_children.router)
    app.dependency_overrides[require_basic_auth] = lambda: None
    app.state.child_hub = hub
    app.state.connection_state_store = ConnectionStateStore()
    await app.state.connection_state_store.upsert(
        "sub", auth="ok", quota=[{"window": "5h", "used_percent": 24}], last_error="stale error",
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async def assert_state(expected, child_presence):
            [conn] = (await client.get("/admin/connections")).json()["connections"]
            [child] = (await client.get("/admin/children")).json()["children"]
            probe = (await client.post("/admin/connections/sub/probe")).json()
            assert child["child_state"] == child_presence
            state = conn["state"]
            assert state["child_state"] == expected
            assert state["workspace_policy"] == child["workspace_policy"]
            if child_presence == "online":
                assert state["workspace_policy"]["codex_sandbox_max"] == "workspace-write"
            if child_presence == "online":
                assert probe["state"]["last_heartbeat"] >= child["last_heartbeat"] >= state["last_heartbeat"]
            else:
                assert state["last_heartbeat"] == probe["state"]["last_heartbeat"] == child["last_heartbeat"]
            assert state["quota"] == []
            assert state["auth"] == "unknown"
            assert state["last_error"] is None
            for projected in (probe["state"], probe["connection"]["state"]):
                assert {k: v for k, v in projected.items() if k != "last_heartbeat"} == {
                    k: v for k, v in state.items() if k != "last_heartbeat"
                }

        await assert_state("waiting", "waiting")
        # Advertising a different provider still leaves the child itself online.
        allow_adapters(child_home, monkeypatch, "codex.app-server")
        allow_logins(child_home, "claude")
        async with child_session(child_home):
            await assert_state("not_enabled", "online")
            await hub.revoke("box")
            await assert_state("revoked", "revoked")
        await hub.stop()
        del app.state.child_hub
        monkeypatch.setenv("KOTOYOSE_CHILD_HOST_HOME", str(hub.home))
        await assert_state("revoked", "revoked")


async def test_reenrolled_child_starts_without_the_previous_login_or_quota(hub, child_home, tmp_path, monkeypatch):
    cfg = child_config()
    store = ConnectionStateStore()
    await store.upsert(
        "sub", auth="ok", checked_at=datetime.now(timezone.utc), quota=[{"window": "5h", "used_percent": 24}],
    )
    await hub.revoke("box")
    new_home = tmp_path / "replacement-child"
    await enroll_child(hub.issue_enrollment_code(f"127.0.0.1:{hub.port}"), new_home, name="box")
    allow_adapters(new_home, monkeypatch, "claude.agent-sdk")
    allow_logins(new_home, "claude")

    async def timeout(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr("kotoyose.child_relay.fetch_child_quota", timeout)
    monkeypatch.setattr("kotoyose.child_relay.fetch_child_login_status", timeout)
    async with child_session(new_home):
        view = connections_probe.child_connection_view(cfg.connections["sub"], await store.get("sub"))
        assert view["child_state"] == "online"
        assert view["auth"] == "unknown"
        assert view["checked_at"] is None
        assert view["quota"] == []
        probe = await connections_probe.probe_connection("sub", cfg=cfg, store=store)
        assert probe["auth"] == "unknown"
        assert probe["quota"] == []
        assert (await store.get("sub"))["quota"] == []


async def test_quota_sharing_updates_on_the_same_python_link(hub, child_home, monkeypatch):
    async def quota(ops, stream, payload):
        await ops._send(stream, "quota_result", {"auth": "ok", "quota": []})

    monkeypatch.setattr(child_ops.ChildOps, "_quota", quota)
    allow_adapters(child_home, monkeypatch, "claude.agent-sdk")
    conn = child_config().connections["sub"]
    store = ConnectionStateStore()
    state = await store.upsert("sub", auth="ok", checked_at=datetime.now(timezone.utc),
                               quota=[{"window": "5h", "used_percent": 24}])
    async with child_session(child_home, heartbeat_interval=0.02):
        live = hub.live_for("box")
        with pytest.raises(ChildStreamError, match="quota refused by child policy"):
            await fetch_child_quota("box", "claude", None, 2)
        allow_logins(child_home, "ANThropic")
        async with asyncio.timeout(2):
            while live.quota_providers != {"claude"}:
                await asyncio.sleep(0.01)
        assert await fetch_child_quota("box", "claude", None, 2) == {"auth": "ok", "quota": []}
        assert connections_probe.child_connection_view(conn, state)["quota"] == state["quota"]
        allow_logins(child_home)
        async with asyncio.timeout(2):
            while live.quota_providers:
                await asyncio.sleep(0.01)
        with pytest.raises(ChildStreamError, match="quota refused by child policy"):
            await fetch_child_quota("box", "claude", None, 2)
        view = connections_probe.child_connection_view(conn, state)
        assert view["auth"] == "ok"
        assert view["quota"] == []
        assert hub.live_for("box") is live


async def test_enabled_provider_without_pinned_status(child_home, monkeypatch):
    path = child_home / "policy.json"
    policy = json.loads(path.read_text())
    path.write_text(json.dumps({**policy, "adapters": ["antigravity.sdk"]}))
    monkeypatch.setattr(child_ops, "resolve_child_binary", fail_host_launch)

    async with child_session(child_home):
        assert await fetch_child_login_status("box", "antigravity", None, 2) == "unknown"
        with pytest.raises(ChildStreamError, match="does not match the pinned profile"):
            await fetch_child_login_status("box", "antigravity", "grok", 2)


@pytest.mark.parametrize(("stdout", "stderr", "code", "state"), [
    (b'{"status":"SUCCESS"}', b"", 0, "logged_in"),
    (b'{"status":"FAILURE"}', b"", 0, "unknown"),
    (b'{"status":null}', b"", 0, "unknown"),
    (b'{"status":[]}', b"", 0, "unknown"),
    (b'{"status":0}', b"", 0, "unknown"),
    (b"{}", b"", 0, "unknown"),
    (b'{"status":false}', b"", 1, "logged_out"),
    (b'{"status":"UNAUTHENTICATED"}', b"", 0, "logged_out"),
    (b"not authenticated", b"", 0, "logged_out"),
    (b"", b"not authenticated", 1, "logged_out"),
    (b"", b"unrelated error", 1, "unknown"),
])
def test_generic_login_state_requires_an_explicit_signed_out_signal(stdout, stderr, code, state):
    spec = replace(status_spec("grok", None), success_json_field="status")
    assert child_ops._login_state("grok", spec, stdout, stderr, code) == state


async def test_status_cache_coalesces_by_provider_and_profile_and_expires(tmp_path, monkeypatch):
    (tmp_path / "policy.json").write_text(json.dumps({
        "workspace_roots": [], "mcp": {}, "adapters": ["cursor.sdk", "claude.agent-sdk"],
    }))
    clock = 100
    monkeypatch.setattr(child_ops, "monotonic", lambda: clock)
    started, release = asyncio.Event(), asyncio.Event()
    calls, frames = [], {}

    async def probe(ops, provider, profile, spec):
        calls.append((provider, profile))
        started.set()
        await release.wait()
        return "logged_in"

    async def send(stream, kind, payload):
        frames[stream] = (kind, payload)

    monkeypatch.setattr(child_ops.ChildOps, "_read_login_state", probe)
    ops = child_ops.ChildOps(tmp_path, send)

    async def status(stream, provider="cursor", profile=None):
        return await ops.handle(Frame(stream, "status", {"provider": provider, "profile": profile}))

    try:
        async with asyncio.timeout(2):
            first = await status(1)
            await started.wait()
            second = await status(2)
            await ops.handle(Frame(1, "status_cancel", {}))
            await asyncio.gather(first, return_exceptions=True)
            release.set()
            await second
            clock = 159
            await (await status(3))
            assert calls == [("cursor", None)]
            await (await status(4, profile="cursor"))
            await (await status(5, provider="claude"))
            clock = 160
            await (await status(6))
    finally:
        await ops.finish()

    assert calls == [("cursor", None), ("cursor", "cursor"), ("claude", None), ("cursor", None)]
    assert frames == {stream: ("status_result", {"state": "logged_in"}) for stream in range(2, 7)}


async def test_console_login_and_quota_online_then_offline(hub, child_home, fake_cli, monkeypatch):
    _, install = fake_cli
    install("""
        import json
        import os
        import sys

        assert "CLAUDE_CONFIG_DIR" not in os.environ
        args = sys.argv[1:]
        if args == ["auth", "login", "--claudeai"]:
            assert sys.stdin.isatty()
            print("Go to https://claude.ai@unexpected.example/authorize")
            print("Go to https://claude.ai/authorize?state=fake")
            print("sk" "-ant-oat-never-relay", flush=True)
            assert input() == "fake-code"
        elif args == ["auth", "status", "--json"]:
            print(json.dumps({"loggedIn": True, "subscriptionType": "Pro", "email": "private@example.invalid"}))
        elif args == ["-p", "/usage", "--output-format", "json"]:
            print(json.dumps({"result": "Current session: 24% used · resets 11:05pm (UTC)"}))
        else:
            sys.exit(2)
    """)
    allow_logins(child_home, "ClAuDe")
    allow_adapters(child_home, monkeypatch, "claude.agent-sdk")
    cfg = child_config("anthropic")
    store = ConnectionStateStore()
    app = FastAPI()
    app.include_router(admin_settings.router)
    app.dependency_overrides[require_basic_auth] = lambda: None
    app.state.connection_state_store = store
    monkeypatch.setattr(admin_settings, "get_config", lambda: cfg)
    frames = []
    send = ChildSession.send_frame

    async def record(session, stream, kind, payload):
        frames.append((kind, payload))
        await send(session, stream, kind, payload)

    monkeypatch.setattr(ChildSession, "send_frame", record)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        async with child_session(child_home):
            start = await client.post("/admin/connections/sub/cli-login")
            assert start.status_code == 200, start.text
            login = start.json()
            assert login["state"] == "waiting_for_code"
            assert login["url"] == "https://claude.ai/authorize?state=fake"
            submit = await client.post(
                f"/admin/connections/sub/cli-login/{login['session_id']}/code", json={"code": "fake-code"},
            )
            assert submit.json() == {"state": "completed"}
            rows = (await client.get("/admin/connections")).json()["connections"]
            state = rows[0]["state"]
            assert state["quota"][0]["used_percent"] == 24
            assert state["quota_source"] == "claude_cli_usage"
            assert state["auth"] == "ok"
        async with asyncio.timeout(2):
            while hub.live_for("box") is not None:
                await asyncio.sleep(0.01)
        rows = (await client.get("/admin/connections")).json()["connections"]
        offline = await connections_probe.probe_connection("sub", cfg=cfg, store=store)
        assert rows[0]["state"] == offline
        assert offline["child_state"] == "disconnected"
        assert offline["last_heartbeat"] == hub.registry.get("box")["last_heartbeat"]
        assert offline["auth"] == "unknown"
        assert offline["quota"] == []
        assert offline["last_error"] is None
        assert offline["checked_at"] == state["checked_at"]
    wire = repr(frames)
    for private in ("sk" "-ant-oat-never-relay", "private@example.invalid", "unexpected.example", "fake-code"):
        assert private not in wire


async def test_policy_and_pinned_argv_refusals(child_home, monkeypatch):
    def unexpected(*_):
        pytest.fail("A refused command resolved a child binary")

    monkeypatch.setattr(child_ops, "resolve_child_binary", unexpected)
    path = child_home / "policy.json"
    policy = json.loads(path.read_text())
    path.write_text(json.dumps({**policy, "adapters": ["claude.agent-sdk"]}))
    async with child_session(child_home):
        with pytest.raises(cli_login.CliLoginStartupError, match="refused by child policy"):
            await cli_login.start_cli_login_session("sub", cfg=child_config())
        with pytest.raises(ChildStreamError, match="quota refused by child policy"):
            await fetch_child_quota("box", "claude", None, 2)
        allow_logins(child_home, "claude")
        for kind, payload in (
            ("login", {"flow": "paste_code", "command": ["setup-token"]}),
            ("login", {"flow": "device_code", "command": ["auth", "login", "--claudeai"]}),
            ("quota", {"command": ["-p", "arbitrary prompt", "--output-format", "json"]}),
        ):
            stream = ChildStream("box")
            try:
                await stream.send(kind, {"provider": "claude", **payload})
                with pytest.raises(ChildStreamError, match="does not match the pinned profile"):
                    await asyncio.wait_for(stream.receive(f"{kind}_output", f"{kind}_result"), 2)
            finally:
                await stream.close(f"{kind}_cancel")


async def test_host_rechecks_the_url_from_the_child(child_home, fake_cli, monkeypatch):
    _, install = fake_cli
    install("""
        print("https://claude.ai/authorize", flush=True)
        input()
    """)
    allow_logins(child_home, "claude")
    send = ChildSession.send_frame

    async def change_url(session, stream, kind, payload):
        if kind == "login_output":
            payload = {**payload, "url": "https://claude.ai@unexpected.example/authorize"}
        await send(session, stream, kind, payload)

    monkeypatch.setattr(ChildSession, "send_frame", change_url)
    async with child_session(child_home):
        with pytest.raises(cli_login.CliLoginStartupError, match="approved sign-in URL"):
            await cli_login.start_cli_login_session("sub", cfg=child_config())


@pytest.mark.parametrize("action", ["cancel", "disconnect", "cap"])
async def test_login_stops_process_group_and_descendants(hub, child_home, fake_cli, monkeypatch, action):
    home, install = fake_cli
    install("""
        import os
        from pathlib import Path
        import subprocess
        import sys
        import time

        descendant = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], process_group=0,
        )
        (Path.home() / "pids").write_text(f"{os.getpid()} {descendant.pid}")
        print("https://claude.ai/authorize", flush=True)
        time.sleep(30)
    """)
    allow_logins(child_home, "claude")
    if action == "cap":
        monkeypatch.setattr(child_ops, "LOGIN_CAP_SECONDS", 0.5)
    async with child_session(child_home):
        login = await cli_login.start_cli_login_session("sub", cfg=child_config())
        sid = login["session_id"]
        pids = [int(item) for item in (home / "pids").read_text().split()]
        if action == "cancel":
            assert (await cli_login.cancel_cli_login_session("sub", sid))["state"] == "cancelled"
        elif action == "disconnect":
            await hub.disconnect("box")
        async with asyncio.timeout(3):
            while any(process_running(pid) for pid in pids):
                await asyncio.sleep(0.02)
            while (state := await cli_login.get_cli_login_status("sub", sid))["state"] == "waiting_for_code":
                await asyncio.sleep(0.02)
        if action == "disconnect":
            assert state["state"] == "failed" and "link closed" in state["error"]
        if action == "cap":
            assert (state["state"], state["error"]) == ("expired", "Login session expired")


def process_running(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except FileNotFoundError:
        return False


async def test_device_login_and_pinned_quota_rpc(child_home, fake_cli, monkeypatch):
    home, install = fake_cli
    install("""
        import json
        from pathlib import Path
        import sys
        import time

        args = sys.argv[1:]
        if args == ["login", "--device-auth"]:
            print("Use https://auth.x.ai/device")
            print("Code: AbC1-2deF", flush=True)
            while not (Path.home() / "signed-in").exists():
                time.sleep(0.01)
        elif args == ["models"]:
            assert (Path.home() / "signed-in").exists()
            print("grok-build-0.1")
        else:
            assert args == ["agent", "stdio"]
            for method, result in (
                ("initialize", {}),
                ("_x.ai/billing", {"config": {
                    "creditUsagePercent": 25,
                    "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "end": "2026-12-01T00:00:00Z"},
                }}),
            ):
                request = json.loads(input())
                assert request["method"] == method
                print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
    """)
    allow_logins(child_home, "grok")
    allow_adapters(child_home, monkeypatch, "grok")
    cfg = child_config("grok", profile="grok")
    async with child_session(child_home):
        login = await cli_login.start_cli_login_session("sub", cfg=cfg)
        assert (login["flow"], login["state"], login["user_code"]) == (
            "device_code", "waiting_for_browser", "AbC1-2deF",
        )
        (home / "signed-in").touch()
        async with asyncio.timeout(3):
            while (state := await cli_login.get_cli_login_status("sub", login["session_id"]))[
                "state"
            ] == "waiting_for_browser":
                await asyncio.sleep(0.02)
        assert state["state"] == "completed"
        quota = await connections_probe.probe_connection("sub", cfg=cfg, store=ConnectionStateStore())
        assert quota["quota_source"] == "grok_acp_billing"
        assert quota["quota"][0]["used_percent"] == 25


async def test_windows_refuses_pty_but_runs_quota(child_home, fake_cli, monkeypatch):
    _, install = fake_cli
    install("""
        import sys

        assert not sys.stdin.isatty()
        if sys.argv[1:] == ["auth", "status", "--json"]:
            print('{"loggedIn": true}')
        else:
            assert sys.argv[1:] == ["-p", "/usage", "--output-format", "json"]
            print('{"result": "Current session: 31% used · resets 11:05pm (UTC)"}')
    """)
    monkeypatch.setattr(child_ops, "sys", SimpleNamespace(platform="win32"))
    allow_logins(child_home, "anthropic")
    allow_adapters(child_home, monkeypatch, "claude.agent-sdk")
    async with child_session(child_home):
        with pytest.raises(cli_login.CliLoginStartupError, match="Windows child does not support login relay"):
            await cli_login.start_cli_login_session("sub", cfg=child_config())
        state = await connections_probe.probe_connection("sub", cfg=child_config(), store=ConnectionStateStore())
        assert state["quota"][0]["used_percent"] == 31


@pytest.mark.parametrize("logins", [None, "claude", ["claude", "CLAUDE"], ["bad provider"], [1]])
def test_policy_rejects_invalid_login_list(tmp_path, logins):
    path = tmp_path / "policy.json"
    base = {"workspace_roots": [], "adapters": [], "mcp": {}}
    path.write_text(json.dumps(base))
    assert load_policy(path).logins == ()
    path.write_text(json.dumps({**base, "logins": logins}))
    with pytest.raises(ValueError, match="logins"):
        load_policy(path)


@pytest.mark.parametrize(("stream", "kind", "payload"), [
    (0, "quota_cancel", {}),
    (1, "login", {"provider": "claude", "flow": [], "command": ["auth"]}),
    (1, "login", {"provider": "claude", "flow": "paste_code", "command": ["a" * 129]}),
    (1, "login_input", {"code": "line\nbreak"}),
    (1, "login_input", {"code": "a" * 513}),
    (1, "login_output", {"url": "a" * 4097}),
    (1, "login_exit", {"returncode": 0, "success": "true"}),
    (0, "status", {"provider": "claude"}),
    (1, "status", {"provider": "claude", "command": ["auth", "login"]}),
    (1, "status", {"provider": "x" * 65}),
    (1, "status", {"provider": "claude", "profile": "x" * 65}),
    (1, "status_result", {"state": "logged_in", "quota": []}),
    (1, "status_result", {"state": "x" * 65}),
    (1, "quota_result", {"auth": "ok", "quota": [{"window": "5h", "used_percent": float("nan")}]}),
])
def test_child_frame_constraints(stream, kind, payload):
    with pytest.raises(ValueError):
        decode_frame(encode_frame(stream, kind, payload))


async def test_codex_quota_uses_official_sdk_without_reading_auth(child_home, fake_cli, monkeypatch):
    import openai_codex

    home, install = fake_cli
    codex_home = home / "custom-codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    install("""
        import os
        from pathlib import Path
        import sys

        assert os.environ["CODEX_HOME"] == str(Path.home() / "custom-codex-home")
        assert sys.argv[1:] == ["--version"]
        (Path.home() / "version-checked").touch()
        print("codex-cli 1.2.3")
    """)
    monkeypatch.setattr(connections_probe, "read_subscription_expiry", fail_host_launch)

    class Sdk:
        def __init__(self, config):
            self._client = self
            assert config.env["CODEX_HOME"] == str(codex_home)
            assert f"CODEX_HOME={codex_home}" in config.launch_args_override
            assert config.launch_args_override[-3:] == ("app-server", "--listen", "stdio://")

        async def account(self, *, refresh_token):
            assert not refresh_token
            return SimpleNamespace(account=SimpleNamespace(plan_type="plus"))

        async def request(self, method, params, *, response_model):
            assert method == "account/rateLimits/read" and params is None
            return SimpleNamespace(rate_limits=SimpleNamespace(primary=SimpleNamespace(used_percent=36)))

        async def models(self, *, include_hidden):
            assert not include_hidden

        async def close(self):
            pass

    monkeypatch.setattr(openai_codex, "AsyncCodex", Sdk)
    allow_logins(child_home, "codex")
    allow_adapters(child_home, monkeypatch, "codex.app-server")
    async with child_session(child_home):
        state = await connections_probe.probe_connection(
            "sub", cfg=child_config("codex"), store=ConnectionStateStore(), force_refresh=True,
        )
        assert state["quota_source"] == "official_rpc"
        assert state["quota"][0]["used_percent"] == 36
        assert (home / "version-checked").exists()


async def test_browser_login_requires_successful_child_status(child_home, fake_cli):
    _, install = fake_cli
    install("""
        import os
        import sys

        if sys.argv[1:] == ["login"]:
            assert os.environ["NO_OPEN_BROWSER"] == "1"
            print("https://cursor.com/login", flush=True)
        else:
            assert sys.argv[1:] == ["status", "--format", "json"]
            print('{"authenticated": false}')
            sys.exit(1)
    """)
    allow_logins(child_home, "cursor")
    async with child_session(child_home):
        login = await cli_login.start_cli_login_session("sub", cfg=child_config("cursor", profile="cursor"))
        assert login["flow"] == "browser_wait"
        async with asyncio.timeout(3):
            while (state := await cli_login.get_cli_login_status("sub", login["session_id"]))[
                "state"
            ] == "waiting_for_browser":
                await asyncio.sleep(0.02)
        assert state["state"] == "failed"
        assert "did not confirm sign-in" in state["error"]


async def test_pty_spawn_survives_repeated_cancellation(tmp_path, monkeypatch):
    spawn_started = asyncio.Event()
    finish_spawn = asyncio.Event()
    fd, writer = os.pipe()
    os.close(writer)
    events = []

    async def spawn(*args, **kwargs):
        spawn_started.set()
        await finish_spawn.wait()
        return SimpleNamespace(pid=123, returncode=None, wait=wait), fd

    async def wait():
        events.append("wait")
        with pytest.raises(OSError):
            os.fstat(fd)

    monkeypatch.setattr(cli_login, "spawn_pty_process", spawn)
    monkeypatch.setattr(child_ops.os, "killpg", lambda pid, sig: events.append((pid, sig)))
    monkeypatch.setattr(child_ops, "kill_session", lambda pid: events.append(("session", pid)))
    session = SimpleNamespace(cli_binary=tmp_path / "fake", temp_cwd=tmp_path)

    async def login():
        async with child_ops.ChildOps._pty(session, ("login",), {}):
            pytest.fail("A cancelled spawn entered the PTY context")

    task = asyncio.create_task(login())
    try:
        await spawn_started.wait()
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
        finish_spawn.set()
        async with asyncio.timeout(2):
            with pytest.raises(asyncio.CancelledError):
                await task
        assert events == [(123, signal.SIGKILL), ("session", 123), "wait"]
    finally:
        finish_spawn.set()
        await asyncio.gather(task, return_exceptions=True)
        if not events:
            os.close(fd)


def test_reaped_process_is_never_signalled(monkeypatch):
    def unexpected(*_):
        pytest.fail("Attempted to signal a reaped process")

    monkeypatch.setattr(child_ops.os, "killpg", unexpected)
    monkeypatch.setattr(child_ops, "kill_session", unexpected)
    child_ops.ChildOps._kill_process_session(SimpleNamespace(pid=123, returncode=0))


@pytest.mark.parametrize(("cancel", "leader_exited"), [(False, False), (True, False), (True, True)])
async def test_status_exec_signals_only_a_live_group(monkeypatch, cancel, leader_exited):
    class Process:
        pid = 123
        returncode = None
        stdout = None
        stderr = None

        async def wait(self):
            self.returncode = 0
            return self.returncode

        def kill(self):
            calls.append(("kill", self.pid))

    process = Process()
    calls = []
    draining = asyncio.Event()

    async def spawn(*args, **kwargs):
        assert kwargs["start_new_session"]
        assert child_ops.TURN_PIDS.get() is None
        return process

    async def drain(*args):
        draining.set()
        if cancel:
            await asyncio.Future()

    monkeypatch.setattr(child_ops.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(connections_probe, "_drain_probe_output", drain)
    monkeypatch.setattr(child_ops, "kill_process_group", lambda pgid: calls.append(("group", pgid)))
    monkeypatch.setattr(child_ops, "kill_session", lambda pid: calls.append(("session", pid)))
    async with child_ops._probe_scope(2):
        task = asyncio.create_task(child_ops.ChildOps._exec(("stub",), {}))
        if cancel:
            await draining.wait()
            if leader_exited:
                process.returncode = 0
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task == (b"", b"", 0)
    expected = [("group", 123), ("session", 123), ("kill", 123)] if cancel and not leader_exited else []
    assert calls == expected


async def test_busy_child_replies_to_login_and_quota(child_home, monkeypatch):
    started = asyncio.Event()
    unblock = asyncio.Event()
    monkeypatch.setattr(child_ops, "_REQUEST_LIMIT", 1)

    async def hold_quota(ops, stream, payload):
        started.set()
        await unblock.wait()
        await ops._send(stream, "quota_result", {"auth": "ok", "quota": []})

    monkeypatch.setattr(child_ops.ChildOps, "_quota", hold_quota)
    allow_logins(child_home, "claude")
    async with child_session(child_home):
        async with asyncio.timeout(3):
            pending = asyncio.create_task(fetch_child_quota("box", "claude", None, 3))
            try:
                await started.wait()
                for kind, payload in (
                    ("login", {"flow": "paste_code", "command": ["auth", "login", "--claudeai"]}),
                    ("quota", {"command": ["-p", "/usage", "--output-format", "json"]}),
                ):
                    stream = ChildStream("box")
                    try:
                        await stream.send(kind, {"provider": "claude", **payload})
                        with pytest.raises(ChildStreamError, match="^child busy$"):
                            await stream.receive(f"{kind}_result")
                    finally:
                        await stream.close(f"{kind}_cancel")
            finally:
                unblock.set()
                assert await pending == {"auth": "ok", "quota": []}


@pytest.mark.parametrize(("kind", "payload"), [
    ("login_error", {"message": ""}),
    ("login_error", {"message": "x" * (LOGIN_MESSAGE_LIMIT + 1)}),
    ("login_exit", {"returncode": 1_000_000, "success": False}),
    ("quota_result", {"auth": "invalid", "quota": []}),
])
async def test_child_sends_a_valid_error_for_invalid_output(tmp_path, kind, payload):
    frames = []

    async def send(stream, response, body):
        frames.append(decode_frame(encode_frame(stream, response, body)))

    await child_ops.ChildOps(tmp_path, send)._send(1, kind, payload)
    operation = kind.split("_", 1)[0]
    assert [(frame.kind, frame.payload) for frame in frames] == [
        (f"{operation}_error", {"message": f"{operation} failed"}),
    ]


async def test_invalid_frames_in_both_directions_keep_the_link_open(hub, child_home, monkeypatch):
    async def quota(ops, stream, payload):
        await ops._send(stream, "quota_result", child_ops._public_quota({
            "auth": "ok",
            "quota": [{"window": "x" * 65, "label": "y" * 201, "used_percent": 24}],
        }))

    monkeypatch.setattr(child_ops.ChildOps, "_quota", quota)
    allow_logins(child_home, "claude")
    async with child_session(child_home) as session:
        live = hub.live_for("box")
        async with live.lock:
            await live.ws.send(encode_frame(1, "login_input", {"code": ""}))
            await live.ws.send("[" * 2000 + "0" + "]" * 2000)
        await session.send_frame(1, "login_error", {"message": ""})
        result = await fetch_child_quota("box", "claude", None, 2)
        [item] = result["quota"]
        assert item["window"] == "x" * QUOTA_WINDOW_LIMIT
        assert item["label"] == "y" * LOGIN_MESSAGE_LIMIT


@pytest.mark.parametrize("failure", [
    TimeoutError(),
    {"auth": "unknown", "quota": [], "plan": None, "last_error": "quota unavailable"},
    {"auth": "ok", "quota": [], "plan": None, "last_error": "quota unavailable"},
])
@pytest.mark.parametrize("status", ["logged_out", None])
async def test_child_quota_errors_preserve_the_last_good_quota(child_home, monkeypatch, failure, status):
    async def fetch(*args):
        if isinstance(failure, TimeoutError):
            raise failure
        return failure

    async def login_status(*_args, **_kwargs):
        if status is None:
            raise TimeoutError
        return status

    monkeypatch.setattr("kotoyose.child_relay.fetch_child_quota", fetch)
    allow_adapters(child_home, monkeypatch, "claude.agent-sdk")
    allow_logins(child_home, "claude")
    monkeypatch.setattr("kotoyose.child_relay.fetch_child_login_status", login_status)
    store = ConnectionStateStore()
    previous = await store.upsert(
        "sub", auth="ok", plan="Pro", quota=[{"window": "5h", "used_percent": 24}],
        quota_source="claude_cli_usage", checked_at=datetime.now(timezone.utc),
    )
    async with child_session(child_home):
        state = await connections_probe.probe_connection("sub", cfg=child_config(), store=store)
    assert state["child_state"] == "online"
    expected_auth = status or (failure["auth"] if isinstance(failure, dict) else "unknown")
    assert state["auth"] == expected_auth
    assert state["quota"] == previous["quota"]
    quota_error = "child quota timed out" if isinstance(failure, TimeoutError) else "quota unavailable"
    assert state["last_error"] == (f"child login status timed out / {quota_error}" if expected_auth == "unknown" else quota_error)
    assert state["checked_at"] > previous["checked_at"]


async def test_console_put_persists_child_on(tmp_path, monkeypatch):
    path = tmp_path / "test-connections.yaml"
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    monkeypatch.setattr(config, "_config", child_config())
    app = FastAPI()
    app.include_router(admin_settings.router)
    app.dependency_overrides[require_basic_auth] = lambda: None
    app.state.connection_state_store = ConnectionStateStore()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put("/admin/connections/sub", json={
            "provider": "claude", "kind": "subscription", "data_policy": "no_training",
            "on": "child:other", "show_quota": False,
        })
        assert response.status_code == 200, response.text
        assert response.json()["connection"]["on"] == "child:other"
    assert config.get_config().connections["sub"].on == "child:other"
    saved = yaml.safe_load(path.read_text())["connections"]["sub"]
    assert (saved["on"], saved["show_quota"]) == ("child:other", False)
