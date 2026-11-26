"""K1: child enrollment, mutual pin, hello, heartbeat, and revoke on localhost TLS."""

from __future__ import annotations

import ast
import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from importlib.metadata import version as package_version

from kotoyose.child_client import (
    ChildRejected,
    HostFingerprintMismatch,
    child_session,
    enroll_child,
    run_child,
)
from kotoyose.child_identity import default_child_home
from kotoyose.child_protocol import PROTOCOL_VERSION, WORKSPACE_POLICY_FEATURE, StreamMux, decode_frame, encode_frame
from kotoyose.child_server import ChildHub, child_listen_config
from kotoyose.config import HarnessConfig, use_config
from kotoyose.routes.admin_children import router as children_router

_HOST_VERSION = package_version("kotoyose")


def _policy(root: Path) -> dict:
    return {
        "workspace_roots": [str(root)],
        "adapters": ["not-installed-adapter"],
        "mcp": {
            "files": {
                "description": "workspace files",
                "command": "npx",
                "args": ["-y", "secret-server"],
                "env": {"TOKEN": "SUPER_SECRET"},
            }
        },
    }


def _write_policy(home: Path, root: Path) -> bytes:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "policy.json"
    path.write_text(json.dumps(_policy(root)), encoding="utf-8")
    return path.read_bytes()


def _retarget_fingerprint(code: str, fingerprint: str) -> str:
    """Rebuild the enrollment code with a different host fingerprint, keeping its signature."""
    kind, body, signature = code.split(".")
    pad = "=" * (-len(body) % 4)
    payload = json.loads(base64.urlsafe_b64decode(body + pad))
    payload["fp"] = fingerprint
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    new_body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{kind}.{new_body}.{signature}"


def _flip_signature(code: str) -> str:
    kind, body, signature = code.split(".")
    pad = "=" * (-len(signature) % 4)
    raw = bytearray(base64.urlsafe_b64decode(signature + pad))
    raw[0] ^= 0x01
    new_signature = base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
    return f"{kind}.{body}.{new_signature}"


async def _wait_child(hub: ChildHub, name: str, *, online: bool, timeout: float = 1.5) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        rows = [row for row in hub.list_children() if row["name"] == name]
        if rows:
            last = rows[0]
            if last["online"] is online:
                return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {name} online={online}: {last}")


@pytest.fixture
async def hub(tmp_path: Path):
    child_hub = ChildHub(tmp_path / "host", heartbeat_interval=0.05)
    await child_hub.start("127.0.0.1", 0)
    try:
        yield child_hub
    finally:
        await child_hub.stop()


def _address(hub: ChildHub) -> str:
    return f"127.0.0.1:{hub.port}"


async def test_enrollment_happy_path_pins_identity_and_lists_the_child(hub: ChildHub, tmp_path: Path):
    """A real code enrolls once, and the admin list shows the child after hello."""
    home = tmp_path / "child"
    _write_policy(home, tmp_path / "work")
    code = hub.issue_enrollment_code(_address(hub))
    await enroll_child(code, home, name="box")
    async with child_session(home, heartbeat_interval=0.05) as session:
        await session.ready.wait()
        row = await _wait_child(hub, "box", online=True)
    assert row["name"] == "box"
    assert row["generation"] == 1
    assert row["enrolled_at"]
    assert row["last_heartbeat"]
    assert row["version"] == _HOST_VERSION
    assert row["protocol"] == PROTOCOL_VERSION
    assert "not-installed-adapter" not in row["adapters"]
    assert row["mcp"] == ["files"]
    assert row["workspace_policy"] == {
        "workspace_roots": [str(tmp_path / "work")], "codex_sandbox_max": "workspace-write",
    }
    assert session.generation == 1


