"""Codex runtime adapter の単体テスト。

openai-codex SDK (AsyncCodex / app-server) を monkeypatch した fake で検証する。
notification は本物の pydantic model で組み立てる (adapter が isinstance 判定するため)。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from openai_codex import SkillInput, TextInput
from openai_codex.generated.v2_all import (
    ThreadTokenUsage,
    TokenUsageBreakdown,
    Turn,
    TurnError,
    TurnStatus,
)
from openai_codex.models import (
    AgentMessageDeltaNotification,
    ErrorNotification,
    ItemCompletedNotification,
    ItemStartedNotification,
    Notification,
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
    TurnStartedNotification,
)

from kotoyose.runtime.base import RuntimeEvent
from kotoyose.runtime.codex import (
    ApprovalMode,
    CodexRuntimeSession,
    Sandbox,
    _codex_mcp_config_entries,
    _dotted_config_entries,
    _map_approval_mode,
    _map_sandbox,
    _model_provider_overrides,
    _per_turn_usage,
    _resolve_codex_bin,
    build_codex_session,
)
from kotoyose.runtime.translation import RuntimeSkill

# ---------------------------------------------------------------------------
# _codex_mcp_config_entries (維持対象・既存のまま)
# ---------------------------------------------------------------------------


def test_codex_mcp_config_entries_for_stdio_server():
    """stdio server: command / args / env / cwd を dotted key に変換する。"""
    entries = _codex_mcp_config_entries(
        "tool",
        {
            "command": "npx",
            "args": ["-y", "some-server"],
            "env": {"API_KEY": "xxx"},
            "cwd": "/work",
        },
    )
    assert ("mcp_servers.tool.command", '"npx"') in entries
    assert ("mcp_servers.tool.args", '["-y", "some-server"]') in entries
    assert ("mcp_servers.tool.env.API_KEY", '"xxx"') in entries
    assert ("mcp_servers.tool.cwd", '"/work"') in entries


def test_codex_mcp_config_entries_for_http_server():
    """http server: url / headers を dotted key (http_headers.*) に変換する。"""
    entries = _codex_mcp_config_entries(
        "remote",
        {
            "type": "http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer xxx"},
        },
    )
    assert ("mcp_servers.remote.url", '"https://example.com/mcp"') in entries
    assert ("mcp_servers.remote.http_headers.Authorization", '"Bearer xxx"') in entries


def test_codex_mcp_config_entries_rejects_sse_type():
    """sse (codex に相当フラグ無し) は silent ignore せず ValueError。"""
    with pytest.raises(ValueError, match="sse"):
        _codex_mcp_config_entries("remote", {"type": "sse", "url": "https://example.com/mcp"})


def test_codex_mcp_config_entries_rejects_header_template():
    """Claude 専用の {{header:...}} テンプレートは codex では未対応として拒否する。"""
    with pytest.raises(ValueError, match="request header"):
        _codex_mcp_config_entries(
            "remote",
            {
                "type": "http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "{{header:x-token}}"},
            },
        )


def test_codex_mcp_config_entries_defaults_stdio_when_command_present():
    """type 未指定でも command があれば stdio として扱う (既存挙動)。"""
    entries = _codex_mcp_config_entries("tool", {"command": "npx"})
    assert ("mcp_servers.tool.command", '"npx"') in entries


def test_codex_mcp_config_entries_forwards_native_tool_filters():
    """Codex native の MCP tool filter と timeout を落とさず渡す。"""
    entries = _codex_mcp_config_entries(
        "memory",
        {
            "command": "memory-mcp",
            "enabled_tools": ["search", "read"],
            "disabled_tools": ["remember"],
            "tool_timeout_sec": 30,
            "required": True,
        },
    )

    assert ("mcp_servers.memory.enabled_tools", '["search", "read"]') in entries
    assert ("mcp_servers.memory.disabled_tools", '["remember"]') in entries
    assert ("mcp_servers.memory.tool_timeout_sec", "30") in entries
    assert ("mcp_servers.memory.required", "true") in entries


# ---------------------------------------------------------------------------
# build_codex_session (維持対象・既存のまま)
# ---------------------------------------------------------------------------


def test_build_codex_session_resolves_prompt_files_into_instructions(tmp_path, monkeypatch):
    """builder: prompt_file_refs で解決した本文を instructions に載せる。"""
    from kotoyose.config import HarnessConfig, ModelPreset, PromptFileDefinition

    prompt = tmp_path / "role.md"
    prompt.write_text("You are Codex.", encoding="utf-8")
    cfg = HarnessConfig(prompt_files={"role": PromptFileDefinition(path=str(prompt))})
    preset = ModelPreset(provider="codex", cwd=str(tmp_path), prompt_file_refs=["role"])

    monkeypatch.setattr("kotoyose.config._config", cfg)
    session = build_codex_session(preset, None, None)

    assert session.instructions == "You are Codex."


def test_build_codex_session_resolves_mcp_refs_into_mcp_servers(tmp_path, monkeypatch):
    """builder: mcp_refs で解決した MCP 設定を mcp_servers に載せる。"""
    from kotoyose.config import HarnessConfig, McpDefinition, ModelPreset

    cfg = HarnessConfig(mcps={"tool": McpDefinition(server={"command": "some-mcp"})})
    preset = ModelPreset(provider="codex", cwd=str(tmp_path), mcp_refs=["tool"])

    monkeypatch.setattr("kotoyose.config._config", cfg)
    session = build_codex_session(preset, None, None)

    assert session.mcp_servers == {"tool": {"command": "some-mcp"}}


def test_build_codex_session_defaults_instructions_to_none_without_prompt_file_refs(tmp_path, monkeypatch):
    """既存挙動維持: prompt_file_refs 未設定の preset は instructions=None のまま。"""
    from kotoyose.config import HarnessConfig, ModelPreset

    cfg = HarnessConfig()
    preset = ModelPreset(provider="codex", cwd=str(tmp_path))

    monkeypatch.setattr("kotoyose.config._config", cfg)
    session = build_codex_session(preset, None, None)

    assert session.instructions is None
    assert session.mcp_servers == {}


def test_build_codex_session_translates_skills_agents_effort_and_timeout(tmp_path, monkeypatch):
    """builder が共通設定を Codex session の native field へまとめて載せる。"""
    import json

    from kotoyose.config import HarnessConfig, ModelPreset, SkillDefinition

    bundle = tmp_path / "bundle"
    skill_dir = bundle / "skills" / "debug"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# debug", encoding="utf-8")
    (bundle / ".claude-plugin").mkdir()
    (bundle / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "bundle", "version": "1.0.0"}), encoding="utf-8"
    )
    cfg = HarnessConfig(
        skill_definitions={
            "debug": SkillDefinition(plugin=str(bundle), skill="debug")
        }
    )
    preset = ModelPreset(
        provider="codex",
        cwd=str(tmp_path),
        skill_refs=["debug"],
        effort="xhigh",
        timeout_seconds=90,
    )

    monkeypatch.setattr("kotoyose.config._config", cfg)
    session = build_codex_session(preset, None, None)

    [skill] = session.skill_folders
    assert skill.name == "debug"
    assert skill.files == {"SKILL.md": b"# debug"}
    assert session.effort == "xhigh"
    assert session.config["timeout"] == 90
    assert "agents.enabled=false" in session._build_config_overrides()


def test_build_codex_session_translates_isolation_and_mcp_deny(tmp_path, monkeypatch):
    """共通の隔離設定と MCP deny を Codex native config へ変換する。"""
    from kotoyose.config import HarnessConfig, McpDefinition, ModelPreset

    cfg = HarnessConfig(
        mcps={"memory-taskhub": McpDefinition(server={"command": "memory-mcp"})}
    )
    preset = ModelPreset(
        provider="codex",
        cwd=str(tmp_path),
        bare=False,
        setting_sources=[],
        mcp_refs=["memory-taskhub"],
        deny=["mcp__memory-taskhub__remember", "CronCreate"],
    )

    monkeypatch.setattr("kotoyose.config._config", cfg)
    session = build_codex_session(preset, None, None)

    assert session.config["config"]["project_doc_max_bytes"] == 0
    assert session.mcp_servers["memory-taskhub"]["disabled_tools"] == ["remember"]


def test_build_codex_session_carries_codex_home(tmp_path, monkeypatch):
    """codex_home を preset から session へ運ぶ (~ は展開する)。

    bare (= project_doc_max_bytes: 0) が塞ぐのは *project* の AGENTS.md だけで、
    $CODEX_HOME/AGENTS.md と plugins は塞げない。隔離したい preset に専用の
    CODEX_HOME を与えられるようにする。
    """
    import os

    from kotoyose.config import HarnessConfig, ModelPreset

    monkeypatch.setattr("kotoyose.config._config", HarnessConfig())
    preset = ModelPreset(provider="codex", cwd=str(tmp_path), codex_home="~/.codex-reviewer")
    session = build_codex_session(preset, None, None)

    assert session.codex_home == os.path.expanduser("~/.codex-reviewer")


def test_build_codex_session_leaves_codex_home_unset_by_default(tmp_path, monkeypatch):
    """既定は None。既存 preset の挙動を変えない。"""
    from kotoyose.config import HarnessConfig, ModelPreset

    monkeypatch.setattr("kotoyose.config._config", HarnessConfig())
    session = build_codex_session(ModelPreset(provider="codex", cwd=str(tmp_path)), None, None)

    assert session.codex_home is None


# ---------------------------------------------------------------------------
# SDK fake 群
# ---------------------------------------------------------------------------


class _FakeHandle:
    """AsyncTurnHandle の代替。notifications を順に stream() で流す。"""

    def __init__(self, notifications: list[Any], turn_id: str = "turn_1") -> None:
        self.id = turn_id
        self._notifications = notifications
        self.interrupted = False

    async def stream(self):
        for n in self._notifications:
            if callable(n):  # 遅延 (idle timeout 検証用) は callable で表現する
                n = await n()
            yield n

    async def interrupt(self):
        self.interrupted = True


class _FakeThread:
    """AsyncThread の代替。turn() 呼び出しの引数を記録する。"""

    def __init__(
        self, thread_id: str = "thread_abc", handle: _FakeHandle | None = None, turn_delay: float = 0
    ) -> None:
        self.id = thread_id
        self._handle = handle
        self._turn_delay = turn_delay
        self.turn_calls: list[tuple[str, dict]] = []

    async def turn(self, prompt, **kwargs):
        if self._turn_delay:  # turn 開始自体がハングするケース (idle timeout 検証用) を模倣する
            await asyncio.sleep(self._turn_delay)
        self.turn_calls.append((prompt, kwargs))
        return self._handle


class _FakeCodex:
    """AsyncCodex の代替。生成時の CodexConfig と thread_start/resume の kwargs を記録する。

    テストごとに流したい notification 列は `next_handle` にあらかじめ積んでおく
    (thread_start/resume の戻り値である _FakeThread に生成時点で紐付ける)。
    テスト間でメソッドを monkeypatch すると状態がクラスに残って他テストへ漏れる
    ため、インスタンス化のたびに消費するクラス変数で受け渡す。
    """

    last_instance: _FakeCodex | None = None
    next_handle: _FakeHandle | None = None
    next_turn_delay: float = 0

    def __init__(self, config=None) -> None:
        type(self).last_instance = self
        self.config = config
        self._client = SimpleNamespace(request=AsyncMock())
        self.__aenter__ = AsyncMock()
        self.closed = False
        self.start_calls: list[dict] = []
        self.resume_calls: list[tuple[str, dict]] = []
        self.thread = _FakeThread(handle=type(self).next_handle, turn_delay=type(self).next_turn_delay)

    async def thread_start(self, **kwargs):
        self.start_calls.append(kwargs)
        return self.thread

    async def thread_resume(self, thread_id, **kwargs):
        self.resume_calls.append((thread_id, kwargs))
        return self.thread

    async def close(self) -> None:
        self.closed = True


def _notification(payload) -> Notification:
    """envelope を組み立てる。method 文字列は adapter が参照しないため任意でよい。"""
    return Notification(method="codex/event", payload=payload)


def _delta(delta: str, turn_id: str = "turn_1", thread_id: str = "thread_abc") -> Notification:
    return _notification(
        AgentMessageDeltaNotification(itemId="i", threadId=thread_id, turnId=turn_id, delta=delta)
    )


def _breakdown(input_tokens=16035, cached=0, output=100, reasoning=0) -> TokenUsageBreakdown:
    return TokenUsageBreakdown(
        cachedInputTokens=cached,
        inputTokens=input_tokens,
        outputTokens=output,
        reasoningOutputTokens=reasoning,
        totalTokens=input_tokens + cached + output + reasoning,
    )


def _usage_notification(
    input_tokens=16035, cached=0, output=100, reasoning=0, turn_id="turn_1", thread_id="thread_abc",
    total: TokenUsageBreakdown | None = None,
) -> Notification:
    """last と total が同値の snapshot (turn 内 1 回目の request 相当) を既定とし、
    total だけ差し替えて累積が進んだ snapshot も作れるようにする。"""
    breakdown = _breakdown(input_tokens, cached, output, reasoning)
    usage = ThreadTokenUsage(last=breakdown, total=total if total is not None else breakdown)
    return _notification(
        ThreadTokenUsageUpdatedNotification(threadId=thread_id, tokenUsage=usage, turnId=turn_id)
    )


def _turn_completed(status: TurnStatus, error: TurnError | None = None, thread_id="thread_abc", turn_id="turn_1") -> Notification:
    turn = Turn(id=turn_id, items=[], status=status, error=error)
    return _notification(TurnCompletedNotification(threadId=thread_id, turn=turn))


def _error_notification(will_retry: bool, message: str = "boom", turn_id="turn_1", thread_id="thread_abc") -> Notification:
    return _notification(
        ErrorNotification(
            error=TurnError(message=message), threadId=thread_id, turnId=turn_id, willRetry=will_retry
        )
    )


def _item_started(item: dict, turn_id="turn_1", thread_id="thread_abc") -> Notification:
    return _notification(
        ItemStartedNotification.model_validate(
            {"threadId": thread_id, "turnId": turn_id, "startedAtMs": 1, "item": item}
        )
    )


def _item_completed(item: dict, turn_id="turn_1", thread_id="thread_abc") -> Notification:
    return _notification(
        ItemCompletedNotification.model_validate(
            {"threadId": thread_id, "turnId": turn_id, "completedAtMs": 2, "item": item}
        )
    )


@pytest.fixture
def patch_async_codex(monkeypatch):
    """kotoyose.runtime.codex.AsyncCodex を _FakeCodex に差し替える。"""
    monkeypatch.setattr("kotoyose.runtime.codex.AsyncCodex", _FakeCodex)
    yield _FakeCodex
    _FakeCodex.last_instance = None
    _FakeCodex.next_handle = None
    _FakeCodex.next_turn_delay = 0


# ---------------------------------------------------------------------------
# stream()
# ---------------------------------------------------------------------------


async def test_stream_yields_deltas_and_final_result(patch_async_codex):
    """正常系: delta を yield し、usage + session_id 付きの RuntimeResult で終端する。"""
    _FakeCodex.next_handle = _FakeHandle(
        [_delta("hel"), _delta("lo"), _usage_notification(), _turn_completed(TurnStatus.completed)]
    )
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")

    chunks = []
    result = None
    async for item in session.stream("hi"):
        if isinstance(item, str):
            chunks.append(item)
        else:
            result = item

    assert chunks == ["hel", "lo"]
    assert result is not None
    assert result.text == "hello"
    assert result.usage["input_tokens"] == 16035
    assert result.runtime_session_id == "thread_abc"


async def test_stream_passes_codex_home_to_the_sdk_env(patch_async_codex):
    """codex_home を CodexConfig.env の CODEX_HOME として渡す。"""
    _FakeCodex.next_handle = _FakeHandle([_delta("ok"), _turn_completed(TurnStatus.completed)])
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work", codex_home="/tmp/rv-home")

    async for _ in session.stream("hi"):
        pass

    assert _FakeCodex.last_instance is not None
    assert _FakeCodex.last_instance.config.env == {"CODEX_HOME": "/tmp/rv-home"}


async def test_stream_omits_env_without_codex_home(patch_async_codex):
    """codex_home 未指定なら env は渡さない (親プロセスの環境をそのまま使う)。"""
    _FakeCodex.next_handle = _FakeHandle([_delta("ok"), _turn_completed(TurnStatus.completed)])
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")

    async for _ in session.stream("hi"):
        pass

    assert _FakeCodex.last_instance is not None
    assert _FakeCodex.last_instance.config.env is None


async def test_stream_yields_command_and_mcp_runtime_events(patch_async_codex):
    """Codex item lifecycle を Claude 互換 tool event として公開する。"""
    command = {
        "type": "commandExecution",
        "id": "cmd-1",
        "command": "rg TODO src",
        "cwd": "/tmp/work",
        "status": "inProgress",
        "commandActions": [],
    }
    mcp = {
        "type": "mcpToolCall",
        "id": "mcp-1",
        "server": "memory-taskhub",
        "tool": "search",
        "arguments": {"query": "x"},
        "status": "completed",
        "result": {"content": []},
    }
    _FakeCodex.next_handle = _FakeHandle(
        [
            _item_started(command),
            _item_completed({**command, "status": "completed", "exitCode": 0}),
            _item_started({**mcp, "status": "inProgress", "result": None}),
            _item_completed(mcp),
            _turn_completed(TurnStatus.completed),
        ]
    )
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")

    items = [item async for item in session.stream("hi")]
    events = [item for item in items if isinstance(item, RuntimeEvent)]

    assert [(event.kind, event.tool_name) for event in events] == [
        ("tool_use", "Bash"),
        ("tool_result", "Bash"),
        ("tool_use", "mcp__memory-taskhub__search"),
        ("tool_result", "mcp__memory-taskhub__search"),
    ]
    assert events[0].summary == "rg TODO src"
    assert events[0].payload == {"tool_use_id": "cmd-1", "input": '{"command":"rg TODO src"}'}
    assert events[1].payload == {
        "tool_use_id": "cmd-1",
        "is_error": False,
        "output": "",
    }
    assert events[2].payload == {"tool_use_id": "mcp-1", "input": '{"query":"x"}'}
    assert events[3].payload == {
        "tool_use_id": "mcp-1",
        "is_error": False,
        "output": '{"content":[]}',
    }


async def test_stream_yields_turn_and_completed_agent_message_events(patch_async_codex):
    """turn 開始と agentMessage 完了を Claude と同じ payload で記録する。"""
    turn = Turn(id="turn_1", items=[], status=TurnStatus.in_progress)
    agent_message = {"type": "agentMessage", "id": "message-1", "text": "回答です\n続きです"}
    _FakeCodex.next_handle = _FakeHandle(
        [
            Notification(
                method="turn/started",
                payload=TurnStartedNotification(threadId="thread_abc", turn=turn),
            ),
            _item_started(agent_message),
            _item_completed(agent_message),
            _turn_completed(TurnStatus.completed),
        ]
    )
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")

    items = [item async for item in session.stream("hi")]
    events = [item for item in items if isinstance(item, RuntimeEvent)]

    assert [(event.kind, event.payload) for event in events] == [
        ("turn", {"turn": 1}),
        ("text", {"text": "回答です\n続きです", "item_id": "message-1", "phase": None}),
    ]
    assert events[1].summary == "回答です 続きです"


async def test_stream_truncates_agent_message_event_to_500_characters(patch_async_codex):
    """agentMessage の timeline payload は Claude と同じ 500 文字上限にする。"""
    agent_message = {"type": "agentMessage", "id": "message-1", "text": "a" * 501}
    _FakeCodex.next_handle = _FakeHandle(
        [_item_completed(agent_message), _turn_completed(TurnStatus.completed)]
    )
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")

    items = [item async for item in session.stream("hi")]
    event = next(item for item in items if isinstance(item, RuntimeEvent) and item.kind == "text")

    assert len(event.summary) == 500
    assert len(event.payload["text"]) == 500


async def test_stream_ignores_item_event_from_another_turn(patch_async_codex):
    """resume 直後に残った別 turn の item notification は timeline へ混ぜない。"""
    command = {
        "type": "commandExecution",
        "id": "cmd-old",
        "command": "pwd",
        "cwd": "/tmp/work",
        "status": "inProgress",
        "commandActions": [],
    }
    _FakeCodex.next_handle = _FakeHandle(
        [_item_started(command, turn_id="turn_old"), _turn_completed(TurnStatus.completed)]
    )
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")

    items = [item async for item in session.stream("hi")]

    assert not any(isinstance(item, RuntimeEvent) for item in items)


async def test_ask_ignores_runtime_events_when_joining_text(patch_async_codex):
    """非 streaming 集約では tool event を文字列連結せず最終本文だけを返す。"""
    command = {
        "type": "commandExecution",
        "id": "cmd-1",
        "command": "pwd",
        "cwd": "/tmp/work",
        "status": "inProgress",
        "commandActions": [],
    }
    _FakeCodex.next_handle = _FakeHandle(
        [_item_started(command), _delta("done"), _turn_completed(TurnStatus.completed)]
    )
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")

    result = await session.ask("hi")

    assert result.text == "done"


async def test_stream_injects_skills_and_effort_into_turn(patch_async_codex, tmp_path):
    """共通 Skill と effort を Codex SDK の native turn 引数へ変換する。"""
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("# skill", encoding="utf-8")
    _FakeCodex.next_handle = _FakeHandle([_turn_completed(TurnStatus.completed)])
    session = CodexRuntimeSession(
        model="gpt-5.1-codex",
        cwd="/tmp/work",
        skills=(RuntimeSkill(name="debug", path=str(skill_file)),),
        effort="high",
    )

    async for _ in session.stream("find the bug"):
        pass

    turn_input, kwargs = _FakeCodex.last_instance.thread.turn_calls[0]
    assert turn_input == [
        SkillInput(name="debug", path=str(skill_file)),
        TextInput("find the bug"),
    ]
    assert kwargs["effort"] == "high"


async def test_stream_passes_service_tier_to_thread_and_turn(patch_async_codex):
    """Fast is the Codex thread's serviceTier, on start, resume, and the turn."""
    _FakeCodex.next_handle = _FakeHandle([_turn_completed(TurnStatus.completed)])
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work", service_tier="priority")

    async for _ in session.stream("hi"):
        pass

    fake = _FakeCodex.last_instance
    assert fake.start_calls[0]["service_tier"] == "priority"
    assert fake.thread.turn_calls[0][1]["service_tier"] == "priority"

    _FakeCodex.next_handle = _FakeHandle([_turn_completed(TurnStatus.completed)])
    resumed = CodexRuntimeSession(
        model="gpt-5.1-codex", cwd="/tmp/work", session_id="thread_abc", service_tier="priority",
    )
    async for _ in resumed.stream("again"):
        pass

    resumed_fake = _FakeCodex.last_instance
    assert resumed_fake.resume_calls[0][1]["service_tier"] == "priority"
    assert resumed_fake.thread.turn_calls[0][1]["service_tier"] == "priority"


