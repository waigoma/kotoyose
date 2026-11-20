import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
from textwrap import dedent
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kotoyose import cli_login
from kotoyose.config import Connection, HarnessConfig
from kotoyose.connection_state_store import ConnectionStateStore
from kotoyose.connections_probe import probe_print_cli_connection
from kotoyose.container_print import build_print_cli_container_plan
from kotoyose.execution_resources import AgentContainerConfig
from kotoyose.print_cli_agent import PrintCliJob, PrintCliWorker, _leader_exited
from kotoyose.print_cli_events import create_print_cli_parser
from kotoyose.print_cli_hooks import classify_stderr, get_models_parser, get_quota_parser
from kotoyose.print_cli_profiles import build_print_cli_env, get_print_cli_profile

PROFILE = get_print_cli_profile("grok")


@pytest.mark.parametrize(("surface", "mode"), [("read", "dontAsk"), ("write", "bypassPermissions")])
def test_grok_argv_keeps_prompt_in_single_option(surface, mode, tmp_path):
    prompt = "--permission-mode=bypassPermissions {model}\n--login"
    argv = PROFILE.render_argv(
        model="grok-build-0.1", prompt=prompt, surface=surface, timeout_seconds=10,
        add_dirs=(tmp_path,),
    )
    assert argv[:6] == ["--output-format", "streaming-json", "-m", "grok-build-0.1", "--permission-mode", mode]
    assert argv[-3:] == ["--add-dir", str(tmp_path), f"--single={prompt}"]
    assert ("--no-subagents" in argv) == ("--disable-web-search" in argv) == (surface == "read")
    assert "--sandbox" not in argv
    assert build_print_cli_env(PROFILE, tmp_path)["HOME"] == build_print_cli_env(PROFILE, tmp_path)["GROK_HOME"]


@pytest.mark.parametrize("answer", [None, "Final answer"])
def test_grok_streaming_json(answer):
    parser = create_print_cli_parser(PROFILE.parser)
    stream = [
        {"type": "thought", "text": "private"},
        {"type": "text", "delta": "Hello "},
        {"type": "tool_call", "toolCall": {"name": "Read", "id": "t1", "input": "private"}},
        {"type": "tool_call_update", "id": "t1", "output": "private"},
        {"type": "text", "text": "world"},
        {"type": "usage", "usage": {"inputTokens": 42, "outputTokens": 5}},
        {"type": "new_type"},
        {"type": "end", "sessionId": "session-1", "requestId": "request-1",
         "stopReason": "end_turn", "usage": {"outputTokens": 8}, "answer": answer},
    ]
    assert parser.feed_line("not json") == []
    events = [item for event in stream for item in parser.feed_line(json.dumps(event))]
    assert [event.kind for event in events] == ["text", "tool", "tool", "text"]
    assert events[1].tool_name == "Read"
    assert events[2].payload == {"tool_id": "t1", "state": "updated"}
    assert "private" not in repr(events)
    outcome = parser.finalize()
    assert outcome.malformed_lines == 1
    assert outcome.result_status == "SUCCESS"
    assert outcome.session_id == "session-1"
    assert outcome.content == (answer or "Hello world")
    assert outcome.usage["input_tokens"] == 42
    assert outcome.usage["output_tokens"] == 8


@pytest.mark.parametrize(("message", "status"), [
    ("Please log in using grok login", "UNAUTHENTICATED"),
    ("HTTP 429 Too Many Requests", "RATE_LIMITED"),
    ("Quota exhausted", "RESOURCE_EXHAUSTED"),
    ("Model grok-build-0.1 is not available", "NOT_FOUND"),
])
def test_grok_stderr_classifier(message, status):
    assert classify_stderr(PROFILE.classify_stderr, message) == {"status": status}


