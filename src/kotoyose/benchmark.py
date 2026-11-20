"""再現可能な Native Execution benchmark の manifest、runner、CLI。

Benchmark はモデルの順位表ではなく、固定した Task / Definition / Profile /
Runtime の組合せを Kotoyose の typed API に一度ずつ送る薄い orchestration 層である。
外部副作用は manifest で分離を宣言し、実行時にも拒否する。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping, Protocol, Sequence

import httpx
from pydantic import ConfigDict, Field, field_validator, model_validator

from .artifact_retention import ArtifactRetentionChange
from .benchmark_policy import external_fixture_ready
from .benchmark_reporting import (
    kotoyose_metrics, caller_metrics, case_variations, dataset_summary, event_metrics,
    pinned_replay_payload, replay_constraints, replay_summary,
)
from .benchmark_retention import request_retention
from .execution_contract import ContractModel, ExecutionEnvelope
from .execution_resources import content_digest
from .execution_snapshot import ResolvedAttemptSnapshot
from .workspace_retention import WorkspaceRetentionChange


_DIGEST = r"^(?:sha256:)?[0-9a-f]{64}$"
_REVISION = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"


class BenchmarkError(ValueError):
    """Benchmark の入力または実行証跡が契約を満たさない。"""


class FailureClassification(StrEnum):
    """Attempt の結果を dataset 間で比較する安定した分類。"""

    SUCCESS = "success"
    VERIFICATION_FAILED = "verification_failed"
    REPAIR_REQUIRED = "repair_required"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    WORKER_LOST = "worker_lost"
    INFRA_FAILURE = "infra_failure"
    OUTCOME_UNKNOWN = "outcome_unknown"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    CONTRACT_ERROR = "contract_error"
    REPAIR_EFFECTIVE = "repair_effective"
    FINAL_FAILURE = "final_failure"
    INTERRUPTED = "interrupted"
    FALSE_POSITIVE = "false_positive"
    ENVIRONMENT_FAILURE = "environment_failure"


class ReplayConstraint(ContractModel):
    """内容またはアクセス権を保持できず、検証済み replay にできない pin。"""

    pin: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_]*$")
    reason: str = Field(min_length=1, max_length=2048)


class ReplayPins(ContractModel):
    """実行を再現する immutable source / runtime の全 pin。"""

    repository_revision: str = Field(pattern=_REVISION)
    untracked_inputs_digest: str | None = Field(default=None, pattern=_DIGEST)
    task_definition_digest: str = Field(pattern=_DIGEST)
    acceptance_digest: str = Field(pattern=_DIGEST)
    provider: str | None = Field(default=None, max_length=128)
    provider_state: Literal["resolved", "not_applicable"] = "resolved"
    model: str | None = Field(default=None, max_length=256)
    model_state: Literal["resolved", "not_applicable"] = "resolved"
    prompt_digest: str | None = Field(default=None, pattern=_DIGEST)
    profile_digest: str = Field(pattern=_DIGEST)
    scope_digest: str = Field(pattern=_DIGEST)
    agent_definition_digest: str | None = Field(default=None, pattern=_DIGEST)
    preset_resolution_digest: str = Field(pattern=_DIGEST)
    runtime_policy_digest: str | None = Field(default=None, pattern=_DIGEST)
    cli_digest: str | None = Field(default=None, pattern=_DIGEST)
    runtime_version: str = Field(min_length=1, max_length=256)
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    lockfile_digest: str = Field(pattern=_DIGEST)
    skill_schema_digests: tuple[str, ...] = ()
    mcp_schema_digests: tuple[str, ...] = ()
    tool_schema_digests: tuple[str, ...] = ()
    pack_generator: str = Field(min_length=1, max_length=256)
    memory_snapshot_digest: str | None = Field(default=None, pattern=_DIGEST)
    fixture_digest: str = Field(pattern=_DIGEST)
    external_response_fixture_digest: str | None = Field(default=None, pattern=_DIGEST)
    unreplayable_inputs: tuple[str, ...] = ()
    replay_constraints: tuple[ReplayConstraint, ...] = ()

    @model_validator(mode="after")
    def applicable_runtime_pins(self) -> ReplayPins:
        """command の provider / model 等は not_applicable として明示する。"""
        if self.provider_state == "resolved" and not self.provider:
            raise ValueError("resolved provider pin requires provider")
        if self.provider_state == "not_applicable" and self.provider is not None:
            raise ValueError("not_applicable provider pin must be null")
        if self.model_state == "resolved" and not self.model:
            raise ValueError("resolved model pin requires model")
        if self.model_state == "not_applicable" and self.model is not None:
            raise ValueError("not_applicable model pin must be null")
        return self

    @field_validator("model")
    @classmethod
    def explicit_model(cls, value: str | None) -> str | None:
        """順位付け用 alias を固定 runtime の pin として受け付けない。"""
        if value is not None and value.lower() in {"best", "default", "inherit", "latest", "auto"}:
            raise ValueError("benchmark model must be an explicit model identifier")
        return value

    @field_validator(
        "skill_schema_digests", "mcp_schema_digests", "tool_schema_digests",
    )
    @classmethod
    def digest_list(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """schema pin の形式と重複を固定する。"""
        if len(set(values)) != len(values) or any(
            re.fullmatch(_DIGEST, value) is None for value in values
        ):
            raise ValueError("schema digest lists must contain unique SHA-256 digests")
        return values

    @field_validator("replay_constraints")
    @classmethod
    def distinct_constraints(
        cls, values: tuple[ReplayConstraint, ...],
    ) -> tuple[ReplayConstraint, ...]:
        """同じ pin の制約を複数理由へ分散させず、report の判定を一意にする。"""
        if len({value.pin for value in values}) != len(values):
            raise ValueError("replay constraint pins must be distinct")
        return values


class RepetitionCacheCondition(ContractModel):
    """一つの repetition を開始するときに Caller が揃える cache 状態。"""

    warmup: Literal["cold", "warm", "recorded"]
    reuse: Literal["disabled", "within_manifest", "prewarmed"]
    cache_snapshot_digest: str | None = Field(default=None, pattern=_DIGEST)


class CacheConditions(ContractModel):
    """cold / warm と cache key の条件を記録する。"""

    key_digest: str = Field(pattern=_DIGEST)
    key_material: dict[str, Any]
    warmup: str
    reuse: str
    repetition_conditions: tuple[RepetitionCacheCondition, ...] = ()

    @field_validator("warmup")
    @classmethod
    def valid_warmup(cls, value: str) -> str:
        if value not in {"cold", "warm", "recorded"}:
            raise ValueError("warmup must be cold, warm, or recorded")
        return value

    @field_validator("reuse")
    @classmethod
    def valid_reuse(cls, value: str) -> str:
        if value not in {"disabled", "within_manifest", "prewarmed"}:
            raise ValueError("reuse must be disabled, within_manifest, or prewarmed")
        return value

    def condition_for(self, repetition: int) -> RepetitionCacheCondition:
        """旧 manifest は case 共通条件を各 repetition の条件として解釈する。"""
        if self.repetition_conditions:
            return self.repetition_conditions[repetition - 1]
        return RepetitionCacheCondition(warmup=self.warmup, reuse=self.reuse)


class SideEffectPolicy(ContractModel):
    """benchmark から実外部変更を排除する宣言。"""

    mode: str
    response_fixture_ref: str | None = None
    external_calls: Literal["none", "fixture_replay"] | None = None
    mutation_replay: Literal["forbidden"] = "forbidden"

    @field_validator("mode")
    @classmethod
    def isolated_mode(cls, value: str) -> str:
        if value not in {"isolated_mock", "read_only"}:
            raise ValueError("benchmark side effects must be isolated_mock or read_only")
        return value

    @model_validator(mode="after")
    def fixture_for_mock(self) -> SideEffectPolicy:
        """mock 応答も、固定 fixture の参照なしには再現可能と扱わない。"""
        if self.mode == "isolated_mock" and not self.response_fixture_ref:
            raise ValueError("isolated_mock requires response_fixture_ref")
        if self.external_calls == "fixture_replay" and not self.response_fixture_ref:
            raise ValueError("fixture_replay requires response_fixture_ref")
        return self


class ArtifactRetentionReference(ContractModel):
    """既存の output Artifact retention CAS へ渡す保持要求。"""

    kind: Literal["artifact"]
    ref: str = Field(min_length=1)
    request: ArtifactRetentionChange


class WorkspaceRetentionReference(ContractModel):
    """既存の workspace retention CAS へ渡す保持要求。"""

    kind: Literal["workspace"]
    ref: str = Field(min_length=1)
    request: WorkspaceRetentionChange

    @model_validator(mode="after")
    def current_workspace_ref(self) -> WorkspaceRetentionReference:
        """CAS body と endpoint が別 workspace を指す要求を送らない。"""
        if self.request.workspace_ref != self.ref:
            raise ValueError("workspace retention request must match ref")
        return self


class FixtureRetentionReference(ContractModel):
    """更新 API がない fixture store に対して Caller が維持する保持理由。"""

    kind: Literal["fixture"]
    ref: str = Field(min_length=1)
    pin: str = Field(min_length=1)
    proof_ref: str = Field(min_length=1)


RetentionReference = Annotated[
    ArtifactRetentionReference | WorkspaceRetentionReference | FixtureRetentionReference,
    Field(discriminator="kind"),
]


class AcceptanceSpec(ContractModel):
    """runtime success と分離して評価する command acceptance。"""

    exit_code: int | None = None
    required_artifacts: tuple[str, ...] = ()
    artifact_digests: dict[str, str] = Field(default_factory=dict)
    required_json_fields: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @field_validator("artifact_digests")
    @classmethod
    def valid_artifact_digests(cls, values: dict[str, str]) -> dict[str, str]:
        if any(re.fullmatch(_DIGEST, value) is None for value in values.values()):
            raise ValueError("artifact_digests must contain SHA-256 digests")
        return values


class ArtifactPayload(ContractModel):
    """Artifact GET の bytes と HTTP metadata。"""

    content: bytes
    media_type: str = Field(min_length=1)
    digest: str = Field(pattern=_DIGEST)


class NativeUnsupportedManifest(ContractModel):
    """能力不足を fallback せず保存する manifest。"""

    required_capabilities: tuple[str, ...]
    observed: dict[str, dict[str, Any]]
    reason: str = Field(min_length=1)


class DatasetCase(ContractModel):
    """Task 定義と exact typed requests の一つの比較条件。"""

    case_id: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    task_definition: dict[str, Any]
    acceptance: AcceptanceSpec
    expected_failure_classification: FailureClassification = FailureClassification.SUCCESS
    requests: list[ExecutionEnvelope] = Field(min_length=1, max_length=20)
    pins: ReplayPins
    cache: CacheConditions
    side_effects: SideEffectPolicy
    retention_references: list[RetentionReference] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def request_is_native_or_explicit(self) -> DatasetCase:
        """各 repetition を独立した immutable request として保持する。"""
        if len({request.execution.idempotency_key for request in self.requests}) != len(self.requests):
            raise ValueError("each benchmark repetition requires a distinct idempotency key")
        if self.pins.task_definition_digest.removeprefix("sha256:") != content_digest(self.task_definition):
            raise ValueError("task_definition_digest does not match task_definition")
        if self.pins.acceptance_digest.removeprefix("sha256:") != content_digest(self.acceptance.model_dump(mode="json")):
            raise ValueError("acceptance_digest does not match acceptance")
        request_digests = [content_digest(request.model_dump(mode="json")) for request in self.requests]
        pin_payloads = [self.pins.model_dump(mode="json")]
        compatible_pins = pinned_replay_payload(self.pins)
        if compatible_pins != pin_payloads[0]:
            pin_payloads.append(compatible_pins)
        expected_key_materials = [{
            "request_digests": request_digests, "pins_digest": content_digest(pin_payload),
            "warmup": self.cache.warmup, "reuse": self.cache.reuse,
        } for pin_payload in pin_payloads]
        if self.cache.repetition_conditions:
            if len(self.cache.repetition_conditions) != len(self.requests):
                raise ValueError("repetition cache conditions must match request count")
            for material in expected_key_materials:
                material["repetition_conditions"] = [
                    value.model_dump(mode="json") for value in self.cache.repetition_conditions
                ]
        if self.cache.key_material not in expected_key_materials:
            raise ValueError("cache key_material does not match requests, pins, and cache conditions")
        if self.cache.key_digest.removeprefix("sha256:") != content_digest(self.cache.key_material):
            raise ValueError("cache key_digest does not match key_material")
        if (
            self.side_effects.external_calls == "fixture_replay"
            and self.pins.external_response_fixture_digest is None
        ):
            raise ValueError("fixture_replay requires external_response_fixture_digest")
        identities = [(value.kind, value.ref) for value in self.retention_references]
        if len(identities) != len(set(identities)):
            raise ValueError("retention references must be distinct")
        return self


class BenchmarkManifest(ContractModel):
    """保存・交換する replay manifest。"""

    schema_version: str = "1"
    suite_id: str = Field(min_length=1, max_length=256)
    dataset_revision: str = Field(pattern=_REVISION)
    cases: list[DatasetCase] = Field(min_length=1, max_length=1000)

    @field_validator("schema_version")
    @classmethod
    def schema_v1(cls, value: str) -> str:
        if value != "1":
            raise ValueError("only benchmark manifest schema version 1 is supported")
        return value

    @property
    def digest(self) -> str:
        """manifest 自身を含めず canonical JSON から算出する。"""
        return content_digest(self.model_dump(mode="json"))


class ArtifactObservation(ContractModel):
    """Attempt が返す immutable artifact metadata。"""

    ref: str = Field(min_length=1)
    name: str = Field(min_length=1)
    digest: str = Field(pattern=_DIGEST)
    size: int = Field(ge=0)
    media_type: str = Field(min_length=1, max_length=256)


class AttemptObservation(ContractModel):
    """Kotoyose describe endpoint の benchmark に必要な subset。"""

    model_config = ConfigDict(extra="allow")

    schema_version: str = "1"
    attempt_id: str
    caller_run_ref: str
    workspace_ref: str
    workspace_generation: int
    status: str
    reason: str | None = None
    stop_confirmation: str | None = None
    side_effect_resolution: str | None = None
    exit_code: int | None = None
    sequence: int = 0
    usage: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactObservation] = Field(default_factory=list)
    artifact_manifest: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str | None = None
    finished_at: str | None = None


class BenchmarkRun(ContractModel):
    """一つの exact request の観測結果。"""

    case_id: str
    repetition: int = Field(ge=1)
    request_digest: str = Field(pattern=_DIGEST)
    pins_digest: str = Field(pattern=_DIGEST)
    attempt_id: str | None = None
    status: str
    classification: FailureClassification
    expected_classification: FailureClassification
    classification_match: bool
    runtime_success: bool
    acceptance_success: bool | None
    execution_certification: Literal["certified", "uncertified", "unsupported"]
    pin_verification: Literal["verified", "unverified", "constrained"]
    observed_pins: dict[str, Any] | None = None
    replay_constraints: tuple[ReplayConstraint, ...] = ()
    reason: str | None = None
    stop_confirmation: str | None = None
    side_effect_resolution: str | None = None
    exit_code: int | None = None
    wall_seconds: float | None = Field(default=None, ge=0)
    active_seconds: float | None = Field(default=None, ge=0)
    wait_seconds: float | None = Field(default=None, ge=0)
    usage: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[ArtifactObservation] = Field(default_factory=list)
    cache: CacheConditions
    repetition_cache_condition: RepetitionCacheCondition
    side_effects: SideEffectPolicy
    unsupported: NativeUnsupportedManifest | None = None
    measurement_notes: tuple[str, ...] = ()

    @property
    def certification(self) -> Literal["certified", "uncertified", "unsupported"]:
        """旧 caller 向け alias。report wire では実行境界名を使用する。"""
        return self.execution_certification


class BenchmarkReport(ContractModel):
    """比較可能な benchmark artifact。"""

    schema_version: str = "1"
    suite_id: str
    manifest_digest: str = Field(pattern=_DIGEST)
    generated_at: datetime
    dataset_summary: dict[str, Any]
    warnings: tuple[str, ...]
    replay: list[dict[str, Any]]
    case_variation: list[dict[str, Any]]
    kotoyose_metrics: dict[str, Any]
    caller_metrics: dict[str, Any]
    retention_requests: list[dict[str, Any]]
    llm_reproducibility: Literal["not_guaranteed"] = "not_guaranteed"
    runs: list[BenchmarkRun]
    summary: dict[str, Any]
    reproducibility_limits: tuple[str, ...]


class NativeExecutionClient(Protocol):
    """Runner が必要とする Kotoyose HTTP 契約。"""

    async def capabilities(self) -> Mapping[str, Any]: ...

    async def submit(self, request: ExecutionEnvelope) -> str: ...

    async def describe(self, attempt_id: str) -> Mapping[str, Any]: ...

    async def events(self, attempt_id: str, after: int = 0) -> Mapping[str, Any]: ...

    async def artifact(self, ref: str) -> ArtifactPayload: ...

    async def retain(self, reference: RetentionReference) -> Mapping[str, Any]: ...


class HttpNativeExecutionClient:
    """Kotoyose v1 endpoint への最小 adapter。token は request transport にだけ存在する。"""

    def __init__(
        self, base_url: str, token: str, *, timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not token or any(character.isspace() for character in token):
            raise ValueError("Kotoyose token must be nonempty and contain no whitespace")
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
        )

    async def capabilities(self) -> Mapping[str, Any]:
        response = await self._http.get("/v1/capabilities")
        response.raise_for_status()
        return response.json()

    async def submit(self, request: ExecutionEnvelope) -> str:
        response = await self._http.post("/v1/executions", json=request.model_dump(mode="json"))
        response.raise_for_status()
        value = response.json()
        attempt_id = value.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise BenchmarkError("Kotoyose response omitted attempt_id")
        return attempt_id

    async def describe(self, attempt_id: str) -> Mapping[str, Any]:
        response = await self._http.get(f"/v1/attempts/{attempt_id}")
        response.raise_for_status()
        return response.json()

    async def events(self, attempt_id: str, after: int = 0) -> Mapping[str, Any]:
        response = await self._http.get(f"/v1/attempts/{attempt_id}/events", params={"after": after})
        response.raise_for_status()
        return response.json()

    async def artifact(self, ref: str) -> ArtifactPayload:
        response = await self._http.get(f"/v1/artifacts/{ref}")
        response.raise_for_status()
        etag = response.headers.get("etag", "").strip('"')
        return ArtifactPayload(content=response.content, media_type=response.headers.get("content-type", ""), digest=etag)

    async def retain(self, reference: RetentionReference) -> Mapping[str, Any]:
        """Manifest に固定した CAS body を既存 retention endpoint へそのまま渡す。"""
        if isinstance(reference, ArtifactRetentionReference):
            path = f"/v1/artifacts/{reference.ref}/retention"
        elif isinstance(reference, WorkspaceRetentionReference):
            path = f"/v1/workspaces/{reference.ref}/retention"
        else:
            raise BenchmarkError("fixture retention has no mutable Kotoyose API")
        response = await self._http.put(path, json=reference.request.model_dump(mode="json"))
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, Mapping):
            raise BenchmarkError("Kotoyose retention response is not an object")
        return value

    async def resolved_snapshot(self, attempt_id: str) -> ResolvedAttemptSnapshot:
        """Attempt 所有者に束縛された read-only projection を取得する。"""
        response = await self._http.get(f"/v1/attempts/{attempt_id}/snapshot")
        response.raise_for_status()
        return ResolvedAttemptSnapshot.model_validate(response.json())

    async def aclose(self) -> None:
        await self._http.aclose()


def _required_capabilities(request: ExecutionEnvelope) -> tuple[str, ...]:
    requirements = request.execution.requirements
    values = list(requirements.capabilities)
    if requirements.isolation == "native" and "native" not in values:
        values.append("native")
    return tuple(dict.fromkeys(values))


def _unsupported(capabilities: Mapping[str, Any], required: Sequence[str]) -> NativeUnsupportedManifest | None:
    values = capabilities.get("capabilities", capabilities)
    if not isinstance(values, Mapping):
        raise BenchmarkError("Kotoyose capabilities response is not an object")
    observed: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for name in required:
        support = values.get(name)
        if not isinstance(support, Mapping):
            missing.append(name)
            observed[name] = {"status": "missing", "reason": "capability was absent"}
            continue
        observed[name] = dict(support)
        if support.get("status") != "supported":
            missing.append(name)
    if not missing:
        return None
    return NativeUnsupportedManifest(
        required_capabilities=tuple(missing), observed=observed,
        reason="A required capability is unsupported; no fallback execution was attempted.",
    )


def _classification(snapshot: AttemptObservation, acceptance_success: bool | None) -> FailureClassification:
    if snapshot.status == "succeeded":
        if snapshot.stop_confirmation != "confirmed" or snapshot.side_effect_resolution != "resolved":
            return FailureClassification.OUTCOME_UNKNOWN
        if acceptance_success is False:
            return FailureClassification.VERIFICATION_FAILED
        return FailureClassification.SUCCESS
    if snapshot.reason == "output_contract_error":
        return FailureClassification.CONTRACT_ERROR
    try:
        return FailureClassification(snapshot.status)
    except ValueError:
        if snapshot.reason in {"deadline_exceeded", "hard_timeout"}:
            return FailureClassification.TIMEOUT
        if snapshot.reason in {"worker_failure", "provider_failure"}:
            return FailureClassification.INFRA_FAILURE
        return FailureClassification.OUTCOME_UNKNOWN


async def _evaluate_acceptance(
    acceptance: AcceptanceSpec,
    snapshot: AttemptObservation,
    client: NativeExecutionClient,
) -> tuple[bool, tuple[str, ...]]:
    """exit code と artifact bytes の実値を検査し、Attempt status を信頼しない。"""
    notes: list[str] = []
    if acceptance.exit_code is not None and snapshot.exit_code != acceptance.exit_code:
        return False, ("exit_code did not satisfy acceptance",)
    artifacts = {artifact.name: artifact for artifact in snapshot.artifacts}
    names = set(acceptance.required_artifacts) | set(acceptance.artifact_digests) | set(acceptance.required_json_fields)
    missing = names - artifacts.keys()
    if missing:
        return False, (f"required artifacts missing: {','.join(sorted(missing))}",)
    payloads: dict[str, ArtifactPayload] = {}
    for name in names:
        artifact = artifacts[name]
        try:
            payload = await client.artifact(artifact.ref)
        except Exception:
            return False, (f"artifact {name} could not be read for digest verification",)
        actual = hashlib.sha256(payload.content).hexdigest()
        if actual != artifact.digest.removeprefix("sha256:") or payload.digest.removeprefix("sha256:") != actual:
            return False, (f"artifact {name} bytes digest did not match metadata",)
        if len(payload.content) != artifact.size or payload.media_type != artifact.media_type:
            return False, (f"artifact {name} bytes metadata did not match",)
        expected = acceptance.artifact_digests.get(name)
        if expected is not None and expected.removeprefix("sha256:") != actual:
            return False, (f"artifact {name} did not match required digest",)
        payloads[name] = payload
    for name, paths in acceptance.required_json_fields.items():
        try:
            value: Any = json.loads(payloads[name].content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False, (f"artifact {name} was not valid JSON",)
        for path in paths:
            current = value
            for part in path.split("."):
                if not part or not isinstance(current, dict) or part not in current:
                    return False, (f"artifact {name} lacked required JSON field {path}",)
                current = current[part]
    if not names:
        notes.append("acceptance evaluated from terminal exit code only")
    return True, tuple(notes)


async def _drain_events(
    client: NativeExecutionClient, attempt_id: str, target_sequence: int,
) -> list[dict[str, Any]]:
    """cursor を進めて terminal sequence まで読み、Attempt identity を検証する。"""
    cursor = 0
    events: dict[int, dict[str, Any]] = {}
    while cursor < target_sequence:
        page = await client.events(attempt_id, cursor)
        if not isinstance(page, Mapping) or not isinstance(page.get("events"), list):
            raise BenchmarkError("Kotoyose events response is not a list")
        prior = cursor
        for raw in page["events"]:
            if not isinstance(raw, Mapping):
                raise BenchmarkError("Kotoyose event is not an object")
            event = dict(raw)
            if event.get("attempt_id") != attempt_id:
                raise BenchmarkError("Kotoyose event belongs to a different Attempt")
            sequence = event.get("sequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
                raise BenchmarkError("Kotoyose event sequence is invalid")
            if sequence > target_sequence:
                raise BenchmarkError("Kotoyose event is newer than the observed Attempt sequence")
            old = events.get(sequence)
            if old is not None and old != event:
                raise BenchmarkError("Kotoyose replay returned conflicting event payload")
            events[sequence] = event
        next_cursor = page.get("next_cursor")
        if isinstance(next_cursor, bool) or not isinstance(next_cursor, int) or next_cursor < cursor:
            raise BenchmarkError("Kotoyose event cursor is invalid")
        if next_cursor > target_sequence:
            raise BenchmarkError("Kotoyose event cursor passed the observed Attempt sequence")
        if events:
            next_cursor = max(next_cursor, max(events))
        cursor = next_cursor
        if cursor == prior:
            raise BenchmarkError("Kotoyose event cursor did not advance before terminal sequence")
    expected_sequences = set(range(1, target_sequence + 1))
    if set(events) != expected_sequences:
        raise BenchmarkError("Kotoyose event sequence has a gap before terminal Attempt")
    return [events[sequence] for sequence in sorted(events) if sequence <= target_sequence]


def _snapshot_evidence(
    snapshot: AttemptObservation, case: DatasetCase, request: ExecutionEnvelope,
    resolved: ResolvedAttemptSnapshot | None,
) -> tuple[Literal["certified", "uncertified"], Literal["verified", "unverified"], dict[str, Any] | None, tuple[str, ...]]:
    """保存済み read-only projection を宣言 pins と別に照合する。"""
    if resolved is None:
        return "uncertified", "unverified", None, (
            "resolved Grant/worker snapshot was not present in the Kotoyose wire response",)
    observed_pins = resolved.model_dump(mode="json")
    if (
        resolved.attempt_id != snapshot.attempt_id
        or resolved.attempt_sequence != snapshot.sequence
        # Kotoyose は判別済み ExecutionRequest を保存し、client は versioned envelope
        # の内側にそれを載せるため、digest の対象をそろえる。
        or resolved.request_digest != content_digest(request.execution.model_dump(mode="json"))
        or resolved.execution_grant_ref != request.execution.execution_grant_ref
    ):
        return "uncertified", "unverified", observed_pins, ("resolved snapshot identity did not match the Attempt request",)
    cert = (
        resolved.runtime_kind == "command" and resolved.isolation == "sandbox"
        and resolved.network == "none" and not resolved.external_capabilities
        and resolved.worker_capability_state == "resolved"
        and resolved.worker_os == "linux"
        and "sandbox" in resolved.worker_isolation_modes
    )
    if resolved.image_state == "resolved":
        cert = cert and resolved.image_digest == case.pins.image_digest
    else:
        cert = False
    observed_fields = {
        "provider": resolved.provider, "model": resolved.model,
        "image_digest": resolved.image_digest, "cli_digest": resolved.cli_digest,
        "sdk_version": resolved.sdk_version,
    }
    declared_fields = {field: getattr(case.pins, field, None) for field in ("provider", "model", "image_digest", "cli_digest")}
    # 投影には prompt / profile / scope / definition / runtime / lockfile pin が
    # 含まれないため、一致する部分集合だけでは全 pin を証明できない。
    comparable = all(
        value is None or declared_fields.get(field) == value
        for field, value in observed_fields.items()
        if field in declared_fields
    )
    declared_pin_names = {
        "repository_revision", "untracked_inputs_digest", "task_definition_digest",
        "acceptance_digest", "provider", "model", "prompt_digest", "profile_digest",
        "scope_digest", "agent_definition_digest", "preset_resolution_digest", "runtime_policy_digest", "cli_digest",
        "runtime_version", "image_digest", "lockfile_digest", "skill_schema_digests",
        "mcp_schema_digests", "tool_schema_digests", "pack_generator", "memory_snapshot_digest",
        "fixture_digest", "external_response_fixture_digest",
    }
    observed_pin_names = {"provider", "model", "image_digest", "cli_digest"}
    all_declared_pins_observed = declared_pin_names.issubset(observed_pin_names)
    pin_verified = comparable and all_declared_pins_observed
    note_parts = []
    if not cert:
        note_parts.append("resolved snapshot did not prove the fixed sandbox Grant and worker image")
    if not all_declared_pins_observed:
        note_parts.append("pin verification is incomplete because the Kotoyose projection omits declared pins")
    note = tuple(note_parts)
    return "certified" if cert else "uncertified", "verified" if pin_verified else "unverified", observed_pins, note


class BenchmarkRunner:
    """Manifest の順序・request・repetition を変更せず Kotoyose へ送る runner。"""

    def __init__(self, client: NativeExecutionClient, *, wait_timeout: float = 600.0, poll_seconds: float = 0.1):
        if wait_timeout <= 0 or poll_seconds <= 0:
            raise ValueError("wait_timeout and poll_seconds must be positive")
        self.client = client
        self.wait_timeout = wait_timeout
        self.poll_seconds = poll_seconds

    async def run(self, manifest: BenchmarkManifest) -> BenchmarkReport:
        capabilities = await self.client.capabilities()
        retention_records = await request_retention(self.client, manifest.cases)
        composition, warnings = dataset_summary(manifest.cases)
        runs: list[BenchmarkRun] = []
        self._halted = False
        for case in manifest.cases:
            if case.side_effects.mode not in {"isolated_mock", "read_only"}:
                raise BenchmarkError("real external side effects are forbidden by the benchmark runner")
            for repetition, request in enumerate(case.requests, 1):
                runs.append(await self._run_request(case, request, repetition, capabilities))
                if self._halted:
                    break
            if self._halted:
                break
        counts: dict[str, int] = {}
        for run in runs:
            counts[run.classification.value] = counts.get(run.classification.value, 0) + 1
        walls = [run.wall_seconds for run in runs if run.wall_seconds is not None]
        variations = case_variations(manifest.cases, runs)
        warnings.extend(
            f"Case {value['case_id']} has inconsistent cache/warmup conditions across repetitions."
            for value in variations if not value["cache_conditions_consistent"]
        )
        return BenchmarkReport(
            suite_id=manifest.suite_id,
            manifest_digest=manifest.digest,
            generated_at=datetime.now(timezone.utc),
            dataset_summary=composition, warnings=tuple(warnings),
            replay=replay_summary(manifest.cases, runs),
            case_variation=variations, kotoyose_metrics=kotoyose_metrics(runs, variations),
            caller_metrics=caller_metrics(), retention_requests=retention_records,
            runs=runs,
            summary={"total_runs": len(runs), "classifications": counts,
                     "runtime_successes": sum(run.runtime_success for run in runs),
                     "acceptance_successes": sum(run.acceptance_success is True for run in runs),
                     "certified_runs": sum(run.execution_certification == "certified" for run in runs),
                     "mean_wall_seconds": sum(walls) / len(walls) if walls else None},
            reproducibility_limits=(
                "LLM/provider output is not guaranteed bit-for-bit reproducible.",
                "Cost is reported by Kotoyose usage when available; unknown values remain unknown.",
                "Execution certification requires a typed resolved Grant/worker snapshot endpoint; pin verification is separate.",
                *(("Execution stopped after an unresolved Attempt; later repetitions were not started.",) if self._halted else ()),
            ),
        )

    async def _run_request(
        self, case: DatasetCase, request: ExecutionEnvelope,
        repetition: int, capabilities: Mapping[str, Any],
    ) -> BenchmarkRun:
        request_digest = content_digest(request.model_dump(mode="json"))
        pins_digest = str(case.cache.key_material["pins_digest"])
        constraints = replay_constraints(case.pins)
        pin_verification = "constrained" if constraints else "unverified"
        cache_condition = case.cache.condition_for(repetition)
        if not external_fixture_ready(case, request):
            unsupported = NativeUnsupportedManifest(
                required_capabilities=("external_response_fixture",), observed={},
                reason="External calls require a pinned response fixture; no execution was submitted.",
            )
            return BenchmarkRun(
                case_id=case.case_id, repetition=repetition, request_digest=request_digest,
                pins_digest=pins_digest, status="unsupported",
                classification=FailureClassification.UNSUPPORTED_CAPABILITY,
                expected_classification=case.expected_failure_classification,
                classification_match=(
                    case.expected_failure_classification == FailureClassification.UNSUPPORTED_CAPABILITY
                ),
                runtime_success=False, acceptance_success=None, execution_certification="unsupported",
                pin_verification=pin_verification, replay_constraints=constraints,
                reason=unsupported.reason, cache=case.cache,
                repetition_cache_condition=cache_condition,
                side_effects=case.side_effects, unsupported=unsupported,
            )
        if request.execution.kind != "command" or request.execution.requirements.isolation != "sandbox":
            unsupported = NativeUnsupportedManifest(
                required_capabilities=("certified_sandbox_command",), observed={},
                reason="Only sandbox command executions are currently benchmark-certified; native and agent requests are unsupported.",
            )
            return BenchmarkRun(
                case_id=case.case_id, repetition=repetition, request_digest=request_digest,
                pins_digest=pins_digest, status="unsupported", classification=FailureClassification.UNSUPPORTED_CAPABILITY,
                expected_classification=case.expected_failure_classification,
                classification_match=case.expected_failure_classification == FailureClassification.UNSUPPORTED_CAPABILITY,
                runtime_success=False, acceptance_success=None, execution_certification="unsupported",
                pin_verification=pin_verification, replay_constraints=constraints,
                reason=unsupported.reason, cache=case.cache,
                repetition_cache_condition=cache_condition,
                side_effects=case.side_effects, unsupported=unsupported,
            )
        unsupported = _unsupported(capabilities, _required_capabilities(request))
        if unsupported is not None:
            return BenchmarkRun(
                case_id=case.case_id, repetition=repetition, request_digest=request_digest,
                pins_digest=pins_digest, status="unsupported", classification=FailureClassification.UNSUPPORTED_CAPABILITY,
                expected_classification=case.expected_failure_classification,
                classification_match=case.expected_failure_classification == FailureClassification.UNSUPPORTED_CAPABILITY,
                runtime_success=False, acceptance_success=None, execution_certification="unsupported",
                pin_verification=pin_verification, replay_constraints=constraints,
                reason=unsupported.reason, cache=case.cache,
                repetition_cache_condition=cache_condition,
                side_effects=case.side_effects, unsupported=unsupported,
            )
        started = time.monotonic()
        attempt_id: str | None = None
        snapshot: AttemptObservation | None = None
        events: list[dict[str, Any]] = []
        try:
            async with asyncio.timeout(self.wait_timeout):
                attempt_id = await self.client.submit(request)
                while True:
                    snapshot = AttemptObservation.model_validate(await self.client.describe(attempt_id))
                    if snapshot.attempt_id != attempt_id:
                        raise BenchmarkError("Kotoyose describe response belongs to a different Attempt")
                    if snapshot.caller_run_ref != request.execution.caller_run_ref:
                        raise BenchmarkError("Kotoyose Attempt caller_run_ref does not match the request")
                    if snapshot.finished_at is not None:
                        if snapshot.sequence <= 0:
                            raise BenchmarkError("terminal Attempt did not expose an event sequence")
                        events = await _drain_events(self.client, attempt_id, snapshot.sequence)
                        if not any(event.get("type") == snapshot.status for event in events):
                            raise BenchmarkError("Kotoyose terminal event did not match Attempt status")
                        break
                    await asyncio.sleep(self.poll_seconds)
                acceptance_success, acceptance_notes = await _evaluate_acceptance(case.acceptance, snapshot, self.client)
        except asyncio.TimeoutError:
            self._halted = True
            return BenchmarkRun(
                case_id=case.case_id, repetition=repetition, request_digest=request_digest,
                pins_digest=pins_digest, attempt_id=attempt_id, status="observation_timeout",
                classification=FailureClassification.OUTCOME_UNKNOWN,
                expected_classification=case.expected_failure_classification,
                classification_match=case.expected_failure_classification == FailureClassification.OUTCOME_UNKNOWN,
                reason="benchmark wait timeout; no cancellation or redispatch was attempted",
                wall_seconds=time.monotonic() - started, events=events, cache=case.cache,
                repetition_cache_condition=cache_condition,
                side_effects=case.side_effects, runtime_success=False, acceptance_success=None,
                execution_certification="uncertified", pin_verification=pin_verification,
                replay_constraints=constraints,
                measurement_notes=("terminal state was not observed; later repetitions were not started",),
            )
        except Exception as exc:
            self._halted = True
            return BenchmarkRun(
                case_id=case.case_id, repetition=repetition, request_digest=request_digest,
                pins_digest=pins_digest, attempt_id=attempt_id, status="unknown",
                classification=FailureClassification.OUTCOME_UNKNOWN,
                expected_classification=case.expected_failure_classification,
                classification_match=False, reason=type(exc).__name__, wall_seconds=time.monotonic() - started,
                events=events, cache=case.cache, side_effects=case.side_effects,
                repetition_cache_condition=cache_condition,
                runtime_success=False, acceptance_success=None, execution_certification="uncertified",
                pin_verification=pin_verification, replay_constraints=constraints,
                measurement_notes=("Kotoyose response or event collection failed; outcome was not guessed",),
            )
        assert snapshot is not None
        active, wait, metric_notes = event_metrics(events)
        classification = _classification(snapshot, acceptance_success)
        if classification == FailureClassification.OUTCOME_UNKNOWN or snapshot.stop_confirmation != "confirmed" or snapshot.side_effect_resolution != "resolved":
            self._halted = True
        resolved = None
        snapshot_reader = getattr(self.client, "resolved_snapshot", None)
        if callable(snapshot_reader):
            try:
                resolved = await snapshot_reader(snapshot.attempt_id)
            except Exception:
                resolved = None
        certification, pin_verification, observed_pins, evidence_notes = _snapshot_evidence(
            snapshot, case, request, resolved,
        )
        if constraints:
            pin_verification = "constrained"
        reason = snapshot.reason
        if acceptance_success is False and reason is None:
            reason = "acceptance_failed"
        return BenchmarkRun(
            case_id=case.case_id, repetition=repetition, request_digest=request_digest,
            pins_digest=pins_digest, attempt_id=attempt_id, status=snapshot.status,
            classification=classification, expected_classification=case.expected_failure_classification,
            classification_match=classification == case.expected_failure_classification, reason=reason,
            runtime_success=snapshot.status == "succeeded", acceptance_success=acceptance_success,
            execution_certification=certification, pin_verification=pin_verification, observed_pins=observed_pins,
            stop_confirmation=snapshot.stop_confirmation, side_effect_resolution=snapshot.side_effect_resolution,
            exit_code=snapshot.exit_code, wall_seconds=time.monotonic() - started, active_seconds=active,
            wait_seconds=wait, usage=snapshot.usage, events=events, artifacts=snapshot.artifacts,
            cache=case.cache, repetition_cache_condition=cache_condition,
            side_effects=case.side_effects, replay_constraints=constraints,
            measurement_notes=metric_notes + acceptance_notes + evidence_notes,
        )


def _load_manifest(path: Path) -> BenchmarkManifest:
    """JSON manifest を厳格に読み込む。"""
    try:
        return BenchmarkManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchmarkError(f"invalid benchmark manifest: {exc}") from exc


async def _run_cli(args: argparse.Namespace) -> int:
    manifest = _load_manifest(Path(args.manifest))
    if args.base_url is None:
        result: dict[str, Any] = {
            "schema_version": "1", "mode": "validation_only", "suite_id": manifest.suite_id,
            "manifest_digest": manifest.digest, "case_count": len(manifest.cases),
        }
    else:
        if not args.token_env:
            raise BenchmarkError("--token-env is required with --base-url")
        token = os.environ.get(args.token_env, "")
        if not token:
            raise BenchmarkError("benchmark token environment variable is not set")
        client = HttpNativeExecutionClient(args.base_url, token, timeout=args.http_timeout)
        try:
            result = (await BenchmarkRunner(client, wait_timeout=args.wait_timeout).run(manifest)).model_dump(mode="json")
        finally:
            await client.aclose()
    output = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m kotoyose.benchmark` の entry point。"""
    parser = argparse.ArgumentParser(description="Run or validate a typed Kotoyose replay manifest")
    parser.add_argument("manifest", help="replay manifest JSON")
    parser.add_argument("--output", help="write benchmark artifact JSON to this path")
    parser.add_argument("--base-url", help="Kotoyose URL; omit for validation-only mode")
    parser.add_argument("--token-env", help="environment variable containing the Kotoyose caller token")
    parser.add_argument("--http-timeout", type=float, default=30.0)
    parser.add_argument("--wait-timeout", type=float, default=600.0)
    try:
        return asyncio.run(_run_cli(parser.parse_args(argv)))
    except (BenchmarkError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