async def test_ask_carries_over_usage_from_stream(patch_async_codex):
    """ask(): stream() の最終 RuntimeResult から usage を引き継ぐ (cursor.py の ask と同型)。"""
    _FakeCodex.next_handle = _FakeHandle(
        [_delta("hel"), _delta("lo"), _usage_notification(), _turn_completed(TurnStatus.completed)]
    )
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")

    result = await session.ask("hi")

    assert result.text == "hello"
    assert result.usage is not None
    assert result.usage["input_tokens"] == 16035
    assert result.runtime_session_id == "thread_abc"


async def test_stream_raises_on_failed_turn(patch_async_codex):
    """turn.failed は Claude 互換 error event を出してから例外にする。"""
    _FakeCodex.next_handle = _FakeHandle([_turn_completed(TurnStatus.failed, error=TurnError(message="boom"))])
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")
    events = []

    with pytest.raises(RuntimeError, match="codex provider turn failed"):
        async for item in session.stream("hi"):
            if isinstance(item, RuntimeEvent):
                events.append(item)

    assert [(event.kind, event.payload) for event in events] == [("error", {"error": "codex provider turn failed (failed)", "retryable": False, "status": "failed"})]
    assert _FakeCodex.last_instance.closed is True


async def test_stream_does_not_duplicate_terminal_error_notification(patch_async_codex):
    """non-retry error 後の failed turn は timeline error を二重記録しない。"""
    _FakeCodex.next_handle = _FakeHandle(
        [
            _error_notification(will_retry=False, message="quota exceeded"),
            _turn_completed(TurnStatus.failed, error=TurnError(message="quota exceeded")),
        ]
    )
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")
    events = []

    with pytest.raises(RuntimeError, match="codex provider turn failed"):
        async for item in session.stream("hi"):
            if isinstance(item, RuntimeEvent):
                events.append(item)

    assert [(event.kind, event.payload) for event in events] == [
        ("error", {"error": "codex provider turn failed", "retryable": False, "will_retry": False})
    ]


