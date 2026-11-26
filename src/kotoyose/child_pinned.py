"""Login, status and quota commands declared by the package on both ends of the link."""

from __future__ import annotations

from .print_cli_profiles import (
    PrintCliLoginSpec,
    get_print_cli_profile,
    get_print_cli_profile_for_provider,
)
from .provider_login import LoginFlowKind
from .provider_spec import sdk_provider

class PinnedCommandRejected(ValueError):
    """The provider does not declare the requested operation."""


def normalize_child_provider(provider: str) -> str:
    """Return the shared host and child provider name for pinned operations."""
    name = provider.casefold()
    spec = sdk_provider(name)
    return spec.id if spec else name


def adapter_matches_provider(adapter: str, provider: str) -> bool:
    from .agent_spec import driver_manifest

    try:
        return normalize_child_provider(driver_manifest(adapter).provider) == provider
    except KeyError:
        return False


def pinned_profile(provider: str, profile_id: str | None):
    try:
        profile = get_print_cli_profile(profile_id) if profile_id else get_print_cli_profile_for_provider(provider)
    except KeyError as exc:
        raise PinnedCommandRejected("No matching pinned profile") from exc
    if profile is None or profile.provider.casefold() != provider.casefold():
        raise PinnedCommandRejected("No matching pinned profile")
    return profile


def login_spec(provider: str, profile_id: str | None) -> tuple[PrintCliLoginSpec, LoginFlowKind]:
    """Resolve the pinned login declaration for an SDK or a matching CLI profile."""
    provider = normalize_child_provider(provider)
    if profile_id is None and (sdk := sdk_provider(provider)):
        return sdk.login.spec, sdk.login.flow
    profile = pinned_profile(provider, profile_id)
    if profile.login is None or profile.login_flow is None:
        raise PinnedCommandRejected("No pinned login command")
    return profile.login, profile.login_flow


def status_spec(provider: str, profile_id: str | None) -> PrintCliLoginSpec | None:
    """Resolve a pinned status command if one is declared."""
    provider = normalize_child_provider(provider)
    if profile_id is None and (sdk := sdk_provider(provider)):
        spec = sdk.login.spec
    else:
        profile = pinned_profile(provider, profile_id) if profile_id is not None else get_print_cli_profile_for_provider(provider)
        spec = profile.login if profile is not None else None
    return spec if spec is not None and spec.status_command else None


def quota_command(provider: str, profile_id: str | None) -> tuple[str, ...]:
    """Return the exact quota argv accepted on the child wire."""
    provider = normalize_child_provider(provider)
    if profile_id is None and (sdk := sdk_provider(provider)):
        return sdk.login.quota_command
    profile = pinned_profile(provider, profile_id)
    if profile.quota_rpc is not None:
        return tuple(profile.quota_rpc.command)
    if profile.quota_command and profile.parse_quota:
        return tuple(profile.quota_command)
    raise PinnedCommandRejected("No pinned quota command")
