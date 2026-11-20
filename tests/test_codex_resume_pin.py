"""世代間の Codex runtime artifact 同一性を、再開 admission より内側で検証する。"""

from types import SimpleNamespace

import pytest

from kotoyose.execution_resources import ExecutionError, content_digest
from kotoyose.execution_contract import AgentExecution, ResumeRequest
from kotoyose.codex_coding_policy import HOST_MODE, TOOL_POLICY_DIGEST, EXTERNAL_TOOL_POLICY_DIGEST, EXTERNAL_SANDBOX_POLICY
from kotoyose.execution_resume import build_resume_manifest
from kotoyose.runtime.resume import ResumeSupport


@pytest.mark.parametrize("changed", [
    {"codex_rootfs": "/reviewed/runtime-b"},
    {"codex_rootfs_digest": "b" * 64},
    {"codex_rootfs": "/reviewed/runtime-b", "codex_rootfs_digest": "b" * 64},
    {"codex_rootfs": None, "codex_rootfs_digest": None},
    {"codex_auth_file": "/host/different-auth.json"},
    {"codex_auth_identity": "b" * 64},
])
def test_durable_resume_rejects_changed_codex_artifact_identity(changed):
    old = {"kind": "agent", "provider": "codex", "codex_rootfs": "/reviewed/runtime-a",
           "codex_rootfs_digest": "a" * 64}
    previous = {"snapshot": old, "request": {"caller_run_ref": "run"},
                "workspace_id": "workspace", "generation": 0}
    request = SimpleNamespace(execution=SimpleNamespace(kind="agent", caller_run_ref="run"),
                              predecessor_snapshot_digest=content_digest(old))
    snapshot = {**old, **changed, "workspace_id": "workspace", "generation": 1}
    with pytest.raises(ExecutionError) as caught:
        build_resume_manifest("caller", previous, [], request, snapshot,
                              ResumeSupport(modes=("durable_state",), reason="fixture"))
    assert (caught.value.code, caught.value.status, caught.value.reason_code) == (
        "resume_unsafe", 409, "runtime_mismatch")


@pytest.mark.parametrize('old_digest,current_digest,environment', [
    ('56283f6706f6175930d9eba87d9402f1780a9357af57cff0e1ba4b1be53ef42b', TOOL_POLICY_DIGEST, None),
    ('ba6544c392b896e914946a933759a45610df527a444f68fb41d5b8436698f51b', EXTERNAL_TOOL_POLICY_DIGEST, 'isolated'),
])
@pytest.mark.parametrize('case', ['bound', 'unbound', 'unknown_policy', 'changed_auth'])
def test_fresh_grant_allows_only_the_known_soft_notification_transition(old_digest, current_digest, environment, case):
    old = {'kind': 'agent', 'provider': 'codex', 'codex_execution_mode': HOST_MODE,
           'codex_tool_policy_digest': old_digest, 'execution_environment': environment,
           'codex_sandbox_policy': EXTERNAL_SANDBOX_POLICY if environment else None}
    previous = {'id': 'attempt-old', 'snapshot': old, 'request': {'caller_run_ref': 'run-old'},
                'workspace_id': 'workspace', 'generation': 0, 'status': 'timeout', 'sequence': 2}
    request = ResumeRequest(schema_version='1', predecessor_snapshot_digest=content_digest(old), execution=AgentExecution(
        schema_version='1', kind='agent', caller_run_ref='run-old' if case == 'unbound' else 'run-new',
        idempotency_key='recovery-key', workspace_ref='workspace:current', execution_grant_ref='grant:new',
        budgets_ref='budget:new', agent_definition_ref='definition', runtime_profile='profile',
        inputs=[{'kind': 'task_request', 'provenance': 'project', 'content_ref': 'input:task'}],
    ))
    snapshot = {**old, 'workspace_id': 'workspace', 'generation': 1,
                'codex_tool_policy_digest': current_digest, 'grant': {'version': 1}}
    if case != 'unbound':
        snapshot['grant']['resume_predecessor_ref'] = previous['id']
    if case == 'unknown_policy':
        snapshot['codex_tool_policy_digest'] = '0' * 64
    if case == 'changed_auth':
        snapshot['codex_auth_identity'] = 'changed'
    before = dict(old)
    if case == 'bound':
        manifest = build_resume_manifest('caller', previous, [], request, snapshot, ResumeSupport(('durable_state',), 'fixture'))
        assert manifest.mode == 'durable_state' and manifest.caller_run_ref == 'run-new'
        assert snapshot['codex_tool_policy_digest'] == current_digest
    else:
        with pytest.raises(ExecutionError) as caught:
            build_resume_manifest('caller', previous, [], request, snapshot, ResumeSupport(('durable_state',), 'fixture'))
        assert caught.value.reason_code == 'runtime_mismatch'
    assert old == before