async def test_stream_ignores_other_turn_deltas(patch_async_codex):
    """turn_id 不一致の delta は yield しない。"""
    _FakeCodex.next_handle = _FakeHandle(
        [_delta("mine", turn_id="turn_1"), _delta("other", turn_id="turn_other"), _turn_completed(TurnStatus.completed)]
    )
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")

    chunks = []
    async for item in session.stream("hi"):
        if isinstance(item, str):
            chunks.append(item)

    assert chunks == ["mine"]


async def test_stream_idle_timeout_closes_client(patch_async_codex):
    """idle timeout 超過: RuntimeError (idle timeout 文言) + client close。"""

    async def slow():
        await asyncio.sleep(1)
        return _delta("never")

    _FakeCodex.next_handle = _FakeHandle([slow])
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work", config={"timeout": 0.05})

    with pytest.raises(RuntimeError, match="codex"):
        async for _ in session.stream("hi"):
            pass

    assert _FakeCodex.last_instance.closed is True


async def test_stream_idle_timeout_covers_thread_turn_start(patch_async_codex):
    """idle timeout は thread.turn() 開始 (app-server 側のハング) も対象にする。

    notification が 1 件も届く前、turn() 呼び出し自体が長引くケースを想定する。
    """
    _FakeCodex.next_handle = _FakeHandle([_turn_completed(TurnStatus.completed)])
    _FakeCodex.next_turn_delay = 1.0
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work", config={"timeout": 0.05})

    with pytest.raises(RuntimeError, match="codex"):
        async for _ in session.stream("hi"):
            pass

    assert _FakeCodex.last_instance.closed is True


