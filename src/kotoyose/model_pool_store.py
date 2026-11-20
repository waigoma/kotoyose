"""pool の状態と選択履歴の永続化。状態は Kotoyose が既に持つ証拠からだけ更新し、問い合わせは行わない。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kotoyose.model_pool_state (
    pool text PRIMARY KEY,
    state text NOT NULL CHECK (state IN ('available','limited','auth_expired','unknown')),
    until timestamptz,
    observed_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS kotoyose.model_pool_selections (
    pool_alias text NOT NULL,
    candidate text NOT NULL,
    selected_at timestamptz NOT NULL,
    selections bigint NOT NULL DEFAULT 1 CHECK (selections >= 1),
    PRIMARY KEY (pool_alias, candidate)
);
"""


class ModelPoolStore:
    """pool 名 (route の pool か route 名) の状態と、pool alias ごとの候補の選択履歴。"""

    def __init__(self, pool):
        self.pool = pool

    async def initialize(self) -> None:
        await self.pool.execute(_SCHEMA)

    async def states(self, names: list[str]) -> dict[str, dict]:
        rows = await self.pool.fetch(
            "SELECT pool,state,until,observed_at FROM kotoyose.model_pool_state WHERE pool = ANY($1::text[])", names)
        return {row["pool"]: dict(row) for row in rows}

    async def observe(self, pool: str, state: str, until: datetime | None, observed_at: datetime) -> None:
        """より古い観測では、新しい観測を上書きしない (同時に終わる attempt の順序は保証されない)。"""
        await self.pool.execute(
            """INSERT INTO kotoyose.model_pool_state(pool,state,until,observed_at) VALUES($1,$2,$3,$4)
            ON CONFLICT (pool) DO UPDATE SET state=EXCLUDED.state, until=EXCLUDED.until,
                observed_at=EXCLUDED.observed_at, updated_at=now()
            WHERE kotoyose.model_pool_state.observed_at <= EXCLUDED.observed_at""",
            pool, state, until, observed_at)

    async def select(self, pool_alias: str, pools: list[str],
                     choose: Callable[[dict[str, dict], dict[str, datetime]], list[str]]) -> list[str]:
        """読み取りから選択の記録までを pool alias 単位の排他の下で行い、同時の受理を同じ候補へ寄せない。"""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", "kotoyose-model-pool:" + pool_alias)
            states = {row["pool"]: dict(row) for row in await conn.fetch(
                "SELECT pool,state,until,observed_at FROM kotoyose.model_pool_state WHERE pool = ANY($1::text[])", pools)}
            last = {row["candidate"]: row["selected_at"] for row in await conn.fetch(
                "SELECT candidate,selected_at FROM kotoyose.model_pool_selections WHERE pool_alias=$1", pool_alias)}
            ordered = choose(states, last)
            if ordered:
                await self._record(conn, pool_alias, ordered[0])
            return ordered

    async def last_selected(self, pool_aliases: list[str]) -> dict[tuple[str, str], datetime]:
        rows = await self.pool.fetch(
            "SELECT pool_alias,candidate,selected_at FROM kotoyose.model_pool_selections WHERE pool_alias = ANY($1::text[])",
            pool_aliases)
        return {(row["pool_alias"], row["candidate"]): row["selected_at"] for row in rows}

    async def record_selection(self, pool_alias: str, candidate: str) -> None:
        async with self.pool.acquire() as conn:
            await self._record(conn, pool_alias, candidate)

    @staticmethod
    async def _record(conn, pool_alias: str, candidate: str) -> None:
        await conn.execute(
            """INSERT INTO kotoyose.model_pool_selections(pool_alias,candidate,selected_at) VALUES($1,$2,clock_timestamp())
            ON CONFLICT (pool_alias,candidate) DO UPDATE SET selected_at=clock_timestamp(),
                selections=kotoyose.model_pool_selections.selections+1""", pool_alias, candidate)
