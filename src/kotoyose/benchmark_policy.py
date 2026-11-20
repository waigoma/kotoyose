"""Benchmark dispatch 前に外部呼び出しの fixture 隔離を判定する。"""

from __future__ import annotations

from typing import Any


def external_fixture_ready(case: Any, request: Any) -> bool:
    """外部 capability がなければ許可し、ある場合は固定 fixture の入力経路まで確認する。"""
    execution = request.execution
    requires_fixture = bool(
        {"client_tools", "external_mcp"} & set(execution.requirements.capabilities)
        or (execution.kind == "agent" and bool(execution.tool_refs or execution.mcp_refs))
    )
    if not requires_fixture:
        return True
    fixture_refs = (
        execution.fixture_refs if execution.kind == "command"
        else [value.content_ref for value in execution.inputs]
    )
    return bool(
        case.side_effects.external_calls != "none"
        and case.side_effects.response_fixture_ref
        and case.pins.external_response_fixture_digest
        and case.side_effects.response_fixture_ref in fixture_refs
    )