async def test_stream_reschedules_idle_timeout_on_each_notification(patch_async_codex):
    """出力が途切れない限り idle timeout がリセットされる (現行テストの意味論移植)。

    0.01 秒間隔で 20 個の delta (計 0.2 秒) を流す。timeout(0.05) より各間隔は
    短いので、reschedule が効いていればタイムアウトしないはず。
    """

    def _delayed(i: int):
        async def _inner():
            await asyncio.sleep(0.01)
            return _delta(str(i))

        return _inner

    notifications = [_delayed(i) for i in range(20)]
    _FakeCodex.next_handle = _FakeHandle([*notifications, _turn_completed(TurnStatus.completed)])
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work", config={"timeout": 0.05})

    result = None
    async for item in session.stream("hi"):
        if not isinstance(item, str):
            result = item

    assert result is not None
    assert result.text == "".join(str(i) for i in range(20))


async def test_stream_error_notification_with_retry_continues(patch_async_codex):
    """ErrorNotification(will_retry=True) は続行し、後続の delta/completed で正常完走する。"""
    _FakeCodex.next_handle = _FakeHandle(
        [_error_notification(will_retry=True), _delta("ok"), _turn_completed(TurnStatus.completed)]
    )
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")

    chunks = []
    result = None
    async for item in session.stream("hi"):
        if isinstance(item, str):
            chunks.append(item)
        else:
            result = item

    assert chunks == ["ok"]
    assert result is not None
    assert result.text == "ok"


async def test_stream_raises_when_notifications_end_without_turn_completed(patch_async_codex):
    """TurnCompletedNotification 無しで notification stream が尽きた場合は
    沈黙成功させず RuntimeError にする (SDK 自身の collector と同じ方針)。"""
    _FakeCodex.next_handle = _FakeHandle([_delta("partial")])
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")

    with pytest.raises(RuntimeError, match="完了通知"):
        async for _ in session.stream("hi"):
            pass

    assert _FakeCodex.last_instance.closed is True


async def test_stream_raises_last_error_when_will_retry_false_error_ends_stream(patch_async_codex):
    """ErrorNotification(will_retry=False) の後 TurnCompleted が届かず stream が
    切れた場合、その error メッセージを含む RuntimeError にする (握り潰さない)。"""
    _FakeCodex.next_handle = _FakeHandle([_error_notification(will_retry=False, message="quota exceeded")])
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")

    with pytest.raises(RuntimeError, match="codex provider turn failed"):
        async for _ in session.stream("hi"):
            pass

    assert _FakeCodex.last_instance.closed is True


async def test_first_stream_starts_thread_and_records_session_id(patch_async_codex):
    """session_id=None: thread_start が model/developer_instructions/sandbox/cwd 付きで呼ばれる。"""
    _FakeCodex.next_handle = _FakeHandle([_turn_completed(TurnStatus.completed)])
    session = CodexRuntimeSession(
        model="gpt-5.1-codex",
        cwd="/tmp/work",
        instructions="Be terse.",
        config={"sandbox": "read-only"},
    )

    async for _ in session.stream("hi"):
        pass

    fake = _FakeCodex.last_instance
    assert len(fake.start_calls) == 1
    call = fake.start_calls[0]
    assert call["model"] == "gpt-5.1-codex"
    assert call["cwd"] == "/tmp/work"
    assert call["developer_instructions"] == "Be terse."
    assert call["sandbox"] == Sandbox.read_only
    # 旧 codex exec の非対話意味論 (escalation を尋ねない = 拒否) を既定で維持する
    assert call["approval_mode"] == ApprovalMode.deny_all
    assert session.session_id == "thread_abc"


