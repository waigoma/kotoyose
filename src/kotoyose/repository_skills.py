"""Alias policy for provider-managed repository skill discovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from .cli_install_spec import platform_key
from .print_cli_grok_profile import GROK_NATIVE_SKILL_LAYOUT

if TYPE_CHECKING:
    from .config import ModelPreset
    from .print_cli_profiles import PrintCliProfile

RepositorySkills = Literal["include", "exclude"]


def validate_repository_skills(
    alias: str, preset: ModelPreset, provider: str, profile: PrintCliProfile | None,
) -> None:
    mode = preset.repository_skills
    context = f"{alias}.repository_skills: {mode}"
    if provider == "claude":
        if mode == "include" and (preset.execution == "native" or preset.agent is not None
                                  or preset.is_jailed or preset.tools == []):
            raise ValueError(f"{context} requires Claude SDK chat with project settings enabled")
        return
    if provider == "codex":
        if mode == "exclude":
            raise ValueError(f"{context} is unsupported by Codex app-server 0.170.0 (project skill discovery cannot be disabled)")
        return
    if provider == "cursor":
        return
    if profile is not None and profile.native_skill_layout == GROK_NATIVE_SKILL_LAYOUT:
        if mode == "exclude":
            raise ValueError(f"{context} is unsupported by Grok 1.0.89 with its real GROK_HOME (native project skill discovery remains enabled)")
        if preset.on == "local" and platform_key() != "linux-x64":
            raise ValueError(f"{context} requires Grok on Linux x64")
        return
    raise ValueError(f"{context} is unsupported by {profile.id if profile else provider}")
