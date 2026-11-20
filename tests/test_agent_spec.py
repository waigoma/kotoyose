"""driver 表が今日の受付と同じ問いへ答えること。"""

from types import SimpleNamespace

import pytest

from kotoyose.agent_spec import (
    DRIVERS, accepted_runs, agent_workspace_tool_error, coding_profile_allowed, egress_limits_allowed,
    execution_environment_error, may_be_isolated, published_agent_execution, requests_room,
    resolve_saved_sandbox, rootfs_digest_is_coding, runtime_for,
)
from kotoyose.codex_coding_policy import CODEX_SANDBOXES, HOST_MODE, HOST_TOOL_REF, DEFAULT_CODEX_MODE


def test_table_declares_todays_drivers_and_does_not_admit_unread_surfaces():
    assert [row.driver for row in DRIVERS] == [
        "claude.agent-sdk", "codex.app-server", "cursor.sdk", "antigravity.agy", "antigravity.sdk",
        "cli.print",
    ]
    admitted = {
        row.driver: tuple(cell.surface for cell in row.surfaces if cell.admitted)
        for row in DRIVERS
    }
    assert admitted["claude.agent-sdk"] == ("none", "write")
    assert admitted["codex.app-server"] == ("read", "exec", "write")
    assert admitted["cursor.sdk"] == ()
    assert admitted["antigravity.agy"] == ()
    assert admitted["antigravity.sdk"] == ()
    assert admitted["cli.print"] == ("write",)
    cli_print = next(row for row in DRIVERS if row.driver == "cli.print")
    cli_print_write = next(cell for cell in cli_print.surfaces if cell.surface == "write")
    assert cli_print_write.requires == frozenset({"agent_container", "egress"})
    assert cli_print_write.admission_requires == frozenset({"agent_container"})
    assert runtime_for("none") == runtime_for("exec") == "jail"
    assert runtime_for("read") == runtime_for("write") == "room"
    codex = next(row for row in DRIVERS if row.driver == "codex.app-server")
    assert codex.sdk_sandbox is not None and codex.sdk_sandbox.values == CODEX_SANDBOXES
    assert codex.host is None
    write = next(cell for cell in codex.surfaces if cell.surface == "write")
    assert write.tool_refs == frozenset({HOST_TOOL_REF})
    assert "egress" in write.requires and "rootfs" in write.requires and "agent_container" in write.requires
    assert "agent_container" not in write.admission_requires
    assert write.external_sandbox is True
    jail = next(cell for cell in codex.surfaces if cell.surface == "exec")
    assert jail.external_sandbox is False


@pytest.mark.parametrize("provider,isolated", [
    ("claude", True), ("codex", True), ("cursor", False), ("antigravity", False),
    ("print_cli", True),
])
def test_isolation_follows_places(provider, isolated):
    assert may_be_isolated(provider) is isolated


def test_explicit_environment_keeps_the_current_rejection_text():
    assert execution_environment_error("claude", "isolated") == (
        "execution_environment='isolated' is not supported for claude"
    )
    assert execution_environment_error("cursor", "isolated") == (
        "execution_environment='isolated' is not supported for cursor"
    )
    assert execution_environment_error("codex", "isolated") is None
    assert execution_environment_error("codex", "host") is None
    assert execution_environment_error("cursor", "host") is None
    assert execution_environment_error("codex", "room") == "execution_environment requires isolated or host"


def test_accepted_runs_match_the_golden_vocabulary():
    claude = SimpleNamespace(backend="native", provider="claude")
    assert accepted_runs(claude, agent_container=True) == [
        {"accepted_tools": "none", "place": "jail"},
        {"accepted_tools": "exactly host-coding", "place": "room"},
    ]
    assert accepted_runs(claude, agent_container=False) == [{"accepted_tools": "none", "place": "jail"}]
    assert accepted_runs(
        SimpleNamespace(backend="native", provider="codex", codex_execution_mode=HOST_MODE),
        agent_container=False,
    ) == [{"accepted_tools": "exactly host-coding", "place": "room"}]
    assert accepted_runs(
        SimpleNamespace(backend="native", provider="codex", codex_execution_mode=DEFAULT_CODEX_MODE),
        agent_container=True,
    ) == [{"accepted_tools": "sandbox-exec subset", "place": "jail"}]
    assert accepted_runs(SimpleNamespace(backend="print_cli"), agent_container=False) == [
        {"accepted_tools": "none or host-coding", "place": "host"},
    ]
    assert accepted_runs(SimpleNamespace(backend="print_cli", _agent_surface="none"), agent_container=False) == [
        {"accepted_tools": "none", "place": "host"},
    ]
    assert accepted_runs(
        SimpleNamespace(backend="print_cli", execution_environment="isolated"), agent_container=False,
    ) == []
    assert accepted_runs(
        SimpleNamespace(backend="print_cli", execution_environment="isolated"), agent_container=True,
    ) == [{"accepted_tools": "none or host-coding", "place": "room"}]
    assert published_agent_execution("native") == "sandbox"
    assert published_agent_execution("print_cli") == "host"
    assert published_agent_execution("print_cli", "isolated") == "sandbox"


