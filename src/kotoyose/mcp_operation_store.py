"""外部 MCP 操作の journal。process 停止と外部効果の解決を混同しない。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Awaitable, Callable
from uuid import uuid4

import asyncpg
from jsonschema import ValidationError

from .execution_operation_guard import authorize_operation
from .execution_resources import ExecutionError, canonical_json
from .mcp_contract import PinnedMcpTool, mcp_content_digest, validate_pinned_mcp_arguments


_SCHEMA = """
CREATE TABLE IF NOT EXISTS kotoyose.execution_mcp_calls (
    id text PRIMARY KEY REFERENCES kotoyose.execution_operations(id),
    principal text NOT NULL, attempt_id text NOT NULL REFERENCES kotoyose.execution_attempts(id),
    generation bigint NOT NULL, binding_ref text NOT NULL, tool_ref text NOT NULL,
    arguments jsonb NOT NULL, arguments_digest text NOT NULL,
    state text NOT NULL CHECK(state IN ('prepared','sent','confirmed','unknown')),
    disposition text CHECK(disposition IN ('applied','not_applied')),
    result jsonb, evidence_ref text, created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS execution_mcp_calls_attempt_idx ON kotoyose.execution_mcp_calls(attempt_id,created_at,id);
"""


class McpOperationRepository:
    """認証済み Attempt 内の固定 Tool だけを、送信前に記録する。"""

    def __init__(self, pool: asyncpg.Pool, event_sink: Callable[..., Awaitable[None]]):
        self.pool, self.event_sink = pool, event_sink

    async def initialize(self):
        """既存 Attempt の台帳とは分離した外部呼び出し行を作る。"""
        await self.pool.execute(_SCHEMA)

    async def list_calls(self, principal: str, attempt_id: str, after: str | None, limit: int) -> list[dict]:
        """秘密を含み得る arguments / result 本文を除き、所有者に journal を返す。"""
        async with self.pool.acquire() as conn:
            if not await conn.fetchval('SELECT id FROM kotoyose.execution_attempts WHERE id=$1 AND principal=$2', attempt_id, principal):
                raise ExecutionError('invalid_request', 'Unknown Attempt', 404)
            if after is not None and not await conn.fetchval('SELECT id FROM kotoyose.execution_mcp_calls WHERE id=$1 AND attempt_id=$2', after, attempt_id):
                raise ExecutionError('invalid_request', 'Unknown MCP call cursor', 400)
            rows = await conn.fetch(
                'SELECT * FROM kotoyose.execution_mcp_calls WHERE attempt_id=$1 AND ($2::text IS NULL OR '
                '(created_at,id) > (SELECT created_at,id FROM kotoyose.execution_mcp_calls WHERE id=$2)) '
                'ORDER BY created_at,id LIMIT $3', attempt_id, after, limit,
            )
            return [mcp_call_view(dict(row)) for row in rows]

    async def prepare(self, principal: str, attempt_id: str, tool_ref: str, arguments: dict) -> dict:
        """Argument の schema と上限を検査し、Call と Operation を同時に確定する。"""
        if not isinstance(arguments, dict):
            raise ExecutionError('invalid_request', 'MCP tool arguments must be an object', 400)
        try:
            encoded = canonical_json(arguments)
        except (TypeError, ValueError) as exc:
            raise ExecutionError('invalid_request', 'MCP arguments must be finite JSON', 400) from exc
        async with self.pool.acquire() as conn, conn.transaction():
            attempt = await authorize_operation(conn, attempt_id, principal)
            snapshot = json.loads(attempt['snapshot'])
            spec = next((item for item in snapshot.get('mcp_tools', []) if item['tool_ref'] == tool_ref), None)
            if spec is None:
                raise ExecutionError('permission_denied', 'MCP tool is not pinned in Attempt', 403)
            if datetime.fromisoformat(snapshot['deadline']) <= datetime.now(timezone.utc):
                raise ExecutionError('permission_denied', 'MCP execution deadline passed', 403)
            ceiling = spec['tool']['ceiling']
            if len(encoded.encode()) > ceiling['max_argument_bytes']:
                raise ExecutionError('invalid_request', 'MCP arguments exceed pinned ceiling', 400)
            try:
                validate_pinned_mcp_arguments(PinnedMcpTool.model_validate(spec['tool']), arguments)
            except ValidationError as exc:
                raise ExecutionError('permission_denied', 'MCP arguments exceed allowed schema', 403) from exc
            if await conn.fetchval('SELECT count(*) FROM kotoyose.execution_mcp_calls WHERE attempt_id=$1', attempt_id) >= 1000:
                raise ExecutionError('request_conflict', 'MCP call limit reached', 409)
            identity = str(uuid4())
            await conn.execute("INSERT INTO kotoyose.execution_operations(id,attempt_id,kind,state) VALUES($1,$2,'mcp_tool','prepared')", identity, attempt_id)
            row = await conn.fetchrow(
                "INSERT INTO kotoyose.execution_mcp_calls(id,principal,attempt_id,generation,binding_ref,tool_ref,arguments,arguments_digest,state) "
                "VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8,'prepared') RETURNING *", identity, principal, attempt_id,
                attempt['generation'], spec['binding_ref'], tool_ref, encoded, mcp_content_digest(arguments),
            )
            await self.event_sink(conn, attempt_id, 'mcp_tool_prepared', {'operation_id': identity, 'tool_ref': tool_ref, 'arguments_digest': row['arguments_digest']})
            return dict(row)

    async def mark_sent(self, principal: str, attempt_id: str, call_id: str) -> None:
        """最後に Grant と世代を再確認してから、外部送信の可能性を durable に記録する。"""
        async with self.pool.acquire() as conn, conn.transaction():
            await authorize_operation(conn, attempt_id, principal)
            changed = await conn.fetchval("UPDATE kotoyose.execution_mcp_calls SET state='sent' WHERE id=$1 AND principal=$2 AND attempt_id=$3 AND state='prepared' RETURNING id", call_id, principal, attempt_id)
            if not changed:
                raise ExecutionError('request_conflict', 'MCP call is already sent or resolved', 409)
            await conn.execute("UPDATE kotoyose.execution_operations SET state='sent',updated_at=now() WHERE id=$1", call_id)
            await self.event_sink(conn, attempt_id, 'mcp_tool_sent', {'operation_id': call_id})

    async def finish(self, principal: str, attempt_id: str, call_id: str, disposition: str | None, result: dict) -> dict:
        """応答不明の効果は unknown に固定する。transport のエラー本文は保存しない。"""
        async with self.pool.acquire() as conn, conn.transaction():
            return await self._finish(conn, principal, attempt_id, call_id, disposition, result)

    async def _finish(self, conn, principal, attempt_id, call_id, disposition, result, evidence_ref=None):
        attempt = await conn.fetchrow('SELECT * FROM kotoyose.execution_attempts WHERE id=$1 AND principal=$2 FOR UPDATE', attempt_id, principal)
        row = await conn.fetchrow('SELECT * FROM kotoyose.execution_mcp_calls WHERE id=$1 AND attempt_id=$2 AND principal=$3 FOR UPDATE', call_id, attempt_id, principal)
        if attempt is None or row is None:
            raise ExecutionError('invalid_request', 'Unknown MCP call', 404)
        if disposition not in {None, 'applied', 'not_applied'} or not isinstance(result, dict) or len(canonical_json(result).encode()) > 1024 * 1024:
            raise ExecutionError('invalid_request', 'Invalid MCP result', 400)
        if row['state'] == 'confirmed':
            if (row['disposition'], json.loads(row['result']), row['evidence_ref']) != (disposition, result, evidence_ref):
                raise ExecutionError('request_conflict', 'MCP result contradicts recorded evidence', 409)
            return dict(row)
        if row['state'] == 'prepared' and disposition != 'not_applied':
            raise ExecutionError('request_conflict', 'Unsent MCP call cannot have an applied result', 409)
        if row['state'] == 'unknown' and disposition is not None and evidence_ref is None:
            raise ExecutionError('request_conflict', 'Unknown MCP result requires reconciliation evidence', 409)
        state = 'unknown' if disposition is None else 'confirmed'
        updated = await conn.fetchrow(
            'UPDATE kotoyose.execution_mcp_calls SET state=$2,disposition=$3,result=$4::jsonb,evidence_ref=$5,finished_at=now() WHERE id=$1 RETURNING *',
            call_id, state, disposition, canonical_json(result), evidence_ref,
        )
        await conn.execute('UPDATE kotoyose.execution_operations SET state=$2,result=$3::jsonb,updated_at=now() WHERE id=$1', call_id, state, canonical_json({'disposition': disposition, 'result_digest': mcp_content_digest(result), 'evidence_ref': evidence_ref}))
        await self.event_sink(conn, attempt_id, 'mcp_tool_result', {'operation_id': call_id, 'state': state, 'disposition': disposition, 'evidence_ref': evidence_ref})
        return dict(updated)

    async def cancel_attempt(self, principal: str, attempt_id: str) -> None:
        """未送信は効果なし、送信済みは不明へ。停止だけで remote write を成功扱いしない。"""
        async with self.pool.acquire() as conn, conn.transaction():
            await self.cancel_in_transaction(conn, principal, attempt_id)

    async def cancel_in_transaction(self, conn, principal: str, attempt_id: str) -> None:
        """Attempt 終端更新と同じ lock を保持し、隙間に新規送信を受け付けない。"""
        attempt = await conn.fetchrow('SELECT id FROM kotoyose.execution_attempts WHERE id=$1 AND principal=$2 FOR UPDATE', attempt_id, principal)
        if attempt is None:
            return
        calls = await conn.fetch("SELECT id,state FROM kotoyose.execution_mcp_calls WHERE attempt_id=$1 AND state IN ('prepared','sent') FOR UPDATE", attempt_id)
        for call in calls:
            await self._finish(conn, principal, attempt_id, call['id'], 'not_applied' if call['state'] == 'prepared' else None, {'error': 'attempt_stopped'})

    async def reconcile(self, principal: str, attempt_id: str, call_id: str, *, tool_ref: str, arguments_digest: str,
                        generation: int, disposition: str, result: dict, evidence_ref: str) -> dict:
        """Caller の照合証拠を同じ操作へ束縛し、別の効果として再送しない。"""
        if not evidence_ref.strip() or len(evidence_ref) > 2048 or disposition not in {'applied', 'not_applied'}:
            raise ExecutionError('invalid_request', 'MCP reconciliation evidence is required', 400)
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.fetchrow('SELECT id FROM kotoyose.execution_attempts WHERE id=$1 AND principal=$2 FOR UPDATE', attempt_id, principal)
            row = await conn.fetchrow('SELECT * FROM kotoyose.execution_mcp_calls WHERE id=$1 AND attempt_id=$2 AND principal=$3 FOR UPDATE', call_id, attempt_id, principal)
            if row is None:
                raise ExecutionError('invalid_request', 'Unknown MCP call', 404)
            if (row['tool_ref'], row['arguments_digest'], row['generation']) != (tool_ref, arguments_digest, generation) or row['state'] not in {'unknown', 'confirmed'}:
                raise ExecutionError('request_conflict', 'MCP reconciliation identity changed', 409)
            return await self._finish(conn, principal, attempt_id, call_id, disposition, result, evidence_ref)


def mcp_call_view(row: dict) -> dict:
    """照合に必要な identity と結果 digest を返し、remote content を応答に混ぜない。"""
    result = json.loads(row['result']) if isinstance(row['result'], str) else row['result']
    return {
        'id': row['id'], 'attempt_id': row['attempt_id'], 'generation': row['generation'],
        'binding_ref': row['binding_ref'], 'tool_ref': row['tool_ref'], 'arguments_digest': row['arguments_digest'],
        'state': row['state'], 'disposition': row['disposition'],
        'result_digest': mcp_content_digest(result) if result is not None else None,
        'evidence_ref': row['evidence_ref'], 'created_at': row['created_at'].isoformat(),
        'finished_at': row['finished_at'].isoformat() if row['finished_at'] else None,
    }
