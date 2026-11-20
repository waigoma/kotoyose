"""Benchmark の dataset 構成、反復変動、Kotoyose 観測指標を集計する。"""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence


_DATASET_BUCKETS = (
    "success", "repair_effective", "final_failure", "interrupted",
    "false_positive", "environment_failure", "outcome_unknown",
)
_EXPECTED_BUCKET = {
    "success": "success",
    "repair_effective": "repair_effective",
    "repair_required": "repair_effective",
    "final_failure": "final_failure",
    "verification_failed": "final_failure",
    "contract_error": "final_failure",
    "interrupted": "interrupted",
    "cancelled": "interrupted",
    "timeout": "interrupted",
    "false_positive": "false_positive",
    "environment_failure": "environment_failure",
    "infra_failure": "environment_failure",
    "worker_lost": "environment_failure",
    "unsupported_capability": "environment_failure",
    "outcome_unknown": "outcome_unknown",
}
CALLER_METRIC_NAMES = (
    "first_pass_verification_rate", "repair_count", "time_to_pr_seconds",
    "human_intervention_count", "review_defect_rate", "browser_success_rate",
)


def pinned_replay_payload(pins: Any) -> dict[str, Any]:
    """旧 manifest の cache identity には、当時存在しなかった default field を混ぜない。"""
    value = pins.model_dump(mode="json")
    for field in ("runtime_policy_digest", "replay_constraints"):
        if field not in pins.model_fields_set:
            value.pop(field, None)
    return value


def dataset_summary(cases: Sequence[Any]) -> tuple[dict[str, Any], list[str]]:
    """TaskHub が採用した期待分類を、比較対象を欠かさない固定 bucket へ写す。"""
    distribution = {name: 0 for name in _DATASET_BUCKETS}
    for case in cases:
        value = case.expected_failure_classification.value
        distribution[_EXPECTED_BUCKET.get(value, "outcome_unknown")] += 1
    warnings: list[str] = []
    if distribution["success"] == len(cases):
        warnings.append(
            "Dataset contains only expected-success cases; failure and interruption bias was not controlled."
        )
    return {
        "case_count": len(cases),
        "expected_failure_classification_distribution": distribution,
    }, warnings


def replay_constraints(pins: Any) -> list[dict[str, str]]:
    """構造化 field と旧 unreplayable_inputs を同じ report 契約へ正規化する。"""
    values = [value.model_dump(mode="json") for value in pins.replay_constraints]
    values.extend(
        {"pin": "unreplayable_inputs", "reason": reason}
        for reason in pins.unreplayable_inputs
    )
    return values


def replay_summary(cases: Sequence[Any], runs: Sequence[Any]) -> list[dict[str, Any]]:
    """制約付き replay を verified と混同しない case 単位の判定を返す。"""
    values: list[dict[str, Any]] = []
    for case in cases:
        constraints = replay_constraints(case.pins)
        case_runs = [run for run in runs if run.case_id == case.case_id]
        verification = "constrained" if constraints else (
            "verified" if case_runs and all(run.pin_verification == "verified" for run in case_runs)
            else "unverified"
        )
        values.append({
            "case_id": case.case_id, "pin_verification": verification,
            "declared_pins": case.pins.model_dump(mode="json"),
            "replay_constraints": constraints,
        })
    return values


def numeric_summary(values: Iterable[Any]) -> dict[str, float | int | None]:
    """欠測や非有限値を 0 にせず、観測数と分布を返す。"""
    observed = [
        float(value) for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(float(value)) and float(value) >= 0
    ]
    if not observed:
        return {
            "observations": 0, "total": None, "mean": None,
            "minimum": None, "maximum": None, "population_stddev": None,
        }
    return {
        "observations": len(observed), "total": sum(observed),
        "mean": statistics.fmean(observed), "minimum": min(observed),
        "maximum": max(observed),
        "population_stddev": statistics.pstdev(observed),
    }