def test_tool_rejection_text_is_unchanged():
    claude = dict(backend="native", provider="claude", execution_mode=None, isolation="sandbox",
                  has_mcp=False, has_approval=False)
    assert agent_workspace_tool_error(
        **claude, tool_refs=[HOST_TOOL_REF], agent_container=False,
    ) == "Claude coding requires the admitted container workspace tool"
    assert agent_workspace_tool_error(**claude, tool_refs=[], agent_container=False) is None
    assert agent_workspace_tool_error(
        backend="native", provider="codex", execution_mode=HOST_MODE, isolation="sandbox",
        tool_refs=["builtin:sandbox-exec@1"], has_mcp=False, has_approval=False, agent_container=True,
    ) == "Codex mode requires its explicitly admitted workspace tool"
    assert requests_room(
        is_agent=True, backend="native", provider="claude", execution_mode=None, tool_refs=[HOST_TOOL_REF],
    )
    assert not requests_room(
        is_agent=True, backend="native", provider="codex", execution_mode=DEFAULT_CODEX_MODE, tool_refs=[HOST_TOOL_REF],
    )


def test_coding_rootfs_and_egress_follow_the_write_cell():
    assert rootfs_digest_is_coding("codex", HOST_MODE)
    assert not rootfs_digest_is_coding("codex", DEFAULT_CODEX_MODE)
    assert egress_limits_allowed("codex", HOST_MODE)
    assert not egress_limits_allowed("codex", DEFAULT_CODEX_MODE)
    assert not egress_limits_allowed("claude", HOST_MODE)
    assert coding_profile_allowed("codex", HOST_MODE, "chatgpt_subscription")
    assert not coding_profile_allowed("codex", HOST_MODE, "environment")
    assert not coding_profile_allowed("claude", HOST_MODE, "runtime_managed")
    assert coding_profile_allowed("claude", DEFAULT_CODEX_MODE, "runtime_managed")
    assert resolve_saved_sandbox(DEFAULT_CODEX_MODE, None) == "read-only"
    assert resolve_saved_sandbox(HOST_MODE, None) == "workspace-write"
    with pytest.raises(ValueError, match="requires read-only"):
        resolve_saved_sandbox(DEFAULT_CODEX_MODE, "workspace-write")


def test_undeclared_native_driver_or_mode_is_refused() -> None:
    """表に無い provider や mode は、道具の判定を素通りせず断る (plugin の行の書き忘れで広げない)。"""
    from kotoyose.agent_spec import UNDECLARED_DRIVER_REJECTION, agent_workspace_tool_error

    common = dict(isolation="sandbox", tool_refs=[], has_mcp=False, has_approval=False, agent_container=True)
    assert (
        agent_workspace_tool_error(backend="native", provider="unknown-provider", execution_mode=None, **common)
        == UNDECLARED_DRIVER_REJECTION
    )
    assert agent_workspace_tool_error(
        backend="native", provider="codex", execution_mode="undeclared_mode_v9", **common
    ) is not None


def test_every_codex_execution_mode_has_an_admitted_cell() -> None:
    """codex_execution_mode の Literal に値を足したら、表にも cell を足すこと。"""
    import typing

    from kotoyose.agent_spec import DRIVERS
    from kotoyose.execution_resources import NativeRuntimeProfile

    modes = typing.get_args(NativeRuntimeProfile.model_fields["codex_execution_mode"].annotation)
    codex = next(m for m in DRIVERS if m.provider == "codex")
    admitted = {c.execution_mode for c in codex.surfaces if c.admitted and c.execution_mode}
    assert set(modes) <= admitted
