"""公開する実行保証と、互換 API の必須能力要求を一元管理する。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


CapabilityName = Literal[
    "direct_chat", "native_execution", "command_execution", "durable_attempts",
    "dispatch_idempotency", "durable_events", "confirmed_cancel", "safe_resume",
    "artifact_access", "workspace_refs", "execution_grants", "sandbox", "native",
    "credential_isolation", "operation_journal", "caller_controlled_compression",
    "strict_cost", "repository_context_pack", "memory_bootstrap", "skill_resources", "client_tools", "artifact_retention", "external_mcp",
    "runtime_managed_compression", "manual_compression", "context_observation",
    "workspace_checkpoints", "workspace_retention",
    "agent_execution_v1",
]


class CapabilitySupport(BaseModel):
    """保証の状態。conditional は条件を検証するまで supported と扱わない。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["supported", "unsupported", "conditional"]
    reason: str
    conditions: tuple[str, ...] = ()


class CapabilityManifest(BaseModel):
    """API の契約能力。provider の起動可否や model の品質は表さない。"""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"] = "1"
    chat_requirements_schema_version: Literal["1"] = "1"
    capabilities: dict[CapabilityName, CapabilitySupport]
    safe_resume_modes: tuple[Literal["native", "durable_state"], ...] = ()


class PMSavedPresetRequirements(BaseModel):
    """Opt-in PM contract: exact saved alias and owner-declared billing route."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    provider: Literal["claude"]
    billing_mode: Literal["subscription"]
    metered_fallback: Literal["forbidden"]


class ChatRequirements(BaseModel):
    """metadata.kotoyose の要求。未知の制約を無視して実行しない。"""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"]
    required_capabilities: list[CapabilityName] = Field(default_factory=list)
    pm_saved_preset: PMSavedPresetRequirements | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("pm_saved_preset", mode="before")
    @classmethod
    def _reject_null_pm_requirement(cls, value):
        if value is None:
            raise ValueError("pm_saved_preset must be an explicit requirement object")
        return value


class ChatMetadata(BaseModel):
    """他クライアントの metadata を保持し、Kotoyose 専用領域だけを検証する。"""

    model_config = ConfigDict(extra="allow")
    kotoyose: ChatRequirements | None = None


_UNSUPPORTED_REASONS: dict[CapabilityName, str] = {
    "native_execution": "Native execution dispatch is not implemented.",
    "command_execution": "Commands do not yet use a common execution boundary.",
    "durable_attempts": "Interactions are not durable execution Attempts.",
    "dispatch_idempotency": "Chat requests do not deduplicate dispatches.",
    "durable_events": "Activity streams do not provide durable Attempt replay.",
    "confirmed_cancel": "Disconnect does not prove process and child termination.",
    "safe_resume": "Session continuation does not establish safe redispatch.",
    "artifact_access": "Authorized immutable ArtifactRef access is not implemented.",
    "workspace_refs": "Plain cwd paths do not enforce workspace generations.",
    "execution_grants": "Caller-scoped immutable Execution Grants are not enforced.",
    "sandbox": "Platform sandbox and credential isolation are not yet verified.",
    "native": "Explicit native execution with Grant enforcement is not implemented.",
    "credential_isolation": "Provider login and working shell isolation is not verified.",
    "operation_journal": "External operations are not journaled and reconciled.",
    "caller_controlled_compression": "Disabling runtime compaction is not guaranteed.",
    "strict_cost": "Usage reporting does not guarantee a monetary ceiling.",
    "repository_context_pack": "Repository Context Pack pinning is not enabled by the runtime.",
    "memory_bootstrap": "Pinned Memory snapshot injection and revocation epoch matching are not enabled.",
    "skill_resources": "Pinned Skill resources are not enabled by the runtime.",
    "client_tools": "Durable authenticated client tool transport is not enabled.",
    "artifact_retention": "Caller retention policy and artifact GC are not enabled.",
    "external_mcp": "Administrator-pinned external MCP transport is not configured.",
    "runtime_managed_compression": "Runtime-managed compression policy is not configured.",
    "manual_compression": "Manual runtime compression has no conformance-tested operation.",
    "context_observation": "Runtime context observations are not available on this entry point.",
    "workspace_checkpoints": "Verified checkpoint capture and fresh workspace restore are not configured.",
    "agent_execution_v1": "Execution service is not enabled.",
}


def capability_manifest() -> CapabilityManifest:
    """現在の共通境界で提供する能力を返す。SDK の名前から保証を推測しない。"""
    manifest = CapabilityManifest(capabilities={
        "direct_chat": CapabilitySupport(
            status="supported",
            reason="OpenAI-compatible chat protocol; runtime availability is checked separately.",
        ),
        **{
            name: CapabilitySupport(status="unsupported", reason=reason)
            for name, reason in _UNSUPPORTED_REASONS.items()
        },
    })
    return manifest


def unsupported_capabilities(required: list[CapabilityName]) -> list[CapabilityName]:
    """要求順を維持して未保証の能力を返す。conditional も自動採用しない。"""
    capabilities = capability_manifest().capabilities
    return list(dict.fromkeys(
        name for name in required if capabilities[name].status != "supported"
    ))


def safe_resume_capability(modes: tuple[str, ...]) -> CapabilitySupport:
    """Native admission と adapter が両方保証する経路だけを公開する。"""
    if not modes:
        return CapabilitySupport(status="unsupported", reason=_UNSUPPORTED_REASONS["safe_resume"])
    return CapabilitySupport(
        status="supported",
        reason="Native Agent resume with immutable manifest, confirmed stop, resolved operations and generation fencing: " + ", ".join(modes) + ".",
    )