async def test_stream_with_session_id_resumes_thread(patch_async_codex):
    """session_id あり: thread_resume が呼ばれ、model kwarg を渡さない。"""
    _FakeCodex.next_handle = _FakeHandle([_turn_completed(TurnStatus.completed)])
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work", session_id="thread_abc")

    async for _ in session.stream("hi"):
        pass

    fake = _FakeCodex.last_instance
    assert len(fake.resume_calls) == 1
    thread_id, kwargs = fake.resume_calls[0]
    assert thread_id == "thread_abc"
    assert "model" not in kwargs
    assert kwargs["approval_mode"] == ApprovalMode.deny_all


async def test_stream_raises_on_interrupted_turn(patch_async_codex):
    """interrupted (failed 以外の非 completed) を沈黙成功させず、部分 text の切り詰めに気付かせる。"""
    _FakeCodex.next_handle = _FakeHandle(
        [_delta("partial"), _turn_completed(TurnStatus.interrupted)]
    )
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")

    with pytest.raises(RuntimeError, match="interrupted"):
        async for _ in session.stream("hi"):
            pass

    assert _FakeCodex.last_instance.closed is True


async def test_stream_approval_mode_auto_review_passthrough(patch_async_codex):
    """preset config approval_mode: auto_review を明示した場合のみ SDK 既定挙動を許す。"""
    _FakeCodex.next_handle = _FakeHandle([_turn_completed(TurnStatus.completed)])
    session = CodexRuntimeSession(
        model="gpt-5.1-codex", cwd="/tmp/work", config={"approval_mode": "auto_review"}
    )

    async for _ in session.stream("hi"):
        pass

    assert _FakeCodex.last_instance.start_calls[0]["approval_mode"] == ApprovalMode.auto_review


async def test_stream_reports_per_turn_usage_not_thread_total(patch_async_codex):
    """thread 累積 (total) ではなく当 turn 分の usage を RuntimeResult に載せる。

    resume 済み thread を模し、first snapshot の total に過去 turn 分 (1000) を
    含めても、報告される usage は当 turn の request 合計だけになることを検証する。
    """
    past = 1000
    first_total = _breakdown(input_tokens=past + 10, cached=0, output=0, reasoning=0)
    latest_total = _breakdown(input_tokens=past + 10 + 20, cached=0, output=0, reasoning=0)
    _FakeCodex.next_handle = _FakeHandle(
        [
            _usage_notification(input_tokens=10, cached=0, output=0, reasoning=0, total=first_total),
            _usage_notification(input_tokens=20, cached=0, output=0, reasoning=0, total=latest_total),
            _turn_completed(TurnStatus.completed),
        ]
    )
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")

    result = None
    async for item in session.stream("hi"):
        if not isinstance(item, str):
            result = item

    # (latest.total - first.total) + first.last = 20 + 10 = 30 (過去 turn の 1000 は含まない)
    assert result.usage["input_tokens"] == 30


async def test_second_stream_reuses_thread(patch_async_codex):
    """同一 session で 2 回 stream しても AsyncCodex 生成と thread_start は 1 回だけ。"""
    # thread.turn() は毎回同じ _FakeHandle を返すため、2 回の stream() 呼び出しは
    # それぞれ独立に notifications の先頭 (completed) から読み始める。
    _FakeCodex.next_handle = _FakeHandle([_turn_completed(TurnStatus.completed)])
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")

    async for _ in session.stream("hi"):
        pass
    async for _ in session.stream("hi again"):
        pass

    fake = _FakeCodex.last_instance
    assert len(fake.start_calls) == 1


# ---------------------------------------------------------------------------
# module 関数
# ---------------------------------------------------------------------------


def test_resolve_codex_bin_uses_pinned_runtime_unless_explicitly_overridden():
    """明示指定だけを優先し、省略時は SDK が固定した同梱 runtime へ委ねる。"""
    assert _resolve_codex_bin({"codex_bin": "~/.local/bin/codex"}).endswith("/.local/bin/codex")
    assert _resolve_codex_bin({}) is None


def test_map_sandbox_values():
    """CLI 語彙 (danger-full-access) と SDK enum のマッピング + dangerously_bypass 優先。"""
    assert _map_sandbox(None, {}) is None
    assert _map_sandbox("read-only", {}) == Sandbox.read_only
    assert _map_sandbox("workspace-write", {}) == Sandbox.workspace_write
    assert _map_sandbox("danger-full-access", {}) == Sandbox.full_access
    assert _map_sandbox("full-access", {}) == Sandbox.full_access
    with pytest.raises(ValueError):
        _map_sandbox("bogus", {})
    # dangerously_bypass_approvals_and_sandbox は sandbox 指定より優先して full_access
    assert _map_sandbox("read-only", {"dangerously_bypass_approvals_and_sandbox": True}) == Sandbox.full_access


def test_build_config_overrides_includes_mcp_and_profile_and_config():
    """dotted 文字列群: config dict / profile / mcp_servers がすべて含まれる。"""
    session = CodexRuntimeSession(
        model="gpt-5.1-codex",
        cwd="/tmp/work",
        config={"config": {"reasoning.effort": "high"}, "profile": "myprofile"},
        mcp_servers={"tool": {"command": "npx"}},
    )
    overrides = session._build_config_overrides()
    assert 'reasoning.effort=high' in overrides or any(o.startswith("reasoning.effort=") for o in overrides)
    assert 'profile="myprofile"' in overrides
    assert 'mcp_servers.tool.command="npx"' in overrides


def test_build_config_overrides_flattens_nested_config_tables():
    """config 内の dict は JSON object にせず Codex の dotted key へ展開する。"""
    session = CodexRuntimeSession(
        model="gpt-5.1-codex",
        cwd="/tmp/work",
        config={"config": {"sandbox_workspace_write": {"network_access": True}}},
    )

    assert session._build_config_overrides() == (
        "sandbox_workspace_write.network_access=true",
        "agents.enabled=false",
    )


# ---------------------------------------------------------------------------
# _dotted_config_entries / _model_provider_overrides (model_provider 一級キー化)
# ---------------------------------------------------------------------------


def test_dotted_config_entries_scalar():
    """dict でない value は 1 entry の TOML 値表現になる。"""
    assert _dotted_config_entries("model_providers.custom.wire_api", "responses") == [
        'model_providers.custom.wire_api="responses"'
    ]


def test_dotted_config_entries_recurses_into_nested_dict():
    """dict value は TOML inline table 非対応のため dotted key へ再帰展開する。"""
    entries = _dotted_config_entries("model_providers.custom.http_headers", {"X-Token": "t"})
    assert entries == ['model_providers.custom.http_headers.X-Token="t"']


def test_model_provider_overrides_none_is_empty():
    """未指定なら overrides に何も追加しない。"""
    assert _model_provider_overrides({}) == []


def test_model_provider_overrides_str_selects_existing_provider():
    """str: ユーザーの config.toml 側で定義済みの provider を id で選択するだけ。"""
    assert _model_provider_overrides({"model_provider": "ollama"}) == ['model_provider="ollama"']


def test_model_provider_overrides_dict_full_form():
    """dict フル形式: id/base_url/env_key/name の 4 値 + model_provider 選択の計 5 entries。"""
    entries = _model_provider_overrides(
        {
            "model_provider": {
                "id": "myproxy",
                "base_url": "http://127.0.0.1:59999/v1",
                "env_key": "MYPROXY_API_KEY",
                "name": "My Proxy",
            }
        }
    )
    assert len(entries) == 5
    assert 'model_providers.myproxy.name="My Proxy"' in entries
    assert 'model_providers.myproxy.base_url="http://127.0.0.1:59999/v1"' in entries
    assert 'model_providers.myproxy.wire_api="responses"' in entries
    assert 'model_providers.myproxy.env_key="MYPROXY_API_KEY"' in entries
    assert 'model_provider="myproxy"' in entries


