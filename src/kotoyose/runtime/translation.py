"""provider 共通の ModelPreset を runtime 向け中間表現へ解決する。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..child_sdk_skills import MAX_TURN_FILE_BYTES
from ..config import (
    HarnessConfig,
    ModelPreset,
    _inject_mcp_request_headers,
    resolve_mcp_secrets,
    resolve_prompt_texts,
    resolve_skill_fallback_texts,
)
from ..logging_setup import log
from .base import SDKConnectionError

# Skill を runtime へ native 注入できる provider。ここに無い provider では
# skill_refs の本文を system prompt へ前置する (skill fallback)。
# Antigravity は workspace plugin overlay の .agents/plugins/*/skills へ変換する。
_SKILL_CAPABLE_PROVIDERS = frozenset({"claude", "codex", "antigravity"})
# preset.effort を実行設定へ渡す runtime。Antigravity SDK は警告するだけで転送しない。
EFFORT_PROVIDERS = frozenset({"claude", "codex", "cursor"})
# リクエストの service_tier=priority (Fast) を実行設定へ渡す runtime。
# Claude と ACP / print CLI は受け取らない。
FAST_PROVIDERS = frozenset({"codex", "cursor"})


def resolve_cli_prompt_texts(config: HarnessConfig, preset: ModelPreset) -> list[str]:
    try:
        return resolve_prompt_texts(
            config.resolve_prompt_files(preset), strict_limits=(MAX_TURN_FILE_BYTES, MAX_TURN_FILE_BYTES),
        )
    except ValueError:
        raise ValueError(f"prompt files are unavailable or invalid (limit {MAX_TURN_FILE_BYTES} bytes)") from None


@dataclass(frozen=True, slots=True)
class RuntimeSkill:
    """runtime へ明示注入できる 1 Skill。path は SKILL.md の絶対パス。"""

    name: str
    path: str


@dataclass(frozen=True, slots=True)
class TranslatedRuntimeConfig:
    """provider 固有 SDK / CLI へ渡す前の、解決済み共通設定。"""

    instructions: str | None
    plugin_paths: tuple[str, ...]
    mcp_servers: dict[str, dict]
    skills: tuple[RuntimeSkill, ...]
    effort: str | None
    timeout_seconds: float | None
    unsupported_fields: tuple[str, ...]


def translate_runtime_config(
    config: HarnessConfig,
    preset: ModelPreset,
    request_headers: dict[str, str] | None = None,
    *,
    resolved_secrets: Mapping[str, str] | None = None,
    effort_override: str | None = None,
    skill_resources: tuple[list[str], list[str] | str | None] | None = None,
) -> TranslatedRuntimeConfig:
    """参照・inline・request header を provider 非依存の規則で 1 回だけ解決する。"""
    plugin_paths, skill_filter, mcp_servers = config.resolve_resources(preset)
    if skill_resources is not None:
        plugin_paths, skill_filter = skill_resources
    texts = resolve_prompt_texts(config.resolve_prompt_files(preset))
    skills = tuple(resolve_plugin_skills(plugin_paths, skill_filter))

    if preset.provider not in _SKILL_CAPABLE_PROVIDERS and preset.skill_refs:
        fallbacks = resolve_skill_fallback_texts(
            config.skill_definitions, preset.skill_refs
        )
        if fallbacks:
            texts.extend(text for _, text in fallbacks)
            log.info(
                "runtime.skill_fallback_injected",
                provider=preset.provider,
                skills=[name for name, _ in fallbacks],
                chars=sum(len(text) for _, text in fallbacks),
            )
        handled = {name for name, _ in fallbacks}
        unhandled = [name for name in preset.skill_refs if name not in handled]
        if unhandled:
            log.warning(
                "runtime.skill_fallback_unresolved",
                provider=preset.provider,
                skills=unhandled,
            )

        def _plugin_of(name: str) -> str | None:
            definition = config.skill_definitions.get(name)
            return definition.plugin if definition else None

        skills = tuple(skill for skill in skills if skill.name not in handled)
        plugin_paths = [
            plugin
            for name in preset.skill_refs
            if name not in handled and (plugin := _plugin_of(name))
        ] + list(preset.plugins)

    return TranslatedRuntimeConfig(
        instructions="\n".join(texts) or None,
        plugin_paths=tuple(plugin_paths),
        mcp_servers=_inject_mcp_request_headers(
            resolve_mcp_secrets(mcp_servers, resolved_secrets=resolved_secrets),
            request_headers or {},
            builtin_delegation=not config._child_mcp_projection and preset.has_delegation_mcp,
        ),
        skills=skills,
        effort=effort_override if effort_override is not None else preset.effort,
        timeout_seconds=preset.timeout_seconds,
        unsupported_fields=_unsupported_fields(preset),
    )


def apply_common_timeout(
    provider_config: dict[str, Any], timeout_seconds: float | None
) -> dict[str, Any]:
    """provider 固有 timeout を優先し、未指定時だけ共通 timeout を補う。"""
    resolved = dict(provider_config)
    if timeout_seconds is not None and "timeout" not in resolved:
        resolved["timeout"] = timeout_seconds
    return resolved


def resolve_plugin_skills(
    plugin_paths: list[str], skill_filter: list[str] | str | None, *, keep_source_path: bool = False,
) -> list[RuntimeSkill]:
    """Claude plugin bundle 内の Skill を Codex/Antigravity でも使える明示参照へ変換する。"""
    discovered: dict[str, Path] = {}
    for plugin_path in plugin_paths:
        skills_dir = Path(plugin_path) / "skills"
        if not skills_dir.is_dir():
            continue
        for skill_file in sorted(skills_dir.glob("*/SKILL.md")):
            discovered.setdefault(skill_file.parent.name, skill_file.absolute() if keep_source_path else skill_file.resolve())

    if skill_filter == []:
        return []
    selected = list(discovered) if skill_filter in (None, "all") else list(skill_filter)
    missing = [name for name in selected if name not in discovered]
    if missing:
        raise ValueError(
            "選択された Skill の SKILL.md が plugin bundle にありません: "
            + ", ".join(missing)
        )
    return [RuntimeSkill(name=name, path=str(discovered[name])) for name in selected]


def _unsupported_fields(preset: ModelPreset) -> tuple[str, ...]:
    """選択 provider で等価に強制できない明示設定を列挙する。"""
    if preset.provider == "claude":
        return ()

    unsupported: list[str] = []
    if preset.tools is not None and preset.provider != "cursor":
        unsupported.append("tools")
    if (
        preset.deny
        and preset.provider != "cursor"
        and (
            preset.provider != "codex"
            or any(
                not item.startswith("mcp__")
                or "__" not in item.removeprefix("mcp__")
                for item in preset.deny
            )
        )
    ):
        # Antigravity CLI の fine-grained permission は現時点で global/project
        # settings に保存され、per-process flag として安全に注入できない。
        # そのため Kotoyose deny を「強制済み」と偽らず明示的に unsupported とする。
        unsupported.append("deny")
    if preset.thinking is not None or preset.thinking_budget_tokens is not None:
        unsupported.append("thinking")
    if preset.effort is not None and preset.provider not in ("codex", "cursor", "antigravity"):
        unsupported.append("effort")
    if (
        "permission_mode" in preset.model_fields_set
        and not (preset.provider == "cursor" and preset.permission_mode == "plan")
    ):
        unsupported.append("permission_mode")
    if "bare" in preset.model_fields_set and preset.provider != "codex":
        unsupported.append("bare")
    if preset.codex_home is not None and preset.provider != "codex":
        unsupported.append("codex_home")
    if preset.setting_sources is not None and preset.provider not in ("codex", "cursor"):
        unsupported.append("setting_sources")
    if preset.require_isolation and preset.provider not in ("codex", "cursor"):
        unsupported.append("require_isolation")
    return tuple(unsupported)