def test_grok_models_text():
    parse = get_models_parser(PROFILE.parse_models)
    models = parse(
        "Available models:\nModel ID\n\x1b[32mgrok-build-0.1\x1b[0m - Grok Build\n"
        "* grok-4.7\n│ grok-4.6 │ Grok 4.6 │ Standard │\n".encode()
    )
    assert [(model["id"], model["label"]) for model in models] == [
        ("grok-build-0.1", "Grok Build"), ("grok-4.7", "grok-4.7"), ("grok-4.6", "Grok 4.6"),
    ]


async def test_grok_device_login_with_models_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "login-home"
    home.mkdir()
    binary = tmp_path / "fixture-cli"
    binary.write_text(f"#!{sys.executable}\n" + dedent("""
        import os
        from pathlib import Path
        import sys
        import time
        home = Path(os.environ["HOME"])
        assert os.environ["GROK_HOME"] == str(home)
        if sys.argv[1:] == ["login", "--device-auth"]:
            print("Use https://accounts.x.ai.attacker.invalid/device", flush=True)
            print("Go to https://auth.x.ai/device", flush=True)
            print("Enter code AbC1-2deF", flush=True)
            while not (home / "finish").exists():
                time.sleep(0.01)
        else:
            assert sys.argv[1:] == ["models"]
            assert (home / "finish").exists()
            print("grok-build-0.1")
    """), encoding="utf-8")
    binary.chmod(0o755)
    cfg = HarnessConfig(connections={"grok-test": {
        "kind": "subscription", "provider": "grok", "profile": "grok", "config_dir": home, "data_policy": "unknown",
    }})
    service = SimpleNamespace(config=SimpleNamespace(drivers={"cli.print": {"profile_binaries": {"grok": binary}}}))
    try:
        login = await cli_login.start_cli_login_session("grok-test", cfg=cfg, execution_service=service)
        assert login["flow"] == "device_code"
        assert login["url"] == "https://auth.x.ai/device"
        assert login["user_code"] == "AbC1-2deF"
        (home / "finish").touch()
        async with asyncio.timeout(5):
            while (state := await cli_login.get_cli_login_status("grok-test", login["session_id"]))[
                "state"
            ] == "waiting_for_browser":
                await asyncio.sleep(0.01)
        assert state["state"] == "completed"
        assert "user_code" not in state
    finally:
        await cli_login.shutdown_cli_login_sessions()


def test_grok_jail_launch_plan(tmp_path):
    login_home = tmp_path / "login"
    workspace = tmp_path / "work"
    login_home.mkdir()
    workspace.mkdir()
    binary = tmp_path / "fixture-cli"
    binary.touch()
    settings = AgentContainerConfig(agent_root=str(tmp_path), home_root=str(tmp_path))
    job = PrintCliJob(
        profile_id="grok", cli_binary=binary, model="grok-build-0.1", workspace=workspace,
        login_home=login_home, surface="read", instructions="", prompt="-unsafe",
        output_contract="text", timeout_seconds=10, grace_seconds=1, output_limit_bytes=1024,
        memory_bytes=256 * 1024**2, pids_limit=8, cpus=1,
    )
    spec, argv, env, cli_args = build_print_cli_container_plan(
        job, PROFILE, settings, image_id="sha256:" + "a" * 64, egress_dir=tmp_path,
        settings_dir=tmp_path, print_timeout_seconds=10, job_id="fixture",
    )
    assert spec.ro_files["/opt/agent/bin/grok"] == binary
    assert spec.workspace_readonly
    assert spec.home == login_home
    assert env["HOME"] == env["GROK_HOME"] == "/home/agent"
    assert "/opt/agent/bin/grok" in argv
    assert cli_args[-1] == "--single=-unsafe"
    assert PROFILE.egress_hosts == ("cli-chat-proxy.grok.com", "auth.x.ai")
    assert PROFILE.egress_ports == (443,)
    assert PROFILE.egress_idle_seconds == 900


