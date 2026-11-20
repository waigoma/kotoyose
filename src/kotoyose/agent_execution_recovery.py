"""Recover interrupted activations and retry physical stop confirmation."""


class AgentExecutionRecovery:
    def __init__(self, hub):
        self.service = hub

    async def recover(self) -> None:
        rows = await self.service.store.pool.fetch('''SELECT e.principal,a.attempt_id
            FROM kotoyose.agent_executions e JOIN kotoyose.agent_execution_activations a ON a.execution_id=e.id
            WHERE e.state!='terminal' AND a.state IN ('starting','running') AND a.attempt_id IS NOT NULL''')
        for row in rows:
            attempt = await self.service.execution.store.attempt(row['principal'], row['attempt_id'])
            if attempt['finished_at'] is not None:
                await self.service.runner.settle(attempt)
        await self.reconcile_pass(self.service.clock())

    async def reconcile_pass(self, now) -> int:
        rows = await self.service.store.pool.fetch('''SELECT e.principal,a.attempt_id
            FROM kotoyose.agent_executions e JOIN kotoyose.agent_execution_activations a
              ON a.execution_id=e.id AND a.nonce=e.current_nonce
            JOIN kotoyose.execution_attempts t ON t.id=a.attempt_id
            WHERE e.state!='terminal' AND t.finished_at IS NOT NULL AND t.stop_confirmation='unconfirmed' ''')
        count = 0
        for row in rows:
            attempt = await self.service.execution.store.attempt(row['principal'], row['attempt_id'])
            if await self.service.execution._stop_attempt(attempt) == 'confirmed':
                await self.service.execution.store.confirm_stop(row['attempt_id'])
                await self.service.runner.settle(attempt)
                count += 1
        # settle は停止が未確認だと activation を ended にして途中で返る。その後に別の経路
        # (起動時の照合など) が停止を確かめると、上の未確認の検索にも running の検索にも
        # かからない。世代がまだ上がっていない (settle の続きが走っていない) ものを settle し直す。
        settled = await self.service.store.pool.fetch('''SELECT e.principal,a.attempt_id
            FROM kotoyose.agent_executions e JOIN kotoyose.agent_execution_activations a
              ON a.execution_id=e.id AND a.nonce=e.current_nonce
            JOIN kotoyose.execution_attempts t ON t.id=a.attempt_id
            WHERE e.state!='terminal' AND a.state='ended' AND t.finished_at IS NOT NULL
              AND t.stop_confirmation='confirmed' AND t.side_effect_resolution='resolved'
              AND e.workspace_generation=t.generation''')
        for row in settled:
            attempt = await self.service.execution.store.attempt(row['principal'], row['attempt_id'])
            await self.service.runner.settle(attempt)
            count += 1
        # 最後の照合が call を保存した後、confirm_stop と settle の前に落ちることがある。
        # 停止は確認済みだが副作用が unresolved のまま残り、上の二つの検索のどちらにもかからない。
        # 未確認の call が残らない attempt だけを、停止の確認から settle までやり直す。
        saved = await self.service.store.pool.fetch('''SELECT e.principal,a.attempt_id
            FROM kotoyose.agent_executions e JOIN kotoyose.agent_execution_activations a
              ON a.execution_id=e.id AND a.nonce=e.current_nonce
            JOIN kotoyose.execution_attempts t ON t.id=a.attempt_id
            WHERE e.state!='terminal' AND t.finished_at IS NOT NULL AND t.stop_confirmation='confirmed'
              AND t.side_effect_resolution='unresolved'
              AND NOT EXISTS (SELECT 1 FROM kotoyose.execution_operations o WHERE o.attempt_id=t.id AND o.state<>'confirmed')''')
        for row in saved:
            await self.service.execution.store.confirm_stop(row['attempt_id'])
            attempt = await self.service.execution.store.attempt(row['principal'], row['attempt_id'])
            await self.service.runner.settle(attempt)
            count += 1
        return count