async def test_enrollment_refuses_wrong_host_fingerprint_before_sending_the_code(hub: ChildHub, tmp_path: Path):
    """The child closes before the enrollment code reaches the host, and the code still works."""
    home = tmp_path / "child"
    code = hub.issue_enrollment_code(_address(hub))
    bad = _retarget_fingerprint(code, "0" * 64)
    with pytest.raises(HostFingerprintMismatch):
        await enroll_child(bad, home, name="box")
    assert hub.enrollment_messages == 0
    await enroll_child(code, home, name="box")
    assert hub.enrollment_messages == 1
    assert hub.list_children()[0]["name"] == "box"


async def test_enrollment_code_is_one_time(hub: ChildHub, tmp_path: Path):
    """The second presentation of a spent code is rejected and does not replace the child."""
    home = tmp_path / "child"
    code = hub.issue_enrollment_code(_address(hub))
    await enroll_child(code, home, name="box")
    with pytest.raises(ChildRejected, match="already used"):
        await enroll_child(code, home, name="other")
    assert [row["name"] for row in hub.list_children()] == ["box"]


async def test_enrollment_code_expires(hub: ChildHub, tmp_path: Path):
    """A code issued already past its lifetime is rejected and enrolls nobody."""
    home = tmp_path / "child"
    code = hub.issue_enrollment_code(_address(hub), ttl_seconds=0)
    with pytest.raises(ChildRejected, match="expired"):
        await enroll_child(code, home, name="box")
    assert hub.list_children() == []


async def test_enrollment_code_attempt_limit(hub: ChildHub, tmp_path: Path):
    """Three rejected presentations burn the code even if the original is offered next."""
    home = tmp_path / "child"
    code = hub.issue_enrollment_code(_address(hub))
    for _ in range(3):
        with pytest.raises(ChildRejected, match="rejected"):
            await enroll_child(_flip_signature(code), home, name="box")
    with pytest.raises(ChildRejected, match="attempts exhausted"):
        await enroll_child(code, home, name="box")
    assert hub.list_children() == []


async def test_reconnect_uses_the_pinned_identity(hub: ChildHub, tmp_path: Path):
    """A second dial after a drop is accepted without a new enrollment code."""
    home = tmp_path / "child"
    _write_policy(home, tmp_path / "work")
    await enroll_child(hub.issue_enrollment_code(_address(hub)), home, name="box")
    async with child_session(home, heartbeat_interval=0.05) as session:
        await session.ready.wait()
    assert hub.hello_count == 1
    stop = asyncio.Event()
    task = asyncio.create_task(run_child(
        home, stop=stop, heartbeat_interval=0.05, backoff=0.05,
    ))
    try:
        await _wait_child(hub, "box", online=True)
        seen = hub.hello_count
        await hub.disconnect("box")
        deadline = asyncio.get_running_loop().time() + 2
        while hub.hello_count < seen + 1 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert hub.hello_count >= seen + 1
        row = await _wait_child(hub, "box", online=True)
        assert row["generation"] == 1
    finally:
        stop.set()
        await task


async def test_version_mismatch_is_rejected_with_a_reason_the_child_can_print(hub: ChildHub, tmp_path: Path):
    """The host states both package versions and the child raises that text."""
    home = tmp_path / "child"
    await enroll_child(hub.issue_enrollment_code(_address(hub)), home, name="box")
    with pytest.raises(ChildRejected, match="version mismatch") as caught:
        async with child_session(
            home, package_version="0.0.0-not-this", features=["codex-turns", "login-status"],
        ):
            pass
    message = str(caught.value)
    assert "0.0.0-not-this" in message
    assert _HOST_VERSION in message
    assert hub.list_children()[0]["online"] is False


