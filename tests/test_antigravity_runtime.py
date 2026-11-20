"""Antigravity CLI runtime tests for the modern headless stream-json protocol."""

from __future__ import annotations

import asyncio
import json
import shutil

import pytest

from kotoyose.runtime.antigravity import (
    AntigravityRuntimeSession,
    _extract_session_id,
    _extract_text_delta,
    _extract_usage,
    build_antigravity_session,
)
from kotoyose.runtime.antigravity_probe import _parse_cli_version
from kotoyose.runtime.antigravity_workspace import (
    AntigravityWorkspaceOverlay,
    _translate_mcp_server,
)
from kotoyose.runtime.base import RuntimeEvent, RuntimeResult
from kotoyose.runtime.translation import RuntimeSkill, TranslatedRuntimeConfig


def test_extracts_antigravity_result_fields():
    payload = {
        "event": "result",
        "result": {
            "conversation_id": "conv_abc",
            "response": "ok",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
    }
    assert _extract_session_id(payload) == "conv_abc"
    assert _extract_text_delta(payload) == "ok"
    assert _extract_usage(payload) == {"input_tokens": 10, "output_tokens": 2}


def test_parse_cli_version_from_current_and_invalid_output():
    assert _parse_cli_version("agy 1.1.24") == (1, 1, 24)
    assert _parse_cli_version("unknown") is None


async def test_stream_skips_invalid_utf8_before_valid_result():
    """壊れた byte を含む診断行の後でも、後続の正常 result を処理する。"""
    reader = asyncio.StreamReader()
    reader.feed_data(b"\xff\n")
    reader.feed_data(
        b'{"event":"result","result":{"status":"SUCCESS","response":"ok"}}\n'
    )
    reader.feed_eof()
    session = AntigravityRuntimeSession(model=None, cwd="/tmp")

    items = [item async for item in session._read_turn(reader, cumulative_usage=False)]

    assert items[0] == "ok"
    assert isinstance(items[1], RuntimeResult)
    assert items[1].text == "ok"


def test_build_command_for_persistent_stream_session():
    session = AntigravityRuntimeSession(
        model="gemini-3.8-flash",
        cwd="/tmp/work",
        config={
            "effort": "high",
            "print_timeout": "30s",
            "dangerously_skip_permissions": True,
            "sandbox": True,
            "agent": "coding",
            "add_dirs": ["/tmp/shared-a", "/tmp/shared-b"],
            "new_project": True,
        },
    )
    cmd = session._build_command(None, persistent=True)
    assert cmd[:3] == ["agy", "--model", "gemini-3.8-flash"]
    assert ["--effort", "high"] == cmd[cmd.index("--effort") : cmd.index("--effort") + 2]
    assert ["--input-format", "stream-json"] == cmd[
        cmd.index("--input-format") : cmd.index("--input-format") + 2
    ]
    assert ["--output-format", "stream-json"] == cmd[
        cmd.index("--output-format") : cmd.index("--output-format") + 2
    ]
    assert "--print" not in cmd
    assert "--sandbox" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert cmd.count("--add-dir") == 2
    assert ["--add-dir", "/tmp/shared-a"] == cmd[
        cmd.index("--add-dir") : cmd.index("--add-dir") + 2
    ]
    assert "--new-project" in cmd


def test_build_command_for_one_shot_stream_json():
    session = AntigravityRuntimeSession(
        model=None,
        cwd="/tmp/work",
        session_id="conv_abc",
    )
    cmd = session._build_command("continue", persistent=False)
    assert ["--conversation", "conv_abc"] == cmd[
        cmd.index("--conversation") : cmd.index("--conversation") + 2
    ]
    assert ["--output-format", "stream-json"] == cmd[
        cmd.index("--output-format") : cmd.index("--output-format") + 2
    ]
    assert cmd[-2:] == ["--print", "continue"]


def test_translate_remote_mcp_to_sparse_cli_schema():
    assert _translate_mcp_server(
        {
            "type": "http",
            "url": "https://example.test/mcp",
            "headers": {"Authorization": "Bearer secret"},
            "disabled_tools": ["remember"],
        }
    ) == {
        "serverUrl": "https://example.test/mcp",
        "headers": {"Authorization": "Bearer secret"},
        "disabledTools": ["remember"],
    }


def test_translate_stdio_mcp_to_sparse_cli_schema():
    assert _translate_mcp_server(
        {
            "type": "stdio",
            "command": "python3",
            "args": ["server.py"],
            "env": {"MODE": "test"},
        }
    ) == {
        "command": "python3",
        "args": ["server.py"],
        "env": {"MODE": "test"},
    }


def test_workspace_overlay_stages_mcp_skill_agent_and_git_exclude(tmp_path):
    from kotoyose.config import AgentPreset

    git_info = tmp_path / ".git" / "info"
    git_info.mkdir(parents=True)
    (git_info / "exclude").write_text("# existing\n", encoding="utf-8")

    skill_dir = tmp_path / "source-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("# Review\n", encoding="utf-8")

    translated = TranslatedRuntimeConfig(
        instructions="system",
        plugin_paths=(),
        mcp_servers={
            "memory": {
                "type": "http",
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "Bearer secret"},
            }
        },
        skills=(RuntimeSkill(name="review", path=str(skill_file)),),
        agents={
            "researcher": AgentPreset(
                description="read only",
                prompt="Inspect the repository.",
                tools=["Read", "Grep"],
                model="haiku",
            )
        },
        effort="high",
        timeout_seconds=None,
        unsupported_fields=(),
    )

    overlay = AntigravityWorkspaceOverlay(cwd=str(tmp_path), translated=translated)
    overlay.prepare()
    assert overlay.plugin_root is not None
    root = overlay.plugin_root

    manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"].startswith("kotoyose-runtime-")
    mcp = json.loads((root / "mcp_config.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["memory"]["serverUrl"] == "https://example.test/mcp"
    assert (root / "skills" / "review" / "SKILL.md").is_file()
    agent_md = (root / "agents" / "researcher" / "agent.md").read_text(encoding="utf-8")
    assert "subagent: true" in agent_md
    assert "model: flash" in agent_md
    assert "view_file" in agent_md
    assert "/.agents/plugins/.kotoyose-runtime-*" in (
        git_info / "exclude"
    ).read_text(encoding="utf-8")

    overlay.cleanup()
    assert not root.exists()


class _FakeStdin:
    def __init__(self):
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _FakeProcess:
    def __init__(self, stdout: asyncio.StreamReader, stderr: asyncio.StreamReader):
        self.stdin = _FakeStdin()
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = None
        self.kill_called = False
        self.wait_calls = 0

    def kill(self) -> None:
        self.kill_called = True
        if not self.stdout.at_eof():
            self.stdout.feed_eof()
        if not self.stderr.at_eof():
            self.stderr.feed_eof()

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            self.returncode = -9 if self.kill_called else 0
        return self.returncode


def _reader(lines: list[dict] | None = None, *, eof: bool = False) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for line in lines or []:
        reader.feed_data((json.dumps(line) + "\n").encode())
    if eof:
        reader.feed_eof()
    return reader


@pytest.fixture
def patch_which(monkeypatch):
    monkeypatch.setattr("kotoyose.runtime.antigravity.shutil.which", lambda _: "/usr/bin/agy")

    async def accept_version(_command):
        return None

    monkeypatch.setattr(
        "kotoyose.runtime.antigravity._verify_cli_version", accept_version
    )


async def test_persistent_process_reused_and_usage_is_per_turn(monkeypatch, patch_which):
    stdout = _reader(
        [
            {"event": "init", "conversation_id": "conv-1", "init": {}},
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv-1",
                    "step_index": 1,
                    "state": "ACTIVE",
                    "step_type": "tool",
                    "tool_info": {"name": "run_command", "id": "tool-1"},
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv-1",
                    "step_index": 2,
                    "state": "DONE",
                    "step_type": "agent_response",
                    "text_delta": "hello",
                },
            },
            {
                "event": "result",
                "result": {
                    "conversation_id": "conv-1",
                    "status": "SUCCESS",
                    "response": "hello",
                    "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
                },
            },
            {
                "event": "step_update",
                "step_update": {
                    "conversation_id": "conv-1",
                    "step_index": 3,
                    "state": "DONE",
                    "step_type": "agent_response",
                    "text_delta": "again",
                },
            },
            {
                "event": "result",
                "result": {
                    "conversation_id": "conv-1",
                    "status": "SUCCESS",
                    "response": "again",
                    "usage": {"input_tokens": 18, "output_tokens": 7, "total_tokens": 25},
                },
            },
        ]
    )
    stderr = _reader()
    proc = _FakeProcess(stdout, stderr)
    calls = 0
    create_kwargs = None

    async def fake_create(*args, **kwargs):
        nonlocal calls, create_kwargs
        calls += 1
        create_kwargs = kwargs
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    session = AntigravityRuntimeSession(model="gemini-3.8-flash", cwd="/tmp/work")

    first = [item async for item in session.stream("first")]
    second = [item async for item in session.stream("second")]

    assert calls == 1
    assert create_kwargs["limit"] > 64 * 1024
    assert json.loads(proc.stdin.writes[0])["message"]["content"] == "first"
    assert json.loads(proc.stdin.writes[1])["message"]["content"] == "second"
    tool = next(item for item in first if isinstance(item, RuntimeEvent))
    assert tool.kind == "tool"
    assert tool.tool_name == "run_command"
    first_result = next(item for item in first if isinstance(item, RuntimeResult))
    second_result = next(item for item in second if isinstance(item, RuntimeResult))
    assert first_result.usage == {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}
    assert second_result.usage == {"input_tokens": 8, "output_tokens": 3, "total_tokens": 11}
    assert first_result.runtime_session_id == "conv-1"

    await session.disconnect()


