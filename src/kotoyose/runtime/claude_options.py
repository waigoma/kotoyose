"""Kotoyose の解決済み設定を Claude Agent SDK option へ変換する adapter。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from claude_agent_sdk import ClaudeAgentOptions, SessionStore
from claude_agent_sdk.types import (
    AgentDefinition,
    SystemPromptPreset,
    ThinkingConfig,
    ToolsPreset,
)

if TYPE_CHECKING:
    from ..config import AgentPreset, HarnessConfig, ModelPreset


def _system_prompt(prompt_texts: list[str], *, snapshot: bool | None = None) -> SystemPromptPreset:
    """解決済み prompt 本文を Claude Code preset へ追記する。"""
    prompt: SystemPromptPreset = {"type": "preset", "preset": "claude_code"}
    if snapshot is not None:
        prompt["snapshot"] = snapshot
    if prompt_texts:
        prompt["append"] = "\n".join(prompt_texts)
    return prompt


def _thinking(preset: ModelPreset) -> ThinkingConfig | None:
    """Kotoyose の thinking 設定を SDK の discriminated union へ変換する。"""
    if preset.thinking is None:
        return None
    if preset.thinking == "adaptive":
        return {"type": "adaptive"}
    if preset.thinking == "disabled":
        return {"type": "disabled"}
    if preset.thinking == "enabled":
        # ModelPreset validator が budget の存在を保証している。
        return {"type": "enabled", "budget_tokens": preset.thinking_budget_tokens}  # type: ignore[typeddict-item]
    return None


def _agents(
    agents: dict[str, AgentPreset],
) -> dict[str, AgentDefinition] | None:
    """Kotoyose の agent 定義を SDK 型へ変換する。"""
    if not agents:
        return None
    return {
        name: AgentDefinition(
            description=agent.description,
            prompt=agent.prompt,
            tools=agent.tools,
            model=agent.model,
        )
        for name, agent in agents.items()
    }


def build_claude_options(
    config: HarnessConfig,
    preset: ModelPreset,
    *,
    model_name: str,
    cwd_override: str | None,
    request_headers: dict[str, str] | None,
    env: dict[str, str],
    session_store: SessionStore | None,
    resolved_secrets: Mapping[str, str] | None = None,
) -> ClaudeAgentOptions:
    """解決済み Kotoyose 設定から ClaudeAgentOptions を構築する。

    Args:
        config: Resource 参照を解決する HarnessConfig。
        preset: Claude provider と確認済みの model preset。
        model_name: Error message に用いる公開 model 名。
        cwd_override: Request body 由来の作業 directory。
        request_headers: MCP header template の解決値。
        env: 優先順位を解決済みの Claude process 環境変数。
        session_store: SDK session store。永続化無効時は None。

    Returns:
        Claude Agent SDK client に渡せる option object。

    Raises:
        ValueError: 許可されていない cwd override または必須 MCP header 不足時。
    """
    # Facade 以外から呼ばれても権限境界を迂回できないよう二重に検証する。
    if cwd_override is not None and not preset.allow_cwd_override:
        raise ValueError(
            f"preset '{model_name}' は allow_cwd_override=False のため"
            " cwd の上書きを許可していません"
        )

    effective_cwd = (
        os.path.expanduser(cwd_override)
        if cwd_override is not None
        else preset.cwd
    )
    effective_sources = (
        preset.setting_sources
        if preset.setting_sources is not None
        else ["project"]
    )
    extra: dict[str, str | None] = {"bare": None} if preset.bare else {}

    plugins, skills, mcp_servers = config.resolve_resources(preset)
    # Local import は config 初期化時の循環を避けつつ、互換 facade の正本を共有する。
    from ..config import _inject_mcp_request_headers, resolve_mcp_secrets, resolve_prompt_texts
    from ..host_place import HostPlace

    resolved_mcp_servers = _inject_mcp_request_headers(
        resolve_mcp_secrets(mcp_servers, resolved_secrets=resolved_secrets),
        request_headers or {},
    )
    effective_tools: list[str] | ToolsPreset = (
        ToolsPreset(type="preset", preset="claude_code")
        if preset.tools is None
        else preset.tools
    )
    options = ClaudeAgentOptions(
        tools=effective_tools,
        setting_sources=effective_sources,
        model=preset.model,
        cwd=effective_cwd,
        permission_mode=preset.permission_mode,
        disallowed_tools=preset.deny,
        system_prompt=_system_prompt(
            resolve_prompt_texts(config.resolve_prompt_files(preset)),
            snapshot=preset.system_prompt_snapshot,
        ),
        extra_args=extra,
        env=env,
        mcp_servers=resolved_mcp_servers,
        strict_mcp_config=preset.strict_mcp_config,
        plugins=[{"type": "local", "path": path} for path in plugins],
        skills=skills,
        agents=_agents(config.resolve_agents(preset)),
        thinking=_thinking(preset),
        effort=preset.effort,
        session_store=session_store,
        output_format=(
            {"type": "json_schema", "schema": preset.output_schema}
            if preset.output_schema is not None else None
        ),
    )
    return HostPlace.constrain_claude_options(
        options,
        allow_host_tools=preset.tools != [],
    )