async def test_revoked_child_is_disconnected_and_refused(hub: ChildHub, tmp_path: Path):
    """Revoke bumps generation, closes the live socket, and refuses the next dial."""
    home = tmp_path / "child"
    await enroll_child(hub.issue_enrollment_code(_address(hub)), home, name="box")
    async with child_session(home, heartbeat_interval=0.05) as session:
        await session.ready.wait()
        revoked = await hub.revoke("box")
        assert revoked["generation"] == 2
        assert revoked["revoked"] is True
        with pytest.raises(ChildRejected, match="revoked"):
            await session.until_closed()
    with pytest.raises(ChildRejected, match="revoked"):
        async with child_session(home, heartbeat_interval=0.05):
            pass
    assert hub.list_children()[0]["online"] is False
    assert hub.list_children()[0]["generation"] == 2


async def test_missed_heartbeats_mark_the_child_offline(hub: ChildHub, tmp_path: Path):
    """Three silent intervals mark the child offline while the socket stays open."""
    home = tmp_path / "child"
    await enroll_child(hub.issue_enrollment_code(_address(hub)), home, name="box")
    async with child_session(home, heartbeat_interval=0.05) as session:
        await session.ready.wait()
        await _wait_child(hub, "box", online=True)
        session.pause_heartbeats()
        await _wait_child(hub, "box", online=False, timeout=1.0)
        assert hub.list_children()[0]["last_heartbeat"]


async def test_hello_mcp_catalog_has_no_launch_definitions(hub: ChildHub, tmp_path: Path):
    """Hello carries MCP names and descriptions only, never command, args, or env."""
    home = tmp_path / "child"
    _write_policy(home, tmp_path / "work")
    await enroll_child(hub.issue_enrollment_code(_address(hub)), home, name="box")
    async with child_session(home, heartbeat_interval=0.05) as session:
        await session.ready.wait()
    assert hub.hello_payloads
    payload = hub.hello_payloads[-1]
    assert payload["mcp"] == [{"name": "files", "description": "workspace files"}]
    raw = json.dumps(payload)
    assert "npx" not in raw
    assert "secret-server" not in raw
    assert "SUPER_SECRET" not in raw
    assert "command" not in raw
    assert "args" not in raw
    assert "env" not in raw


async def test_workspace_report_is_replaced_on_reconnect_and_cleared_for_an_old_child(hub: ChildHub, tmp_path: Path):
    home = tmp_path / "child"
    _write_policy(home, tmp_path / "work")
    await enroll_child(hub.issue_enrollment_code(_address(hub)), home, name="box")

    async with child_session(home):
        assert WORKSPACE_POLICY_FEATURE in hub.hello_payloads[-1]["features"]
        assert hub.list_children()[0]["workspace_policy"]["workspace_roots"] == [str(tmp_path / "work")]
    assert (await _wait_child(hub, "box", online=False))["workspace_policy"] is not None

    updated = {"workspace_roots": [str(tmp_path / "new")], "codex_sandbox_max": "read-only"}
    (home / "policy.json").write_text(json.dumps({**_policy(tmp_path / "new"), **updated}), encoding="utf-8")
    async with child_session(home):
        assert hub.list_children()[0]["workspace_policy"] == updated
        assert hub.connection_status("box", "codex").workspace_policy == updated
    disk = ChildHub(hub.home)
    assert disk.connection_status("box", "codex").workspace_policy == updated

    async with child_session(home, features=[]):
        assert hub.list_children()[0]["workspace_policy"] is None
        assert hub.connection_status("box", "codex").workspace_policy is None