async def test_persistent_restart_resets_cumulative_usage(monkeypatch, patch_which):
    first_proc = _FakeProcess(
        _reader([
            {"event": "init", "conversation_id": "a"},
            {"event": "result", "result": {"conversation_id": "a", "status": "SUCCESS", "response": "a", "usage": {"input_tokens": 100, "output_tokens": 20}}},
        ]),
        _reader(),
    )
    second_proc = _FakeProcess(
        _reader([
            {"event": "init", "conversation_id": "b"},
            {"event": "result", "result": {"conversation_id": "b", "status": "SUCCESS", "response": "b", "usage": {"input_tokens": 5, "output_tokens": 2}}},
        ]),
        _reader(),
    )
    procs = iter([first_proc, second_proc])

    async def fake_create(*args, **kwargs):
        return next(procs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    session = AntigravityRuntimeSession(model=None, cwd="/tmp/work")
    first = [item async for item in session.stream("a")]
    first_proc.returncode = 1
    second = [item async for item in session.stream("b")]
    assert next(x for x in first if isinstance(x, RuntimeResult)).usage["input_tokens"] == 100
    assert next(x for x in second if isinstance(x, RuntimeResult)).usage["input_tokens"] == 5
    await session.disconnect()


async def test_idle_timeout_invalidates_session(monkeypatch, patch_which):
    stdout = _reader()
    stderr = _reader()
    proc = _FakeProcess(stdout, stderr)

    async def fake_create(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    session = AntigravityRuntimeSession(
        model=None,
        cwd="/tmp/work",
        config={"timeout": 0.01},
    )
    with pytest.raises(RuntimeError, match="idle timeout"):
        async for _ in session.stream("never replies"):
            pass
    assert session.invalidated is True
    await session.disconnect()


async def test_one_shot_result_exit_timeout_kills_and_reaps_process(
    monkeypatch, patch_which
):
    """Result 後に pipe を閉じない CLI を待ち続けず、process を回収する。"""
    stdout = _reader(
        [{"event": "result", "result": {"status": "SUCCESS", "response": "ok"}}],
        eof=True,
    )
    proc = _FakeProcess(stdout, _reader())

    async def fake_create(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(
        "kotoyose.runtime.antigravity._PROCESS_EXIT_TIMEOUT_SECONDS", 0.001
    )
    session = AntigravityRuntimeSession(
        model=None, cwd="/tmp", config={"persistent": False}
    )

    stream = session.stream("hello")
    assert await anext(stream) == "ok"
    with pytest.raises(RuntimeError, match="result 後に終了しませんでした"):
        await anext(stream)

    assert proc.kill_called is True
    assert proc.wait_calls == 1


async def test_one_shot_yields_completion_after_clean_exit(monkeypatch, patch_which):
    """正常終了を確認した後にだけ one-shot の完了結果を通知する。"""
    stdout = _reader(
        [{"event": "result", "result": {"status": "SUCCESS", "response": "ok"}}],
        eof=True,
    )
    proc = _FakeProcess(stdout, _reader(eof=True))

    async def fake_create(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    session = AntigravityRuntimeSession(
        model=None, cwd="/tmp", config={"persistent": False}
    )

    items = [item async for item in session.stream("hello")]

    assert items[0] == "ok"
    assert isinstance(items[1], RuntimeResult)
    assert items[1].text == "ok"
    assert proc.kill_called is False
    assert proc.wait_calls == 1


async def test_cancelled_turn_kills_process_before_next_turn(monkeypatch, patch_which):
    """途中で閉じた turn の残りを、次の turn の応答として再利用しない。"""
    first_proc = _FakeProcess(
        _reader(
            [
                {
                    "event": "step_update",
                    "step_update": {
                        "state": "ACTIVE",
                        "step_type": "agent_response",
                        "text_delta": "OLD",
                    },
                },
                {
                    "event": "result",
                    "result": {"status": "SUCCESS", "response": "OLD"},
                },
            ]
        ),
        _reader(),
    )
    second_proc = _FakeProcess(
        _reader(
            [
                {
                    "event": "result",
                    "result": {"status": "SUCCESS", "response": "NEW"},
                }
            ]
        ),
        _reader(),
    )
    procs = iter([first_proc, second_proc])

    async def fake_create(*args, **kwargs):
        return next(procs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    session = AntigravityRuntimeSession(model=None, cwd="/tmp/work")

    stream = session.stream("first")
    assert await anext(stream) == "OLD"
    await stream.aclose()

    assert session.invalidated is True
    assert first_proc.kill_called is True
    second = [item async for item in session.stream("second")]
    assert next(item for item in second if isinstance(item, str)) == "NEW"
    await session.disconnect()


def test_workspace_overlay_rejects_resource_free_session_in_same_cwd(tmp_path):
    """resource なし session も、別 session の MCP 設定を発見できない。"""
    translated = TranslatedRuntimeConfig(
        instructions=None,
        plugin_paths=(),
        mcp_servers={
            "memory": {"type": "http", "url": "https://example.test/mcp"}
        },
        skills=(),
        agents={},
        effort=None,
        timeout_seconds=None,
        unsupported_fields=(),
    )
    first = AntigravityWorkspaceOverlay(cwd=str(tmp_path), translated=translated)
    empty = TranslatedRuntimeConfig(
        instructions=None,
        plugin_paths=(),
        mcp_servers={},
        skills=(),
        agents={},
        effort=None,
        timeout_seconds=None,
        unsupported_fields=(),
    )
    second = AntigravityWorkspaceOverlay(cwd=str(tmp_path), translated=empty)

    first.prepare()
    try:
        with pytest.raises(RuntimeError, match="同じ cwd"):
            second.prepare()
    finally:
        first.cleanup()

    second.prepare()
    second.cleanup()


def test_native_plugin_partial_copy_is_tracked_for_cleanup(tmp_path):
    """native plugin の copy 失敗後に discovery 対象を残さない。"""
    source = tmp_path / "native-source"
    source.mkdir()
    (source / "plugin.json").write_text('{"name":"native"}', encoding="utf-8")
    (source / "broken-link").symlink_to(source / "missing")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    translated = TranslatedRuntimeConfig(
        instructions=None,
        plugin_paths=(),
        mcp_servers={},
        skills=(),
        agents={},
        effort=None,
        timeout_seconds=None,
        unsupported_fields=(),
    )
    overlay = AntigravityWorkspaceOverlay(
        cwd=str(workspace),
        translated=translated,
        native_plugin_paths=(str(source),),
    )

    with pytest.raises(shutil.Error):
        overlay.prepare()
    overlay.cleanup()

    assert not list((workspace / ".agents" / "plugins").glob(".kotoyose-runtime-*"))


def test_build_antigravity_session_resolves_mcp_without_unsupported_warning(tmp_path, monkeypatch):
    from structlog.testing import capture_logs

    from kotoyose.config import HarnessConfig, McpDefinition, ModelPreset

    cfg = HarnessConfig(
        mcps={
            "memory": McpDefinition(
                server={"type": "http", "url": "https://example.test/mcp"}
            )
        }
    )
    preset = ModelPreset(
        provider="antigravity",
        model="gemini-3.8-flash",
        cwd=str(tmp_path),
        mcp_refs=["memory"],
        effort="high",
    )
    monkeypatch.setattr("kotoyose.config._config", cfg)

    with capture_logs() as cap:
        session = build_antigravity_session(preset, None, None)

    assert session.config["effort"] == "high"
    assert session.overlay is not None and session.overlay.plugin_root is not None
    mcp_file = session.overlay.plugin_root / "mcp_config.json"
    assert mcp_file.is_file()
    unsupported = [
        item for item in cap
        if item.get("event") == "antigravity.runtime_resources_unsupported"
    ]
    assert unsupported == []
    session.overlay.cleanup()


@pytest.mark.parametrize("status", ["ERROR", "CANCELLED", "TIMEOUT", "MAX_TURNS", "", "FUTURE_STATUS"])
async def test_partial_failure_retains_usage_and_never_returns_success(status):
    from kotoyose.runtime.base import RuntimeTurnError
    reader = _reader([
        {"event": "init", "init": {"conversation_id": "turn-session"}},
        {"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "partial"}},
        {"event": "result", "result": {"status": status, "response": "", "error": "SECRET error", "usage": {"input_tokens": 9}}},
    ], eof=True)
    session = AntigravityRuntimeSession(model=None, cwd="/tmp")
    items = []
    with pytest.raises(RuntimeTurnError) as error:
        async for item in session._read_turn(reader, cumulative_usage=False):
            items.append(item)
    assert items == ["partial"]
    assert error.value.partial_result.text == "partial"
    assert error.value.partial_result.runtime_session_id == "turn-session"
    assert error.value.partial_result.usage == {"input_tokens": 9}
    assert session.invalidated
    assert "SECRET" not in str(error.value)
    assert "SECRET" not in repr(error.value.diagnostics)


@pytest.mark.parametrize("result_status", ["SUCCESS", "ERROR", None])
async def test_one_shot_exit3_uses_only_typed_agy_error_metadata(monkeypatch, patch_which, result_status):
    from kotoyose.runtime.base import RuntimeTurnError
    stdout = _reader([
        {"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "partial"}},
        *([{"event": "result", "result": {"status": result_status, "usage": {"output_tokens": 2}}}] if result_status else []),
    ], eof=True)
    stderr = _reader(eof=False)
    stderr.feed_data(b'SECRET raw diagnostic\nAGY_ERROR: {"status":"RESOURCE_EXHAUSTED","code":429,"retryable":false,"error_id":"deadbeef-1234","message":"SECRET credential"}\n')
    stderr.feed_eof()
    proc = _FakeProcess(stdout, stderr)
    proc.returncode = 3
    async def create(*args, **kwargs):
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    session = AntigravityRuntimeSession(model=None, cwd="/tmp", config={"persistent": False})
    items = []
    with pytest.raises(RuntimeTurnError) as error:
        async for item in session.stream("hi"):
            items.append(item)
    assert not any(isinstance(item, RuntimeResult) for item in items)
    assert error.value.partial_result.text == "partial"
    assert error.value.api_error_status == 429
    assert error.value.retryable is False
    assert error.value.diagnostics["status"] == "RESOURCE_EXHAUSTED"
    assert error.value.diagnostics["exit_code"] == 3
    assert error.value.diagnostics["error_id"] == "deadbeef-1234"
    assert "SECRET" not in str(error.value)
    assert "SECRET" not in repr(error.value.diagnostics)


def test_agy_error_parser_ignores_untyped_or_sensitive_fields():
    from kotoyose.runtime.antigravity import _parse_agy_error
    assert _parse_agy_error('AGY_ERROR: {"status":"SECRET","code":"429","retryable":"false","error_id":"token-secret"}') == {}
    assert _parse_agy_error("AGY_ERROR: invalid") == {}
    assert _parse_agy_error('prefix AGY_ERROR: {"code":429}') == {}
    assert _parse_agy_error('AGY_ERROR: {"status":"UNAVAILABLE","code":503,"retryable":true}') == {
        "status": "UNAVAILABLE", "api_error_status": 503, "retryable": True,
    }


def test_usage_projects_only_nonnegative_integer_counters():
    assert _extract_usage({"usage": {"input_tokens": 4, "output_tokens": -1, "thinking_tokens": True, "total_tokens": "4", "credential": "SECRET"}}) == {"input_tokens": 4}


async def test_ask_consumes_terminal_before_reusing_persistent_process(monkeypatch, patch_which):
    stdout = _reader([
        {"event": "result", "result": {"status": "SUCCESS", "response": "one"}},
        {"event": "result", "result": {"status": "SUCCESS", "response": "two"}},
    ])
    proc = _FakeProcess(stdout, _reader())
    async def create(*args, **kwargs):
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    session = AntigravityRuntimeSession(model=None, cwd="/tmp")
    assert (await session.ask("one")).text == "one"
    assert not proc.kill_called
    assert (await session.ask("two")).text == "two"
    assert not session.invalidated
    await session.disconnect()


def test_build_antigravity_session_uses_connection_login_home_and_minimal_env(tmp_path, monkeypatch):
    """chat の agy も Kotoyose の環境変数を引き継がず、route の connection の login home を HOME にする。"""
    from kotoyose.config import HarnessConfig, ModelPreset

    login_home = tmp_path / "agy-home"
    login_home.mkdir()
    monkeypatch.setenv("KOTOYOSE_SENTINEL_SECRET", "must-not-leak")
    cfg = HarnessConfig(
        connections={"agy-home": {"provider": "antigravity", "kind": "subscription", "data_policy": "no_training", "config_dir": str(login_home)}},
        routes={"agy-route": {"driver": "antigravity.agy", "model": "gemini-3.8-flash", "connection": "agy-home"}},
        models={"agy-chat": {"route": "agy-route", "cwd": str(tmp_path)}},
    )
    monkeypatch.setattr("kotoyose.config._config", cfg)
    preset = cfg.preset_for("agy-chat")
    assert preset.route == "agy-route"

    session = build_antigravity_session(preset, None, None)
    assert session.env is not None
    assert session.env["HOME"] == str(login_home)
    assert "KOTOYOSE_SENTINEL_SECRET" not in session.env
    assert set(session.env) <= {"PATH", "LANG", "TERM", "HOME"}

    # 専用の home が無い connection でも、この機械の HOME と allowlist だけ。
    other = tmp_path / "other"
    other.mkdir()
    plain = ModelPreset(provider="antigravity", model="gemini-3.8-flash", cwd=str(other))
    session_plain = build_antigravity_session(plain, None, None)
    assert "KOTOYOSE_SENTINEL_SECRET" not in session_plain.env
    assert set(session_plain.env) <= {"PATH", "LANG", "TERM", "HOME"}
    for item in (session, session_plain):
        if item.overlay is not None:
            item.overlay.cleanup()