def test_model_provider_overrides_dict_minimal_form_defaults():
    """dict 最小形式 (base_url のみ): id/name は既定 'custom'、wire_api は既定 'responses'。"""
    entries = _model_provider_overrides(
        {"model_provider": {"base_url": "http://127.0.0.1:59999/v1"}}
    )
    assert len(entries) == 4
    assert 'model_providers.custom.name="custom"' in entries
    assert 'model_providers.custom.base_url="http://127.0.0.1:59999/v1"' in entries
    assert 'model_providers.custom.wire_api="responses"' in entries
    assert 'model_provider="custom"' in entries


def test_model_provider_overrides_rejects_wire_api_chat():
    """stable 0.144.4 で廃止済みの wire_api="chat" は silent ignore せず ValueError。"""
    with pytest.raises(ValueError, match="chat"):
        _model_provider_overrides({"model_provider": {"base_url": "http://x", "wire_api": "chat"}})


def test_model_provider_overrides_requires_base_url():
    """base_url 欠落は ValueError (組み込み provider の選択だけなら文字列で id を指定)。"""
    with pytest.raises(ValueError, match="base_url"):
        _model_provider_overrides({"model_provider": {"id": "myproxy"}})


def test_model_provider_overrides_rejects_invalid_id_chars():
    """id は TOML キーとして安全な文字列のみ許可する。"""
    with pytest.raises(ValueError, match="id"):
        _model_provider_overrides({"model_provider": {"id": "my proxy", "base_url": "http://x"}})


def test_model_provider_overrides_passthrough_nested_extra_keys():
    """id/base_url/wire_api/name/env_key 以外の残余 key は dotted key で passthrough する。"""
    entries = _model_provider_overrides(
        {
            "model_provider": {
                "base_url": "http://x",
                "http_headers": {"X-Token": "t"},
            }
        }
    )
    assert 'model_providers.custom.http_headers.X-Token="t"' in entries


def test_build_config_overrides_includes_model_provider():
    """_build_config_overrides() 統合: config.model_provider の展開結果が含まれる。"""
    session = CodexRuntimeSession(
        model="gpt-5.1-codex",
        cwd="/tmp/work",
        config={"model_provider": {"id": "myproxy", "base_url": "http://127.0.0.1:59999/v1"}},
    )
    overrides = session._build_config_overrides()
    assert 'model_provider="myproxy"' in overrides
    assert any(o.startswith("model_providers.myproxy.base_url=") for o in overrides)


def test_ignored_cli_keys_do_not_reach_sdk(patch_async_codex):
    """skip_git_repo_check 等は CodexConfig / thread_start kwargs に現れない。"""
    session = CodexRuntimeSession(
        model="gpt-5.1-codex",
        cwd="/tmp/work",
        config={"skip_git_repo_check": True, "ignore_user_config": True, "ignore_rules": True},
    )
    overrides = session._build_config_overrides()
    assert not any("skip_git_repo_check" in o for o in overrides)
    assert not any("ignore_user_config" in o for o in overrides)
    assert not any("ignore_rules" in o for o in overrides)


async def test_stream_closes_client_on_generator_aclose(patch_async_codex):
    """途中 aclose: client を close する (孤児 app-server 防止)。"""

    async def never_completes():
        await asyncio.sleep(10)
        return _delta("never")

    _FakeCodex.next_handle = _FakeHandle([
        _item_completed(_agent_message("progress", "Working", "commentary")), never_completes,
    ])
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")

    agen = session.stream("hi")
    first = await agen.__anext__()
    assert isinstance(first, RuntimeEvent)
    assert first.payload["phase"] == "commentary"

    await agen.aclose()

    assert _FakeCodex.last_instance.closed is True


async def test_disconnect_closes_client(patch_async_codex):
    """disconnect(): client を close し _codex/_thread を None へ戻す。"""
    _FakeCodex.next_handle = _FakeHandle([_turn_completed(TurnStatus.completed)])
    session = CodexRuntimeSession(model="gpt-5.1-codex", cwd="/tmp/work")

    async for _ in session.stream("hi"):
        pass

    fake = _FakeCodex.last_instance
    await session.disconnect()

    assert fake.closed is True
    assert session._codex is None
    assert session._thread is None


def test_per_turn_usage_single_snapshot_is_last():
    """snapshot 1 回の turn: latest.total - first.total が 0 になり first.last が当 turn 分。"""
    last = TokenUsageBreakdown(
        cachedInputTokens=1, inputTokens=2, outputTokens=3, reasoningOutputTokens=4, totalTokens=10
    )
    # total は thread 累積 (resume 済み thread では last より大きい) を模す
    total = TokenUsageBreakdown(
        cachedInputTokens=11, inputTokens=22, outputTokens=33, reasoningOutputTokens=44, totalTokens=110
    )
    usage = ThreadTokenUsage(last=last, total=total)
    assert _per_turn_usage(usage, usage) == {
        "input_tokens": 2,
        "cached_input_tokens": 1,
        "output_tokens": 3,
        "reasoning_output_tokens": 4,
        "total_tokens": 10,
    }
    assert _per_turn_usage(None, None) is None


def test_per_turn_usage_multi_snapshot_subtracts_thread_baseline():
    """複数 request の turn: latest.total - first.total + first.last で当 turn 合計になる。

    resume 直後 (first.total に過去 turn の累積 100 が含まれる) でも、
    過去分が差し引かれることを検証する。
    """
    first_last = TokenUsageBreakdown(
        cachedInputTokens=0, inputTokens=10, outputTokens=5, reasoningOutputTokens=0, totalTokens=15
    )
    first_total = TokenUsageBreakdown(
        cachedInputTokens=0, inputTokens=110, outputTokens=55, reasoningOutputTokens=0, totalTokens=165
    )
    latest_total = TokenUsageBreakdown(
        cachedInputTokens=0, inputTokens=130, outputTokens=65, reasoningOutputTokens=0, totalTokens=195
    )
    first = ThreadTokenUsage(last=first_last, total=first_total)
    latest = ThreadTokenUsage(last=first_last, total=latest_total)
    assert _per_turn_usage(first, latest) == {
        "input_tokens": 30,   # (130-110) + 10
        "cached_input_tokens": 0,
        "output_tokens": 15,  # (65-55) + 5
        "reasoning_output_tokens": 0,
        "total_tokens": 45,   # (195-165) + 15
    }


def test_map_approval_mode_defaults_to_deny_all():
    """escalation 自動承認 (SDK 既定 auto_review) で sandbox 境界が破れないよう deny_all が既定。"""
    assert _map_approval_mode({}) == ApprovalMode.deny_all
    assert _map_approval_mode({"approval_mode": "deny_all"}) == ApprovalMode.deny_all
    assert _map_approval_mode({"approval_mode": "auto_review"}) == ApprovalMode.auto_review
    with pytest.raises(ValueError, match="approval_mode"):
        _map_approval_mode({"approval_mode": "yolo"})


# ---------------------------------------------------------------------------
# Phase-aware SDK 0.160 contract and safe observations
# ---------------------------------------------------------------------------


def _agent_message(item_id, text, phase=None):
    return {"type": "agentMessage", "id": item_id, "text": text, "phase": phase}


def _message_delta(item_id, text):
    return _notification(AgentMessageDeltaNotification(
        itemId=item_id, threadId="thread_abc", turnId="turn_1", delta=text,
    ))


