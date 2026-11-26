"""Opt-in saved-PM selection for the existing full-featured Claude chat path.

No Native Attempt/Grant or new runtime registry is involved. The owner policy
and the actual direct SDK options are checked and pinned before session reuse.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.datastructures import Headers

from . import process_env
from .chat_policy import validate_cwd, validate_required_headers
from .execution_billing import owner_claude_subscription_billing
from .execution_resources import ExecutionError
from .runtime.base import SDKConnectionError
from .runtime.claude_options import build_claude_options
from .runtime.skill_resources import folder_binding, resolve_native_folders
from .secret_store import SecretStoreError, resolve_secret_sync

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions

    from .config import HarnessConfig, ModelPreset
    from .models import ChatCompletionRequest
    from .skill_folders import SkillFolder


@dataclass(frozen=True, slots=True)
class PinnedChatSelection:
    """Request-local options; secrets never appear in repr or public metadata."""

    preset_key: str
    preset: ModelPreset = field(repr=False)
    options: ClaudeAgentOptions = field(repr=False)
    billing: dict
    identity: tuple[str, str, str]
    # 選んだ時の config から一度だけ解いた connection。session は config を引き直さない。
    connection_id: str | None = None
    skill_folders: tuple[SkillFolder, ...] = field(default=(), repr=False)


def _identity_value(value):
    if dataclasses.is_dataclass(value):
        return {item.name: _identity_value(getattr(value, item.name)) for item in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {key: _identity_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_identity_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def pin_pm_saved_preset(
    config: HarnessConfig,
    request: ChatCompletionRequest,
    headers: dict[str, str],
) -> PinnedChatSelection:
    """Resolve an exact registered PM alias once, preserving its whole recipe.

    Owner policy is read only from routes, and inherited only by its
    identical resolved endpoint/source. The client may request a guarantee,
    never assert a guarantee or supply an alternative route.
    """
    requirements = request.metadata.kotoyose.pm_saved_preset if request.metadata and request.metadata.kotoyose else None
    if requirements is None:
        raise ValueError("An explicit PM saved-preset requirement is required")
    # Deep-copy synchronously before any await or option/resource construction.
    # A config reload or in-place owner edit cannot mix selection generations.
    config = config.model_copy(deep=True)
    if config.compatibility_config.has_model(request.model):
        raise ExecutionError("unsupported_capability", "Saved PM preset does not support ordinary chat", 501,
                             reason_code="pm_chat_transport_unavailable")
    if request.model not in config.models:
        raise ExecutionError("invalid_request", "Saved PM preset alias is not registered", 422,
                             reason_code="pm_saved_preset_not_found")
    if config.models[request.model].execution != "direct":
        raise ExecutionError("unsupported_capability", "Saved PM preset does not support ordinary chat", 501,
                             reason_code="pm_chat_transport_unavailable")
    declared = config.models[request.model]
    if declared.route is not None and any(
        key in declared.model_fields_set
        for key in (
            "provider",
            "model",
            "anthropic_base_url",
            "anthropic_auth_token_env",
            "anthropic_auth_token_secret",
        )
    ):
        raise ExecutionError("invalid_request", "Saved PM preset contains fields owned by its route", 422,
                             reason_code="pm_saved_preset_invalid")
    preset = config.preset_for(request.model)
    if preset.provider != requirements.provider:
        raise ExecutionError("permission_denied", "Saved PM preset provider does not meet request", 403,
                             reason_code="runtime_provider_mismatch")
    subscription_only = preset.route is not None and preset.subscription_only and preset.provider == "claude"
    runtime = {
        "backend": "direct", "provider": preset.provider, "model": preset.model,
        "base_url": preset.anthropic_base_url, "credential_mode": "environment",
        # Keep the ordinary SDK's existing header/auth convention. The owner
        # assertion belongs to endpoint/source, not the spelling of this name.
        "credential_name": "ANTHROPIC_API_KEY", "credential_env": preset.anthropic_auth_token_env,
        "subscription_only": subscription_only,
    }
    billing = owner_claude_subscription_billing(runtime, adapter="direct_claude_sdk")
    if billing is None:
        raise ExecutionError("unsupported_capability", "Subscription-only billing is not established for this PM preset", 501,
                             reason_code="billing_policy_unavailable")
    validate_required_headers(preset, Headers(headers))
    validate_cwd(preset, request.model, request.cwd)
    if preset.anthropic_auth_token_secret:
        try:
            token = resolve_secret_sync(preset.anthropic_auth_token_secret)
        except SecretStoreError as exc:
            raise ExecutionError(
                "unsupported_capability",
                "Saved PM preset credential secret is unavailable",
                503,
                reason_code="pm_credential_unavailable",
            ) from exc
    else:
        token = process_env.get(preset.anthropic_auth_token_env)
    if not token:
        raise ExecutionError("unsupported_capability", "Saved PM preset credential source is unavailable", 503,
                             reason_code="pm_credential_unavailable")
    from .session_store import get_default_store
    options = build_claude_options(
        config, preset, model_name=request.model, cwd_override=request.cwd,
        request_headers=headers, env={}, session_store=get_default_store(),
        effort_override=request.reasoning_effort,
    )
    # Pin after full recipe composition. Preserve tools, MCP, memory prompts,
    # plugins, skills, session store, system prompt snapshots, and streaming.
    # No OAuth/API-key fallback is consulted when the approved source is empty.
    options = dataclasses.replace(options, model=preset.model, fallback_model=None, env={
        **options.env,
        "ANTHROPIC_BASE_URL": preset.anthropic_base_url,
        "ANTHROPIC_API_KEY": token,
        "ANTHROPIC_AUTH_TOKEN": "",
        "CLAUDE_CODE_OAUTH_TOKEN": "",
        "ANTHROPIC_CUSTOM_HEADERS": "",
        "CLAUDE_CODE_USE_BEDROCK": "0",
        "CLAUDE_CODE_USE_VERTEX": "0",
        "CLAUDE_CODE_USE_FOUNDRY": "0",
    })
    try:
        folders = resolve_native_folders(config, preset)
    except SDKConnectionError as exc:
        raise ExecutionError("invalid_request", str(exc), 422) from None
    # Include actual resource contents/options, not only alias or model labels.
    # Session store is infrastructure; its contents must not become identity.
    # Header and credential values enter the digest only, never persisted raw.
    effective_options = {
        item.name: _identity_value(getattr(options, item.name))
        for item in dataclasses.fields(options) if item.name not in {"session_store", "debug_stderr"}
    }
    identity_source = {
        "contract": "pm_saved_preset_v1", "alias": request.model,
        "preset": preset.model_dump(mode="json"), "options": effective_options,
        "billing": billing,
        **({"skill_scope": folder_binding(folders)} if folders else {}),
        "required_headers": [(name.lower(), {key.lower(): value for key, value in headers.items()}.get(name.lower(), ""))
                             for name in sorted(preset.required_request_headers, key=str.lower)],
    }
    digest = hashlib.sha256(json.dumps(identity_source, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    route = config.routes.get(preset.route) if preset.route else None
    return PinnedChatSelection(request.model, preset, options, billing,
                               (preset.provider, request.model, digest),
                               connection_id=route.connection if route is not None else None, skill_folders=folders)
