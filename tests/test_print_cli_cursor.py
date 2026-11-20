import asyncio
from pathlib import Path
import sys
from textwrap import dedent
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kotoyose import cli_login, model_discovery
from kotoyose.agent_route import resolve_declared_agent
from kotoyose.config import HarnessConfig
from kotoyose.connection_state_store import ConnectionStateStore
from kotoyose.connections_probe import probe_connection
from kotoyose.execution_resources import DriverInstall
from kotoyose.print_cli_agent import PrintCliJob, PrintCliWorker
from kotoyose.print_cli_events import create_print_cli_parser
from kotoyose.print_cli_hooks import classify_stderr
from kotoyose.print_cli_profiles import get_print_cli_profile


@pytest.fixture
def cursor(tmp_path, monkeypatch):
    binary = tmp_path / "agent"
    binary.write_text(f"#!{sys.executable}\n" + dedent("""
        import json
        import os
        from pathlib import Path
        import sys
        import time

        args = sys.argv[1:]
        if args == ["--version"]:
            print("2026.11.13-eacade1")
            sys.exit(0)
        home = Path(os.environ["HOME"])
        assert os.environ["CURSOR_CONFIG_DIR"] == str(home / ".cursor")
        assert os.environ["NODE_USE_ENV_PROXY"] == "1"
        if args == ["login"]:
            assert os.environ["NO_OPEN_BROWSER"] == "1"
            print("https://cursor.com/loginDeepControl?challenge=demo", flush=True)
            while not (home / "finish").exists():
                time.sleep(0.01)
            sys.exit(0)
        assert "NO_OPEN_BROWSER" not in os.environ
        if args == ["status", "--format", "json"]:
            if (home / "finish").exists():
                print("{}")
                sys.exit(0)
            sys.exit("Not authenticated. Run agent login.")
        if args == ["models"]:
            print("Available models\\n\\n\\x1b[32mauto\\x1b[0m - Auto\\nsonnet-4  Sonnet 4\\nauto - Auto\\n│ opus-4 │ Opus 4 │")
            sys.exit(0)

        assert args[:5] == ["--print", "--output-format", "stream-json", "--model", "auto"]
        assert args[5:-2] in [
            ["--mode", "ask", "--trust"], ["--force", "--trust"],
        ]
        assert args[-2] == "--"
        prompt = args[-1]
        if prompt == "auth":
            sys.exit("Not authenticated. Run agent login.")
        if prompt == "rate":
            sys.exit("Request failed: HTTP 429 Too Many Requests")

        def emit(kind, **fields):
            print(json.dumps(dict(fields, type=kind, session_id="cursor-session")))

        emit("system", subtype="init", apiKeySource="login")
        emit("user", message={"role": "user", "content": [{"type": "text", "text": prompt}]})
        for text in ("Hel", "Hello", "Hello"):
            emit("assistant", message={"role": "assistant", "content": [{"type": "text", "text": text}]})
        for state in ("started", "completed"):
            emit("tool_call", subtype=state, call_id="tool-1", tool_call={"readToolCall": {
                "args": {"path": "private.txt"}, "result": {"success": {"content": "private output"}},
            }})
        if prompt != "missing":
            emit("result", subtype="success", is_error=False, result="Hello",
                 usage={"input_tokens": 5, "output_tokens": 2})
        """), encoding="utf-8")
    binary.chmod(0o700)
    home = tmp_path / "home"
    cfg = HarnessConfig(
        connections={
            "cursor": {
                "provider": "cursor", "kind": "subscription", "profile": "cursor",
                "data_policy": "no_training", "config_dir": home,
            },
            "sdk": {"provider": "cursor", "kind": "api_key", "data_policy": "unknown", "secret": "sdk-key"},
        },
        routes={"cursor": {"driver": "cli.print", "profile": "cursor", "connection": "cursor", "model": "auto"}},
        models={"cursor": {"route": "cursor", "agent": {"place": "host", "surface": "read"}}},
    )
    assert cfg.cursor_connection() is cfg.connections["sdk"]
    service = SimpleNamespace(config=SimpleNamespace(drivers={"cli.print": DriverInstall(
        profile_binaries={"cursor": binary},
    )}))
    monkeypatch.setattr(model_discovery, "get_config", lambda: cfg)
    model_discovery.clear_connection_discovery_cache()
    return cfg, service, home


