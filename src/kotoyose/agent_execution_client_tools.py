"""Execution-scoped access to the existing client tool ledger."""
from datetime import datetime
from typing import Literal

from pydantic import Field

from .client_tool_bridge import ClientToolCallOutcome, ClientToolResultRequest, ClientToolReconciliationRequest
from .execution_contract import ContractModel
from .agent_execution_contract import AgentExecutionError
from .agent_execution_digest import agent_utc
from .agent_execution_store import _json


class AgentExecutionClientConnectionRequest(ContractModel):
    """Execution に固定した client と、その接続世代を表す。"""
    client_key: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0, le=2**31 - 1, strict=True)


class AgentExecutionClientResultRequest(AgentExecutionClientConnectionRequest):
    """配送済み call の identity と適用結果を表す。"""
    activation_nonce: str = Field(min_length=1, max_length=256)
    tool_ref: str = Field(min_length=1, max_length=512)
    arguments_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    disposition: Literal['applied', 'not_applied', 'unknown']
    result: dict


class AgentExecutionClientReconciliationRequest(AgentExecutionClientResultRequest):
    """不明な適用結果を、証拠を伴う確定結果として照合する。"""
    disposition: Literal['applied', 'not_applied']
    evidence_ref: str = Field(min_length=1, max_length=2048)


class AgentExecutionClientPolicy:
    """Attempt 単位の bridge に execution の世代と再生規則を提供する。"""
    def __init__(self, hub):
        self.service = hub

    async def is_agent_exec(self, attempt_id):
        """Attempt が Hub execution の activation に属するか返す。"""
        return bool(await self.service.store.pool.fetchval(
            'SELECT EXISTS(SELECT 1 FROM kotoyose.agent_execution_activations WHERE attempt_id=$1)', attempt_id))

    async def min_generation(self, attempt_id, client_key):
        """以前の activation で使った世代を越える最小値を返す。"""
        return await self.min_generation_in_transaction(self.service.store.pool, attempt_id, client_key)

    async def min_generation_in_transaction(self, conn, attempt_id, client_key):
        """取得済み connection を使い、同じ transaction 内で最小世代を調べる。"""
        return int(await conn.fetchval('''SELECT COALESCE(MAX(c.generation)+1,0)
            FROM kotoyose.agent_execution_activations current
            JOIN kotoyose.agent_execution_activations previous ON previous.execution_id=current.execution_id
                AND previous.ordinal<current.ordinal
            JOIN kotoyose.execution_client_connections c ON c.attempt_id=previous.attempt_id
            WHERE current.attempt_id=$1 AND c.client_key=$2''', attempt_id, client_key))

    async def replay_for(self, attempt_id, tool_ref, arguments_digest):
        """照合済み結果を一度だけ消費し、該当しなければ None を返す。"""
        async with self.service.store.pool.acquire() as conn, conn.transaction():
            return await self.replay_in_transaction(conn, attempt_id, tool_ref, arguments_digest)

    async def replay_in_transaction(self, conn, attempt_id, tool_ref, arguments_digest):
        """呼び出し元の transaction で照合済み結果を一度だけ消費する。"""
        row = await conn.fetchrow('''SELECT r.call_id,c.result FROM kotoyose.agent_execution_client_replays r
            JOIN kotoyose.agent_execution_activations a ON a.execution_id=r.execution_id AND a.attempt_id=$1
            JOIN kotoyose.agent_execution_activations previous ON previous.execution_id=r.execution_id
            JOIN kotoyose.execution_client_calls c ON c.id=r.call_id AND c.attempt_id=previous.attempt_id
            WHERE previous.ordinal<a.ordinal AND r.tool_ref=$2 AND r.arguments_digest=$3
              AND r.consumed_by IS NULL AND c.state='succeeded' AND c.reconciliation_ref IS NOT NULL
            ORDER BY previous.ordinal,c.created_at FOR UPDATE OF r SKIP LOCKED LIMIT 1''',
            attempt_id, tool_ref, arguments_digest)
        if not row:
            return None
        await conn.execute('UPDATE kotoyose.agent_execution_client_replays SET consumed_by=$2 WHERE call_id=$1', row['call_id'], attempt_id)
        return ClientToolCallOutcome('succeeded', _json(row['result']), call_id=row['call_id'])


