"""Strict public Hub execution wire types and authorization bounds."""
from __future__ import annotations

import re
from decimal import Decimal
from datetime import timedelta
from typing import Annotated, Literal, Mapping

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, model_validator

from .agent_execution_digest import agent_canonical_json, agent_work_digest, parse_agent_utc

AGENT_EXECUTION_SCHEMA_VERSION = 'agent_execution_v1'
LEASE_WALL_FACTOR = 3
MAX_GRACE_MS = 60_000
RECEIPT_PAGE_LIMIT = 100

Ref = Annotated[str, Field(min_length=1, max_length=256)]
Key = Annotated[str, Field(min_length=1, max_length=256, pattern=r'^[^/]+$')]
Digest = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]
Units = Annotated[str, Field(pattern=r'^\d+(\.\d{1,12})?$')]
SafeInt = Annotated[int, Field(ge=0, le=2**53 - 1)]
Positive = Annotated[int, Field(ge=1, le=2**53 - 1)]

def _utc(value: str) -> str:
    parse_agent_utc(value)
    return value

Utc = Annotated[str, AfterValidator(_utc)]
BudgetLimit = Literal['run_budget', 'task_units', 'project_units', 'day_units', 'turns', 'output_bytes', 'unknown']
AuthorityCause = Literal['task_changed', 'authority_withdrawn', 'policy_unavailable', 'scope_policy', 'approval_expired', 'human_ceiling_expired', 'archived']


class AgentExecutionModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    @model_validator(mode='before')
    @classmethod
    def valid_json(cls, value):
        if isinstance(value, dict):
            agent_canonical_json(value)
        return value


class AgentExecutionInput(AgentExecutionModel):
    kind: Literal['instructions', 'task_request', 'repository_context', 'memory_snapshot', 'handoff', 'image']
    provenance: Literal['trusted', 'verified_fact', 'project', 'agent_generated', 'external_untrusted', 'mixed']
    content_ref: Ref
    expected_revocation_epoch: str | None


class AgentExecutionWorkerRequirements(AgentExecutionModel):
    os: Literal['linux', 'windows', 'macos'] | None
    applications: list[str]
    gpu: Literal['none', 'any', 'nvidia', 'amd', 'intel']
    workspace_locality: Literal['local', 'remote', 'any']
    native: Literal['forbidden', 'allowed', 'required']


class AgentExecutionRequirements(AgentExecutionModel):
    isolation: Literal['sandbox', 'native']
    capabilities: list[str]
    worker_requirements: AgentExecutionWorkerRequirements


class AgentExecutionResultContract(AgentExecutionModel):
    format: Literal['text', 'json']
    json_schema: dict[str, JsonValue] | None


class AgentExecutionApprovalBinding(AgentExecutionModel):
    approval_id: str
    subject_digest: Digest


class AgentExecutionWork(AgentExecutionModel):
    work_kind: Literal['coding', 'repair', 'verification_agent', 'role', 'judge']
    role: Literal['pm', 'structurer', 'task_agent', 'coding', 'reviewer', 'butler'] | None
    automatic: bool
    workspace_ref: Ref
    agent_definition_ref: Ref
    artifact_spec_ref: Ref | None
    policy_ref: Ref
    scope_grant_ref: Ref
    resource_refs: list[Ref]
    model: Ref | None
    runtime_profile: Ref | None
    required_authentication_mode: Literal['chatgpt_subscription', 'api_key'] | None
    expected_authentication_binding_digest: Digest | None
    required_provider: Literal['codex', 'claude'] | None
    required_billing_mode: Literal['subscription'] | None
    inputs: list[AgentExecutionInput]
    tool_refs: list[Ref]
    skill_refs: list[Ref]
    mcp_refs: list[Ref]
    requirements: AgentExecutionRequirements
    result_contract: AgentExecutionResultContract
    approval_bindings: list[AgentExecutionApprovalBinding]
    continuation_mode: Literal['fresh', 'safe_resume']
    client_key: str | None


class AgentExecutionMoneyFixed(AgentExecutionModel):
    mode: Literal['fixed_units']
    basis: Literal['subscription_execution_units_v1']
    allocation_units: Units
    run_units_ceiling: Units


class AgentExecutionMoneyUnbudgeted(AgentExecutionModel):
    mode: Literal['unbudgeted']


class AgentExecutionCharge(AgentExecutionModel):
    charge_ref: Ref
    reservation_ref: Ref
    allocation_units: Units


class AgentExecutionAuthorityCeiling(AgentExecutionModel):
    kind: Literal['human_repair', 'retained_recovery', 'repository_recovery', 'approval']
    evidence_ref: Ref
    expires_at: Utc