async def test_new_child_can_use_legacy_hello_handler(hub: ChildHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "child"
    _write_policy(home, tmp_path / "work")
    await enroll_child(hub.issue_enrollment_code(_address(hub)), home, name="box")
    current_hello = hub._on_hello

    async def legacy_hello(ws, mux, payload):
        assert WORKSPACE_POLICY_FEATURE in payload["features"]
        assert payload["workspace_policy"] == {
            "workspace_roots": [str(tmp_path / "work")], "codex_sandbox_max": "workspace-write",
        }
        legacy_keys = ("protocol", "version", "adapters", "mcp", "quota_providers", "features")
        await current_hello(ws, mux, {key: payload[key] for key in legacy_keys})

    monkeypatch.setattr(hub, "_on_hello", legacy_hello)
    async with child_session(home) as session:
        assert session.generation == 1
        assert hub.list_children()[0]["workspace_policy"] is None


@pytest.mark.parametrize("features, roots", [([WORKSPACE_POLICY_FEATURE], ["relative"]), ([], ["/absolute"])])
async def test_host_accepts_hello_without_storing_invalid_or_unflagged_metadata(hub, tmp_path, monkeypatch, features, roots):
    home = tmp_path / "child"
    _write_policy(home, tmp_path / "work")
    await enroll_child(hub.issue_enrollment_code(_address(hub)), home, name="box")

    def hello_with_report(stream, kind, payload):
        if kind == "hello":
            payload = {**payload, "workspace_policy": {"workspace_roots": roots, "codex_sandbox_max": "read-only"}}
        return encode_frame(stream, kind, payload)

    monkeypatch.setattr("kotoyose.child_client.encode_frame", hello_with_report)
    async with child_session(home, features=features):
        row = hub.list_children()[0]
        assert row["online"] is True
        assert row["workspace_policy"] is None


async def test_host_never_writes_the_child_policy_file(hub: ChildHub, tmp_path: Path):
    """Enrollment, hello, list, and revoke leave the child policy bytes untouched."""
    home = tmp_path / "child"
    before = _write_policy(home, tmp_path / "work")
    await enroll_child(hub.issue_enrollment_code(_address(hub)), home, name="box")
    async with child_session(home, heartbeat_interval=0.05) as session:
        await session.ready.wait()
        await hub.revoke("box")
        with pytest.raises(ChildRejected, match="revoked"):
            await session.until_closed()
    hub.list_children()
    assert (home / "policy.json").read_bytes() == before
    assert not list((tmp_path / "host").rglob("policy.json"))


async def test_admin_api_issues_lists_and_revokes(hub: ChildHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The admin routes issue a code, list the live child, and revoke it."""
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    home = tmp_path / "child"
    _write_policy(home, tmp_path / "work")
    app = FastAPI()
    app.state.child_hub = hub
    app.include_router(children_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://child.test") as client:
        idle = ChildHub(tmp_path / "idle")
        app.state.child_hub = idle
        refused = await client.post("/admin/children/enrollment-codes", json={"address": "127.0.0.1:9"})
        assert refused.status_code == 409
        app.state.child_hub = hub
        issued = await client.post(
            "/admin/children/enrollment-codes", json={"address": _address(hub)},
        )
        assert issued.status_code == 200
        assert issued.headers["cache-control"] == "no-store"
        code = issued.json()["code"]
        assert issued.json()["fingerprint"] == hub.host_fingerprint
        await enroll_child(code, home, name="box")
        async with child_session(home, heartbeat_interval=0.05) as session:
            await session.ready.wait()
            config = HarnessConfig(models={"outside": {
                "on": "child:box", "allow_cwd_override": True,
                "cwd_allowed_roots": [str(tmp_path / "other")],
            }})
            with use_config(config):
                listed = await client.get("/admin/children")
            assert listed.status_code == 200
            row = listed.json()["children"][0]
            assert row["name"] == "box"
            assert row["online"] is True
            assert row["version"] == _HOST_VERSION
            assert row["mcp"] == ["files"]
            assert row["alias_warnings"] == [{
                "alias": "outside", "message": f"Alias outside allows {tmp_path / 'other'} "
                                                f"but child box's workspace has {tmp_path / 'work'}.",
            }]
            revoked = await client.post("/admin/children/box/revoke")
            assert revoked.status_code == 200
            assert revoked.json()["revoked"] is True
            assert revoked.json()["generation"] == 2
            with pytest.raises(ChildRejected, match="revoked"):
                await session.until_closed()


def test_full_stream_queue_fails_the_stream_and_never_blocks_control_frames():
    """A saturated data stream ends with an explicit error frame instead of silently losing data."""
    mux = StreamMux(max_queue=3)
    for seq in range(1, 6):
        mux.dispatch(decode_frame(encode_frame(1, "delta", {"seq": seq})))
    mux.dispatch(decode_frame(encode_frame(0, "heartbeat", {})))
    heartbeat = mux.control.get_nowait()
    assert heartbeat.kind == "heartbeat" and heartbeat.stream == 0
    queued = mux.queue(1)
    frames = [queued.get_nowait() for _ in range(queued.qsize())]
    assert [frame.payload.get("seq") for frame in frames[:2]] == [1, 2]
    assert frames[-1].kind == "error"
    assert frames[-1].payload["diagnostics"]["terminal_reason"] == "stream_overflow"
    assert len(frames) == 3
    raw = json.loads(encode_frame(0, "heartbeat", {"n": 1}))
    assert raw == {"stream": 0, "kind": "heartbeat", "payload": {"n": 1}}


def test_child_home_and_listener_follow_the_environment(monkeypatch: pytest.MonkeyPatch):
    """The child state dir and the host listener stay off unless the operator points them."""
    monkeypatch.delenv("KOTOYOSE_CHILD_HOME", raising=False)
    monkeypatch.setenv("HOME", "/users/child-user")
    assert default_child_home() == Path("/users/child-user/.local/share/kotoyose-child")
    monkeypatch.setenv("KOTOYOSE_CHILD_HOME", "/var/child")
    assert default_child_home() == Path("/var/child")
    monkeypatch.delenv("KOTOYOSE_CHILD_PORT", raising=False)
    assert child_listen_config() is None
    monkeypatch.setenv("KOTOYOSE_CHILD_PORT", "9443")
    assert child_listen_config() == ("0.0.0.0", 9443)


def test_child_modules_do_not_import_pty_login():
    """The child import graph loads PTY login only when the login operation runs."""
    root = Path(__file__).resolve().parents[1] / "src" / "kotoyose"
    forbidden = {"cli_login", "pty", "termios", "fcntl"}
    seen: set[str] = set()

    def walk(module: str) -> None:
        if module in seen:
            return
        seen.add(module)
        path = root / f"{module}.py"
        if not path.is_file():
            return
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                targets = [node.module.split(".")[0]]
            for target in targets:
                if target in forbidden:
                    raise AssertionError(f"{module} imports {target}")
                if target == "kotoyose" and isinstance(node, ast.ImportFrom) and node.module and "." in node.module:
                    walk(node.module.split(".", 1)[1].split(".")[0])
                elif target == "kotoyose" and isinstance(node, ast.Import):
                    continue
                elif (root / f"{target}.py").is_file():
                    walk(target)

    for entry in ("child_cli", "child_client", "child_identity", "child_protocol", "child_server"):
        walk(entry)
    assert "cli_login" not in seen


def test_app_exposes_child_admin_routes():
    """The running service mounts the child admin routes next to the other admin APIs."""
    from kotoyose.app import app

    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/admin/children" in paths
    assert "/admin/children/enrollment-codes" in paths
    assert "/admin/children/{name}/revoke" in paths


async def test_run_child_reports_unreachable_host_once(tmp_path, monkeypatch, capsys):
    import asyncio as _asyncio
    import kotoyose.child_client as client

    calls = {"n": 0}

    class _Boom:
        async def __aenter__(self):
            calls["n"] += 1
            raise ConnectionRefusedError(111, "Connect call failed")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(client, "child_session", lambda *a, **k: _Boom())
    stop = _asyncio.Event()

    async def stopper():
        while calls["n"] < 3:
            await _asyncio.sleep(0.01)
        stop.set()

    await _asyncio.gather(client.run_child(tmp_path, stop=stop, backoff=0.01), stopper())
    err = capsys.readouterr().err
    assert err.count("cannot reach host: ConnectionRefusedError") == 1