@pytest.mark.parametrize("phase", [None, "final_answer"])
async def test_stream_excludes_commentary_from_final_body(patch_async_codex, phase):
    commentary = _agent_message("progress", "Checking files…", "commentary")
    answer = _agent_message("answer", '{"ok":true}', phase)
    _FakeCodex.next_handle = _FakeHandle([
        _item_started(commentary), _message_delta("progress", "Checking files…"),
        _item_completed(commentary), _message_delta("answer", '{"ok":'),
        _message_delta("answer", 'true}'), _item_completed(answer),
        _turn_completed(TurnStatus.completed),
    ])
    session = CodexRuntimeSession(model="test", cwd="/tmp/work")
    items = [item async for item in session.stream("hi")]
    assert "".join(item for item in items if isinstance(item, str)) == '{"ok":true}'
    assert items[-1].text == '{"ok":true}'
    progress = next(item for item in items if isinstance(item, RuntimeEvent) and item.payload.get("phase") == "commentary")
    assert progress.payload["item_id"] == "progress"
    assert progress.payload["text"] == "Checking files…"


@pytest.mark.parametrize("messages, expected", [
    ([("first", None), ("last", None)], "last"),
    ([("final", "final_answer"), ("later legacy", None)], "final"),
    ([("first final", "final_answer"), ("last final", "final_answer")], "last final"),
    ([("commentary only", "commentary")], ""),
])
async def test_stream_selects_same_final_as_sdk(patch_async_codex, messages, expected):
    from openai_codex._run import _final_assistant_response_from_items

    notifications = [_item_completed(_agent_message(str(i), text, phase)) for i, (text, phase) in enumerate(messages)]
    assert (_final_assistant_response_from_items([n.payload.item for n in notifications]) or "") == expected
    _FakeCodex.next_handle = _FakeHandle([*notifications, _turn_completed(TurnStatus.completed)])
    result = await CodexRuntimeSession(model="test", cwd="/tmp/work").ask("hi")
    assert result.text == expected


async def test_completed_message_replaces_incomplete_deltas(patch_async_codex):
    _FakeCodex.next_handle = _FakeHandle([
        _message_delta("answer", "partial"),
        _item_completed(_agent_message("answer", "authoritative", "final_answer")),
        _turn_completed(TurnStatus.completed),
    ])
    items = [item async for item in CodexRuntimeSession(model="test", cwd="/tmp/work").stream("hi")]
    assert [item for item in items if isinstance(item, str)] == ["authoritative"]
    assert items[-1].text == "authoritative"


async def test_failed_turn_never_releases_partial_body(patch_async_codex):
    _FakeCodex.next_handle = _FakeHandle([
        _message_delta("answer", "partial"),
        _turn_completed(TurnStatus.interrupted, TurnError(message="blocked", codexErrorInfo="tooManyDenials")),
    ])
    items = []
    with pytest.raises(RuntimeError, match="tooManyDenials"):
        async for item in CodexRuntimeSession(model="test", cwd="/tmp/work").stream("hi"):
            items.append(item)
    assert not any(isinstance(item, str) for item in items)
    assert items[-1].payload == {"error": "codex provider turn failed (interrupted): tooManyDenials", "retryable": False, "code": "tooManyDenials", "status": "interrupted"}


async def test_foreign_errors_completion_and_thread_deltas_are_ignored(patch_async_codex):
    _FakeCodex.next_handle = _FakeHandle([
        _delta("foreign", thread_id="thread_other"),
        _error_notification(False, "foreign error", turn_id="turn_other"),
        _turn_completed(TurnStatus.failed, TurnError(message="foreign"), turn_id="turn_other"),
        _turn_completed(TurnStatus.completed, thread_id="thread_other"),
        _delta("mine"), _turn_completed(TurnStatus.completed),
    ])
    result = await CodexRuntimeSession(model="test", cwd="/tmp/work").ask("hi")
    assert result.text == "mine"


async def test_output_schema_is_forwarded_and_validated_before_chunks(patch_async_codex):
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
    _FakeCodex.next_handle = _FakeHandle([
        _item_completed(_agent_message("answer", '{"ok":true}', "final_answer")),
        _turn_completed(TurnStatus.completed),
    ])
    session = CodexRuntimeSession(model="test", cwd="/tmp/work", output_schema=schema)
    result = await session.ask("hi")
    assert result.text == '{"ok":true}'
    assert _FakeCodex.last_instance.thread.turn_calls[0][1]["output_schema"] == schema


@pytest.mark.parametrize("text", ['not JSON', '{"ok":"not boolean"}', '{"secret":"sk-test-secret"}', '{"ok":NaN}'])
async def test_invalid_structured_output_never_reaches_response(patch_async_codex, text):
    _FakeCodex.next_handle = _FakeHandle([
        _message_delta("answer", text),
        _item_completed(_agent_message("answer", text, "final_answer")),
        _turn_completed(TurnStatus.completed),
    ])
    session = CodexRuntimeSession(model="test", cwd="/tmp/work", output_schema={
        "type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
    })
    items = []
    with pytest.raises(RuntimeError, match="failed the configured JSON schema"):
        async for item in session.stream("hi"):
            items.append(item)
    assert not any(isinstance(item, str) for item in items)
    assert _FakeCodex.last_instance.closed is True
    assert "sk-test-secret" not in str(items[-1].payload)


async def test_invalid_schema_is_rejected_before_sdk_launch(patch_async_codex):
    session = CodexRuntimeSession(model="test", cwd="/tmp/work", output_schema={"type": "invalid"})
    with pytest.raises(ValueError, match="output_schema"):
        await session.ask("hi")
    assert _FakeCodex.last_instance is None


@pytest.mark.parametrize("code,retryable", [("flexUnavailable", True), ("tooManyDenials", False), ("badRequest", False), ("unauthorized", False)])
async def test_structured_codex_error_classification(patch_async_codex, code, retryable):
    from kotoyose.runtime.codex import CodexTurnError

    _FakeCodex.next_handle = _FakeHandle([
        _turn_completed(TurnStatus.failed, TurnError(message="failure Authorization: Bearer sensitive-token", codexErrorInfo=code, additionalDetails="secret details")),
    ])
    events = []
    with pytest.raises(CodexTurnError) as error:
        async for item in CodexRuntimeSession(model="test", cwd="/tmp/work").stream("hi"):
            events.append(item)
    assert error.value.code == code
    assert error.value.retryable is retryable
    assert events[-1].payload["code"] == code
    assert "sensitive-token" not in str(events)
    assert "secret details" not in str(events)


def test_structured_http_error_keeps_status_without_details():
    from kotoyose.runtime.codex import CodexTurnError

    error = CodexTurnError.from_turn_error(TurnError.model_validate({
        "message": "connection failed", "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 401}},
    }))
    assert error.http_status_code == 401
    assert error.code == "httpConnectionFailed"
    assert error.retryable is False


async def test_context_window_is_reported_without_affecting_turn_tokens(patch_async_codex):
    notification = _usage_notification(input_tokens=10)
    notification.payload.token_usage.model_context_window = 100000
    _FakeCodex.next_handle = _FakeHandle([notification, _turn_completed(TurnStatus.completed)])
    result = await CodexRuntimeSession(model="test", cwd="/tmp/work").ask("hi")
    assert result.usage["model_context_window"] == 100000
    assert result.usage["input_tokens"] == 10


