"""controller 専用の停止観測を型と所有 identity に固定する。外部入力は証拠にしない。"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .execution_resources import content_digest


Stage = Literal["provider", "scope", "tasks", "pipes", "egress"]
Outcome = Literal["confirmed", "unconfirmed", "exception", "timeout", "pending", "unavailable"]
STAGES: tuple[Stage, ...] = ("provider", "scope", "tasks", "pipes", "egress")


class ObservedProcess(BaseModel):
    """入力送信前に procfs から採取した process identity の保存形式。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    pid: int = Field(gt=0)
    start_ticks: int = Field(ge=0)
    boot_id: str = Field(min_length=1, max_length=128)
    uid: int = Field(ge=0)


class ObservedScope(BaseModel):
    """保持 FD の inode と、同じ scope 内で観測した private namespace init。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    unit: str = Field(min_length=1, max_length=256)
    control_group: str = Field(min_length=1, max_length=4096)
    device: int = Field(ge=0)
    inode: int = Field(gt=0)
    namespace_init: ObservedProcess


class ObservedOwnership(ObservedProcess):
    """controller PID と provider scope を一つの観測として固定する。"""

    codex_provider_scope: ObservedScope


class StopComponents(BaseModel):
    """全 component が確定した時だけ、全体の停止証明として使用できる。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    provider: Outcome
    scope: Outcome
    tasks: Outcome
    pipes: Outcome
    egress: Outcome


class StopEvidence(BaseModel):
    """private 操作台帳の版付き観測。digest は破損検出用で、DB 書込権限の認証ではない。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["1"]
    attempt_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)
    workspace_device: int = Field(ge=0)
    workspace_inode: int = Field(gt=0)
    ownership_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    components: StopComponents
    pending_tasks: int = Field(ge=0, le=5)
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    def diagnostic(self) -> dict:
        """公開可能な固定 stage/reason と件数だけを返す。PID・本文・path は含めない。"""
        first = next((stage for stage in STAGES if getattr(self.components, stage) != "confirmed"), None)
        return {"schema_version": "1", "stage": first or "complete",
                "reason": getattr(self.components, first) if first else "confirmed",
                "pending_tasks": self.pending_tasks}

    def confirmed(self) -> bool:
        """pending task を含まない完全な肯定観測かを返す。"""
        return self.pending_tasks == 0 and all(getattr(self.components, stage) == "confirmed" for stage in STAGES)


def stop_evidence_context(attempt: dict) -> dict:
    """保存 admission から照合に必要な所有境界だけを取り出す。欠落は拒否する。"""
    return {"attempt_id": attempt["id"], "generation": attempt["generation"],
            "workspace_device": attempt["snapshot"]["workspace_device"],
            "workspace_inode": attempt["snapshot"]["workspace_inode"]}


def make_stop_evidence(context: dict, ownership: dict, components: StopComponents, pending_tasks: int) -> StopEvidence:
    """保持中の観測を所有 identity に固定する。I/O や台帳更新は行わない。"""
    value = {"schema_version": "1", **context, "ownership_digest": content_digest(ownership),
             "components": components.model_dump(), "pending_tasks": pending_tasks}
    return StopEvidence.model_validate({**value, "digest": content_digest(value)})


def validate_stop_evidence(raw: object, context: dict, ownership: dict) -> StopEvidence:
    """版・完全性・所有境界を厳密に照合し、不一致なら ValueError を返す。"""
    proof = StopEvidence.model_validate(raw)
    expected = make_stop_evidence(context, ownership, proof.components, proof.pending_tasks)
    if proof != expected:
        raise ValueError("invalid_stop_evidence")
    observed = ObservedOwnership.model_validate(ownership)
    init = observed.codex_provider_scope.namespace_init
    if (observed.codex_provider_scope.unit != f"kotoyose-native-{proof.attempt_id}-provider-{proof.generation}.scope"
            or init.uid != observed.uid or init.boot_id != observed.boot_id
            or init.start_ticks < observed.start_ticks):
        raise ValueError("invalid_stop_ownership")
    return proof


@dataclass(slots=True)
class CleanupObservation:
    """cleanup 内の固定順で最初の失敗を診断する、有限個の観測値。"""

    provider: Outcome = "unavailable"
    scope: Outcome = "unavailable"
    tasks: Outcome = "confirmed"
    pipes: Outcome = "unavailable"
    egress: Outcome = "confirmed"
    pending_tasks: int = 0

    def components(self) -> StopComponents:
        """保存時に未知の stage/reason や本文が混入しない型へ変換する。"""
        return StopComponents(**{stage: getattr(self, stage) for stage in STAGES})


class NamespaceStopEvidence(BaseModel):
    """保存済み private init の終了確認を、元の部分証拠へ追加で束縛する。

    cgroup の観測結果は書き換えない。PID namespace の init が終了すると kernel が
    子孫を終了させる保証を、起動時に観測した所有 identity に対してだけ使用する。
    根拠: https://man7.org/linux/man-pages/man7/pid_namespaces.7.html
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["1"]
    kind: Literal["private_pid_namespace_exit"]
    attempt_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)
    workspace_device: int = Field(ge=0)
    workspace_inode: int = Field(gt=0)
    ownership_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_stop_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def namespace_recovery_source(context: dict, ownership: dict, raw: object) -> StopEvidence:
    """scope 以外が肯定された部分証拠だけを復旧元とする。不備は ValueError。"""
    proof = validate_stop_evidence(raw, context, ownership)
    other_stages = (stage for stage in STAGES if stage != "scope")
    if (proof.pending_tasks != 0 or proof.components.scope == "confirmed"
            or any(getattr(proof.components, stage) != "confirmed" for stage in other_stages)):
        raise ValueError("namespace_recovery_requires_scope_only_uncertainty")
    return proof


