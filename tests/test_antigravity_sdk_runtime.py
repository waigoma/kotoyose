"""Optional Antigravity SDK backend translation tests without installing the SDK."""

from __future__ import annotations

from enum import Enum
from types import SimpleNamespace

import pytest

from kotoyose.runtime.antigravity_sdk import (
    _build_sdk_config_kwargs,
    _normalize_usage,
)
from kotoyose.runtime.translation import RuntimeSkill, TranslatedRuntimeConfig


class _BuiltinTools(str, Enum):
    VIEW_FILE = "view_file"
    FIND_FILE = "find_file"
    SEARCH_DIR = "search_directory"
    RUN_COMMAND = "run_command"
    CREATE_FILE = "create_file"
    EDIT_FILE = "edit_file"
    SEARCH_WEB = "search_web"
    READ_URL_CONTENT = "read_url_content"
    FINISH = "finish"
    INVOKE_SUBAGENT = "invoke_subagent"
    GENERATE_IMAGE = "generate_image"
    SCHEDULE = "schedule"
    FUTURE_TOOL = "future_tool"

    @classmethod
    def read_only(cls):
        return [cls.VIEW_FILE, cls.READ_URL_CONTENT, cls.SCHEDULE, cls.FUTURE_TOOL]


class _Record:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _Types:
    BuiltinTools = _BuiltinTools
    McpStdioServer = _Record
    McpStreamableHttpServer = _Record
    SubagentCapabilities = _Record
    SubagentConfig = _Record
    RunCommandConfig = _Record


class _Capabilities(_Record):
    pass


class _Policy:
    @staticmethod
    def allow_all():
        return _Record(tool="*", decision="allow")

    @staticmethod
    def deny(tool, **kwargs):
        return _Record(tool=tool, decision="deny", **kwargs)

    @staticmethod
    def confirm_run_command():
        return [_Policy.deny("run_command"), _Policy.allow_all()]


def _translated(tmp_path):
    skill = tmp_path / "skill" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("# skill", encoding="utf-8")
    return TranslatedRuntimeConfig(
        instructions="system",
        plugin_paths=(),
        mcp_servers={
            "local": {
                "type": "stdio",
                "command": "python3",
                "args": ["server.py"],
                "env": {"TOKEN": "x"},
                "enabled_tools": ["search"],
                "timeout_seconds": 12,
            },
            "remote": {
                "type": "http",
                "url": "https://example.test/mcp",
                "headers": {"Authorization": "Bearer token"},
                "disabled_tools": ["write"],
            },
        },
        skills=(RuntimeSkill(name="review", path=str(skill)),),
        effort="high",
        timeout_seconds=600,
        unsupported_fields=(),
    )


def test_sdk_config_translates_resources_and_autonomous_policy(tmp_path):
    sdk = {
        "types": _Types,
        "CapabilitiesConfig": _Capabilities,
        "policy": _Policy,
    }
    kwargs = _build_sdk_config_kwargs(
        sdk=sdk,
        model="gemini-3.8-flash",
        cwd=str(tmp_path),
        translated=_translated(tmp_path),
        config={"allow_all": True},
    )

    assert kwargs["model"] == "gemini-3.8-flash"
    assert kwargs["workspaces"] == [str(tmp_path)]
    assert kwargs["system_instructions"] == "system"
    assert kwargs["skills_paths"] == [str(tmp_path / "skill")]
    assert kwargs["policies"][-1].kwargs == {"tool": "*", "decision": "allow"}
    assert isinstance(kwargs["capabilities"], _Capabilities)

    local, remote = kwargs["mcp_servers"]
    assert local.kwargs["name"] == "local"
    assert local.kwargs["enabled_tools"] == ["search"]
    assert local.kwargs["timeout_seconds"] == 12
    assert remote.kwargs["url"] == "https://example.test/mcp"
    assert remote.kwargs["disabled_tools"] == ["write"]
    assert kwargs["subagents"] == []


def test_sdk_api_key_env_is_resolved_without_storing_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_GEMINI_KEY", "secret-value")
    kwargs = _build_sdk_config_kwargs(
        sdk={"types": _Types, "CapabilitiesConfig": _Capabilities, "policy": _Policy},
        model=None,
        cwd=str(tmp_path),
        translated=TranslatedRuntimeConfig(
            instructions=None,
            plugin_paths=(),
            mcp_servers={},
            skills=(),
            effort=None,
            timeout_seconds=None,
            unsupported_fields=(),
        ),
        config={"api_key_env": "TEST_GEMINI_KEY"},
    )
    assert kwargs["api_key"] == "secret-value"
    assert kwargs["capabilities"].kwargs["enabled_tools"] == [_BuiltinTools.VIEW_FILE, _BuiltinTools.READ_URL_CONTENT, _BuiltinTools.FINISH]


