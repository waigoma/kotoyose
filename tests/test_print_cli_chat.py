import asyncio
from contextlib import aclosing
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient
import pytest

from kotoyose import process_env, session
from kotoyose.app import app
from kotoyose.config import HarnessConfig
from kotoyose.conversation.contracts import Limits
from kotoyose.conversation.service import Conversations
from kotoyose.execution_resources import NativeExecutionConfig
from kotoyose.print_cli_home import acquire_login_home
from kotoyose.routes import chat
from kotoyose.runtime.print_cli import build_print_cli_session


PROMPT = "$(touch unwanted) {model} ; --force"
MESSAGES = [
    {"role": "system", "content": "System instructions"},
    {"role": "user", "content": "Previous question"},
    {"role": "assistant", "content": "Previous answer"},
    {"role": "user", "content": PROMPT},
]


@pytest.fixture
def print_chat(tmp_path, monkeypatch):
    login_home = tmp_path / "login"
    login_home.mkdir()
    observation = login_home / "observation.json"
    binary = tmp_path / "fake-agent"
    binary.write_text(f"#!{sys.executable}\n" + """
import json
import os
from pathlib import Path
import signal
import stat
import sys
import time

home = Path(os.environ["HOME"])
(home / "observation.json").write_text(json.dumps({
    "argv": sys.argv[1:],
    "cwd": str(Path.cwd()),
    "cwd_entries": list(os.listdir()),
    "cwd_mode": stat.S_IMODE(Path.cwd().stat().st_mode),
    "secret_inherited": any(key.startswith("KOTOYOSE_PRINT_TEST_") for key in os.environ),
    "login_config": os.environ["CURSOR_CONFIG_DIR"],
    "fixed_env": os.environ["NODE_USE_ENV_PROXY"],
    "pid": os.getpid(),
}))
if "auth-error" in sys.argv[-1]:
    sys.exit("Authentication required: private diagnostic")
if "quota-error" in sys.argv[-1]:
    sys.exit("Quota exceeded: private diagnostic")
if "keep-running" in sys.argv[-1]:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(json.dumps({"type": "assistant", "message": {
    "content": [{"type": "text", "text": "streamed text"}]
}}), flush=True)
if "keep-running" in sys.argv[-1]:
    time.sleep(30)
if "leave-helper" in sys.argv[-1] and os.fork() == 0:
    # Like Cursor's worker-server: leave the process group, stay in the session, keep stderr open.
    os.setpgid(0, 0)
    (home / "helper.pid").write_text(str(os.getpid()))
    time.sleep(60)
    os._exit(0)
print(json.dumps({"type": "result", "subtype": "success", "result": "final answer",
                  "session_id": "print-session", "usage": {"input_tokens": 7, "output_tokens": 3}}))
""")
    binary.chmod(0o700)
    settings = NativeExecutionConfig.model_validate({
        "schema_version": "1", "image_id": "sha256:" + "1" * 64,
        "drivers": {"cli.print": {"profile_binaries": {"cursor": str(binary)}}},
    })
    cfg = HarnessConfig.model_validate({
        "connections": {"local": {"provider": "cursor", "kind": "subscription", "on": "local",
                                  "profile": "cursor", "config_dir": str(login_home), "data_policy": "no_training"}},
        "routes": {"print": {"driver": "cli.print", "profile": "cursor", "connection": "local",
                             "model": "cursor-test"}},
        "models": {"print-chat": {"route": "print", "place": "host", "allow": {}}},
        "clients": {"wildcard": {"models": ["*"]},
                    "restricted": {"models": ["print-chat"]}},
    })
    monkeypatch.setitem(process_env._SCRUBBED, "KOTOYOSE_PRINT_TEST_SECRET", "synthetic-secret")
    monkeypatch.setenv("KOTOYOSE_PRINT_TEST_INHERITED_SECRET", "synthetic-secret")
    from tests.fixtures.api_keys import sync_fixture_key_store

    monkeypatch.setattr(app.state, "api_key_store", sync_fixture_key_store(
        wildcard="print-test-caller", restricted="print-test-restricted",
    ))
    monkeypatch.setattr("kotoyose.config._config", cfg)
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    monkeypatch.setattr(app.state, "execution_service", SimpleNamespace(config=settings), raising=False)
    monkeypatch.setattr(app.state, "conversations", Conversations(Limits(), lambda _: pytest.fail("unexpected launch"),
                                                             caller_ids=frozenset({"wildcard", "restricted"})),
                        raising=False)
    monkeypatch.setattr(session, "sessions", {})
    monkeypatch.setattr(session, "session_last_used", {})
    monkeypatch.setattr(session, "session_cache_identities", {})
    lookup = AsyncMock(return_value="old-session")
    monkeypatch.setattr(session, "lookup_runtime_session", lookup)
    for name in ("record_received", "record_completed", "record_failed", "_safe_upsert_session",
                 "_rotate_hash_key", "backfill_conversation_key", "record_connection_auth_failure"):
        monkeypatch.setattr(chat, name, AsyncMock(return_value=None))
    stream_auth_failure = AsyncMock()
    monkeypatch.setattr("kotoyose.chat_streaming.record_connection_auth_failure", stream_auth_failure)
    monkeypatch.setattr(chat, "start_event_writer", lambda *_: asyncio.Queue())
    return SimpleNamespace(
        config=cfg, settings=settings, login_home=login_home, observation=observation,
        lookup=lookup, stream_auth_failure=stream_auth_failure,
    )


