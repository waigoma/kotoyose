"""実操作直前の Grant・Attempt・workspace writer の共通照合。"""

import json
from datetime import datetime, timezone
import asyncpg

from .execution_resources import ExecutionError
from .execution_delegation import require_live_parent
from .agent_execution_authority import agent_execution_lease_guard


async def authorize_operation(conn: asyncpg.Connection, attempt_id: str, principal: str | None = None, *,
                              now: datetime | None = None, shared_provider: bool = False):
    """Grant → Attempt の lock 順を維持し、取消と古い世代の新規効果を拒否する。"""
    initial = await conn.fetchrow('SELECT * FROM kotoyose.execution_attempts WHERE id=$1', attempt_id)
    if initial is None or (principal is not None and initial['principal'] != principal):
        raise ExecutionError('invalid_request', 'Unknown attempt', 404)
    request = json.loads(initial['request'])
    is_agent_exec = initial['api_operation'] == 'agent_activation'
    if is_agent_exec:
        await agent_execution_lease_guard(conn, attempt_id, now or datetime.now(timezone.utc))
    grant = await conn.fetchrow(
        "SELECT revoked_at,(payload->>'expires_at')::timestamptz > now() "
        "AND (payload->>'not_before')::timestamptz <= now() AS valid FROM kotoyose.execution_resources "
        "WHERE principal=$1 AND ref=$2 FOR UPDATE", initial['principal'], request['execution_grant_ref'],
    )
    writer = attempt_id
    snapshot = json.loads(initial["snapshot"])
    if parent_id := snapshot.get("parent_attempt_id"):
        if not shared_provider:
            raise ExecutionError("permission_denied", "Shared read execution cannot admit this operation", 403)
        parent = await conn.fetchrow(
            "SELECT status,snapshot FROM kotoyose.execution_attempts WHERE id=$1 AND principal=$2 FOR UPDATE",
            parent_id, initial["principal"],
        )
        if parent is None:
            raise ExecutionError("permission_denied", "Shared workspace parent is unavailable", 403)
        require_live_parent({"status": parent["status"], "snapshot": json.loads(parent["snapshot"])}, snapshot)
        writer = parent_id
    row = await conn.fetchrow(
        'SELECT a.* FROM kotoyose.execution_attempts a JOIN kotoyose.execution_workspaces w ON w.id=a.workspace_id '
        'WHERE a.id=$1 AND w.generation=a.generation AND w.writer_attempt=$2 FOR UPDATE OF a', attempt_id, writer,
    )
    if row is None or row['status'] != 'running' or grant is None or grant['revoked_at'] or (not is_agent_exec and not grant['valid']):
        raise ExecutionError('permission_denied', 'Attempt no longer admits operations', 403)
    if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM kotoyose.execution_operations WHERE attempt_id=$1 AND state='unknown')", attempt_id):
        raise ExecutionError('request_conflict', 'Prior operation has an unresolved outcome', 409)
    return row