async def test_grok_host_reaps_leader_holding_stream_open(tmp_path):
    binary = tmp_path / "fixture-cli"
    binary.write_text(f"#!{sys.executable}\n" + dedent("""
        import os
        from pathlib import Path
        import sys
        import time

        assert sys.argv[-1] == "--single=--login"
        assert os.environ["HOME"] == os.environ["GROK_HOME"]
        leader = os.fork()
        if leader == 0:
            os.setpgid(0, 0)
            time.sleep(10)
            os._exit(0)
        Path("leader.pid").write_text(str(leader))
        print('{"type":"text","text":"answer"}')
        print('{"type":"end","sessionId":"fixture","stopReason":"end_turn"}')
    """), encoding="utf-8")
    binary.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    job = PrintCliJob(
        profile_id="grok", cli_binary=binary, model="grok-build-0.1", workspace=tmp_path,
        login_home=home, surface="read", instructions="", prompt="--login",
        output_contract="text", timeout_seconds=4, grace_seconds=0.2, output_limit_bytes=1024,
        memory_bytes=256 * 1024**2, pids_limit=8, cpus=1,
    )
    result = await PrintCliWorker(chat=True).execute(
        job, asyncio.Event(), on_process_started=AsyncMock(), on_event=AsyncMock(),
    )
    assert result.status == "succeeded"
    assert result.content == b"answer"
    assert result.provider_session_id == "fixture"
    async with asyncio.timeout(2):
        leader = int((tmp_path / "leader.pid").read_text())
        while not _leader_exited(leader):
            await asyncio.sleep(0.01)


def test_grok_connection_example():
    import yaml

    data = Path(__file__).resolve().parents[1] / "config.example.yml"
    blocks = {}
    for section, key in (("connections", "grok-cli-home"), ("routes", "grok-cli"), ("models", "grok-cli-review")):
        lines = iter(data.read_text().splitlines())
        start = next(line for line in lines if line == f"  # {key}:")
        uncommented = [start[4:]]
        for line in lines:
            if not line.startswith("  #   "):
                break
            uncommented.append(line[4:])
        blocks[section] = yaml.safe_load("\n".join(uncommented))

    config = HarnessConfig.model_validate(blocks)
    assert config.routes["grok-cli"].profile == "grok"
    assert config.print_cli_profile_for_connection("grok-cli-home") is PROFILE
    assert config.models["grok-cli-review"].agent.surface == "read"


def test_grok_status_treats_not_authenticated_as_signed_out():
    """`grok models` exits 0 when signed out; the marker must win (measured 2026-11-19)."""
    from kotoyose.print_cli_profiles import get_print_cli_profile

    spec = get_print_cli_profile("grok").login
    signed_out = "You are not authenticated.\n\nDefault model: grok-4.6\n\nAvailable models:\n  * grok-4.6 (default)\n"
    assert spec.matches_status(0, None, signed_out) is False
    assert spec.matches_status(0, None, "Available models:\n  * grok-4.6 (default)\n") is True
    assert spec.matches_status(1, None, "Available models:\n") is False


def test_parser_reads_the_measured_grok_stream_shape():
    """1.0.89 の本物の出力の形 (id は偽): available_commands、text (data)、usage、end。"""
    import json

    from kotoyose import print_cli_hooks
    import kotoyose.print_cli_grok  # noqa: F401  (registers the hooks)

    lines = [
        {"type": "available_commands", "commands": [], "tools": []},
        {"type": "text", "data": "ok"},
        {"type": "usage", "signature": "sig", "usage": {"input_tokens": 4254, "output_tokens": 20,
         "cache_read_input_tokens": 7808, "cache_creation_input_tokens": 0, "reasoning_tokens": 19}},
        {"type": "end", "stopReason": "end_turn", "sessionId": "00000000-0000-4000-8000-000000000000",
         "requestId": "req", "cwd": "/tmp/x", "num_turns": 1, "total_cost_usd": 0.012532,
         "total_cost_usd_ticks": 125320000, "usage": {"input_tokens": 4254, "output_tokens": 20,
         "total_tokens": 12082}, "modelUsage": {}},
    ]
    parser = print_cli_hooks.get_parser("grok_streaming_json_v1")()
    for line in lines:
        parser.feed_line(json.dumps(line).encode() + b"\n")
    outcome = parser.finalize()
    assert outcome.result_status == "SUCCESS"
    assert outcome.content == "ok"
    assert outcome.session_id == "00000000-0000-4000-8000-000000000000"
    assert outcome.usage["input_tokens"] == 4254
    assert outcome.usage["output_tokens"] == 20
    assert outcome.usage["cost"] == 0.012532


