"""Caller が発行する不変 resource と、認証済み上限の契約。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, AliasChoices, Field, PrivateAttr, TypeAdapter, field_validator, model_serializer, model_validator
import structlog

from . import process_env
from .claude_cli import bundled_claude_cli
from .agent_spec import (
    coding_profile_allowed, driver_manifest, egress_limits_allowed,
    unmapped_backend, published_agent_execution, surface_exceeds,
)
from .execution_contract import ContractModel, Ref
from .child_registry import child_on_name
from .codex_preferences import CodexModelPreferences
from .preset_prompts import PresetPromptSnapshot
from .execution_artifacts import ArtifactCaptureSpec
from .artifact_storage_config import ArtifactStorageConfig, ArtifactDiskLimits
from .artifact_retention import OutputArtifactLimits
from .workspace_retention import WorkspaceLimits
from .input_artifacts import InputArtifactLimits
from .remote_worker_contract import RemoteWorkerRef
from .mcp_contract import ExternalMcpServerConfig, McpBindingResource

Provenance = Literal["trusted", "verified_fact", "project", "agent_generated", "external_untrusted", "mixed"]
log = structlog.get_logger()
PINNED_RESOURCE_MAX_DEPENDENCIES = 64
REPOSITORY_CONTEXT_PACK_MAX_BYTES = 4 * 1024 * 1024
SKILL_RESOURCE_MAX_BYTES = 1024 * 1024
_DIGEST_PATTERN = r"^(?:sha256:)?[0-9a-f]{64}$"
_REVISION_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"

class ExecutionError(Exception):
    """実行の受理可否と、再送の安全性を独立に伝える。"""

    def __init__(self, code: str, message: str, status: int = 422, *, safe_to_retry: bool = True,
                 reason_code: str | None = None, recovery_at: str | None = None):
        super().__init__(message)
        self.recovery_at = recovery_at
        self.code = code
        self.status = status
        self.safe_to_retry = safe_to_retry
        self.reason_code = reason_code


def codex_execution_failure(exc: BaseException) -> str | None:
    """認可境界の既知停止理由だけを公開し、例外本文や未知の code は保存しない。"""
    if isinstance(exc, ExecutionError):
        if exc.reason_code == "authentication_binding_mismatch":
            return exc.reason_code
        if exc.code in {"authentication_failed", "permission_denied", "request_conflict"}:
            return exc.code
    return None


def canonical_json(value: object) -> str:
    """順序に依存しない request/resource の同一性を得る。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def content_digest(value: object) -> str:
    """Version を含む内容から不変参照の digest を得る。"""
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def relative_directory(value: str) -> str:
    """Sandbox 内の cwd を workspace 相対に限定する。"""
    path = PurePosixPath(value)
    if not value or "\x00" in value or path.is_absolute() or ".." in path.parts:
        raise ValueError("cwd must be relative to the workspace")
    return str(path)


class ResourceBase(ContractModel):
    """保存形式の版を content digest に含める。"""

    schema_version: Literal["1"]


class CommandSpec(ResourceBase):
    """Caller が固定した executable。argv は別の不変参照で固定する。"""

    kind: Literal["command_spec"]
    executable: Annotated[str, Field(min_length=1, max_length=4096, pattern=r"^[^\x00]+$")]


class ArgumentList(ResourceBase):
    """Shell 展開を行わない引数列。"""

    kind: Literal["argv"]
    values: list[Annotated[str, Field(max_length=65536, pattern=r"^[^\x00]*$")]] = Field(max_length=1024)


class WorkingDirectory(ResourceBase):
    """Host path を含まない作業ディレクトリ。"""

    kind: Literal["cwd"]
    path: str
    _relative = field_validator("path")(relative_directory)