class AgentExecutionPredecessor(AgentExecutionModel):
    idempotency_key: Key
    execution_id: str
    relation: Literal['condition_change', 'cap_raise', 'resume_unavailable', 'rerun']


class AgentExecutionApproval(AgentExecutionModel):
    call_id: Ref
    subject_digest: Digest
    decision: Literal['approved', 'denied']
    decision_ref: Ref


class AgentExecutionAllow(AgentExecutionModel):
    decision: Literal['allow']
    revision: SafeInt
    activation_nonce: str
    work_digest: Digest
    caller_run_ref: Ref
    issued_at: Utc
    not_before: Utc
    expires_at: Utc
    allowed_active_ms: SafeInt
    stop_limit: Literal['run_seconds', 'task_seconds']
    charge: AgentExecutionCharge | None
    authority_ceiling_at: Utc | None
    approval: AgentExecutionApproval | None
    settled_receipt_cursor: SafeInt


class AgentExecutionDenyCost(AgentExecutionModel):
    kind: Literal['cost_limit']
    limit: BudgetLimit


class AgentExecutionDenyTime(AgentExecutionModel):
    kind: Literal['time_limit']
    limit: Literal['run_seconds', 'task_seconds']


class AgentExecutionDenyAuthority(AgentExecutionModel):
    kind: Literal['authority_denied']
    cause: AuthorityCause


class AgentExecutionDenyCancel(AgentExecutionModel):
    kind: Literal['cancelled']


class AgentExecutionDeny(AgentExecutionModel):
    decision: Literal['deny']
    revision: SafeInt
    activation_nonce: str
    work_digest: Digest
    caller_run_ref: Ref
    reason: Annotated[AgentExecutionDenyCost | AgentExecutionDenyTime | AgentExecutionDenyAuthority | AgentExecutionDenyCancel, Field(discriminator='kind')]
    settled_receipt_cursor: SafeInt


AgentExecutionAuthorization = Annotated[AgentExecutionAllow | AgentExecutionDeny, Field(discriminator='decision')]


class AgentExecutionTime(AgentExecutionModel):
    run_hard_ms: Positive
    grace_ms: Annotated[int, Field(ge=1, le=MAX_GRACE_MS)]


class AgentExecutionResources(AgentExecutionModel):
    max_turns: Positive
    max_output_bytes: Positive


class AgentExecutionRecoveryPolicy(AgentExecutionModel):
    provider_wait: Literal['enabled']
    fault_retry_limit: Annotated[int, Field(ge=1, le=1)]
    fault_retries_used: Annotated[int, Field(ge=0, le=1)]


class AgentExecutionCreateBody(AgentExecutionModel):
    schema_version: Literal['agent_execution_v1']
    kind: Literal['agent']
    idempotency_key: Key
    caller_run_ref: Ref
    work_digest: Digest
    work: AgentExecutionWork
    money: Annotated[AgentExecutionMoneyFixed | AgentExecutionMoneyUnbudgeted, Field(discriminator='mode')]
    time: AgentExecutionTime
    resources: AgentExecutionResources
    recovery: AgentExecutionRecoveryPolicy
    predecessor: AgentExecutionPredecessor | None
    authority_ceilings: list[AgentExecutionAuthorityCeiling]
    initial_authorization: AgentExecutionAllow


class AgentExecutionCreate(AgentExecutionModel):
    execution: AgentExecutionCreateBody


class AgentExecutionAck(AgentExecutionModel):
    schema_version: Literal['agent_execution_v1']
    execution_id: str
    idempotency_key: Key
    request_digest: Digest


class AgentExecutionProviderLimit(AgentExecutionModel):
    provider: Literal['codex', 'claude', 'print_cli']
    limit_window: Literal['five_hour', 'seven_day', 'seven_day_opus', 'seven_day_sonnet', 'overage', 'rate', 'unknown']
    reset_at: Utc | None
    detected_at: Utc
    resume: Literal['native', 'unavailable']
    unavailable_reason: Literal['no_session', 'not_retained', 'adapter_unsupported'] | None


class AgentExecutionReceipt(AgentExecutionModel):
    cursor: SafeInt
    receipt_id: Ref
    digest: Digest
    charge_ref: Ref | None
    reservation_ref: Ref | None
    allocation_units: Units | None
    activation_nonces: list[str]
    outcome: Literal['executed', 'provider_zero_turn', 'not_started_closed']
    units: Units | None
    active_ms: SafeInt
    turns: SafeInt
    output_bytes: SafeInt
    execution_confirmed: bool
    stop_confirmation: Literal['confirmed']
    side_effect_resolution: Literal['resolved']
    finished_at: Utc


