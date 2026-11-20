"""Cursor SDK runtime adapter の単体テスト。"""
import asyncio
from types import SimpleNamespace

import pytest

from kotoyose.runtime.base import RuntimeResult
from kotoyose.runtime.cursor import (
    CursorRuntimeSession,
    _cursor_setting_sources,
    _normalize_usage,
    build_cursor_session,
)


class FakeClient:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


class FakeRun:
    def __init__(self, events, usage=None):
        self._events = events
        self.usage = usage
        self.id = "run-test"
        self.status = "running"
        self.cancel_count = 0

    async def events(self):
        for event in self._events:
            if event.result is not None:
                self.status = event.result.get("status", self.status)
            yield event
        self.status = "finished"

    async def wait(self):
        return SimpleNamespace(id=self.id, status=self.status, result="", usage=self.usage)

    async def cancel(self):
        self.cancel_count += 1
        self.status = "cancelled"


class FakeAgent:
    def __init__(self, agent_id="agent-new", run=None):
        self.agent_id = agent_id
        self.run = run
        self.prompts = []
        self.closed = False

    async def send(self, prompt):
        self.prompts.append(prompt)
        return self.run

    async def close(self):
        self.closed = True


def assistant(text):
    return SimpleNamespace(
        sdk_message=SimpleNamespace(
            type="assistant",
            message=SimpleNamespace(content=[SimpleNamespace(text=text)]),
        ),
        result=None,
    )


@pytest.fixture
def sdk(monkeypatch):
    client = FakeClient()
    calls = {}

    async def launch_bridge(**kwargs):
        calls["bridge"] = kwargs
        return client

    async def create(options, *, client, api_key=None):
        calls["create"] = options
        calls["create_api_key"] = api_key
        return calls["agent"]

    async def resume(agent_id, options, *, client):
        calls["resume"] = (agent_id, options)
        return calls["agent"]
    monkeypatch.setenv("CURSOR_API_KEY", "test")
    monkeypatch.setattr("kotoyose.runtime.cursor.AsyncClient.launch_bridge", launch_bridge)
    monkeypatch.setattr("kotoyose.runtime.cursor.AsyncAgent.create", create)
    monkeypatch.setattr("kotoyose.runtime.cursor.AsyncAgent.resume", resume)
    return client, calls


async def test_new_session_prepends_preamble_streams_text_and_returns_agent_id(sdk):
    _, calls = sdk
    calls["agent"] = FakeAgent(run=FakeRun([assistant("hello"), assistant(" world")]))
    session = CursorRuntimeSession("model", "/tmp/work", system_preamble="be helpful")
    values = [item async for item in session.stream("hi")]
    assert values[:-1] == ["hello", " world"]
    assert values[-1] == RuntimeResult("hello world", None, "agent-new", "cursor")
    assert calls["agent"].prompts == ["be helpful\n\nhi"]
    assert calls["bridge"] == {"workspace": "/tmp/work"}
    assert calls["create"].local.cwd == "/tmp/work"


async def test_resume_omits_preamble(sdk):
    _, calls = sdk
    calls["agent"] = FakeAgent("agent-old", FakeRun([]))
    session = CursorRuntimeSession(None, "/tmp/work", session_id="agent-old", system_preamble="ignored")
    [item async for item in session.stream("continue")]
    assert calls["resume"][0] == "agent-old"
    assert calls["agent"].prompts == ["continue"]


async def test_settings_cursor_key_overrides_environment_key(sdk, monkeypatch):
    """Cursor SDK へ cursor connection 優先の API key を明示注入する。"""
    from kotoyose.config import Connection, HarnessConfig

    _, calls = sdk
    calls["agent"] = FakeAgent(run=FakeRun([]))
    monkeypatch.setenv("CURSOR_API_KEY", "env-key")
    monkeypatch.setenv("FIXTURE_CURSOR_KEY", "settings-key")
    cfg = HarnessConfig(connections={"cursor": Connection(provider="cursor", kind="api_key", data_policy="no_training", secret="env:FIXTURE_CURSOR_KEY")})
    monkeypatch.setattr("kotoyose.runtime.cursor.get_config", lambda: cfg)
    await CursorRuntimeSession(None, "/tmp")._ensure_agent()
    assert calls["create_api_key"] == "settings-key"


async def test_settings_cursor_key_is_used_without_environment_key(sdk, monkeypatch):
    """CURSOR_API_KEY がなくても cursor connection の key だけで Cursor SDK を起動できる。"""
    from kotoyose.config import Connection, HarnessConfig

    _, calls = sdk
    calls["agent"] = FakeAgent(run=FakeRun([]))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setenv("FIXTURE_CURSOR_KEY", "settings-key")
    cfg = HarnessConfig(connections={"cursor": Connection(provider="cursor", kind="api_key", data_policy="no_training", secret="env:FIXTURE_CURSOR_KEY")})
    monkeypatch.setattr("kotoyose.runtime.cursor.get_config", lambda: cfg)
    await CursorRuntimeSession(None, "/tmp")._ensure_agent()
    assert calls["create_api_key"] == "settings-key"


async def test_cursor_key_missing_fails_fast_with_settings_guidance(sdk, monkeypatch):
    """connection と環境変数の両方がない場合は設定先を示して失敗する。"""
    from kotoyose.config import HarnessConfig

    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setattr("kotoyose.runtime.cursor.get_config", lambda: HarnessConfig())
    with pytest.raises(RuntimeError, match="Connections 画面 .*CURSOR_API_KEY"):
        await CursorRuntimeSession(None, "/tmp")._ensure_agent()