def test_sdk_rejects_legacy_sse_mcp_transport(tmp_path):
    translated = _translated(tmp_path)
    translated.mcp_servers["remote"] = {
        "type": "sse",
        "url": "https://example.test/sse",
    }

    with pytest.raises(ValueError, match="legacy SSE"):
        _build_sdk_config_kwargs(
            sdk={
                "types": _Types,
                "CapabilitiesConfig": _Capabilities,
                "policy": _Policy,
            },
            model=None,
            cwd=str(tmp_path),
            translated=translated,
            config={},
        )


def test_sdk_vertex_config_does_not_require_api_key(tmp_path):
    kwargs = _build_sdk_config_kwargs(
        sdk={"types": _Types, "CapabilitiesConfig": _Capabilities, "policy": _Policy},
        model=None,
        cwd=str(tmp_path),
        translated=TranslatedRuntimeConfig(
            instructions=None,
            plugin_paths=(),
            mcp_servers={},
            skills=(),
            effort=None,
            timeout_seconds=None,
            unsupported_fields=(),
        ),
        config={"vertex": True, "project": "p", "location": "us-central1"},
    )
    assert kwargs["vertex"] is True
    assert kwargs["project"] == "p"
    assert "api_key" not in kwargs


def test_sdk_usage_is_normalized_for_kotoyose_accounting():
    usage = SimpleNamespace(
        prompt_token_count=100,
        cached_content_token_count=40,
        candidates_token_count=20,
        thoughts_token_count=5,
        total_token_count=125,
    )
    assert _normalize_usage(usage) == {
        "input_tokens": 100,
        "output_tokens": 20,
        "thinking_tokens": 5,
        "cache_read_tokens": 40,
        "total_tokens": 125,
    }


@pytest.mark.parametrize("config", [{}, {"allow_all": True}, {"autonomous_tools": True}])
def test_explicit_tool_snapshots_cannot_gain_schedule_or_future_defaults(tmp_path, config):
    from kotoyose.runtime.antigravity_sdk import _AUTONOMOUS_TOOLS, _READ_ONLY_TOOLS
    translated = _translated(tmp_path)
    kwargs = _build_sdk_config_kwargs(
        sdk={"types": _Types, "CapabilitiesConfig": _Capabilities, "policy": _Policy},
        model=None, cwd=str(tmp_path), translated=translated, config=config,
    )
    caps = kwargs["capabilities"].kwargs
    expected = _AUTONOMOUS_TOOLS if config else _READ_ONLY_TOOLS
    assert [item.value for item in caps["enabled_tools"]] == list(expected)
    assert not {"schedule", "manage_task", "future_tool"} & set(caps["enabled_tools"])
    assert caps["run_command_config"].kwargs == {"enable_daemons": False}
    assert kwargs["subagents"] == []
    denied = {policy.kwargs["tool"] for policy in kwargs["policies"] if policy.kwargs["decision"] == "deny"}
    assert {"schedule", "manage_task", "run_command", "invoke_subagent"} <= denied


def test_generic_options_cannot_override_lifespan_or_toolsets(tmp_path):
    kwargs = _build_sdk_config_kwargs(
        sdk={"types": _Types, "CapabilitiesConfig": _Capabilities, "policy": _Policy},
        model=None, cwd=str(tmp_path), translated=_translated(tmp_path),
        config={"options": {"capabilities": {"enabled_tools": ["schedule"]}, "policies": [], "subagents": []}},
    )
    assert isinstance(kwargs["capabilities"], _Capabilities)
    assert kwargs["policies"]
    assert kwargs["subagents"] == []


def test_vertex_express_accepts_explicit_key_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_VERTEX_KEY", "vertex-test-key")
    kwargs = _build_sdk_config_kwargs(
        sdk={"types": _Types, "CapabilitiesConfig": _Capabilities, "policy": _Policy},
        model=None, cwd=str(tmp_path), translated=_translated(tmp_path),
        config={"vertex": True, "api_key_env": "TEST_VERTEX_KEY"},
    )
    assert kwargs["vertex"] is True
    assert kwargs["api_key"] == "vertex-test-key"