class AgentExecutionTerminalSimple(AgentExecutionModel):
    kind: Literal['succeeded', 'cancelled']


class AgentExecutionTerminalCost(AgentExecutionDenyCost):
    pass


class AgentExecutionTerminalTime(AgentExecutionDenyTime):
    pass


class AgentExecutionTerminalAuthority(AgentExecutionDenyAuthority):
    pass


class AgentExecutionTerminalFault(AgentExecutionModel):
    kind: Literal['execution_fault']
    cause: Literal['launch_failure', 'worker_lost', 'agent_failure', 'output_invalid', 'unknown_failure', 'hub_inconsistency']
    code: str | None


class AgentExecutionTerminalResume(AgentExecutionModel):
    kind: Literal['resume_unavailable']
    cause: Literal['no_session', 'not_retained', 'adapter_unsupported', 'predecessor_mismatch']
    handoff_refs: list[Ref]
    workspace_ref: Ref
    work_digest: Digest


AgentExecutionTerminal = Annotated[AgentExecutionTerminalSimple | AgentExecutionTerminalCost | AgentExecutionTerminalTime | AgentExecutionTerminalAuthority | AgentExecutionTerminalFault | AgentExecutionTerminalResume, Field(discriminator='kind')]


class AgentExecutionActivation(AgentExecutionModel):
    nonce: str
    ordinal: SafeInt
    reason: Literal['initial', 'provider_resume', 'fault_retry', 'approval_continue', 'lease_resume']
    charge_ref: Ref | None
    authority_required: bool
    authorization_revision: SafeInt


class AgentExecutionPause(AgentExecutionModel):
    kind: Literal['approval_required']
    call_id: Ref
    subject_digest: Digest


class AgentExecutionUsage(AgentExecutionModel):
    state: Literal['not_started', 'running', 'ended', 'unknown']
    active_ms: SafeInt
    turns: SafeInt | None
    output_bytes: SafeInt | None
    units: Units | None
    complete: bool
    observed_at: Utc
    started_at: Utc | None
    finished_at: Utc | None


class AgentExecutionArtifact(AgentExecutionModel):
    ref: Ref
    name: str
    digest: Digest
    size: SafeInt
    media_type: str


class AgentExecutionSnapshot(AgentExecutionModel):
    schema_version: Literal['agent_execution_v1']
    execution_id: str
    idempotency_key: Key
    caller_run_ref: Ref
    work_digest: Digest
    request_digest: Digest
    sequence: SafeInt
    state: Literal['queued', 'running', 'waiting_provider', 'resuming', 'retrying', 'terminal']
    state_revision: SafeInt
    activation: AgentExecutionActivation
    pause: AgentExecutionPause | None
    provider_limit: AgentExecutionProviderLimit | None
    next_probe_at: Utc | None
    fault_retries_used: Annotated[int, Field(ge=0, le=1)]
    cancel_requested: bool
    admission_closed: bool
    usage: AgentExecutionUsage
    stop_confirmation: Literal['confirmed', 'unconfirmed']
    side_effect_resolution: Literal['resolved', 'unresolved']
    workspace_ref: Ref | None
    terminal: AgentExecutionTerminal | None
    receipts: list[AgentExecutionReceipt]
    next_receipt_cursor: SafeInt
    final_receipt_cursor: SafeInt
    receipts_complete: bool
    artifacts: list[AgentExecutionArtifact]


class AgentExecutionAction(AgentExecutionModel):
    action_key: Key
    expected_revision: SafeInt


class AgentExecutionCancel(AgentExecutionModel):
    action_key: Key
    reason: Literal['owner_abort', 'task_cancelled', 'authority_withdrawn']


class AgentExecutionClosed(AgentExecutionModel):
    schema_version: Literal['agent_execution_v1']
    idempotency_key: Key
    outcome: Literal['not_admitted']
    admission_closed: bool
    execution_id: None
    proof_ref: Ref

    @model_validator(mode='after')
    def closed_is_true(self):
        if not self.admission_closed:
            raise ValueError('admission_closed must be true')
        return self


class AgentExecutionErrorBody(AgentExecutionModel):
    type: Literal['agent_execution_error']
    code: Literal['invalid_request', 'request_conflict', 'stale_revision', 'stale_activation', 'permission_denied', 'unsupported_capability', 'not_found']
    message: str
    current: AgentExecutionSnapshot | None


class AgentExecutionErrorEnvelope(AgentExecutionModel):
    error: AgentExecutionErrorBody