_BILLING_RESULT = {
    "config": {
        "creditUsagePercent": 1.0,
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-11-08T23:16:17.887+09:00",
            "end": "2026-11-15T23:16:17.887+09:00",
        },
        "prepaidBalance": {"val": 0},
        "isUnifiedBillingUser": True,
    },
    "subscription_tier": "SuperGrok",
}
_WEEKLY_QUOTA = {
    "window": "7d", "label": "Weekly", "used_percent": 1.0,
    "resets_at": "2026-11-15T14:16:17.887000+00:00", "source": "grok_acp_billing",
}


@pytest.mark.parametrize(("percent", "used"), [
    (1.0, 1.0), ({"val": 3.5}, 3.5), (0, 0), (-1, 0), (101, 100),
    (None, None), (True, None), ({"val": "2"}, None),
])
def test_grok_billing_parser(percent, used):
    payload = {**_BILLING_RESULT, "config": dict(_BILLING_RESULT["config"])}
    if percent is None:
        payload["config"].pop("creditUsagePercent")
    else:
        payload["config"]["creditUsagePercent"] = percent
    parse = get_quota_parser(PROFILE.quota_rpc.parser)
    result = parse(json.dumps(payload).encode(), stderr=b"")
    assert result == {
        "auth": "ok",
        "quota": [] if used is None else [{**_WEEKLY_QUOTA, "used_percent": used}],
        "plan": "SuperGrok",
        "last_error": None,
    }


def test_grok_quota_rpc_canonical_and_exclusive():
    canonical = PROFILE.to_canonical_dict()["quota_rpc"]
    assert canonical["command"] == ["agent", "stdio"]
    assert canonical["parser"] == "grok_billing_v1"
    assert canonical["result_id"] == 2
    assert canonical["timeout_seconds"] == 20
    assert [request["method"] for request in canonical["requests"]] == ["initialize", "_x.ai/billing"]
    with pytest.raises(ValueError, match="mutually exclusive"):
        replace(PROFILE, quota_command=("usage",))


