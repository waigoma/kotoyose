"""Classify terminal attempt failures using the bounded retry policy."""
from dataclasses import dataclass

from .agent_execution_units import ActivationFacts


@dataclass(frozen=True)
class FaultDecision:
    """故障の原因と retry 可否。"""
    cause: str
    code: str | None
    retryable: bool


def classify_fault(attempt_row: dict, facts: ActivationFacts) -> FaultDecision:
    """Attempt と起動証拠から、許可された故障だけを retry 対象にする。"""
    reason = attempt_row['reason']
    if facts.launch == 'not_started' and reason in ('worker_failure', 'sdk_error', 'provider_error'):
        return FaultDecision('launch_failure', reason, True)
    if facts.launch == 'not_started' and reason in ('authentication_failed', 'permission_denied', 'request_conflict', 'resume_unsafe', 'unsupported_capability'):
        return FaultDecision('launch_failure', reason, False)
    if attempt_row['status'] == 'worker_lost':
        return FaultDecision('worker_lost', reason, True)
    if reason in ('invalid_json_output', 'output_contract_error', 'output_schema_mismatch'):
        return FaultDecision('output_invalid', reason, True)
    if facts.launch == 'started' and reason in ('sdk_error', 'provider_error', 'missing_completion', 'invalid_protocol', 'worker_failure'):
        return FaultDecision('agent_failure', reason, True)
    return FaultDecision('unknown_failure', reason, False)
