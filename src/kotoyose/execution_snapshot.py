"""Attempt の resolved 設定を秘密情報なしで再照合する wire projection。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from .execution_contract import ContractModel, ResumeManifest, ResumeMode
from .execution_resources import content_digest, CodexEgressLimits, pinned_codex_egress_limits
from .compression_contract import CompressionPolicy
from .codex_preferences import CodexModelPreferences
from .preset_prompts import stored_preset_prompts


_DIGEST = r"^(?:sha256:)?[0-9a-f]{64}$"


class ResolvedMemorySnapshot(ContractModel):
    """Memory 本文を除き、失効再評価に必要な pin identity だけを公開する。"""

    resource_ref: str = Field(min_length=1)
    content_digest: str = Field(pattern=_DIGEST)
    revocation_epoch: str = Field(min_length=1, max_length=256)
    captured_at: str = Field(min_length=1)


class ResolvedRuntimeBilling(ContractModel):
    """Established admission policy only; no credentials or routing internals."""

    mode: Literal["subscription"]
    metered_fallback: Literal["forbidden"]
    policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class ResolvedAttemptSnapshot(ContractModel):
    """保存済み Attempt / Grant の参照と実効制約だけを公開する。"""

    schema_version: Literal["1"] = "1"
    attempt_id: str = Field(min_length=1)
    attempt_sequence: int = Field(ge=0)
    request_digest: str = Field(pattern=_DIGEST)
    execution_grant_ref: str = Field(min_length=1)
    grant_digest: str = Field(pattern=_DIGEST)
    runtime_kind: Literal["command", "agent"]
    isolation: Literal["sandbox", "native"]
    network: Literal["none", "unrestricted"]
    backend: Literal["native", "print_cli"] | None = Field(default=None, exclude_if=lambda value: value is None)
    alias: str | None = Field(default=None, exclude_if=lambda value: value is None)
    base_url: str | None = Field(default=None, exclude_if=lambda value: value is None)
    runtime_ref: str | None = Field(default=None, exclude_if=lambda value: value is None)
    tools_mode: Literal["provider_native"] | None = Field(default=None, exclude_if=lambda value: value is None)
    credential_isolation: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    sandbox: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    network_policy_enforced: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    safe_resume: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    compression_control: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    max_turns_enforced: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    resource_limits: str | None = Field(default=None, exclude_if=lambda value: value is None)
    cli_version: str | None = Field(default=None, exclude_if=lambda value: value is None)
    external_capabilities: tuple[str, ...]
    worker_id: str = Field(min_length=1)
    worker_capability_state: Literal["resolved", "unobserved"] = "unobserved"
    worker_os: Literal["linux", "windows", "macos"] | None = None
    worker_applications: tuple[str, ...] = ()
    worker_gpu: Literal["none", "nvidia", "amd", "intel"] | None = None
    worker_isolation_modes: tuple[Literal["sandbox", "native"], ...] = ()
    worker_native: bool | None = None
    worker_workspace_locality: Literal["local", "remote"] | None = None
    image_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    image_state: Literal["resolved", "not_applicable"]
    provider: str | None = None
    provider_state: Literal["resolved", "not_applicable"]
    model: str | None = None
    model_state: Literal["resolved", "not_applicable"]
    cli_digest: str | None = Field(default=None, pattern=_DIGEST)
    cli_state: Literal["resolved", "not_applicable"]
    sdk_version: str | None = None
    codex_execution_mode: Literal["host_native_coding_v1", "room_read_v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    codex_tool_policy_digest: str | None = Field(default=None, pattern=_DIGEST, exclude_if=lambda value: value is None)
    codex_sandbox: Literal["read-only", "workspace-write", "danger-full-access"] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    execution_environment: Literal["isolated", "host"] | None = Field(default=None, exclude_if=lambda value: value is None)
    codex_sandbox_policy: Literal["external_sandbox_restricted_v1", "workspace_read_sandbox_v1"] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    sdk_state: Literal["resolved", "not_applicable"]
    resource_refs: dict[str, str]
    memory_snapshots: tuple[ResolvedMemorySnapshot, ...] = ()
    compression_policy: CompressionPolicy | None = None
    codex_preferences: CodexModelPreferences | None = Field(default=None, exclude_if=lambda value: value is None)
    codex_egress_limits: CodexEgressLimits | None = Field(default=None, exclude_if=lambda value: value is None)
    preset_prompt_digest: str | None = Field(default=None, pattern=_DIGEST, exclude_if=lambda value: value is None)
    snapshot_digest: str | None = Field(default=None, pattern=_DIGEST)
    runtime_billing: ResolvedRuntimeBilling | None = Field(default=None, exclude_if=lambda value: value is None)
    resume_manifest: ResumeManifest | None = None
    resume_manifest_digest: str | None = Field(default=None, pattern=_DIGEST)
    resume_mode: ResumeMode | None = None

    @field_validator("codex_egress_limits", mode="before")
    @classmethod
    def explicit_egress_limits(cls, value):
        if value is None:
            raise ValueError("An explicit Codex egress pin cannot be null")
        return value


def resolved_attempt_snapshot(attempt: dict) -> ResolvedAttemptSnapshot:
    """Attempt の保存 snapshot から本文・path・env・credential を除外して作る。"""
    request = attempt["request"]
    execution = request
    snapshot = attempt["snapshot"]
    grant = snapshot.get("grant")
    if not isinstance(grant, dict):
        raise ValueError("Attempt snapshot has no persisted Grant projection")
    runtime_kind = execution.get("kind")
    if runtime_kind not in {"command", "agent"}:
        raise ValueError("Attempt snapshot has an unknown runtime kind")
    resource_refs = {
        name: execution[name]
        for name in (
            "command_spec_ref", "argv_ref", "cwd_ref", "budgets_ref", "timeout_policy_ref",
            "artifact_spec_ref", "agent_definition_ref", "execution_grant_ref",
        )
        if isinstance(execution.get(name), str)
    }
    resource_refs.update({f"env_ref:{index}": ref for index, ref in enumerate(execution.get("env_refs", [])) if isinstance(ref, str)})
    resource_refs.update({f"fixture_ref:{index}": ref for index, ref in enumerate(execution.get("fixture_refs", [])) if isinstance(ref, str)})
    resource_refs.update({f"tool_ref:{index}": ref for index, ref in enumerate(execution.get("tool_refs", [])) if isinstance(ref, str)})
    resource_refs.update({f"skill_ref:{index}": ref for index, ref in enumerate(execution.get("skill_refs", [])) if isinstance(ref, str)})
    resource_refs.update({f"mcp_ref:{index}": ref for index, ref in enumerate(execution.get("mcp_refs", [])) if isinstance(ref, str)})
    memory_snapshots = tuple(
        ResolvedMemorySnapshot.model_validate(item)
        for item in snapshot.get("memory_snapshots", [])
    )
    resource_refs.update({
        f"memory_snapshot_ref:{index}": item.resource_ref
        for index, item in enumerate(memory_snapshots)
    })
    provider = snapshot.get("provider") if runtime_kind == "agent" else None
    model = snapshot.get("model") if runtime_kind == "agent" else None
    cli_digest = snapshot.get("cli_digest") if runtime_kind == "agent" else None
    sdk_version = snapshot.get("sdk_version") if runtime_kind == "agent" else None
    image_digest = snapshot.get("image_id")
    worker = snapshot.get("worker_capabilities")
    if not isinstance(worker, dict):
        worker = None
    return ResolvedAttemptSnapshot(
        attempt_id=attempt["id"], attempt_sequence=attempt["sequence"],
        request_digest=attempt["request_digest"], execution_grant_ref=request["execution_grant_ref"],
        grant_digest=content_digest(grant), runtime_kind=runtime_kind,
        isolation=grant["isolation"], network=snapshot.get("network", grant["network"]),
        **{key: snapshot.get(key) for key in (
            "backend", "alias", "base_url", "runtime_ref", "tools_mode", "credential_isolation", "sandbox",
            "network_policy_enforced", "safe_resume", "compression_control", "max_turns_enforced",
            "resource_limits", "cli_version", "codex_execution_mode", "codex_tool_policy_digest", "codex_sandbox",
            "execution_environment", "codex_sandbox_policy",
        )},
        external_capabilities=tuple(
            capability for capability in execution.get("requirements", {}).get("capabilities", [])
            if isinstance(capability, str) and (capability.startswith("external") or capability in {"network", "internet"})
        ),
        worker_id=snapshot["worker_id"],
        worker_capability_state="resolved" if worker is not None else "unobserved",
        worker_os=worker.get("os") if worker else None,
        worker_applications=tuple(worker.get("applications", ())) if worker else (),
        worker_gpu=worker.get("gpu") if worker else None,
        worker_isolation_modes=tuple(worker.get("isolation_modes", ())) if worker else (),
        worker_native=worker.get("native") if worker else None,
        worker_workspace_locality=worker.get("workspace_locality") if worker else None,
        image_digest=image_digest, image_state="resolved" if image_digest else "not_applicable",
        provider=provider, provider_state="resolved" if provider else "not_applicable",
        model=model, model_state="resolved" if model else "not_applicable",
        cli_digest=cli_digest, cli_state="resolved" if cli_digest else "not_applicable",
        sdk_version=sdk_version, sdk_state="resolved" if sdk_version else "not_applicable",
        resource_refs=resource_refs,
        memory_snapshots=memory_snapshots,
        compression_policy=snapshot.get('compression_policy'),
        codex_preferences=snapshot.get('codex_preferences'),
        **({"codex_egress_limits": pinned_codex_egress_limits(snapshot)} if "codex_egress_limits" in snapshot else {}),
        preset_prompt_digest=(stored_preset_prompts(snapshot["preset_prompt_snapshot"]).digest
                              if "preset_prompt_snapshot" in snapshot else None),
        snapshot_digest=content_digest(snapshot),
        runtime_billing=snapshot.get("runtime_billing"),
        resume_manifest=snapshot.get("resume_manifest"),
        resume_manifest_digest=snapshot.get("resume_manifest_digest"),
        resume_mode=snapshot.get("resume_mode"),
    )