async def test_file_collab_web_compaction_lifecycle_is_bounded(patch_async_codex):
    file = {"type": "fileChange", "id": "file-1", "status": "inProgress", "changes": [
        {"path": "config.json", "kind": {"type": "update", "move_path": None}, "diff": "secret file contents"},
    ]}
    collab = {"type": "collabAgentToolCall", "id": "agent-1", "tool": "spawnAgent", "status": "inProgress",
              "senderThreadId": "thread_abc", "receiverThreadIds": ["child"], "agentsStates": {"child": {"status": "running", "message": "secret result"}}, "prompt": "secret task"}
    web = {"type": "webSearch", "id": "search-1", "query": "documentation", "results": [{"secret": "ignored"}]}
    compact = {"type": "contextCompaction", "id": "compact-1"}
    _FakeCodex.next_handle = _FakeHandle([
        _item_started(file), _item_completed({**file, "status": "completed"}),
        _item_started(collab), _item_completed({**collab, "status": "completed"}),
        _item_started(web), _item_completed(web), _item_started(compact), _item_completed(compact),
        _turn_completed(TurnStatus.completed),
    ])
    events = [item async for item in CodexRuntimeSession(model="test", cwd="/tmp/work").stream("hi") if isinstance(item, RuntimeEvent)]
    assert len(events) == 8
    assert [event.tool_name for event in events[:6]] == ["FileChange", "FileChange", "collab__spawnAgent", "collab__spawnAgent", "WebSearch", "WebSearch"]
    assert events[0].payload["tool_use_id"] == events[1].payload["tool_use_id"] == "file-1"
    assert events[-1].kind == "context_compaction"
    assert "secret" not in str(events)


def test_tool_observations_redact_structured_credentials():
    from kotoyose.runtime.codex import _compact_json

    result = _compact_json({"api_key": "can include spaces", "headers": {"Authorization": "Bearer token"}, "query": "safe"})
    assert "can include spaces" not in result
    assert "Bearer token" not in result
    assert "safe" in result


async def test_terminal_completion_preserves_earlier_error_classification_and_usage(patch_async_codex):
    from kotoyose.runtime.codex import CodexTurnError

    _FakeCodex.next_handle = _FakeHandle([
        _delta("partial"), _usage_notification(input_tokens=7, output=3),
        _notification(ErrorNotification(threadId="thread_abc", turnId="turn_1", willRetry=False,
                                       error=TurnError(message="capacity", codexErrorInfo="flexUnavailable"))),
        _turn_completed(TurnStatus.failed),
    ])
    with pytest.raises(CodexTurnError) as error:
        await CodexRuntimeSession(model="test", cwd="/tmp/work").ask("hi")
    assert error.value.code == "flexUnavailable"
    assert error.value.diagnostics["subtype"] == "flexUnavailable"
    assert error.value.status == "failed"
    assert error.value.partial_result.text == "partial"
    assert error.value.partial_result.usage["input_tokens"] == 7
    assert error.value.partial_result.runtime_session_id == "thread_abc"


async def test_unknown_notification_never_logs_payload(patch_async_codex, monkeypatch):
    from openai_codex.models import UnknownNotification

    logs = []
    monkeypatch.setattr("kotoyose.runtime.codex.log.debug", lambda *args, **kwargs: logs.append((args, kwargs)))
    _FakeCodex.next_handle = _FakeHandle([
        Notification(method="item/newFeature", payload=UnknownNotification(params={"secret": "private-token"})),
        _delta("ok"), _turn_completed(TurnStatus.completed),
    ])
    result = await CodexRuntimeSession(model="test", cwd="/tmp/work").ask("hi")
    assert result.text == "ok"
    assert logs == [(("codex.notification.unknown",), {"method": "item/newFeature"})]
    assert "private-token" not in str(logs)


def test_codex_sdk_signatures_accept_discovery_and_schema_options():
    import inspect
    from openai_codex import AsyncCodex, AsyncThread

    receiver = object()
    inspect.signature(AsyncCodex.models).bind(receiver, include_hidden=False)
    inspect.signature(AsyncThread.turn).bind(receiver, "prompt", effort="high", output_schema={"type": "object"})


def test_builder_forwards_validated_output_schema(tmp_path, monkeypatch):
    from kotoyose.config import HarnessConfig, ModelPreset

    monkeypatch.setattr("kotoyose.config._config", HarnessConfig())
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    preset = ModelPreset(provider="codex", cwd=str(tmp_path), output_schema=schema)
    assert build_codex_session(preset, None, None).output_schema == schema


@pytest.mark.parametrize("status", [TurnStatus.failed, TurnStatus.interrupted])
async def test_public_codex_error_never_contains_raw_provider_message(patch_async_codex, status):
    from kotoyose.runtime.codex import CodexTurnError

    private = "Provider says prompt SECRET-PROMPT customer private detail https://private.example/a?token=secret"
    error = TurnError(message=private, codexErrorInfo="tooManyDenials", additionalDetails=private)
    _FakeCodex.next_handle = _FakeHandle([_turn_completed(status, error=error)])
    events = []
    with pytest.raises(CodexTurnError) as raised:
        async for item in CodexRuntimeSession(model="test", cwd="/tmp/work").stream("hi"):
            events.append(item)
    public = str(raised.value) + str(raised.value.diagnostics) + str(events)
    for fragment in ("SECRET-PROMPT", "customer private", "private.example", "token=", "Provider says"):
        assert fragment not in public
    assert raised.value.code == "tooManyDenials"
    assert raised.value.status == status.value


@pytest.mark.parametrize("will_retry", [False, True])
async def test_retry_and_terminal_error_notifications_never_expose_provider_text(patch_async_codex, will_retry):
    from kotoyose.runtime.codex import CodexTurnError

    private = "unrecognized customer content private_url=https://private.example/path?password=hunter2"
    _FakeCodex.next_handle = _FakeHandle([
        _notification(ErrorNotification(threadId="thread_abc", turnId="turn_1", willRetry=will_retry,
                                       error=TurnError(message=private, codexErrorInfo="flexUnavailable"))),
        _turn_completed(TurnStatus.failed, error=TurnError(message=private)),
    ])
    events = []
    with pytest.raises(CodexTurnError) as raised:
        async for item in CodexRuntimeSession(model="test", cwd="/tmp/work").stream("hi"):
            events.append(item)
    public = str(raised.value) + str(raised.value.diagnostics) + str(events)
    assert "unrecognized customer content" not in public
    assert "private.example" not in public
    assert "hunter2" not in public
    assert events[0].payload["code"] == "flexUnavailable"


def test_codex_public_error_classification_rejects_untrusted_values():
    from kotoyose.runtime.codex import CodexTurnError

    error = CodexTurnError(code="SECRET-CODE", status="SECRET-STATUS", http_status_code=999999, will_retry="SECRET")
    assert str(error) == "codex provider turn failed"
    assert error.code is None and error.status is None
    assert error.http_status_code is None and error.will_retry is None
    assert "SECRET" not in str(error.as_event()) + str(error.diagnostics)


async def test_untyped_sdk_launch_exception_is_public_safe(patch_async_codex, monkeypatch):
    from kotoyose.runtime.codex import CodexTurnError

    async def fail_start(self, **kwargs):
        raise RuntimeError("SDK echoed SECRET-PROMPT https://private.example/a?token=secret")

    monkeypatch.setattr(_FakeCodex, "thread_start", fail_start)
    events = []
    with pytest.raises(CodexTurnError) as raised:
        async for item in CodexRuntimeSession(model="test", cwd="/tmp/work").stream("hi"):
            events.append(item)
    assert str(raised.value) == "codex provider turn failed (failed)"
    assert "SECRET-PROMPT" not in str(events)
    assert "private.example" not in str(events)
    assert raised.value.__suppress_context__ is True
    assert _FakeCodex.last_instance.closed
