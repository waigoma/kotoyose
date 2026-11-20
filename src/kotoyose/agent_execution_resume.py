"""Build native continuation evidence for a Hub activation."""
from .execution_contract import ResumeRequest
from .execution_resources import ExecutionError, content_digest
from .execution_resume import build_resume_manifest, check_predecessor
from .native_sessions import session_scope


class ResumeUnavailable(Exception):
    def __init__(self, cause: str):
        super().__init__(cause)
        self.cause = cause


async def session_available(execution, attempt: dict) -> str | None:
    snapshot = attempt['snapshot']
    provider = snapshot.get('provider')
    if snapshot.get('backend') != 'native' or provider not in ('claude', 'codex'):
        return 'adapter_unsupported'
    runtime = execution.agent_executor.runtime_for(provider)
    available = getattr(runtime, 'can_resume_session', None)
    if available is None or 'native' not in execution.resume_support(provider).modes:
        return 'adapter_unsupported'
    ref = attempt.get('provider_session_ref')
    if not ref:
        return 'no_session'
    if not await available(session_scope(attempt['principal'], attempt['workspace_id'], provider), ref):
        return 'not_retained'
    return None


async def check_predecessor_resume(execution, principal, row: dict) -> dict:
    """同じ principal の predecessor と workspace を照合し、最後の Attempt を返す。

    検査に失敗した場合は ResumeUnavailable を送出する。
    """
    predecessor = row['request']['execution']['predecessor']
    if predecessor is None:
        raise ResumeUnavailable('predecessor_mismatch')
    old = await execution.store.pool.fetchrow("""SELECT * FROM kotoyose.agent_executions
        WHERE id::text=$1 AND principal=$2 AND idempotency_key=$3""",
        predecessor['execution_id'], principal, predecessor['idempotency_key'])
    if old is None or old['state'] != 'terminal':
        raise ResumeUnavailable('predecessor_mismatch')
    if (old['workspace_id'] != row['workspace_id'] or old['workspace_generation'] != row['workspace_generation']
            or old['workspace_ref'] != row['request']['execution']['work']['workspace_ref']):
        raise ResumeUnavailable('predecessor_mismatch')
    workspace = await execution.store.pool.fetchrow('SELECT generation,writer_attempt,lifecycle_state FROM kotoyose.execution_workspaces WHERE id=$1 AND principal=$2', row['workspace_id'], principal)
    if (not workspace or workspace['generation'] != row['workspace_generation']
            or workspace['writer_attempt'] or workspace['lifecycle_state'] != 'active'):
        raise ResumeUnavailable('predecessor_mismatch')
    attempt_id = await execution.store.pool.fetchval("""SELECT attempt_id FROM kotoyose.agent_execution_activations
        WHERE execution_id=$1 AND attempt_id IS NOT NULL ORDER BY ordinal DESC LIMIT 1""", old['id'])
    if not attempt_id:
        raise ResumeUnavailable('no_session')
    previous = await execution.store.attempt(principal, attempt_id)
    if previous['stop_confirmation'] != 'confirmed' or previous['side_effect_resolution'] != 'resolved':
        raise ResumeUnavailable('predecessor_mismatch')
    if previous['snapshot'].get('expected_authentication_binding_digest') != row['pinned_plan'].get('expected_authentication_binding_digest'):
        raise ResumeUnavailable('predecessor_mismatch')
    return previous


async def resume_snapshot(execution, principal, request, snapshot: dict, previous: dict, *, run_bound: bool = False) -> dict:
    """停止証拠と session を検証し、native 再開用 manifest を snapshot に付ける。

    run_bound は predecessor と Run の関係を検証済みの場合だけ指定する。
    再開できない場合は ResumeUnavailable を送出する。
    """
    cause = await session_available(execution, previous)
    if cause:
        raise ResumeUnavailable(cause)
    try:
        check_predecessor(previous)
        resume = ResumeRequest(schema_version='1', execution=request,
            predecessor_snapshot_digest=content_digest(previous['snapshot']), required_resume_mode='native')
        operations = await execution.store.operations(previous['id'])
        manifest = build_resume_manifest(principal, previous, operations, resume, snapshot,
                                         execution.resume_support(previous['snapshot']['provider']), run_bound=run_bound)
    except ExecutionError as exc:
        cause = ('adapter_unsupported' if exc.reason_code == 'adapter_unsupported' else 'predecessor_mismatch') if run_bound else ('adapter_unsupported' if exc.reason_code in ('adapter_unsupported', 'provider_mismatch', 'runtime_mismatch') else 'not_retained')
        raise ResumeUnavailable(cause) from exc
    snapshot['resume_manifest'] = manifest.model_dump(mode='json')
    snapshot['resume_manifest_digest'] = content_digest(snapshot['resume_manifest'])
    snapshot['resume_mode'] = 'native'
    snapshot['required_resume_mode'] = 'native'
    return snapshot
