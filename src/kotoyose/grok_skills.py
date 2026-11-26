"""The pinned Grok CLI's native skill placement and compatibility settings."""

from pathlib import Path

from .cli_install_spec import platform_key
from .print_cli_grok_profile import GROK_INTEGRATION_ENV, GROK_LINUX_SKILL_DIGEST, GROK_NATIVE_SKILL_LAYOUT
from .print_cli_profiles import PrintCliProfile
from .repository_skills import RepositorySkills


def uses_grok_native_skills(profile: PrintCliProfile, binary: Path) -> bool:
    if profile.native_skill_layout != GROK_NATIVE_SKILL_LAYOUT or platform_key() != "linux-x64":
        return False
    # A declaration's launcher checks the managed binary against the exact pin.
    if profile.data_declaration is not None:
        return True
    from .provider_data_runtime import _cached_digest

    try:
        binary = binary.resolve(strict=True)
        return f"sha256:{_cached_digest(binary, binary.stat())}" == GROK_LINUX_SKILL_DIGEST
    except OSError:
        return False


def grok_skill_env(
    env: dict[str, str], home: Path, mode: RepositorySkills | None, profile: PrintCliProfile,
) -> None:
    env["HOME"] = str(home)
    env.update(GROK_INTEGRATION_ENV)
    for vendor in ("CLAUDE", "CURSOR"):
        if mode == "include":
            env[f"GROK_{vendor}_SKILLS_ENABLED"] = "1"
        elif mode is None and profile.data_declaration is None:
            env.pop(f"GROK_{vendor}_SKILLS_ENABLED", None)