class Environment(ResourceBase):
    """Caller 上限で許可した値だけを sandbox へ渡す。"""

    kind: Literal["env"]
    values: dict[str, str]

    @field_validator("values")
    @classmethod
    def validate_values(cls, values: dict[str, str]) -> dict[str, str]:
        """Provider/host 環境の暗黙継承を受け付けない。"""
        if len(values) > 128:
            raise ValueError("too many environment values")
        for key, value in values.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\x00" in value or len(value) > 65536:
                raise ValueError("invalid environment value")
            if key in {"HOME", "PATH", "DOCKER_HOST", "DOCKER_CONTEXT", "CLAUDE_CODE_OAUTH_TOKEN",
                       "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}:
                raise ValueError("reserved environment key")
        return values


class TimeoutPolicy(ResourceBase):
    """用途ごとの警告と最終期限、停止猶予を分離する。"""

    kind: Literal["timeout_policy"]
    soft_seconds: float = Field(gt=0, allow_inf_nan=False)
    hard_seconds: float = Field(gt=0, allow_inf_nan=False)
    grace_seconds: float = Field(gt=0, le=60, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered(self) -> TimeoutPolicy:
        """警告が停止期限の後に届く設定を拒む。"""
        if self.soft_seconds >= self.hard_seconds:
            raise ValueError("soft threshold must precede hard deadline")
        return self


class ExecutionBudget(ResourceBase):
    """Workspace command の resource 上限。provider 課金は観測値として別に扱う。"""

    kind: Literal["budget"]
    reservation_ref: Ref
    mode: Literal["observed"]
    resource_scope: Literal["workspace_commands"] = "workspace_commands"
    deadline: datetime
    memory_bytes: int = Field(ge=16 * 1024 * 1024)
    pids_limit: int = Field(ge=1)
    cpus: float = Field(gt=0, allow_inf_nan=False)
    output_limit_bytes: int = Field(ge=1024, le=8 * 1024 * 1024)

    @field_validator("deadline")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        """Caller と Worker の timezone 解釈を一致させる。"""
        if value.tzinfo is None:
            raise ValueError("deadline must include a timezone")
        return value


class ExecutionGrant(ResourceBase):
    """認証主体に紐づく不変 Grant。principal は body に持たない。"""

    kind: Literal["grant"]
    grant_id: Ref
    version: int = Field(ge=1)
    caller_run_ref: Ref
    # 新 Run の復旧に限り、同じ認証主体が発行した Grant をこの前 Attempt へ束縛する。
    resume_predecessor_ref: Ref | None = None
    workspace_ref: Ref
    isolation: Literal["sandbox", "native"]
    network: Literal["none"]
    command_spec_refs: list[Ref] = Field(default_factory=list)
    runtime_profiles: list[Ref] = Field(default_factory=list)
    agent_models: list[Ref] = Field(default_factory=list)
    resource_refs: list[Ref] = Field(default_factory=list)
    policy_ref: Ref
    not_before: datetime
    expires_at: datetime
    max_seconds: float = Field(gt=0, allow_inf_nan=False)
    delegation: Literal["none"]

    @model_serializer(mode="wrap")
    def omit_unset_optional_fields(self, handler):
        """旧 Grant にない model と復旧 binding を補完せず、保存済み wire 形状を維持する。"""
        data = handler(self)
        if "agent_models" not in self.model_fields_set:
            data.pop("agent_models", None)
        if "resume_predecessor_ref" not in self.model_fields_set:
            data.pop("resume_predecessor_ref", None)
        return data

    @model_validator(mode="after")
    def valid_interval(self) -> ExecutionGrant:
        """曖昧な時刻や空の許可期間を拒む。"""
        if self.not_before.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("grant timestamps must include a timezone")
        if self.not_before >= self.expires_at:
            raise ValueError("grant interval is empty")
        return self


class InputText(ResourceBase):
    """信頼度を持つ入力。出所に応じた注入位置は runtime が決める。"""

    kind: Literal["text"]
    text: str = Field(min_length=1, max_length=262144)
    provenance: Provenance


class AgentDefinition(ResourceBase):
    """System 指示と出力契約を Caller が版として固定する。"""

    kind: Literal["agent_definition"]
    instructions: str = Field(min_length=1, max_length=65536)
    output_contract: Literal["text", "json"]
    max_turns: int = Field(ge=1, le=1000)
    timeout_policy_ref: Ref


class ClientToolSpec(ResourceBase):
    """管理者が登録し、provider へ公開する caller client tool の固定定義。"""

    kind: Literal["client_tool"]
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    description: str = Field(min_length=1, max_length=4096)
    input_schema: dict = Field(max_length=64)
    effect: Literal["set", "play", "append", "read"]
    max_payload_bytes: int = Field(default=256 * 1024, ge=1, le=1024 * 1024)
    timeout_seconds: int = Field(default=30, ge=1, le=3600)

    @model_validator(mode="after")
    def strict_schema(self) -> ClientToolSpec:
        """provider が解釈できない schema を client 側へ転送しない。"""
        if self.input_schema.get("type") != "object" or self.input_schema.get("additionalProperties") is not False:
            raise ValueError("client tool input_schema must be a closed object schema")
        if set(self.input_schema) - {"type", "properties", "required", "additionalProperties"}:
            raise ValueError("client tool input_schema has unsupported fields")
        from jsonschema import Draft202012Validator, SchemaError
        encoded = canonical_json(self.input_schema)
        if len(encoded.encode()) > 32768 or '"$ref"' in encoded or '"$dynamicRef"' in encoded:
            raise ValueError("client tool schema must be bounded and self-contained")
        try:
            Draft202012Validator.check_schema(self.input_schema)
        except SchemaError as exc:
            raise ValueError("Invalid client tool input schema") from exc
        return self


class PinnedContentResource(ResourceBase):
    """本文と生成条件を固定し、実行時に再生成・昇格しない resource。"""

    content: str = Field(min_length=1)
    content_digest: str = Field(pattern=_DIGEST_PATTERN)
    schema_digest: str = Field(pattern=_DIGEST_PATTERN)
    generator: Ref
    version: Ref = Field(validation_alias=AliasChoices("version", "generator_version"))
    config_digest: str = Field(pattern=_DIGEST_PATTERN)
    source_revision: str = Field(pattern=_REVISION_PATTERN)
    repository_ref: Ref = Field(validation_alias=AliasChoices("repository_ref", "repository_identity"))
    provenance: Literal["verified_fact", "project", "agent_generated", "external_untrusted", "mixed"]
    dependency_refs: list[Ref] = Field(default_factory=list, max_length=PINNED_RESOURCE_MAX_DEPENDENCIES)

    @model_validator(mode="after")
    def immutable_content(self) -> PinnedContentResource:
        """保存前に本文・schema の digest を照合し、trusted 自己申告を拒む。"""
        actual_content = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.content_digest.removeprefix("sha256:") != actual_content:
            raise ValueError("content_digest does not match content")
        expected_schema = schema_digest_for(self.kind)
        if self.schema_digest.removeprefix("sha256:") != expected_schema:
            raise ValueError("schema_digest does not match resource kind")
        if len(self.content.encode("utf-8")) > self.max_content_bytes:
            raise ValueError("pinned resource content exceeds the limit")
        if len(set(self.dependency_refs)) != len(self.dependency_refs):
            raise ValueError("dependency_refs must be unique")
        return self

    @property
    def max_content_bytes(self) -> int:
        """種別ごとの bounded 本文上限。"""
        return REPOSITORY_CONTEXT_PACK_MAX_BYTES


class RepositoryContextPack(PinnedContentResource):
    """revision から導出した project context の immutable pack。"""

    kind: Literal["repository_context_pack"]

    @property
    def max_content_bytes(self) -> int:
        return REPOSITORY_CONTEXT_PACK_MAX_BYTES


class MemorySnapshot(PinnedContentResource):
    """Memory Platform が生成し、Caller が選択した bootstrap の不変入力。"""

    kind: Literal["memory_snapshot"]
    provenance: Literal["agent_generated", "external_untrusted", "mixed"]
    namespace_refs: list[Ref] = Field(min_length=1, max_length=64)
    captured_at: datetime
    revocation_epoch: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^\S+$")]

    @model_validator(mode="after")
    def valid_memory_identity(self) -> MemorySnapshot:
        """失効再評価に必要な時点と namespace 集合を曖昧にしない。"""
        if self.captured_at.tzinfo is None:
            raise ValueError("memory snapshot captured_at must include a timezone")
        if len(set(self.namespace_refs)) != len(self.namespace_refs):
            raise ValueError("memory snapshot namespace_refs must be unique")
        return self


class SkillResource(PinnedContentResource):
    """実行面を追加しない、本文だけの pinned Skill resource。"""

    kind: Literal["skill"]

    @property
    def max_content_bytes(self) -> int:
        return SKILL_RESOURCE_MAX_BYTES


Resource = Annotated[
    CommandSpec | ArgumentList | WorkingDirectory | Environment | TimeoutPolicy
    | ExecutionBudget | ExecutionGrant | InputText | AgentDefinition | ArtifactCaptureSpec
    | RepositoryContextPack | MemorySnapshot | SkillResource | ClientToolSpec | McpBindingResource,
    Field(discriminator="kind"),
]
RESOURCE_ADAPTER = TypeAdapter(Resource)


def schema_digest_for(kind: str) -> str:
    """resource kind と schema version から schema pin の digest を得る。"""
    return hashlib.sha256(canonical_json({"kind": kind, "schema_version": "1"}).encode("utf-8")).hexdigest()


class ResourceEnvelope(ContractModel):
    """Resource を登録する versioned envelope。"""

    resource: Resource


class WorkspaceRegistration(ContractModel):
    """Caller の管理ディレクトリに存在する独立 clone を登録する。"""

    schema_version: Literal["1"]
    path: Ref
    caller_workspace_ref: Ref


def _reject_replaced_keys(value, replacements: dict[str, str]):
    """ContractModel の extra=forbid より先に、消した key を名前つきで拒む。"""
    if isinstance(value, dict):
        for key, replacement in replacements.items():
            if key in value:
                raise ValueError(f"{key} was replaced by {replacement}")
    return value


def _validate_client_place(place: str) -> str:
    if place in {"host", "isolated", "child"} or child_on_name(place):
        return place
    raise ValueError("place must be 'host', 'isolated', 'child', or 'child:<name>'")


ClientPlace = Annotated[str, Field(strict=True), AfterValidator(_validate_client_place)]


class CallerPolicy(ContractModel):
    """HTTP body から変更できない認証主体の権限上限。"""

    workspace_roots: list[Path] = Field(default_factory=list)
    agent_models: list[str] = Field(default_factory=list)
    mcp_servers: list[Ref] = Field(default_factory=list, max_length=32)
    places: list[ClientPlace] = Field(default_factory=lambda: ["isolated"])
    max_surface: Literal["none", "read", "exec", "write"] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    allow_full: bool = Field(default=False, exclude_if=lambda value: not value)
    env_keys: list[str] = Field(default_factory=list)
    provenance: list[Provenance] = Field(
        default_factory=lambda: ["project", "agent_generated", "external_untrusted", "mixed"],
    )
    max_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_memory_bytes: int = Field(ge=16 * 1024 * 1024)
    max_pids: int = Field(ge=1)
    max_cpus: float = Field(gt=0, allow_inf_nan=False)
    max_concurrent: int = Field(ge=1)

    @model_validator(mode="before")
    @classmethod
    def reject_replaced_caller_keys(cls, value):
        """消した Caller の key は黙って捨てず、置き換えた先の名前で拒む。"""
        return _reject_replaced_keys(value, {
            "runtime_profiles": "agent_models",
            "isolation_modes": "places",
        })

    @model_serializer(mode="wrap")
    def omit_unset_optional_fields(self, handler):
        """旧 Caller 設定に存在しなかった agent_models は出力に追加しない。"""
        data = handler(self)
        if "agent_models" not in self.model_fields_set:
            data.pop("agent_models", None)
        if "places" not in self.model_fields_set:
            data.pop("places", None)
        if "max_surface" not in self.model_fields_set:
            data.pop("max_surface", None)
        if not self.allow_full:
            data.pop("allow_full", None)
        return data


_EgressPositiveInt = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


class CodexEgressLimits(ContractModel):
    """One administrator-owned, admission-pinned broker limit set; never tool authority."""

    model_config = {"extra": "forbid", "frozen": True, "revalidate_instances": "always"}
    version: Literal[1]
    total_bytes: _EgressPositiveInt | Literal["disabled"]
    total_connections: _EgressPositiveInt | Literal["disabled"]
    request_bytes: _EgressPositiveInt
    response_bytes: _EgressPositiveInt

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported Codex egress limits version")
        return value


def pinned_codex_egress_limits(snapshot: dict) -> CodexEgressLimits | None:
    """Only absence means the old contract. Never fill a stored pin from today's profile."""
    if "codex_egress_limits" not in snapshot:
        return None
    return CodexEgressLimits.model_validate(snapshot["codex_egress_limits"])


class DriverCell(ContractModel):
    """rootfs の種類 1 つ分の材料。種類の名前は agent_spec の rootfs_digest。"""

    rootfs: Path | None = None
    rootfs_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    image_id: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    codex_egress_limits: CodexEgressLimits | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("codex_egress_limits", mode="before")
    @classmethod
    def explicit_egress_limits(cls, value):
        if value is None:
            raise ValueError("Omit codex_egress_limits to leave limits unset, or provide a limits object")
        return value

    @model_validator(mode="after")
    def cell_materials_valid(self) -> "DriverCell":
        if (self.rootfs is None) != (self.rootfs_digest is None):
            raise ValueError("rootfs and rootfs_digest must be provided together")
        if self.rootfs is None and self.image_id is None:
            raise ValueError("DriverCell requires either rootfs/rootfs_digest or image_id")
        return self


class DriverInstall(ContractModel):
    """この機械に入っている driver。cli が空ならその driver は binary を持たない。

    rootfs は cell の種類ごとに 1 つ。jail の standard と coding を同じ欄に畳まない。
    """

    cli_binary: Path | None = Field(default=None, exclude_if=lambda value: value is None)
    profile_binaries: dict[str, Path] = Field(default_factory=dict, exclude_if=lambda value: not value)
    cells: dict[str, DriverCell] = Field(default_factory=dict, exclude_if=lambda value: not value)

    @model_validator(mode="before")
    @classmethod
    def reject_flat_cell_materials(cls, value):
        """rootfs を driver 直下に置く形は、cell ごとの材料に置き換え、credentials は connections へ移した。"""
        return _reject_replaced_keys(value, {
            "credentials": "connections",
            "rootfs": "cells",
            "rootfs_digest": "cells",
            "codex_egress_limits": "cells",
        })

    @model_validator(mode="after")
    def cells_match_rootfs_kinds(self) -> "DriverInstall":
        for kind, cell in self.cells.items():
            if kind not in {"standard", "coding"}:
                raise ValueError(f"unknown rootfs cell: {kind}")
            if kind != "coding" and cell.codex_egress_limits is not None:
                raise ValueError("codex_egress_limits belong on the coding cell")
            if kind != "coding" and cell.image_id is not None:
                raise ValueError("image_id belongs on the coding cell")
        return self


class NativeRuntimeProfile(ContractModel):
    """隔離 adapter が解釈する固定 profile。Caller domain は内容を解釈しない。"""

    _agent_surface: str | None = PrivateAttr(default=None)
    backend: Literal["native"] = "native"
    provider: Literal["claude", "codex"]
    codex_rootfs: Path | None = None
    codex_rootfs_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    codex_execution_mode: Literal["sandbox_exec_v1", "host_native_coding_v1", "room_read_v1"] = Field(
        default="sandbox_exec_v1", exclude_if=lambda value: value == "sandbox_exec_v1")
    model: str = Field(min_length=1)
    compression_mode: Literal['runtime_managed', 'caller_controlled'] = 'runtime_managed'
    cli_binary: Path
    base_url: str | None = None
    credential_mode: Literal["runtime_managed", "environment", "secret", "chatgpt_subscription"]
    credential_env: str | None = None
    credential_secret: str | None = Field(default=None, exclude_if=lambda value: value is None)
    credential_name: Literal["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "CODEX_CHATGPT_AUTH"] = "CLAUDE_CODE_OAUTH_TOKEN"
    codex_auth_file: Path | None = None
    subscription_only: bool = Field(default=False, strict=True, exclude_if=lambda value: not value)
    codex_egress_limits: CodexEgressLimits | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("codex_egress_limits", mode="before")
    @classmethod
    def explicit_egress_limits(cls, value):
        if value is None:
            raise ValueError("Omit codex_egress_limits to leave limits unset, or provide a limits object")
        return value

    @model_validator(mode="after")
    def fixed_configuration(self) -> NativeRuntimeProfile:
        """自動選択 alias と曖昧な credential source を拒否する。"""
        if self.codex_egress_limits is not None and not egress_limits_allowed(
                self.provider, self.codex_execution_mode):
            raise ValueError("Codex egress limits require the coding runtime")
        if self.model in {"opus", "sonnet", "haiku", "best", "inherit"}:
            raise ValueError("Native runtime requires an explicit model identifier")
        if self.base_url is not None:
            parsed = urlsplit(self.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or "\x00" in self.base_url:
                raise ValueError("Native runtime base_url requires an absolute HTTP(S) URL")
        if self.credential_mode == "environment" and not self.credential_env:
            raise ValueError("environment mode requires a credential environment name")
        if self.credential_mode == "secret" and not self.credential_secret:
            raise ValueError("secret mode requires a credential secret name")
        if self.credential_mode != "secret" and self.credential_secret is not None:
            raise ValueError("credential_secret requires secret credential_mode")
        if self.credential_mode == "runtime_managed" and self.credential_env is not None:
            raise ValueError("runtime_managed mode cannot override credential environment")
        if self.provider == "codex":
            if self.credential_mode == "chatgpt_subscription":
                if (self.credential_name != "CODEX_CHATGPT_AUTH" or self.credential_env is not None
                        or self.credential_secret is not None
                        or self.codex_auth_file is None or not self.codex_auth_file.is_absolute()):
                    raise ValueError("Codex subscription requires an explicit host auth file and no API credential")
            elif self.credential_mode not in {"environment", "secret"} or self.credential_name != "OPENAI_API_KEY":
                raise ValueError("Codex sandbox requires an explicit environment or secret credential")
            elif self.codex_auth_file is not None:
                raise ValueError("Codex API profile cannot use subscription authentication")
            if self.base_url is not None:
                raise ValueError("Codex sandbox does not inherit endpoint overrides")
        elif self.credential_name in {"OPENAI_API_KEY", "CODEX_CHATGPT_AUTH"} or self.credential_mode == "chatgpt_subscription" or self.codex_auth_file is not None:
            raise ValueError("Claude profile cannot use a Codex credential")
        if not coding_profile_allowed(self.provider, self.codex_execution_mode, self.credential_mode):
            raise ValueError("Host coding requires an explicit Codex subscription profile")
        if self.subscription_only:
            endpoint = urlsplit(self.base_url or "")
            valid_env = self.credential_mode == "environment" and bool(
                re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.credential_env or "")
            )
            valid_secret = self.credential_mode == "secret" and bool(self.credential_secret)
            valid_endpoint = (
                endpoint.scheme in {"http", "https"} and bool(endpoint.hostname)
                and endpoint.username is None and endpoint.password is None and not endpoint.query and not endpoint.fragment
                and not any(character.isspace() or character == "\x00" for character in self.base_url or "")
                and self.credential_name == "ANTHROPIC_AUTH_TOKEN"
                and (valid_env or valid_secret)
            )
            valid_oauth_subscription = (
                self.base_url is None
                and self.credential_mode == "runtime_managed"
                and self.credential_name == "CLAUDE_CODE_OAUTH_TOKEN"
                and self.credential_env is None
                and self.credential_secret is None
            )
            if self.provider != "claude" or not (valid_endpoint or valid_oauth_subscription):
                raise ValueError("subscription_only requires an explicit Claude endpoint or OAuth subscription source")
        return self


class ResolvedCodexRuntimeProfile(NativeRuntimeProfile):
    """Ephemeral alias resolution. It is not written back into config."""

    codex_preferences: CodexModelPreferences | None = Field(default=None, exclude_if=lambda value: value is None)
    execution_environment: Literal["isolated"] | None = Field(default=None, exclude_if=lambda value: value is None)
    codex_sandbox: Literal["read-only", "workspace-write", "danger-full-access"] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    preset_prompt_snapshot: PresetPromptSnapshot | None = Field(default=None, exclude_if=lambda value: value is None)


def codex_authentication_binding(snapshot: dict) -> dict:
    """Codex の明示認証方式と source/account の digest だけを Caller に公開する。

    サブスクは account の digest、API key は設定された source を固定する。
    """
    modes = {("chatgpt_subscription", "CODEX_CHATGPT_AUTH"): "chatgpt_subscription",
             ("environment", "OPENAI_API_KEY"): "api_key",
             ("secret", "OPENAI_API_KEY"): "api_key"}
    mode = modes.get((snapshot.get("credential_mode"), snapshot.get("credential_name")))
    if mode is None:
        raise ValueError("invalid_codex_authentication")
    if mode == "chatgpt_subscription":
        binding = {"mode": mode, "account": snapshot.get("codex_auth_identity")}
    elif snapshot["credential_mode"] == "environment":
        binding = {"mode": mode, "source": f"env:{snapshot.get('credential_env')}"}
    else:
        binding = {"mode": mode, "source": f"secret:{snapshot.get('credential_secret')}"}
    return {"mode": mode, "binding_digest": content_digest(binding)}


def codex_profile_authentication(profile: NativeRuntimeProfile) -> dict:
    """固定 profile の source と現在 account を token を保持せず snapshot 化する。"""
    if profile.backend != "native" or profile.provider != "codex":
        raise ValueError("invalid_codex_authentication")
    binding = {"credential_mode": profile.credential_mode, "credential_env": profile.credential_env,
               "credential_name": profile.credential_name}
    if profile.credential_secret is not None:
        binding["credential_secret"] = profile.credential_secret
    if profile.credential_mode == "chatgpt_subscription":
        from .codex_subscription import read_subscription
        binding.update({"codex_auth_file": str(profile.codex_auth_file),
                        "codex_auth_identity": read_subscription(profile.codex_auth_file).identity})
    return binding


class PrintCliRuntimeProfile(ContractModel):
    """Official `-p` CLI execution profile resolved from a `cli.print` route."""

    _agent_surface: str | None = PrivateAttr(default=None)
    backend: Literal["print_cli"] = "print_cli"
    provider: Literal["print_cli"] = "print_cli"
    acp: bool = Field(default=False, exclude_if=lambda value: not value)
    # None: the alias did not restrict tools (surface and place decide). (): no tools at all.
    acp_tools: tuple[str, ...] | None = Field(default=None, exclude_if=lambda value: value is None)
    acp_deny: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)
    profile: str = Field(min_length=1)
    model: str = Field(min_length=1)
    cli_binary: Path
    connection_id: str = Field(min_length=1)
    login_home: Path | None = None
    api_key_secret: str | None = None
    execution_environment: Literal["host", "isolated"] = "host"
    image_id: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )


class AliasRuntimeProfile(ContractModel):
    """config.yml の alias を参照し、Claude の起動設定だけ profile に保持する。"""

    backend: Literal["alias"] = "alias"
    alias: str = Field(min_length=1)
    cli_binary: Path | None = None
    credential_mode: Literal["runtime_managed", "environment"] | None = None
    credential_env: str | None = None
    credential_name: Literal["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"] = "CLAUDE_CODE_OAUTH_TOKEN"
    compression_mode: Literal["runtime_managed", "caller_controlled"] = "runtime_managed"

RuntimeProfile = Annotated[
    NativeRuntimeProfile | PrintCliRuntimeProfile | AliasRuntimeProfile,
    Field(discriminator="backend"),
]


def resolve_runtime_profile(
    profile: NativeRuntimeProfile | PrintCliRuntimeProfile | AliasRuntimeProfile, *, config=None,
) -> NativeRuntimeProfile | PrintCliRuntimeProfile:
    """alias の provider を確認し、admission で使う backend と model を確定する。"""
    if isinstance(profile, PrintCliRuntimeProfile):
        return profile
    if isinstance(profile, NativeRuntimeProfile):
        return profile
    from .config import get_config
    config = config if config is not None else get_config()
    if profile.alias not in config.models:
        raise ValueError(f"Unknown models alias: {profile.alias}")
    preset = config.preset_for(profile.alias)
    if preset.route is not None:
        route = config.routes.get(preset.route)
        route_conn = config.connections.get(route.connection) if route is not None else None
        if preset.provider == "claude" and route_conn is not None and route_conn.config_dir is not None:
            raise ValueError(
                f"Claude alias '{profile.alias}' uses config_dir connection '{route.connection}', "
                "which supports host placement only; isolated/jail routes are not supported"
            )
    backend = unmapped_backend(preset.provider)
    if backend is None:
        raise ValueError(f"Alias '{profile.alias}' has no agent backend")
    if backend == "print_cli":
        raise ValueError(f"cli.print alias '{profile.alias}' supports host placement only")
    if profile.cli_binary is None:
        raise ValueError(f"Claude alias '{profile.alias}' requires cli_binary")
    from .provider_jail import build_jail_command
    try:
        build_jail_command(profile.cli_binary, [])
    except ValueError as exc:
        raise ValueError(f"Claude alias '{profile.alias}' has an invalid cli_binary: {exc}") from exc
    if preset.anthropic_base_url and not preset.anthropic_auth_token_env and not preset.anthropic_auth_token_secret:
        raise ValueError(f"Claude alias '{profile.alias}' with a custom endpoint requires anthropic_auth_token_env or secret credential")
    credential_secret: str | None = None
    if preset.anthropic_auth_token_secret:
        credential_mode = "secret"
        credential_env = None
        credential_secret = preset.anthropic_auth_token_secret
        credential_name = "ANTHROPIC_AUTH_TOKEN"
    elif preset.anthropic_auth_token_env:
        credential_mode = "environment"
        credential_env = preset.anthropic_auth_token_env
        credential_name = "ANTHROPIC_AUTH_TOKEN"
    else:
        credential_mode = profile.credential_mode or "runtime_managed"
        credential_env = profile.credential_env
        credential_name = profile.credential_name
    subscription_only = preset.route is not None and preset.subscription_only and preset.provider == "claude"
    declared = config.models.get(profile.alias)
    if declared is not None and declared.anthropic_base_url is not None:
        subscription_only = subscription_only and declared.anthropic_base_url == preset.anthropic_base_url
    return NativeRuntimeProfile(
        provider="claude", model=preset.model or "", cli_binary=profile.cli_binary,
        base_url=preset.anthropic_base_url,
        credential_mode=credential_mode, credential_env=credential_env,
        credential_secret=credential_secret,
        credential_name=credential_name, compression_mode=profile.compression_mode,
        subscription_only=subscription_only,
    )


def resolve_agent_model(alias: str, settings: "NativeExecutionConfig | None" = None, *, config=None,
                        resolve_preferences: bool = True, resolve_prompts: bool = True):
    """models alias から Agent backend を決め、CLI 設定を全体既定から取得する。"""
    if settings is None:
        raise ValueError("Native execution configuration is unavailable")
    from .config import get_config
    selected_config = config if config is not None else get_config()
    if alias not in selected_config.models:
        raise ValueError(f"Unknown models alias: {alias}")
    if child_on_name(getattr(selected_config.models[alias], "on", "local")):
        raise ValueError("Child aliases run through the chat API")
    preset = selected_config.preset_for(alias, resolve_execution_environment=resolve_preferences)
    if preset.agent is None:
        raise ValueError("alias has no agent binding")
    from .agent_route import resolve_declared_agent
    return resolve_declared_agent(
        alias, preset, settings, selected_config,
        resolve_preferences=resolve_preferences, resolve_prompts=resolve_prompts,
    )


def agent_execution_mode(alias: str, settings: "NativeExecutionConfig | None" = None, *, config=None) -> str | None:
    """alias の Agent 実行 mode。解決できない alias は実行不可として公開する。"""
    try:
        resolved = resolve_agent_model(alias, settings, config=config, resolve_prompts=False)
    except (OSError, ValueError):
        return None
    return published_agent_execution(resolved.backend, getattr(resolved, "execution_environment", None))


def agent_model_allowed(alias: str, authority: CallerPolicy | ExecutionGrant | Any) -> bool:
    """Client または Grant の models / agent_models に alias があるときだけ許す。"""
    if "models" in getattr(type(authority), "model_fields", {}):
        return "*" in authority.models or alias in authority.models
    fields_set = getattr(authority, "model_fields_set", None)
    if fields_set is not None and "agent_models" not in fields_set:
        return False
    return "*" in authority.agent_models or alias in authority.agent_models


def agent_profile_allowed(alias: str, authority: CallerPolicy | ExecutionGrant,
                          settings: "NativeExecutionConfig | None" = None, *, config=None) -> bool:
    """alias を agent_models と、Caller なら places と max_surface で許す。"""
    if not agent_model_allowed(alias, authority):
        return False
    if config is None:
        from .config import get_config
        config = get_config()
    pool = getattr(config, "pools", None) or {}
    if alias in pool:
        return bool(agent_pool_candidates(alias, authority, settings, config))
    return _profile_within(alias, authority, settings, config)


def agent_pool_candidates(alias: str, authority: CallerPolicy | ExecutionGrant,
                          settings: "NativeExecutionConfig | None", config) -> list[str]:
    """Filter pool candidates by profile ceilings after checking the pool name."""
    pool = config.pools.get(alias)
    if pool is None:
        return []
    return [candidate for candidate in pool.candidates
            if _profile_within(candidate, authority, settings, config)]


def _blocked_agent_place(alias: str, authority: CallerPolicy | ExecutionGrant,
                         settings: "NativeExecutionConfig | None", config) -> str | None:
    preset = (getattr(config, "models", None) or {}).get(alias)
    places = getattr(authority, "places", None)
    if places is None or getattr(preset, "agent", None) is None:
        return None
    on = getattr(preset, "on", "local")
    if child_on_name(on):
        return None if "child" in places or on in places else on
    place = preset.agent.place
    if (mode := agent_execution_mode(alias, settings, config=config)) is not None:
        place = "isolated" if mode == "sandbox" else "host"
    return place if place not in places else None


def agent_place_blockers(alias: str, authority: CallerPolicy,
                         settings: "NativeExecutionConfig | None", *, config) -> list[str]:
    """Return the required places when every candidate fails the place gate."""
    pool = config.pools.get(alias)
    candidates = pool.candidates if pool is not None else [alias]
    blocked = [_blocked_agent_place(candidate, authority, settings, config) for candidate in candidates]
    if any(place is None for place in blocked):
        return []
    return list(dict.fromkeys(place for place in blocked if place is not None))


def _profile_within(alias: str, authority: CallerPolicy | ExecutionGrant,
                    settings: "NativeExecutionConfig | None", config) -> bool:
    preset = (getattr(config, "models", None) or {}).get(alias)
    if preset is None:
        return True
    allow = getattr(preset, "allow", None)
    if allow is not None and getattr(allow, "full", False) and not getattr(authority, "allow_full", False):
        return False
    if getattr(preset, "agent", None) is None:
        return True
    if _blocked_agent_place(alias, authority, settings, config) is not None:
        return False
    max_surface = getattr(authority, "max_surface", None)
    if max_surface is not None:
        if surface_exceeds(_alias_surface_ceiling(alias, settings, config=config), max_surface):
            return False
    return True


def caller_allows_isolation(policy: CallerPolicy, isolation: str) -> bool:
    """要求の isolation を Caller の places へ写す。sandbox は isolated、native は host。"""
    place = "isolated" if isolation == "sandbox" else "host"
    return place in policy.places


def permitted_agent_execution(alias: str, policy: CallerPolicy | None,
                              settings: "NativeExecutionConfig | None", *, config=None) -> str | None:
    """Caller の alias 許可と place / surface 上限を通過した実行 mode だけ返す。"""
    if policy is None or settings is None or not agent_profile_allowed(alias, policy, settings, config=config):
        return None
    return agent_execution_mode(alias, settings, config=config)


def _alias_surface_ceiling(alias: str, settings, *, config) -> str:
    """Caller の max_surface と比べる上限。宣言が無ければ今の解決から導く。"""
    preset = config.preset_for(alias, resolve_execution_environment=False)
    if preset.agent is not None:
        return preset.agent.surface
    try:
        resolved = resolve_agent_model(alias, settings, config=config, resolve_prompts=False)
    except (OSError, ValueError):
        return "write"
    if getattr(resolved, "provider", None) != "codex":
        return "write"
    from .agent_spec import admitted_execution_cell

    cell = admitted_execution_cell("codex", getattr(resolved, "codex_execution_mode", None))
    if cell is not None:
        return cell.surface
    return "write"


class SharedWorkspaceBinding(ContractModel):
    """Kotoyose と remote root が同じ実体を読むための管理者固定 binding。"""

    caller_workspace_ref: Ref
    remote_workspace_ref: str = Field(min_length=1, max_length=512)
    local_path: Path
    challenge_relative: str = Field(min_length=1, max_length=256)
    challenge_digest: str = Field(pattern=_DIGEST_PATTERN)
    remote_workspace_generation: int = Field(ge=0, default=0)

    @model_validator(mode="after")
    def validate_binding(self) -> SharedWorkspaceBinding:
        relative = PurePosixPath(self.challenge_relative)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("workspace challenge must be relative")
        return self

    def public_snapshot(self) -> dict:
        """binding snapshot から Kotoyose の絶対 path を除く。"""
        return {
            "caller_workspace_ref": self.caller_workspace_ref,
            "remote_workspace_ref": self.remote_workspace_ref,
            "remote_workspace_generation": self.remote_workspace_generation,
            "challenge_relative": self.challenge_relative,
            "challenge_digest": self.challenge_digest,
        }


class RemoteWorkerConnection(ContractModel):
    """管理者が enrollment 済み TLS identity と秘密鍵 path を固定する。"""

    worker: RemoteWorkerRef
    endpoint: str = Field(pattern=r"^https://[^/?#]+:[0-9]+$")
    ca_file: Path
    certificate_file: Path
    key_file: Path
    journal_path: Path

    @model_validator(mode="after")
    def validate_paths(self) -> RemoteWorkerConnection:
        parsed = urlsplit(self.endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.port is None or parsed.username or parsed.password:
            raise ValueError("remote endpoint must be an https origin without credentials")
        for path in (self.ca_file, self.certificate_file, self.key_file, self.journal_path):
            if not path.is_absolute():
                raise ValueError("remote TLS and journal paths must be absolute")
        if not self.journal_path.is_symlink() and self.journal_path.exists() and self.journal_path.stat().st_mode & 0o077:
            raise ValueError("remote journal must be private")
        return self


class WorkerCapabilityConfig(ContractModel):
    """管理者が登録する Worker の実能力。Caller の要求で拡張しない。"""

    worker_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    os: Literal["linux", "windows", "macos"]
    applications: list[str] = Field(default_factory=list, max_length=256)
    gpu: Literal["none", "nvidia", "amd", "intel"] = "none"
    isolation_modes: list[Literal["sandbox", "native"]] = Field(min_length=1)
    native: bool = False
    workspace_locality: Literal["local", "remote"] = "local"
    remote_connection: RemoteWorkerConnection | None = None
    workspace_bindings: list[SharedWorkspaceBinding] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def validate_registered_worker(self) -> WorkerCapabilityConfig:
        """現在の Local backend が実際に検証できる capability だけを受け付ける。"""
        if self.native and "native" not in self.isolation_modes:
            raise ValueError("native worker must support native isolation")
        if self.worker_id == "local-docker":
            if self.os != "linux" or self.workspace_locality != "local" or self.gpu != "none":
                raise ValueError("local-docker supports only Linux local CPU sandbox execution")
            if self.isolation_modes != ["sandbox"] or self.native:
                raise ValueError("local-docker has fixed sandbox capability")
            if self.applications:
                raise ValueError("local-docker application inventory is not verified")
        elif self.worker_id == "local-print-cli":
            if (self.os != "linux" or self.workspace_locality != "local" or self.gpu != "none"
                    or self.isolation_modes != ["native"] or not self.native or self.applications):
                raise ValueError("local-print-cli supports only Linux host print CLI execution")
        elif self.worker_id.startswith("remote-"):
            if self.remote_connection is None:
                raise ValueError("Unknown Worker ID: remote worker requires an explicit connection")
            if self.os != "linux" or self.workspace_locality != "remote" or self.gpu != "none":
                raise ValueError("remote worker supports only Linux shared-filesystem CPU sandbox execution")
            if self.isolation_modes != ["sandbox"] or self.native:
                raise ValueError("remote worker has fixed sandbox capability")
            if self.applications:
                raise ValueError("Unknown Worker ID: remote worker requires an explicit connection and no unverified applications")
            if self.remote_connection.worker.worker_id != self.worker_id:
                raise ValueError("remote connection worker ID does not match registry key")
            if not self.workspace_bindings:
                raise ValueError("remote worker requires an explicit shared workspace binding")
        else:
            raise ValueError(f"Unknown Worker ID: {self.worker_id}")
        return self

    def public_snapshot(self) -> dict:
        """実行 snapshot へ秘密鍵 path を複製せず worker identity を残す。"""
        payload = self.model_dump(mode="json")
        connection = payload.get("remote_connection")
        if connection is not None:
            payload["remote_connection"] = {
                "worker": {
                    "worker_id": connection["worker"]["worker_id"],
                    "generation": connection["worker"]["generation"],
                    "principal": connection["worker"]["principal"],
                    "certificate_fingerprint": connection["worker"]["certificate_fingerprint"],
                    "server_certificate_fingerprint": connection["worker"].get("server_certificate_fingerprint"),
                },
                "endpoint": connection["endpoint"],
            }
            payload["workspace_bindings"] = [
                item.public_snapshot() for item in self.workspace_bindings
            ]
        return payload


def default_worker_capabilities() -> dict[str, WorkerCapabilityConfig]:
    """既存 LocalWorker の実装に対応する保守的な管理者初期値。"""
    return {
        "local-docker": WorkerCapabilityConfig(
            worker_id="local-docker", os="linux", isolation_modes=["sandbox"],
        ),
    }


class ContainerCleanupLimits(ContractModel):
    """Docker command container の定期回収に使う上限と猶予。"""

    interval_seconds: int = Field(default=300, ge=60, le=86400)
    unknown_grace_seconds: int = Field(default=3600, ge=60, le=7 * 86400)
    batch_size: int = Field(default=100, ge=1, le=1000)


class AgentContainerConfig(ContractModel):
    """ネット隔離 container で Agent CLI を動かす資源と配置の上限。明示設定時だけ存在する。"""

    agent_root: str = Field(min_length=1)
    home_root: str = Field(min_length=1)
    # 機械ごとの既定。KOTOYOSE_EXECUTION_CONFIG で上書きできる。
    workspace_memory_bytes: int = Field(default=6 * 1024**3, gt=0)
    cpus: float = Field(default=2, gt=0)
    pids_limit: int = Field(default=2048, gt=0)
    min_free_disk_bytes: int = Field(default=20 * 1024**3, ge=0)
    max_containers: int = Field(default=2, ge=1)
    # UNIX socket path は 108 byte 上限。Run id 36 文字 + ".sock" を足しても 100 byte 以内に収める。
    egress_socket_root: str = Field(default="/run/kotoyose", min_length=1)

    @field_validator("agent_root", "home_root", "egress_socket_root")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("agent container paths must be absolute")
        return value

    @field_validator("home_root")
    @classmethod
    def home_root_is_dedicated(cls, value: str) -> str:
        # 起動時の掃除が kotoyose-agent-home-* を消すので、共有の dir は置き場にしない。
        if value.rstrip("/") in {"", "/tmp", "/var/tmp", "/dev/shm"}:
            raise ValueError("home_root must be a directory dedicated to Kotoyose")
        return value

    @field_validator("egress_socket_root")
    @classmethod
    def socket_root_is_short(cls, value: str) -> str:
        if len(value.encode()) > 48:
            raise ValueError("egress_socket_root leaves no room for a UNIX socket path under 100 bytes")
        return value


def authenticate_client(key_client: str, *, clients: Mapping[str, Any] | None = None) -> tuple[str, Any]:
    """Resolve the client bound to a verified API key."""
    from .config import get_clients

    client_map = get_clients() if clients is None else clients
    if key_client not in client_map:
        raise ExecutionError("authentication_failed", "Invalid client credential", 401)
    return key_client, client_map[key_client]


class NativeExecutionConfig(ContractModel):
    """明示設定時だけ Native API を有効化する。DB と実 worker は必須。"""

    schema_version: Literal["1"]
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    drivers: dict[str, DriverInstall] | None = Field(default=None, exclude_if=lambda value: value is None)
    agent_compression_mode: Literal["runtime_managed", "caller_controlled"] = "runtime_managed"
    # 公式の口の外の route (needs_ack) を止める範囲。機械ごとの方針で、admin UI が書き換える config.yml ではなくここに置く。
    sanction_enforcement: Literal["off", "agent", "all"] = "off"
    mcp_servers: dict[str, ExternalMcpServerConfig] = Field(default_factory=dict, max_length=32)
    artifact_storage: ArtifactStorageConfig = Field(default_factory=ArtifactStorageConfig)
    artifact_disk_limits: ArtifactDiskLimits | None = None
    native_claude_session_store: Literal['postgres'] | None = None
    native_codex_session_root: Path | None = None
    input_artifact_limits: InputArtifactLimits = Field(default_factory=InputArtifactLimits)
    output_artifact_limits: OutputArtifactLimits = Field(default_factory=OutputArtifactLimits)
    workspace_limits: WorkspaceLimits = Field(default_factory=WorkspaceLimits)
    container_cleanup: ContainerCleanupLimits = Field(default_factory=ContainerCleanupLimits)
    # 明示されたときだけ、部屋の同時数と空き disk を受付で読む。
    agent_container: AgentContainerConfig | None = None
    worker_capabilities: dict[str, WorkerCapabilityConfig] = Field(
        default_factory=default_worker_capabilities, min_length=1,
    )

    @model_validator(mode="before")
    @classmethod
    def reject_replaced_execution_keys(cls, value):
        """消した実行設定の key は黙って捨てず、置き換えた先の名前で拒む。"""
        if isinstance(value, dict) and "callers" in value:
            raise ValueError("callers moved to config.yml clients (ADR-0011)")
        return _reject_replaced_keys(value, {
            "agent_sandbox_profiles": "alias agent",
            "agent_cli_binary": "drivers",
            "agent_credential_mode": "connections",
            "agent_credential_env": "connections",
            "agent_credential_name": "connections",
        })

    @model_validator(mode="after")
    def worker_registry_is_consistent(self) -> NativeExecutionConfig:
        """設定 key と Worker identity を一致させ、未実装 backend を登録しない。"""
        if self.drivers is not None:
            for name in self.drivers:
                try:
                    driver_manifest(name)
                except KeyError as exc:
                    raise ValueError(f"unknown driver: {name}") from exc
            print_cli = self.drivers.get("cli.print")
            if print_cli is not None and "cli_binary" in print_cli.model_fields_set:
                raise ValueError("Set drivers.cli.print.profile_binaries.<profile> instead of drivers.cli.print.cli_binary")
            claude = self.drivers.get("claude.agent-sdk")
            if claude is not None and claude.cli_binary is None:
                claude.cli_binary = bundled_claude_cli()
        for key, worker in self.worker_capabilities.items():
            if key != worker.worker_id:
                raise ValueError("Worker registry key must equal worker_id")
        self.worker_capabilities.setdefault("local-print-cli", WorkerCapabilityConfig(
            worker_id="local-print-cli", os="linux", isolation_modes=["native"], native=True,
        ))
        return self

    def validate_client_resources(self, *, clients: Mapping[str, Any] | None = None) -> None:
        """Client の resource 参照と外部 MCP の起動条件を検証する。"""
        from .config import get_clients

        client_map = get_clients() if clients is None else clients
        for policy in client_map.values():
            if set(policy.mcp_servers) - self.mcp_servers.keys():
                raise ValueError("Client policy references an unknown MCP server")
            for root in policy.workspace_roots:
                resolved = root.resolve(strict=True)
                if not resolved.is_dir() or Path(__file__).resolve().is_relative_to(resolved):
                    raise ValueError("Workspace root must exclude the controller installation")
        for server in self.mcp_servers.values():
            if server.credential_env and not process_env.get(server.credential_env):
                raise ValueError("MCP server requires its configured credential environment variable")
