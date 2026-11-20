"""Resolve and pin existing admission checks for a Hub work request."""
from typing import get_args

from .capabilities import CapabilityName
from .execution_contract import AgentExecution
from .execution_resources import AgentDefinition, ExecutionBudget, ExecutionError, ExecutionGrant
from .agent_execution_contract import AgentExecutionError


def internal_agent_request(create, *, idempotency_key: str, workspace_ref: str, budgets_ref: str) -> AgentExecution:
    body, work = create.execution, create.execution.work
    data = work.model_dump(mode='json')
    return AgentExecution.model_validate({
        'schema_version': '1', 'kind': 'agent', 'idempotency_key': idempotency_key,
        'caller_run_ref': body.caller_run_ref, 'workspace_ref': workspace_ref,
        'execution_grant_ref': work.scope_grant_ref, 'budgets_ref': budgets_ref,
        'agent_definition_ref': work.agent_definition_ref, 'artifact_spec_ref': work.artifact_spec_ref,
        'model': work.model, 'runtime_profile': work.runtime_profile,
        'required_authentication_mode': work.required_authentication_mode,
        'expected_authentication_binding_digest': work.expected_authentication_binding_digest,
        'required_provider': work.required_provider, 'required_billing_mode': work.required_billing_mode,
        'inputs': data['inputs'], 'tool_refs': work.tool_refs, 'skill_refs': work.skill_refs,
        'mcp_refs': work.mcp_refs, 'requirements': data['requirements'],
        'approval_bindings': data['approval_bindings'], 'client_key': work.client_key, 'limits_mode': 'agent_work_v1',
    })


async def _predecessor_candidate(execution, principal: str, create) -> str | None:
    """pool alias で safe_resume するとき、predecessor が選んだ候補。選び直すと resume の同一性が崩れる。"""
    body, work = create.execution, create.execution.work
    if work.model is None or body.predecessor is None or work.continuation_mode != 'safe_resume':
        return None
    return await execution.store.pool.fetchval(
        """SELECT pinned_plan->>'resolved_model' FROM kotoyose.agent_executions
        WHERE id::text=$1 AND principal=$2 AND idempotency_key=$3 AND pinned_plan->>'requested_model'=$4""",
        str(body.predecessor.execution_id), principal, body.predecessor.idempotency_key, work.model)


async def pin_work(execution, principal: str, policy, create, api_key_name: str | None = None) -> dict:
    body, work = create.execution, create.execution.work
    if not getattr(policy, "workspace_roots", None):
        raise AgentExecutionError('permission_denied', 'Caller has no workspace roots configured', 403)
    if set(work.requirements.capabilities) - set(get_args(CapabilityName)):
        raise AgentExecutionError('unsupported_capability', 'Unknown capability', 501)
    grant = await execution.store.resource(principal, work.scope_grant_ref)
    if not isinstance(grant, ExecutionGrant):
        raise ExecutionError('invalid_request', 'Scope grant is not a grant', 422)
    if grant.policy_ref != work.policy_ref or not set(work.resource_refs).issubset(grant.resource_refs):
        raise AgentExecutionError('permission_denied', 'Resource is outside scope grant', 403)
    resources = [await execution.store.resource(principal, ref) for ref in work.resource_refs]
    budgets = [ref for ref, resource in zip(work.resource_refs, resources) if isinstance(resource, ExecutionBudget)]
    if len(budgets) != 1:
        raise AgentExecutionError('invalid_request', 'Exactly one budget resource is required', 422)
    definition = await execution.store.resource(principal, work.agent_definition_ref)
    if not isinstance(definition, AgentDefinition) or definition.output_contract != work.result_contract.format:
        raise AgentExecutionError('invalid_request', 'Result format does not match definition', 422)
    if body.time.run_hard_ms > policy.max_seconds * 1000:
        raise AgentExecutionError('permission_denied', 'Run time exceeds caller ceiling', 403)
    request = internal_agent_request(create, idempotency_key='agent-exec:pending:initial', workspace_ref=work.workspace_ref,
                                     budgets_ref=budgets[0])
    pinned = await _predecessor_candidate(execution, principal, create)
    plan = await execution.admission.resolve(principal, policy, request, **({'pinned_model': pinned} if pinned else {}))
    if api_key_name is not None:  # 旧 token の実行は snapshot を変えない (digest が変わる)
        plan['api_key_name'] = api_key_name
    plan['budgets_ref'] = budgets[0]
    plan['agent_result_contract'] = work.result_contract.model_dump(mode='json')
    return plan