class AgentExecutionClientTools:
    """principal と execution key を検証して client tool の台帳へアクセスする。"""
    def __init__(self, hub):
        self.service = hub

    async def _current(self, principal, key):
        row = await self.service.store.pool.fetchrow('''SELECT e.id,e.current_nonce,a.attempt_id
            FROM kotoyose.agent_executions e JOIN kotoyose.agent_execution_activations a
              ON a.execution_id=e.id AND a.nonce=e.current_nonce
            WHERE e.principal=$1 AND e.idempotency_key=$2''', principal, key)
        if not row:
            raise AgentExecutionError('not_found', 'Execution not found', 404)
        if not row['attempt_id']:
            raise AgentExecutionError('request_conflict', 'Activation has no attempt', 409)
        return row

    @staticmethod
    def _view(value, execution_id, nonce):
        # wire の日時は UTC の Z で渡す。datetime のまま返すと JSON で +00:00 になり、Hub が読めない。
        return {**{key: agent_utc(val) if isinstance(val, datetime) else val for key, val in value.items() if key != 'attempt_id'},
                'execution_id': str(execution_id), 'activation_nonce': nonce}

    async def connect(self, principal, key, raw):
        """現 activation の接続を取得し、Attempt ID を除いた応答を返す。"""
        request = AgentExecutionClientConnectionRequest.model_validate(raw)
        row = await self._current(principal, key)
        connection = await self.service.execution.store.client_tools.register_connection(
            principal, row['attempt_id'], request.client_key, request.generation)
        return {'schema_version': 'agent_execution_v1', 'connection': self._view(connection, row['id'], row['current_nonce'])}

    async def calls(self, principal, key, client_key, generation, limit):
        """現接続の配送可能な call を最大 limit 件取得し、送信済みにする。"""
        AgentExecutionClientConnectionRequest(client_key=client_key, generation=generation)
        row = await self._current(principal, key)
        calls = await self.service.execution.store.client_tools.pending(principal, row['attempt_id'], client_key, generation, limit)
        return {'schema_version': 'agent_execution_v1', 'calls': [self._view(c, row['id'], row['current_nonce']) for c in calls]}

    async def unresolved(self, principal, key, limit):
        """全 activation の unknown call を状態を変えずに最大 limit 件返す。"""
        execution = await self.service.store.pool.fetchrow(
            'SELECT id FROM kotoyose.agent_executions WHERE principal=$1 AND idempotency_key=$2', principal, key)
        if not execution:
            raise AgentExecutionError('not_found', 'Execution not found', 404)
        rows = await self.service.store.pool.fetch('''SELECT c.*,a.nonce AS activation_nonce
            FROM kotoyose.execution_client_calls c JOIN kotoyose.agent_execution_activations a ON a.attempt_id=c.attempt_id
            WHERE a.execution_id=$1 AND c.principal=$2 AND c.state='unknown'
            ORDER BY c.created_at,c.id LIMIT $3''', execution['id'], principal, limit)
        calls = [self._view(self.service.execution.store.client_tools._call(r), execution['id'], r['activation_nonce']) for r in rows]
        return {'schema_version': 'agent_execution_v1', 'calls': calls}

    async def result(self, principal, key, call_id, raw):
        """現 activation の配送結果を記録する。古い activation は拒否する。"""
        return await self._save(principal, key, call_id, AgentExecutionClientResultRequest.model_validate(raw), False)

    async def reconcile(self, principal, key, call_id, raw):
        """証拠付きで過去の call を照合し、停止済みなら settlement を再試行する。"""
        return await self._save(principal, key, call_id, AgentExecutionClientReconciliationRequest.model_validate(raw), True)

    async def _save(self, principal, key, call_id, request, reconciliation):
        row = await self._current(principal, key)
        call = await self.service.store.pool.fetchrow('''SELECT c.attempt_id,a.nonce FROM kotoyose.execution_client_calls c
            JOIN kotoyose.agent_execution_activations a ON a.attempt_id=c.attempt_id
            WHERE c.id=$1 AND c.principal=$2 AND a.execution_id=$3''', call_id, principal, row['id'])
        if not call:
            raise AgentExecutionError('not_found', 'Client call not found', 404)
        if request.activation_nonce != call['nonce'] or (not reconciliation and call['nonce'] != row['current_nonce']):
            raise AgentExecutionError('stale_activation', 'Client call activation is stale', 409)
        data = request.model_dump(exclude={'activation_nonce'})
        model = ClientToolReconciliationRequest if reconciliation else ClientToolResultRequest
        inner = model.model_validate({'schema_version': '1', **data})
        result = await self.service.execution.store.client_tools.save_result(
            principal, call['attempt_id'], call_id, inner.client_key, inner.generation, inner.result,
            tool_ref=inner.tool_ref, arguments_digest=inner.arguments_digest, disposition=inner.disposition,
            reconciliation_ref=inner.evidence_ref if reconciliation else None,
            agent_execution_context=(row['id'], call['nonce'], row['attempt_id'], reconciliation))
        if reconciliation:
            attempt = await self.service.execution.store.attempt(principal, call['attempt_id'])
            if attempt['finished_at'] is not None and attempt['stop_confirmation'] == 'confirmed':
                await self.service.execution.store.confirm_stop(call['attempt_id'])
                await self.service.runner.settle(attempt)
        return {'schema_version': 'agent_execution_v1', 'call': self._view(result, row['id'], call['nonce'])}