async def test_resume_passes_settings_cursor_key_in_agent_options(sdk, monkeypatch):
    """SDK の resume 契約に合わせ、API key は AgentOptions で渡す。"""
    from kotoyose.config import Connection, HarnessConfig

    _, calls = sdk
    calls["agent"] = FakeAgent(run=FakeRun([]))
    monkeypatch.setenv("FIXTURE_CURSOR_KEY", "settings-key")
    cfg = HarnessConfig(connections={"cursor": Connection(provider="cursor", kind="api_key", data_policy="no_training", secret="env:FIXTURE_CURSOR_KEY")})
    monkeypatch.setattr("kotoyose.runtime.cursor.get_config", lambda: cfg)
    await CursorRuntimeSession(None, "/tmp", session_id="existing-agent")._ensure_agent()
    assert calls["resume"][1].api_key == "settings-key"


def test_normalizes_sdk_usage():
    usage = SimpleNamespace(input_tokens=2, output_tokens=3, cache_read_tokens=5, cache_write_tokens=7, total_tokens=10)
    assert _normalize_usage(usage) == {"input_tokens": 2, "output_tokens": 3, "cache_read_tokens": 5, "cache_write_tokens": 7, "total_tokens": 10, "reasoning_tokens": None}


async def test_error_event_becomes_runtime_error(sdk):
    _, calls = sdk
    calls["agent"] = FakeAgent(
        run=FakeRun([SimpleNamespace(sdk_message=None, result={"status": "error", "message": "boom"})])
    )
    with pytest.raises(RuntimeError, match="status error"):
        async for _ in CursorRuntimeSession(None, "/tmp").stream("x"):
            pass


async def test_stream_normalizes_usage_into_runtime_result(sdk):
    _, calls = sdk
    usage = SimpleNamespace(input_tokens=2, output_tokens=3, cache_read_tokens=5, cache_write_tokens=7, total_tokens=10)
    calls["agent"] = FakeAgent(run=FakeRun([], usage=usage))
    values = [item async for item in CursorRuntimeSession(None, "/tmp").stream("x")]
    assert values[-1].usage == _normalize_usage(usage)


async def test_idle_timeout_becomes_runtime_error(sdk):
    _, calls = sdk
    class SlowRun(FakeRun):
        def __init__(self):
            super().__init__([])
        async def events(self):
            await asyncio.sleep(.05)
            yield assistant("late")
    calls["agent"] = FakeAgent(run=SlowRun())
    with pytest.raises(RuntimeError, match="idle timeout"):
        async for _ in CursorRuntimeSession(None, "/tmp", config={"timeout": .01}).stream("x"):
            pass


def test_builder_resolves_preamble_mcp_and_cwd(tmp_path, monkeypatch):
    from kotoyose.config import HarnessConfig, McpDefinition, ModelPreset, PromptFileDefinition
    prompt = tmp_path / "role.md"
    prompt.write_text("role")
    cfg = HarnessConfig(
        prompt_files={"p": PromptFileDefinition(path=str(prompt))},
        mcps={"m": McpDefinition(server={"command": "tool"})},
    )
    monkeypatch.setattr("kotoyose.config._config", cfg)
    session = build_cursor_session(
        ModelPreset(provider="cursor", cwd="/ignored", prompt_file_refs=["p"], mcp_refs=["m"]),
        str(tmp_path),
        None,
    )
    assert session.cwd == str(tmp_path)
    assert session.system_preamble == "role"
    assert session.mcp_servers["m"].command == "tool"


def test_builder_maps_cursor_sdk_1_native_options(tmp_path, monkeypatch):
    """Cursor SDK 1.x が追加した tool policy、agent、setting source を配線する。"""
    from kotoyose.config import AgentPreset, HarnessConfig, ModelPreset

    monkeypatch.setattr("kotoyose.config._config", HarnessConfig())
    session = build_cursor_session(
        ModelPreset(
            provider="cursor",
            cwd=str(tmp_path),
            tools=["Read", "Grep"],
            deny=["Bash"],
            setting_sources=["user", "project"],
            permission_mode="plan",
            agents={
                "reviewer": AgentPreset(
                    description="review",
                    prompt="review carefully",
                )
            },
        ),
        None,
        None,
    )

    assert session.tools == ["Read", "Grep"]
    assert session.disallowed_tools == ["Bash"]
    assert session.setting_sources == ["user", "project"]
    assert session.mode == "plan"
    assert session.agents["reviewer"].description == "review"
    assert session.agents["reviewer"].model == "inherit"


async def test_sdk_1_options_are_passed_to_agent_create(sdk):
    """session の native option が AgentOptions / LocalAgentOptions まで届く。"""
    _, calls = sdk
    calls["agent"] = FakeAgent(run=FakeRun([]))
    session = CursorRuntimeSession(
        None,
        "/tmp/work",
        tools=["Read"],
        disallowed_tools=["Bash"],
        agents={"reviewer": {"description": "review", "prompt": "review"}},
        setting_sources=["project"],
        mode="plan",
    )

    await session._ensure_agent()

    options = calls["create"]
    assert options.tools == ["Read"]
    assert options.disallowed_tools == ["Bash"]
    assert "reviewer" in options.agents
    assert options.local.setting_sources == ["project"]
    assert options.mode == "plan"


def test_cursor_setting_sources_drops_claude_local_scope(caplog):
    """Cursor に存在しない Claude local layer だけを除外する。"""
    assert _cursor_setting_sources(["user", "local", "project"]) == ["user", "project"]