def _usage_value(usage: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in usage:
            return usage[name]
    return None


def case_variations(cases: Sequence[Any], runs: Sequence[Any]) -> list[dict[str, Any]]:
    """case ごとの反復数、成功率、時間、cost、cache 条件差をまとめる。"""
    values: list[dict[str, Any]] = []
    for case in cases:
        case_runs = [run for run in runs if run.case_id == case.case_id]
        conditions = [
            case.cache.condition_for(index).model_dump(mode="json")
            for index in range(1, len(case.requests) + 1)
        ]
        consistent = not conditions or all(value == conditions[0] for value in conditions[1:])
        observed_count = len(case_runs)
        values.append({
            "case_id": case.case_id,
            "planned_repetitions": len(case.requests),
            "observed_repetitions": observed_count,
            "success_rate": (
                sum(run.classification.value == "success" for run in case_runs) / observed_count
                if observed_count else None
            ),
            "runtime_success_rate": (
                sum(run.runtime_success for run in case_runs) / observed_count
                if observed_count else None
            ),
            "wall_seconds": numeric_summary(run.wall_seconds for run in case_runs),
            "active_seconds": numeric_summary(run.active_seconds for run in case_runs),
            "wait_seconds": numeric_summary(run.wait_seconds for run in case_runs),
            "cost": numeric_summary(
                _usage_value(run.usage, "cost", "cost_usd") for run in case_runs
            ),
            "cache_conditions_consistent": consistent,
            "repetition_cache_conditions": conditions,
        })
    return values


def _operation_id(event: Mapping[str, Any]) -> str | None:
    direct = event.get("operation_id")
    if isinstance(direct, str) and direct:
        return direct
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        nested = payload.get("operation_id")
        if isinstance(nested, str) and nested:
            return nested
    return None


def kotoyose_metrics(runs: Sequence[Any], variations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Kotoyose の Attempt、event、usage だけで根拠を持てる指標を集計する。"""
    runtime_total = len(runs)
    accepted = [run.acceptance_success for run in runs if run.acceptance_success is not None]
    infra_retries = 0
    unknown_operations: set[tuple[str, str]] = set()
    resolved_operations: set[tuple[str, str]] = set()
    for run in runs:
        for event in run.events:
            event_type = event.get("type")
            if event_type in {"infra_retry", "infra.retry", "runtime.retry", "provider.retry"}:
                infra_retries += 1
            operation_id = _operation_id(event)
            if operation_id is None:
                continue
            identity = (run.attempt_id or run.request_digest, operation_id)
            payload = event.get("payload")
            client_state = payload.get("state") if isinstance(payload, Mapping) else None
            if event_type in {"operation.unknown", "operation_unknown"} or (
                event_type == "client_tool_result" and client_state == "unknown"
            ):
                unknown_operations.add(identity)
            elif event_type in {"operation.reconciled", "operation_reconciled"} or (
                event_type == "client_tool_result" and client_state in {"succeeded", "failed"}
            ):
                resolved_operations.add(identity)
    resolved_unknown = unknown_operations & resolved_operations
    unknown_count = len(unknown_operations)
    return {
        "runtime_success_rate": (
            sum(run.runtime_success for run in runs) / runtime_total if runtime_total else None
        ),
        "acceptance_success_rate": (
            sum(value is True for value in accepted) / len(accepted) if accepted else None
        ),
        "wall_seconds": numeric_summary(run.wall_seconds for run in runs),
        "active_seconds": numeric_summary(run.active_seconds for run in runs),
        "wait_seconds": numeric_summary(run.wait_seconds for run in runs),
        "cost": numeric_summary(_usage_value(run.usage, "cost", "cost_usd") for run in runs),
        "input_tokens": numeric_summary(
            _usage_value(run.usage, "input_tokens", "prompt_tokens") for run in runs
        ),
        "output_tokens": numeric_summary(
            _usage_value(run.usage, "output_tokens", "completion_tokens") for run in runs
        ),
        "cache_read_tokens": numeric_summary(
            _usage_value(run.usage, "cache_read_tokens", "cached_input_tokens") for run in runs
        ),
        "cache_write_tokens": numeric_summary(
            _usage_value(run.usage, "cache_write_tokens") for run in runs
        ),
        "infra_retry_count": infra_retries,
        "infra_retry_observation": "durable_attempt_events",
        "outcome_unknown_operation_count": unknown_count,
        "resolved_outcome_unknown_operation_count": len(resolved_unknown),
        "outcome_unknown_operation_resolution_rate": (
            len(resolved_unknown) / unknown_count if unknown_count else None
        ),
        "outcome_unknown_operation_observation": "durable_client_tool_events",
        "cache_conditions_consistent": all(
            item["cache_conditions_consistent"] for item in variations
        ),
    }


def caller_metrics() -> dict[str, None]:
    """意味情報を所有する TaskHub が後から埋める固定 slot を返す。"""
    return {name: None for name in CALLER_METRIC_NAMES}


def event_metrics(
    events: Sequence[Mapping[str, Any]],
) -> tuple[float | None, float | None, tuple[str, ...]]:
    """event timestamp に根拠がある値だけを計算し、欠落を 0 と解釈しない。"""
    parsed: list[tuple[datetime, Mapping[str, Any]]] = []
    for event in events:
        raw = event.get("created_at")
        if not isinstance(raw, str):
            continue
        try:
            parsed.append((datetime.fromisoformat(raw.replace("Z", "+00:00")), event))
        except ValueError:
            continue
    parsed.sort(key=lambda item: item[0])
    running = next(
        (stamp for stamp, event in parsed if event.get("type") in {"running", "started"}), None,
    )
    finished = next((stamp for stamp, event in reversed(parsed) if event.get("type") in {
        "finished", "succeeded", "failed", "cancelled", "timeout", "worker_lost",
    }), None)
    first = parsed[0][0] if parsed else None
    if running is None or finished is None:
        return None, None, (
            "active/wait time unavailable because running and terminal event timestamps were incomplete",
        )
    active = max(0.0, (finished - running).total_seconds())
    wait = max(0.0, (running - first).total_seconds()) if first else None
    return active, wait, ()
