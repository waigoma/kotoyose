"""互換 request と Native dispatch の不変対応。実行の正本は Native Attempt に置く。"""

from collections.abc import Awaitable, Callable
import json

import asyncpg

from .execution_contract import AgentExecution
from .execution_resources import ExecutionError, canonical_json


class CompatibilityRequestStore:
    """HTTP 応答消失後も同じ Grant / workspace 世代 / dispatch key を再利用する。"""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def initialize(self) -> None:
        """実行 ledger と同じ DB に adapter の対応表を保存する。"""
        await self.pool.execute("""
            CREATE TABLE IF NOT EXISTS kotoyose.compatibility_requests (
                principal text NOT NULL, request_key text NOT NULL,
                request_digest text NOT NULL, execution jsonb NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (principal, request_key)
            )
        """)

    async def prepare(
        self, principal: str, key: str, digest: str,
        create: Callable[[], Awaitable[AgentExecution]],
    ) -> AgentExecution:
        """dispatch より前に対応を記録し、異なる入力での key 再利用を拒む。"""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                canonical_json(["compatibility", principal, key]),
            )
            prior = await conn.fetchrow(
                "SELECT request_digest,execution FROM kotoyose.compatibility_requests "
                "WHERE principal=$1 AND request_key=$2", principal, key,
            )
            if prior:
                if prior["request_digest"] != digest:
                    raise ExecutionError("request_conflict", "Chat request key has different input or policy", 409)
                payload = prior["execution"]
                return AgentExecution.model_validate(json.loads(payload) if isinstance(payload, str) else payload)
            execution = await create()
            await conn.execute(
                "INSERT INTO kotoyose.compatibility_requests(principal,request_key,request_digest,execution) "
                "VALUES($1,$2,$3,$4::jsonb)",
                principal, key, digest, canonical_json(execution.model_dump(mode="json")),
            )
            return execution