class AgentExecutionError(Exception):
    def __init__(self, code: str, message: str, status: int, current: dict | None = None):
        super().__init__(message)
        self.code, self.status, self.current = code, status, current


def lease_wall_cap(time: AgentExecutionTime) -> timedelta:
    """許可の期限の上限。issued_at はコンテナ起動より前なので、走り切る Run のために稼働上限の 3 倍を取る。"""
    return timedelta(milliseconds=LEASE_WALL_FACTOR * time.run_hard_ms + time.grace_ms)


def validate_allow_bounds(create: AgentExecutionCreate, allow: AgentExecutionAllow) -> None:
    body = create.execution
    issued, before, expires = map(parse_agent_utc, (allow.issued_at, allow.not_before, allow.expires_at))
    ceilings = [parse_agent_utc(item.expires_at) for item in body.authority_ceilings]
    ceiling = parse_agent_utc(allow.authority_ceiling_at) if allow.authority_ceiling_at else None
    bad = not before < expires <= issued + lease_wall_cap(body.time)
    bad |= (not ceilings and ceiling is not None) or (bool(ceilings) and ceiling is None)
    if ceiling is not None and ceilings:
        bad |= ceiling > min(ceilings) or expires > ceiling - timedelta(milliseconds=body.time.grace_ms)
    bad |= allow.allowed_active_ms > body.time.run_hard_ms
    if isinstance(body.money, AgentExecutionMoneyFixed):
        bad |= allow.charge is None or Decimal(allow.charge.allocation_units) != Decimal(body.money.allocation_units)
    else:
        bad |= allow.charge is not None
    if bad:
        raise AgentExecutionError('invalid_request', 'Authorization exceeds execution bounds', 422)


def validate_create(create: AgentExecutionCreate, raw: Mapping) -> None:
    body = create.execution
    work = body.work
    allow = body.initial_authorization
    bad = (work.work_kind == 'role') != (work.role is not None)
    bad |= work.result_contract.format == 'text' and work.result_contract.json_schema is not None
    bad |= (work.model is None) == (work.runtime_profile is None)
    bad |= work.continuation_mode == 'safe_resume' and body.predecessor is None
    bad |= body.work_digest != agent_work_digest(raw)
    bad |= allow.work_digest != body.work_digest or allow.caller_run_ref != body.caller_run_ref or allow.approval is not None
    if bad:
        raise AgentExecutionError('invalid_request', 'Create does not match its work or authorization', 422)
    if work.result_contract.json_schema is not None:
        from jsonschema import Draft202012Validator
        try:
            # The wire contract permits any object schema, including an empty schema.
            def check_refs(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key in ('$ref', '$dynamicRef', '$recursiveRef') and (not isinstance(child, str) or not child.startswith('#')):
                            raise ValueError('external reference')
                        check_refs(child)
                elif isinstance(value, list):
                    for child in value:
                        check_refs(child)
            check_refs(work.result_contract.json_schema)
            Draft202012Validator.check_schema(work.result_contract.json_schema)
        except Exception as exc:
            raise AgentExecutionError('invalid_request', 'Invalid result schema', 422) from exc
    validate_allow_bounds(create, allow)


EVENT_CURSOR_START = '000000:0000000000'
EVENT_CURSOR_PATTERN = r'\d{6}:\d{10}'


def event_cursor(activation_ordinal: int, sequence: int) -> str:
    """activation の順序と attempt 内の sequence を、文字列比較で単調な cursor にする。"""
    return f'{activation_ordinal:06d}:{sequence:010d}'


def parse_event_cursor(value: str) -> tuple[int, int]:
    if not re.fullmatch(EVENT_CURSOR_PATTERN, value):
        raise AgentExecutionError('invalid_request', 'Invalid event cursor', 422)
    ordinal, sequence = value.split(':')
    return int(ordinal), int(sequence)


class _AgentExecutionEventModel(BaseModel):
    """event payload は digest 対象ではなく、小数を含む観測値も返す。"""
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class AgentExecutionEvent(_AgentExecutionEventModel):
    cursor: Annotated[str, Field(pattern=r'^' + EVENT_CURSOR_PATTERN + r'$')]
    activation_ordinal: Positive
    attempt_id: Ref
    sequence: SafeInt
    type: Ref
    payload: dict[str, JsonValue]
    created_at: Utc


class AgentExecutionEvents(_AgentExecutionEventModel):
    schema_version: Literal['agent_execution_v1']
    idempotency_key: Key
    events: list[AgentExecutionEvent]
    next_cursor: Annotated[str, Field(pattern=r'^' + EVENT_CURSOR_PATTERN + r'$')]
    complete: bool