def client(token="print-test-caller"):
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": f"Bearer kys_{token}", "X-Conversation-Key": "print-test"},
    )


@pytest.mark.asyncio
async def test_print_cli_chat_stream_and_non_stream_use_fresh_read_turns(print_chat):
    async with client() as http:
        result = await http.post("/v1/chat/completions", json={"model": "print-chat", "messages": MESSAGES})
        assert result.status_code == 200, result.text
        assert result.json()["choices"][0]["message"]["content"] == "final answer"
        assert result.json()["usage"]["prompt_tokens"] == 7
        assert result.json()["usage"]["completion_tokens"] == 3
        assert result.headers["x-session-event"] == "created"
        first = json.loads(print_chat.observation.read_text())
        assert not Path(first["cwd"]).exists()

        result = await http.post(
            "/v1/chat/completions", json={"model": "print-chat", "messages": MESSAGES, "stream": True},
        )
        assert result.status_code == 200, result.text
        assert "streamed text" in result.text and "[DONE]" in result.text
        assert "final answer" not in result.text

    second = json.loads(print_chat.observation.read_text())
    assert first["cwd"] != second["cwd"]
    for observed in (first, second):
        assert not Path(observed["cwd"]).exists()
        assert observed["cwd_entries"] == []
        assert observed["cwd_mode"] == 0o700
        assert observed["secret_inherited"] is False
        assert observed["fixed_env"] == "1"
        assert observed["login_config"] == str(print_chat.login_home / ".cursor")
        assert observed["argv"] == [
            "--print", "--output-format", "stream-json", "--model", "cursor-test",
            "--mode", "ask", "--trust", "--",
            "system: System instructions\n\nuser: Previous question\n\n"
            f"assistant: Previous answer\n\nuser: {PROMPT}",
        ]
    print_chat.lookup.assert_not_awaited()
    assert chat.record_completed.call_count == 2
    chat._safe_upsert_session.assert_not_awaited()
    chat._rotate_hash_key.assert_not_awaited()
    for call in chat.record_completed.call_args_list:
        assert call.kwargs["response"] == "final answer"
        assert call.kwargs["runtime_session_id"] == "print-session"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_print_cli_chat_marks_auth_failure_without_raw_stderr(print_chat, stream):
    async with client() as http:
        result = await http.post("/v1/chat/completions", json={
            "model": "print-chat", "messages": [{"role": "user", "content": "auth-error"}], "stream": stream,
        })
    assert result.status_code == (200 if stream else 401)
    assert "authentication_failed" in result.text
    assert "private diagnostic" not in result.text
    marker = print_chat.stream_auth_failure if stream else chat.record_connection_auth_failure
    marker.assert_awaited_once_with("local", error="authentication_failed")
    chat.record_completed.assert_not_awaited()


@pytest.mark.asyncio
async def test_print_cli_quota_and_attachment_errors(print_chat):
    async with client() as http:
        quota = await http.post("/v1/chat/completions", json={
            "model": "print-chat", "messages": [{"role": "user", "content": "quota-error"}], "stream": True,
        })
        assert quota.status_code == 200
        assert '"code": "provider_usage_limit"' in quota.text and '"retryable": false' in quota.text
        assert "private diagnostic" not in quota.text
        print_chat.observation.unlink()
        attachment = await http.post("/v1/chat/completions", json={
            "model": "print-chat", "messages": [{"role": "user", "content": "hi", "attachments": ["fixture"]}],
        })
        assert attachment.status_code == 400
        assert "does not support images or attachments" in attachment.text
        assert not print_chat.observation.exists()


@pytest.mark.asyncio
async def test_cancelling_print_cli_turn_reaps_process_and_removes_cwd(print_chat):
    preset = print_chat.config.preset_for("print-chat")
    runtime = build_print_cli_session(preset, None, None, None, settings=print_chat.settings)
    async with aclosing(runtime.stream("keep-running")) as events:
        assert await asyncio.wait_for(anext(events), 3) == "streamed text"
        observed = json.loads(print_chat.observation.read_text())
        running = asyncio.create_task(anext(events))
        await asyncio.sleep(0)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    with pytest.raises(ProcessLookupError):
        os.killpg(observed["pid"], 0)
    assert not Path(observed["cwd"]).exists()


@pytest.mark.parametrize("scope,status", [("print-test-caller", 404), ("print-test-restricted", 403)])
@pytest.mark.asyncio
async def test_unknown_chat_and_conversation_alias_respects_scope(print_chat, scope, status):
    async with client(scope) as http:
        for path, extra in (
            ("/v1/chat/completions", {}),
            ("/v1/conversations/executions", {"schema_version": "1", "instructions": "Answer", "max_output_tokens": 10}),
        ):
            response = await http.post(path, json={"model": "unknown", "messages": MESSAGES[-1:], **extra})
            assert response.status_code == status, response.text
            if status == 404:
                assert response.json()["error"]["code"] == "model_not_found"
                assert response.json()["error"]["message"] == "Model 'unknown' does not exist"
    assert not print_chat.observation.exists()


