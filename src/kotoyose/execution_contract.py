"""Native Execution v1 の受付契約。参照の存在・権限確認は実行時の責務。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .capabilities import CapabilityName


Ref = Annotated[str, Field(min_length=1, pattern=r"\S")]


class ContractModel(BaseModel):
    """未対応 field を黙って捨てない wire model。"""

    model_config = ConfigDict(extra="forbid")


class WorkerRequirements(ContractModel):
    """Worker 選択時に緩和してはならない実行環境の条件。"""

    os: Literal["linux", "windows", "macos"] | None = None
    applications: list[str] = Field(default_factory=list, max_length=64)
    gpu: Literal["none", "any", "nvidia", "amd", "intel"] = "none"
    workspace_locality: Literal["local", "remote", "any"] = "any"
    native: Literal["forbidden", "allowed", "required"] = "allowed"


class ExecutionRequirements(ContractModel):
    """隔離 mode は sandbox が既定。native への自動 fallback は許可しない。"""

    isolation: Literal["sandbox", "native"] = "sandbox"
    capabilities: list[CapabilityName] = Field(default_factory=list)
    worker_requirements: WorkerRequirements = Field(default_factory=WorkerRequirements)


class ExecutionInput(ContractModel):
    """出所つき入力参照。trusted の自己申告だけで指示権限を与えない。"""

    kind: Literal[
        "instructions", "task_request", "repository_context", "memory_snapshot", "handoff", "image",
    ]
    provenance: Literal[
        "trusted", "verified_fact", "project", "agent_generated", "external_untrusted", "mixed",
    ]
    content_ref: Ref
    expected_revocation_epoch: str | None = Field(
        default=None, min_length=1, max_length=256, pattern=r"^\S+$",
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def memory_epoch_is_explicit(self) -> "ExecutionInput":
        """Memory snapshot の再利用判断を Caller の明示した失効世代に束縛する。"""
        if self.kind == "memory_snapshot" and self.expected_revocation_epoch is None:
            raise ValueError("memory_snapshot input requires expected_revocation_epoch")
        if self.kind != "memory_snapshot" and self.expected_revocation_epoch is not None:
            raise ValueError("expected_revocation_epoch is only valid for memory_snapshot input")
        return self


class ApprovalBinding(ContractModel):
    """事前に固定した短期 approval と provider tool 引数の対応。"""

    approval_id: str = Field(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}$")
    subject_digest: str = Field(pattern=r"^(?:sha256:)?[0-9a-f]{64}$")
    wait_seconds: int = Field(default=300, ge=1, le=3600)
    mode: Literal["blocking", "asynchronous"] = "blocking"


class ExecutionPreferences(ContractModel):
    """現段階で選好は未提供。空 object のみ受け付け、未知の設定は拒否する。"""


class ExecutionBase(ContractModel):
    """Caller が固定した dispatch の参照。principal は認証から取得する。"""

    schema_version: Literal["1"]
    idempotency_key: Ref
    caller_run_ref: Ref
    workspace_ref: Ref
    execution_grant_ref: Ref
    artifact_spec_ref: Ref | None = None
    budgets_ref: Ref
    requirements: ExecutionRequirements = Field(default_factory=ExecutionRequirements)
    preferences: ExecutionPreferences = Field(default_factory=ExecutionPreferences)
    limits_mode: Literal["agent_work_v1"] | None = Field(default=None, exclude_if=lambda value: value is None)


class AgentExecution(ExecutionBase):
    """Agent の開始要求。alias と移行用 profile の一方を指定する。"""

    kind: Literal["agent"]
    agent_definition_ref: Ref
    model: Ref | None = Field(default=None, exclude_if=lambda value: value is None)
    runtime_profile: Ref | None = Field(default=None, exclude_if=lambda value: value is None)
    required_authentication_mode: Literal["chatgpt_subscription", "api_key"] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    expected_authentication_binding_digest: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$", exclude_if=lambda value: value is None)
    required_provider: Literal["claude", "codex", "cursor", "antigravity"] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    # Subscription means a subscription route with no metered-provider fallback.
    # The server resolves the registered alias once and checks the plan it saves.
    required_billing_mode: Literal["subscription"] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    inputs: list[ExecutionInput] = Field(min_length=1)
    tool_refs: list[Ref] = Field(default_factory=list)
    skill_refs: list[Ref] = Field(default_factory=list)
    mcp_refs: list[Ref] = Field(default_factory=list, max_length=32)
    approval_bindings: list[ApprovalBinding] = Field(default_factory=list, max_length=32)
    client_key: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def one_runtime_name(self) -> "AgentExecution":
        if (self.model is None) == (self.runtime_profile is None):
            raise ValueError("Exactly one of model or runtime_profile is required")
        return self


class RuntimeAuthenticationResolution(ContractModel):
    """許可された alias/profile の認証 binding を process 起動なしで取得する。"""

    schema_version: Literal["1"]
    model: Ref | None = Field(default=None, exclude_if=lambda value: value is None)
    runtime_profile: Ref | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def one_runtime_name(self) -> "RuntimeAuthenticationResolution":
        if (self.model is None) == (self.runtime_profile is None):
            raise ValueError("Exactly one of model or runtime_profile is required")
        return self


class CommandExecution(ExecutionBase):
    """Versioned command の開始要求。任意の host path や shell は受け付けない。"""

    kind: Literal["command"]
    command_spec_ref: Ref
    argv_ref: Ref
    cwd_ref: Ref
    env_refs: list[Ref] = Field(default_factory=list)
    fixture_refs: list[Ref] = Field(default_factory=list)
    timeout_policy_ref: Ref


ExecutionRequest = Annotated[AgentExecution | CommandExecution, Field(discriminator="kind")]


class ExecutionEnvelope(ContractModel):
    """Native API の versioned request envelope。"""

    execution: ExecutionRequest


class ExecutionCancellation(ContractModel):
    """受理証が届かない dispatch も、同じ principal/key の admission を閉じて取り消す。"""

    schema_version: Literal["1"]
    idempotency_key: Ref


ResumeMode = Literal["native", "durable_state"]
ResumeReason = Literal[
    "attempt_not_terminal", "stop_unconfirmed", "effects_unresolved", "operation_unknown",
    "workspace_mismatch", "workspace_unavailable", "snapshot_mismatch", "checkpoint_mismatch",
    "grant_mismatch", "runtime_mismatch", "provider_mismatch", "run_mismatch",
    "already_resumed", "adapter_unsupported", "execution_kind_unsupported", "admission_rejected",
]
SnapshotDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ResumeRequest(ContractModel):
    """新 Attempt の意図と必要な回復点を固定する。key は execution.idempotency_key。"""

    schema_version: Literal["1"]
    execution: ExecutionRequest
    predecessor_snapshot_digest: SnapshotDigest
    required_resume_mode: Literal["native"] | None = Field(default=None, exclude_if=lambda value: value is None)
    checkpoint_ref: Ref | None = None
    checkpoint_digest: SnapshotDigest | None = None

    @model_validator(mode="after")
    def checkpoint_pair(self):
        """回復点を指定した場合は bytes の期待値も必須にする。"""
        if (self.checkpoint_ref is None) != (self.checkpoint_digest is None):
            raise ValueError("checkpoint_ref and checkpoint_digest must be supplied together")
        return self


class ResumeManifest(ContractModel):
    """Kotoyose が停止証拠と継続経路を固定する不変 manifest。digest は自身を含まない。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1"] = "1"
    predecessor_attempt_id: Ref
    predecessor_generation: int = Field(ge=0)
    predecessor_status: Literal[
        "succeeded", "failed", "cancelled", "timeout", "worker_lost", "budget_exceeded", "approval_required",
    ]
    predecessor_sequence: int = Field(ge=0)
    stop_confirmation: Literal["confirmed"]
    side_effect_resolution: Literal["resolved"]
    operations_digest: SnapshotDigest
    predecessor_snapshot_digest: SnapshotDigest
    provider_session_ref: Ref | None
    provider_session_reason: Ref | None
    provider: Ref
    runtime_profile: Ref | None = Field(default=None, exclude_if=lambda value: value is None)
    model: Ref | None = Field(default=None, exclude_if=lambda value: value is None)
    mode: ResumeMode
    workspace_ref: Ref
    workspace_generation: int = Field(ge=0)
    checkpoint_ref: Ref | None
    checkpoint_digest: SnapshotDigest | None
    execution_grant_ref: Ref
    grant_version: int = Field(ge=1)
    grant_digest: SnapshotDigest
    principal: Ref
    caller_run_ref: Ref
    resume_request_key: Ref
    request_digest: SnapshotDigest

    @model_validator(mode="after")
    def session_evidence(self):
        """native の宣言には ref、durable_state には会話を継続しない理由を要求する。"""
        if self.mode == "native":
            if self.provider_session_ref is None or self.provider_session_reason is not None:
                raise ValueError("Native resume requires a retained session ref")
        elif self.provider_session_ref is not None or self.provider_session_reason is None:
            raise ValueError("Durable-state resume requires a non-native reason")
        return self


class ResumeError(ContractModel):
    """resume_unsafe の error 詳細。HTTP は既存と同じ error object で包む。"""

    type: Literal["resume_unsafe"] = "resume_unsafe"
    code: Literal["resume_unsafe"] = "resume_unsafe"
    message: str
    request_id: str
    retryable: bool
    safe_to_retry: bool
    reason_code: ResumeReason