def _fake_grok_billing(tmp_path, mode):
    binary = tmp_path / "grok-fixture"
    binary.write_text(
        f"#!{sys.executable}\nMODE = {mode!r}\nBILLING = {_BILLING_RESULT!r}\n" + dedent("""
        import json
        import os
        from pathlib import Path
        import select
        import sys
        import time

        def send(**fields):
            print(json.dumps({"jsonrpc": "2.0", **fields}), flush=True)

        if sys.argv[1:] == ["--version"]:
            print("grok 1.0.89")
            sys.exit()
        if sys.argv[1:] == ["models"]:
            assert MODE == "status"
            print("You are not authenticated.")
            sys.exit()
        assert sys.argv[1:] == ["agent", "stdio"]
        assert MODE != "status"
        home = Path(os.environ["GROK_HOME"])
        assert str(home) == os.environ["HOME"]
        assert Path.cwd() != home
        assert os.getsid(0) == os.getpid()

        stdin = os.fdopen(0, "rb", buffering=0)
        init = json.loads(stdin.readline())
        assert init == {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": 1, "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False,
            }},
        }
        if MODE == "init_error":
            send(id=1, error={"code": -32603, "message": "private diagnostics"})
            sys.exit()
        if MODE == "interleaved":
            print("unrelated startup text", flush=True)
            send(method="notification", params={})
            send(id=True, result={})
            send(id=99, result={})
            os.write(2, b"private diagnostics" * 8192)
        assert not select.select([stdin], [], [], 0.02)[0]
        send(id=1, result={"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []})
        billing = json.loads(stdin.readline())
        assert billing == {"jsonrpc": "2.0", "id": 2, "method": "_x.ai/billing", "params": {}}

        # Emulate the CLI suppressing its billing response when stdin closes early.
        if select.select([stdin], [], [], 0.02)[0]:
            sys.exit()
        if MODE in ("helper", "cancel"):
            child = os.fork()
            if child == 0:
                os.setpgid(0, 0)
                pidfile = home / "helper.pid.tmp"
                pidfile.write_text(str(os.getpid()))
                pidfile.rename(home / "helper.pid")
                time.sleep(30)
                os._exit(0)
            while not (home / "helper.pid").exists():
                time.sleep(0.005)
        if MODE in ("timeout", "cancel"):
            stdin.read()
            sys.exit()
        if MODE in ("rpc_error", "signed_out"):
            message = "You are not authenticated." if MODE == "signed_out" else "private diagnostics"
            send(id=2, error={"code": -32603, "message": message})
        elif MODE == "oversized":
            print("x" * (1024 * 1024 + 1), flush=True)
        elif MODE == "total_limit":
            for _ in range(9):
                print("x" * (512 * 1024), flush=True)
        else:
            if MODE == "interleaved":
                send(method="notification", params={})
                send(id=1, result={})
                BILLING["config"]["creditUsagePercent"] = {"val": 1}
            send(id=2, result=BILLING)
        stdin.read()
        """),
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


@pytest.mark.parametrize(("mode", "auth", "error"), [
    ("success", "ok", None),
    ("interleaved", "ok", None),
    ("helper", "ok", None),
    ("init_error", "unknown", "Quota RPC returned an error"),
    ("rpc_error", "unknown", "Quota RPC returned an error"),
    ("signed_out", "logged_out", "Quota RPC requires authentication"),
    ("timeout", "unknown", "Quota RPC timed out after 0.3s"),
    ("oversized", "unknown", "Quota RPC output line exceeded its limit"),
    ("total_limit", "unknown", "Quota RPC output exceeded its limit"),
    ("status", "logged_out", None),
])
async def test_grok_quota_probe(tmp_path, mode, auth, error):
    binary = _fake_grok_billing(tmp_path, mode)
    conn = Connection(kind="subscription", provider="grok", profile="grok", data_policy="unknown", config_dir=tmp_path, show_quota=mode != "status")
    state = await probe_print_cli_connection(
        "fixture", conn, cli_binary=binary, store=ConnectionStateStore(),
        profile=replace(PROFILE, quota_rpc=replace(PROFILE.quota_rpc, timeout_seconds=0.3 if mode == "timeout" else 5)),
    )
    assert state["auth"] == auth
    assert state["last_error"] == error
    assert state["quota_source"] == (None if mode == "status" else "grok_acp_billing")
    assert state["plan"] == ("SuperGrok" if auth == "ok" else None)
    assert state["quota"] == ([_WEEKLY_QUOTA] if auth == "ok" else [])
    if mode == "helper":
        async with asyncio.timeout(2):
            while not _leader_exited(int((tmp_path / "helper.pid").read_text())):
                await asyncio.sleep(0.01)


async def test_grok_quota_probe_cancellation_stops_helper(tmp_path):
    binary = _fake_grok_billing(tmp_path, "cancel")
    conn = Connection(kind="subscription", provider="grok", profile="grok", data_policy="unknown", config_dir=tmp_path)
    store = ConnectionStateStore()
    task = asyncio.create_task(probe_print_cli_connection("fixture", conn, cli_binary=binary, store=store))
    try:
        async with asyncio.timeout(5):
            while not (tmp_path / "helper.pid").exists():
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await store.get("fixture") is None
    async with asyncio.timeout(2):
        while not _leader_exited(int((tmp_path / "helper.pid").read_text())):
            await asyncio.sleep(0.01)