def make_namespace_stop_evidence(context: dict, ownership: dict, raw_source: object) -> NamespaceStopEvidence:
    """controller が init と provider の終了を確認した後に記録する観測を作る。"""
    proof = namespace_recovery_source(context, ownership, raw_source)
    value = {"schema_version": "1", "kind": "private_pid_namespace_exit", **context,
             "ownership_digest": content_digest(ownership), "source_stop_digest": proof.digest}
    return NamespaceStopEvidence.model_validate({**value, "digest": content_digest(value)})


def validate_namespace_stop_evidence(raw: object, context: dict, ownership: dict,
                                     raw_source: object) -> NamespaceStopEvidence:
    """復旧観測の版・元証拠・所有・workspace を照合し、破損や流用を拒否する。"""
    proof = NamespaceStopEvidence.model_validate(raw)
    if proof != make_namespace_stop_evidence(context, ownership, raw_source):
        raise ValueError("invalid_namespace_stop_evidence")
    return proof


EgressBasis = Literal["previous_coordinator", "previous_boot", "settled_in_process"]


class RecoveringProcess(BaseModel):
    """egress の所有者が居ないと判断した Kotoyose process の identity。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    pid: int = Field(gt=0)
    start_ticks: int = Field(ge=0)
    boot_id: str = Field(min_length=1, max_length=128)


class EgressStopEvidence(BaseModel):
    """egress だけが未確認の部分証拠に、所有 process の不在の観測を追加で束縛する。

    egress proxy は attempt を実行した Kotoyose process 内の asyncio task である。provider tree が
    停止した後に残り得るのはその process 内の task だけなので、process の交代 (別の coordinator、
    別の boot) か、同じ process で task が全て終わったことを確かめたときだけ記録する。
    元の部分証拠は書き換えない。
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["1"]
    kind: Literal["egress_owner_absent"]
    basis: EgressBasis
    attempt_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)
    workspace_device: int = Field(ge=0)
    workspace_inode: int = Field(gt=0)
    ownership_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_stop_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    recovered_by: RecoveringProcess
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def egress_recovery_source(context: dict, ownership: dict, raw: object) -> StopEvidence:
    """egress 以外が全て肯定され、pending task も無い部分証拠だけを復旧元とする。不備は ValueError。"""
    proof = validate_stop_evidence(raw, context, ownership)
    other_stages = (stage for stage in STAGES if stage != "egress")
    if (proof.pending_tasks != 0 or proof.components.egress == "confirmed"
            or any(getattr(proof.components, stage) != "confirmed" for stage in other_stages)):
        raise ValueError("egress_recovery_requires_egress_only_uncertainty")
    return proof


def make_egress_stop_evidence(context: dict, ownership: dict, raw_source: object, *,
                              basis: EgressBasis, recovered_by: dict) -> EgressStopEvidence:
    """所有 process の不在を確かめた後に記録する観測を作る。I/O は行わない。"""
    proof = egress_recovery_source(context, ownership, raw_source)
    value = {"schema_version": "1", "kind": "egress_owner_absent", "basis": basis, **context,
             "ownership_digest": content_digest(ownership), "source_stop_digest": proof.digest,
             "recovered_by": RecoveringProcess.model_validate(recovered_by).model_dump()}
    return EgressStopEvidence.model_validate({**value, "digest": content_digest(value)})


def validate_egress_stop_evidence(raw: object, context: dict, ownership: dict,
                                  raw_source: object) -> EgressStopEvidence:
    """復旧観測の版・元証拠・所有・workspace を照合し、破損や流用を拒否する。"""
    proof = EgressStopEvidence.model_validate(raw)
    expected = make_egress_stop_evidence(context, ownership, raw_source, basis=proof.basis,
                                         recovered_by=proof.recovered_by.model_dump())
    if proof != expected:
        raise ValueError("invalid_egress_stop_evidence")
    return proof