@pytest.mark.asyncio
async def test_print_cli_chat_deadline_reaps_process(print_chat):
    print_chat.config.models["print-chat"].timeout_seconds = 0.5
    async with client() as http:
        response = await http.post("/v1/chat/completions", json={
            "model": "print-chat", "messages": [{"role": "user", "content": "keep-running"}],
        })
    assert response.status_code == 504, response.text
    assert response.json()["error"]["code"] == "timeout"
    observed = json.loads(print_chat.observation.read_text())
    with pytest.raises(ProcessLookupError):
        os.killpg(observed["pid"], 0)
    assert not Path(observed["cwd"]).exists()


@pytest.mark.asyncio
async def test_print_cli_chat_respects_login_home_lock(print_chat):
    release = acquire_login_home(print_chat.login_home)
    try:
        async with client() as http:
            response = await http.post("/v1/chat/completions", json={"model": "print-chat", "messages": MESSAGES})
    finally:
        release()
    assert response.json()["error"]["code"] == "login_in_progress"
    assert not print_chat.observation.exists()


@pytest.mark.asyncio
async def test_print_cli_chat_does_not_wait_for_helpers_holding_stderr(print_chat):
    """A helper the CLI leaves behind must neither stall the turn nor survive it."""
    import os
    import time

    messages = [{"role": "user", "content": "leave-helper"}]
    started = time.monotonic()
    async with client() as http:
        result = await http.post("/v1/chat/completions", json={"model": "print-chat", "messages": messages})
    elapsed = time.monotonic() - started
    assert result.status_code == 200, result.text
    assert result.json()["choices"][0]["message"]["content"] == "final answer"
    assert elapsed < 15
    helper = int((print_chat.login_home / "helper.pid").read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(helper, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("helper process outlived the turn")



async def test_acp_route_reuses_live_session_across_http_completion_modes(print_chat, tmp_path, monkeypatch):
    cfg = print_chat.config
    cfg.routes["print"].driver = "cli.acp"
    cfg.routes["print"].model = "model-x"
    alias_cwd = tmp_path / "alias-cwd"
    alias_cwd.mkdir()
    cfg.models["print-chat"].cwd = str(alias_cwd)
    cfg.models["print-chat"].tools = ["Read"]
    print_chat.lookup.return_value = None
    monkeypatch.setattr("kotoyose.chat_session.sessions", session.sessions)
    # Host chat runs in a private temporary cwd (like cli.print chat), never the alias cwd.
    chat_dirs = []

    class _ChatDir:
        def __init__(self, prefix):
            self.path = tmp_path / f"{prefix}{len(chat_dirs)}"

        def __enter__(self):
            self.path.mkdir()
            chat_dirs.append(self.path)
            return str(self.path)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("kotoyose.runtime.acp.tempfile.TemporaryDirectory", lambda prefix: _ChatDir(prefix))
    monkeypatch.setattr("kotoyose.chat_session.session_cache_identities", session.session_cache_identities)

    binary = Path(print_chat.settings.drivers["cli.print"].profile_binaries["cursor"])
    fixture = Path(__file__).with_name("fixtures") / "fake_acp_agent.py"
    binary.write_text(f"#!{sys.executable}\n" + f"""
import runpy, sys
assert sys.argv[1:] == ["acp"]
runpy.run_path({str(fixture)!r}, run_name="__main__")
""")
    try:
        async with client() as http:
            first = await http.post("/v1/chat/completions", json={"model": "print-chat", "messages": MESSAGES})
            assert first.status_code == 200, first.text
            assert first.json()["choices"][0]["message"]["content"] == "hello world"
            assert first.json()["usage"]["prompt_tokens"] == 10
            assert first.headers["x-session-event"] == "created"
            follow_up = [{"role": "user", "content": "next"}]
            second = await http.post("/v1/chat/completions", json={
                "model": "print-chat", "messages": follow_up, "stream": True,
            })
            assert second.status_code == 200, second.text
            assert second.headers["x-session-event"] == "cache_hit"
            assert "world" in second.text
    finally:
        for active_session in session.sessions.values():
            await active_session.disconnect()

    assert len(chat_dirs) == 1 and chat_dirs[0].name.startswith("kotoyose-acp-chat-")
    assert not (alias_cwd / "acp-requests.jsonl").exists()
    calls = [json.loads(line) for line in (chat_dirs[0] / "acp-requests.jsonl").read_text().splitlines()]
    assert [call["method"] for call in calls if "method" in call] == [
        "initialize", "session/new", "session/set_config_option", "session/prompt", "session/prompt",
    ]
    prompts = [call["params"]["prompt"] for call in calls if call.get("method") == "session/prompt"]
    assert prompts[-1] == [{"type": "text", "text": "next"}]
