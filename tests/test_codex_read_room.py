"""Codex の read 部屋。道具は workspace-read だけ、SDK sandbox は read-only、write との入れ替えは拒む。"""

from pathlib import Path

import pytest

from kotoyose.agent_spec import (
    DRIVERS, agent_workspace_tool_error, requests_room, resolve_saved_sandbox, runtime_for,
)
from kotoyose.codex_coding_policy import (
    EXTERNAL_TOOL_POLICY_DIGEST, HOST_MODE, HOST_TOOL_REF,
)

READ_MODE = "room_read_v1"
READ_TOOL_REF = "builtin:workspace-read@1"
READ_SANDBOX_POLICY = "workspace_read_sandbox_v1"
from kotoyose.codex_provider_scope import CodexProviderExecutor, PROVIDER_WORKER_ID
from kotoyose.execution_resources import ExecutionError
from kotoyose.execution_service import ExecutionService


def _codex():
    return next(row for row in DRIVERS if row.driver == "codex.app-server")


def _read_cell():
    return next(cell for cell in _codex().surfaces if cell.surface == "read")


def test_codex_read_is_admitted_only_for_workspace_read():
    """host-coding や sandbox-exec を混ぜた read は、write と同じ文言で断る。"""
    cell = _read_cell()
    claude = next(row for row in DRIVERS if row.driver == "claude.agent-sdk")
    claude_read = next(item for item in claude.surfaces if item.surface == "read")
    assert cell.admitted is True
    assert claude_read.admitted and claude_read.workspace_readonly
    assert cell.tool_refs == frozenset({READ_TOOL_REF})
    assert cell.tool_match == "exact"
    assert cell.execution_mode == READ_MODE
    assert runtime_for(cell.surface) == "room"
    assert cell.sdk_sandbox_fixed == "read-only"
    assert cell.workspace_readonly is True
    assert cell.sandbox_policy == READ_SANDBOX_POLICY
    assert cell.isolated_tool_policy_digest != EXTERNAL_TOOL_POLICY_DIGEST
    common = dict(backend="native", provider="codex", execution_mode=READ_MODE, isolation="sandbox",
                  has_mcp=False, has_approval=False, agent_container=True)
    assert agent_workspace_tool_error(**common, tool_refs=[READ_TOOL_REF]) is None
    for refs in ([HOST_TOOL_REF], ["builtin:sandbox-exec@1"], [READ_TOOL_REF, HOST_TOOL_REF], []):
        assert agent_workspace_tool_error(**common, tool_refs=refs) == (
            "Codex mode requires its explicitly admitted workspace tool"
        )
    assert requests_room(
        is_agent=True, backend="native", provider="codex", execution_mode=READ_MODE, tool_refs=[READ_TOOL_REF],
    )
    assert not requests_room(
        is_agent=True, backend="native", provider="claude", execution_mode=None, tool_refs=[READ_TOOL_REF],
    )


def test_read_room_sdk_sandbox_rejects_workspace_write():
    """read の cell は保存値が無くても read-only。workspace-write は mode 違反。"""
    assert resolve_saved_sandbox(READ_MODE, None) == "read-only"
    assert resolve_saved_sandbox(READ_MODE, "read-only") == "read-only"
    with pytest.raises(ValueError, match="invalid_codex_execution_mode"):
        resolve_saved_sandbox(READ_MODE, "workspace-write")
    assert resolve_saved_sandbox(HOST_MODE, None) == "workspace-write"


def _read_snapshot(**change):
    snapshot = {
        "kind": "agent", "backend": "native", "provider": "codex", "codex_execution_mode": READ_MODE,
        "codex_tool_policy_digest": _read_cell().isolated_tool_policy_digest,
        "codex_sandbox_policy": READ_SANDBOX_POLICY,
        "execution_environment": "isolated", "codex_sandbox": "read-only", "worker_id": PROVIDER_WORKER_ID,
        "worker_capabilities": CodexProviderExecutor.public_snapshot(), "tool_refs": [READ_TOOL_REF],
        "image_id": None,
    }
    snapshot.update(change)
    return snapshot


def test_resume_refuses_read_write_swap():
    """read の pin を write の mode や道具へ差し替えると、再開の executor 検査が拒む。"""
    service = ExecutionService.__new__(ExecutionService)
    assert service._worker_for(_read_snapshot()) is None
    for change in (
        {"codex_execution_mode": HOST_MODE},
        {"tool_refs": [HOST_TOOL_REF]},
        {"codex_tool_policy_digest": EXTERNAL_TOOL_POLICY_DIGEST, "codex_sandbox_policy": "external_sandbox_restricted_v1"},
        {"codex_sandbox": "workspace-write"},
    ):
        with pytest.raises(ExecutionError, match="Pinned host coding"):
            service._worker_for(_read_snapshot(**change))


def test_models_publish_isolated_read_surface(tmp_path):
    """codex.app-server の isolated/read alias は /v1/models に surface read を出す。"""
    from kotoyose.agent_route import published_agent
    from kotoyose.config import AgentRoute, AliasAgent, Connection, HarnessConfig, ModelPreset
    from kotoyose.execution_resources import CallerPolicy, DriverInstall

    config = HarnessConfig(
        connections={"codex-chatgpt": Connection(provider="codex", kind="subscription", data_policy="no_training", auth_file=Path("/tmp/auth.json"))},
        routes={"terra": AgentRoute(driver="codex.app-server", model="gpt-6.1-terra", connection="codex-chatgpt")},
        models={"taskhub-review-read": ModelPreset(
            route="terra", effort="high",
            agent=AliasAgent(place="isolated", surface="read"),
        )},
    )
    settings = type("Settings", (), {
        "drivers": {"codex.app-server": DriverInstall(cli_binary=Path("/tmp/codex"))},
        "agent_compression_mode": "runtime_managed",
    })()
    caller = CallerPolicy(
        workspace_roots=[tmp_path], agent_models=["taskhub-review-read"],
        places=["isolated"], max_surface="read", max_seconds=60, max_memory_bytes=128 * 1024 * 1024,
        max_pids=32, max_cpus=1, max_concurrent=1,
    )
    published = published_agent("taskhub-review-read", caller, settings, config=config)
    assert published == {
        "place": "isolated", "surface": "read", "provider": "codex",
        "driver": "codex.app-server", "enforced": True,
    }
    hidden = caller.model_copy(update={"max_surface": "none"})
    assert published_agent("taskhub-review-read", hidden, settings, config=config) is None
