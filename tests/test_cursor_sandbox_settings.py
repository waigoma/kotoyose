"""Cursor SDK local sandbox / setting source parity tests."""

from types import SimpleNamespace

import pytest

from kotoyose.runtime.cursor import (
    CursorRuntimeSession,
    _cursor_sandbox_options,
    _cursor_setting_sources,
    build_cursor_session,
)


def test_cursor_native_setting_sources_are_accepted():
    assert _cursor_setting_sources(
        ["project", "user", "team", "mdm", "plugins", "all"],
        strict=True,
    ) == ["project", "user", "team", "mdm", "plugins", "all"]


def test_common_claude_local_setting_source_is_still_dropped():
    """共通 schema の Claude 専用 local layer は後方互換で warning + drop する。"""
    assert _cursor_setting_sources(["user", "local", "project"]) == [
        "user",
        "project",
    ]


def test_cursor_specific_unknown_setting_source_fails_fast():
    with pytest.raises(ValueError, match="cursor.setting_sources"):
        _cursor_setting_sources(["project", "typo"], strict=True)


def test_cursor_sandbox_options_validate_shape():
    assert _cursor_sandbox_options({"enabled": True}) == {"enabled": True}
    with pytest.raises(ValueError, match="object"):
        _cursor_sandbox_options(True)
    with pytest.raises(ValueError, match="boolean"):
        _cursor_sandbox_options({"enabled": "yes"})


def test_builder_exposes_cursor_native_settings_and_sandbox(tmp_path, monkeypatch):
    from kotoyose.config import HarnessConfig, ModelPreset

    monkeypatch.setattr("kotoyose.config._config", HarnessConfig())
    session = build_cursor_session(
        ModelPreset(
            provider="cursor",
            cwd=str(tmp_path),
            setting_sources=["project"],
            cursor={
                "setting_sources": ["project", "plugins", "team"],
                "sandbox_options": {"enabled": True},
            },
        ),
        None,
        None,
    )

    # provider-specific Cursor settings override the narrower common field.
    assert session.setting_sources == ["project", "plugins", "team"]
    assert session.sandbox_options == {"enabled": True}


def test_cursor_setting_source_override_cannot_break_isolation(tmp_path, monkeypatch):
    from kotoyose.config import HarnessConfig, ModelPreset

    monkeypatch.setattr("kotoyose.config._config", HarnessConfig())
    preset = ModelPreset(
        provider="cursor",
        cwd=str(tmp_path),
        require_isolation=True,
        setting_sources=[],
        cursor={"setting_sources": ["project"]},
    )

    with pytest.raises(ValueError, match="require_isolation=True"):
        build_cursor_session(preset, None, None)


@pytest.mark.asyncio
async def test_sdk_local_options_receive_sandbox_and_native_sources(monkeypatch):
    from kotoyose.config import HarnessConfig

    class FakeClient:
        async def aclose(self):
            return None

    captured = {}
    fake_agent = SimpleNamespace(agent_id="agent-test")

    async def launch_bridge(**kwargs):
        captured["bridge"] = kwargs
        return FakeClient()

    async def create(options, *, client, api_key=None):
        captured["options"] = options
        captured["api_key"] = api_key
        return fake_agent

    from kotoyose.config import Connection
    monkeypatch.setenv("FIXTURE_CURSOR_KEY", "settings-key")
    monkeypatch.setattr(
        "kotoyose.runtime.cursor.get_config",
        lambda: HarnessConfig(connections={"cursor": Connection(provider="cursor", kind="api_key", data_policy="no_training", secret="env:FIXTURE_CURSOR_KEY")}),
    )
    monkeypatch.setattr(
        "kotoyose.runtime.cursor.AsyncClient.launch_bridge",
        launch_bridge,
    )
    monkeypatch.setattr("kotoyose.runtime.cursor.AsyncAgent.create", create)

    session = CursorRuntimeSession(
        None,
        "/tmp/work",
        setting_sources=["project", "plugins"],
        sandbox_options={"enabled": True},
    )
    await session._ensure_agent()

    assert captured["options"].local.setting_sources == ["project", "plugins"]
    assert captured["options"].local.sandbox_options == {"enabled": True}
    assert captured["api_key"] == "settings-key"