@pytest.mark.parametrize(("surface", "prompt", "status", "reason"), [
    ("read", "--force {prompt}\nhello", "succeeded", None),
    ("write", "hello", "succeeded", None),
    ("read", "missing", "failed", "missing_completion"),
    ("read", "auth", "failed", "missing_completion"),
    ("read", "rate", "failed", "provider_usage_limit"),
])
async def test_cursor_host(cursor, surface, prompt, status, reason):
    cfg, service, home = cursor
    home.mkdir()
    preset = cfg.preset_for("cursor")
    resolved = resolve_declared_agent(
        "cursor", preset, service.config, cfg, resolve_preferences=False, resolve_prompts=False,
    )
    assert resolved.profile == "cursor"
    assert resolved.login_home == home
    job = PrintCliJob(
        profile_id=resolved.profile, cli_binary=resolved.cli_binary, model=resolved.model,
        workspace=home.parent, login_home=home, surface=surface, instructions="", prompt=prompt,
        output_contract="text", timeout_seconds=3, grace_seconds=0.2, output_limit_bytes=8192,
        memory_bytes=128 * 1024 * 1024, pids_limit=32, cpus=1.0,
    )
    events = AsyncMock()
    result = await PrintCliWorker().execute(job, asyncio.Event(), on_process_started=AsyncMock(), on_event=events)
    assert result.status == status
    if reason:
        assert result.reason == reason
    else:
        assert result.content == b"Hello"
        assert result.usage["input_tokens"] == 5
        progress = [call.args[1] for call in events.call_args_list if call.args[0] == "provider_progress"]
        tool_events = [event for event in progress if event["kind"] == "tool"]
        assert [(event["tool_name"], event["payload"]) for event in tool_events] == [
            ("read", {"state": "started", "tool_id": "tool-1"}),
            ("read", {"state": "completed", "tool_id": "tool-1"}),
        ]
        assert progress[0]["summary"] == "Hel"
        assert "private" not in repr(progress)


async def test_cursor_browser_login_probe_and_models(cursor):
    cfg, service, home = cursor
    store = ConnectionStateStore()
    kwargs = {"cfg": cfg, "execution_service": service}
    state = await probe_connection("cursor", store=store, **kwargs)
    assert state["auth"] == "logged_out"
    home.mkdir()
    state = await probe_connection("cursor", store=store, **kwargs)
    assert state["auth"] == "logged_out"
    try:
        login = await cli_login.start_cli_login_session("cursor", **kwargs)
        assert login["state"] == "waiting_for_browser"
        assert login["flow"] == "browser_wait"
        assert login["url"] == "https://cursor.com/loginDeepControl?challenge=demo"
        (home / "finish").touch()
        async with asyncio.timeout(3):
            while (state := await cli_login.get_cli_login_status("cursor", login["session_id"]))["state"] == "waiting_for_browser":
                await asyncio.sleep(0.01)
        assert state["state"] == "completed"
        assert state["url"] is None
    finally:
        await cli_login.shutdown_cli_login_sessions()

    state = await probe_connection("cursor", store=store, **kwargs)
    assert state["auth"] == "ok"
    assert state["cli_version"] == "2026.11.13-eacade1"
    assert state["quota"] == []
    assert state["quota_source"] is None
    discovered = await model_discovery.discover_models("cursor", connection="cursor", execution_service=service)
    assert discovered["models"] == ["auto", "sonnet-4", "opus-4"]
    assert discovered["source"] == "cursor_cli_models"
    assert discovered["model_details"][1]["label"] == "Sonnet 4"