@pytest.mark.parametrize("arguments,expected", [
    ({"IsDaemon": True}, True), ({"IsDaemon": False}, False), ({"IsDaemon": "true"}, True),
    ({"background": True}, True), ({"command": "echo test"}, False),
])
def test_daemon_policy_predicate(arguments, expected):
    from kotoyose.runtime.antigravity_sdk import _requests_daemon
    assert _requests_daemon(arguments) is expected


async def test_sdk_partial_iteration_failure_is_sanitized_and_cancelled(tmp_path):
    from kotoyose.runtime.antigravity_sdk import AntigravitySdkRuntimeSession
    from kotoyose.runtime.base import RuntimeTurnError
    class Response:
        usage_metadata = {"prompt_token_count": 7, "candidates_token_count": 2}
        cancelled = False
        async def __aiter__(self):
            yield "partial"
            raise RuntimeError("SECRET provider error")
        @property
        def tool_calls(self):
            async def tools():
                if False:
                    yield
            return tools()
        async def cancel(self):
            self.cancelled = True
    response = Response()
    class Agent:
        async def chat(self, prompt):
            return response
    session = AntigravitySdkRuntimeSession(model=None, cwd=str(tmp_path), translated=_translated(tmp_path))
    session._agent = Agent()
    with pytest.raises(RuntimeTurnError) as error:
        await session.ask("hi")
    assert error.value.partial_result.text == "partial"
    assert error.value.partial_result.usage == {"input_tokens": 7, "output_tokens": 2}
    assert "SECRET" not in str(error.value)
    assert response.cancelled
    assert session.invalidated
    assert session._agent is None


def _sdk_response_session(tmp_path, *, token_delay=0, trailing_delay=0, timeout=0.01):
    import asyncio
    from kotoyose.runtime.antigravity_sdk import AntigravitySdkRuntimeSession
    class Response:
        usage_metadata = {"prompt_token_count": 3}
        cancelled = False
        async def __aiter__(self):
            await asyncio.sleep(token_delay)
            yield "answer"
            await asyncio.sleep(trailing_delay)
        @property
        def tool_calls(self):
            async def tools():
                if False:
                    yield
            return tools()
        async def cancel(self):
            self.cancelled = True
    response = Response()
    class Agent:
        async def chat(self, prompt):
            return response
    session = AntigravitySdkRuntimeSession(
        model=None, cwd=str(tmp_path), translated=_translated(tmp_path),
        config={"timeout_seconds": timeout},
    )
    session._agent = Agent()
    return session, response


async def test_sdk_slow_consumer_is_not_cancelled_by_turn_timeout(tmp_path):
    import asyncio
    from kotoyose.runtime.base import RuntimeResult
    session, response = _sdk_response_session(tmp_path)
    stream = session.stream("hi")
    try:
        assert await anext(stream) == "answer"
        await asyncio.sleep(0.03)
        assert isinstance(await anext(stream), RuntimeResult)
        await asyncio.sleep(0.03)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    finally:
        await stream.aclose()
    assert not response.cancelled
    assert not session.invalidated


async def test_sdk_actual_provider_wait_still_times_out_and_cancels(tmp_path):
    session, response = _sdk_response_session(tmp_path, trailing_delay=1)
    stream = session.stream("hi")
    assert await anext(stream) == "answer"
    with pytest.raises(RuntimeError, match="秒以内"):
        await anext(stream)
    assert response.cancelled
    assert session.invalidated
    assert session._agent is None


async def test_sdk_timeout_budget_is_cumulative_across_provider_waits(tmp_path):
    # Each wait fits separately; their total must still exceed the turn budget.
    session, response = _sdk_response_session(tmp_path, token_delay=0.04, trailing_delay=0.04, timeout=0.06)
    stream = session.stream("hi")
    assert await anext(stream) == "answer"
    with pytest.raises(RuntimeError, match="秒以内"):
        await anext(stream)
    assert response.cancelled
    assert session.invalidated


async def test_sdk_closing_partial_stream_cancels_and_closing_completed_stream_does_not(tmp_path):
    from kotoyose.runtime.base import RuntimeResult
    partial_dir = tmp_path / "partial"
    partial_dir.mkdir()
    session, response = _sdk_response_session(partial_dir)
    stream = session.stream("hi")
    assert await anext(stream) == "answer"
    await stream.aclose()
    assert response.cancelled
    assert session.invalidated
    assert session._agent is None

    complete_dir = tmp_path / "complete"
    complete_dir.mkdir()
    session, response = _sdk_response_session(complete_dir)
    stream = session.stream("hi")
    assert await anext(stream) == "answer"
    assert isinstance(await anext(stream), RuntimeResult)
    await stream.aclose()
    assert not response.cancelled
    assert not session.invalidated