def test_cursor_profile_version_argv_and_placement(cursor):
    cfg, _, _ = cursor
    profile = get_print_cli_profile("cursor")
    assert profile.supports_version("2026.11.13-eacade1")
    assert profile.supports_version("2026.12.1-deadbee")
    assert not profile.supports_version("2026.11.12-deadbee")
    assert not profile.supports_version("unknown")

    rendered = profile.render_argv(
        model="auto", prompt="--force {model}", surface="read", timeout_seconds=3, add_dirs=(Path("/extra"),),
    )
    assert rendered == [
        "--print", "--output-format", "stream-json", "--model", "auto",
        "--mode", "ask", "--trust", "--add-dir", "/extra", "--", "--force {model}",
    ]
    assert profile.render_argv(model="auto", prompt="hello", surface="write", timeout_seconds=3) == [
        "--print", "--output-format", "stream-json", "--model", "auto", "--force", "--trust", "--", "hello",
    ]
    raw = cfg.model_dump()
    raw["models"]["cursor"]["agent"]["place"] = "isolated"
    assert HarnessConfig.model_validate(raw).models["cursor"].agent.place == "isolated"


def test_cursor_stream_result_and_error_hooks():
    profile = get_print_cli_profile("cursor")
    parser = create_print_cli_parser(profile.parser)
    parser.feed_line('{"type":"system","subtype":"init","session_id":"initial"}')
    parser.feed_line('{"type":"result","subtype":"success","session_id":"final","result":""}')
    outcome = parser.finalize()
    assert (outcome.saw_result, outcome.result_status, outcome.content, outcome.session_id) == (
        True, "SUCCESS", "", "final",
    )
    assert classify_stderr(profile.classify_stderr, "Not authenticated. Run agent login.") == {"status": "UNAUTHENTICATED"}
    assert classify_stderr(profile.classify_stderr, "Usage limit reached") == {"status": "RESOURCE_EXHAUSTED"}
    assert classify_stderr(profile.classify_stderr, "Model sonnet-4 not available") == {"status": "NOT_FOUND"}
    assert classify_stderr(profile.classify_stderr, "Unknown error") == {}


def test_parser_reads_the_measured_cursor_stream_shape():
    """2026.11.13 の本物の出力の形 (id は偽): system/init、user、thinking、assistant、result (camelCase の usage)。"""
    import json

    from kotoyose import print_cli_hooks
    import kotoyose.print_cli_cursor  # noqa: F401  (registers the hooks)

    sid = "00000000-0000-4000-8000-000000000000"
    lines = [
        {"type": "system", "subtype": "init", "apiKeySource": "login", "cwd": "/tmp/x", "model": "Auto",
         "permissionMode": "default", "session_id": sid},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}, "session_id": sid},
        {"type": "thinking", "subtype": "delta", "text": "...", "session_id": sid, "timestamp_ms": 1},
        {"type": "thinking", "subtype": "completed", "session_id": sid, "timestamp_ms": 2},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
         "session_id": sid},
        {"type": "result", "subtype": "success", "is_error": False, "result": "ok", "session_id": sid,
         "request_id": "req", "duration_ms": 10, "duration_api_ms": 9,
         "usage": {"inputTokens": 6359, "outputTokens": 28, "cacheReadTokens": 3968, "cacheWriteTokens": 0,
                   "reasoningTokens": 27}},
    ]
    parser = print_cli_hooks.get_parser("cursor_stream_json_v1")()
    for line in lines:
        parser.feed_line(json.dumps(line).encode() + b"\n")
    outcome = parser.finalize()
    assert outcome.result_status == "SUCCESS"
    assert outcome.content == "ok"
    assert outcome.session_id == sid
    assert outcome.usage["input_tokens"] == 6359
    assert outcome.usage["output_tokens"] == 28
    assert outcome.malformed_lines == 0
