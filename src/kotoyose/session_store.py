"""SDK SessionStore の Postgres 実装。

公式 examples (anthropics/claude-agent-sdk-python) の PostgresSessionStore を
kotoyose 用に移植したもの。テーブル名は kotoyose.claude_session_store 固定で運用する。

参考:
    https://github.com/anthropics/claude-agent-sdk-python/blob/main/examples/session_stores/postgres_session_store.py
    https://code.claude.com/docs/en/agent-sdk/session-storage
"""

from __future__ import annotations

import json
import re

import asyncpg
from claude_agent_sdk import (
    SessionKey,
    SessionListSubkeysKey,
    SessionStore,
    SessionStoreEntry,
    SessionStoreListEntry,
)

# テーブル名を SQL に補間するため、識別子文字のみ許容する正規表現。
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


class PostgresSessionStore(SessionStore):
    """Postgres-backed SessionStore implementation.

    既存 kotoyose の asyncpg.Pool を共有することで、追加のコネクション設定を不要にする。
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        table: str = "kotoyose.claude_session_store",
        max_session_bytes: int | None = None,
    ) -> None:
        if pool is None:
            raise ValueError("PostgresSessionStore requires 'pool'")
        if not _IDENT_RE.match(table):
            raise ValueError(
                f"table {table!r} must match [A-Za-z_][A-Za-z0-9_.]* "
                "(it is interpolated into SQL)"
            )
        self._pool = pool
        self._table = table
        self.max_session_bytes = max_session_bytes

    async def create_schema(self) -> None:
        """テーブルとインデックスを作成 (idempotent)。

        kotoyose.db.init_db でも同じ DDL を作るため、独立利用 (テスト等) のための
        補助メソッド。本番運用では init_db 経由で十分。
        """
        # NOTE: must stay in sync with src/kotoyose/db.py init_db DDL block
        await self._pool.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                project_key TEXT NOT NULL,
                session_id  TEXT NOT NULL,
                subpath     TEXT NOT NULL DEFAULT '',
                seq         BIGSERIAL,
                entry       JSONB NOT NULL,
                mtime       BIGINT NOT NULL,
                PRIMARY KEY (project_key, session_id, subpath, seq)
            );
            CREATE INDEX IF NOT EXISTS claude_session_store_list_idx
                ON {self._table} (project_key, session_id) WHERE subpath = '';
            """
        )

    async def append(
        self, key: SessionKey, entries: list[SessionStoreEntry]
    ) -> None:
        if not entries:
            return
        if self.max_session_bytes is None:
            await self._append(self._pool, key, entries)
            return
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", key['project_key']+':'+key['session_id'])
            state = await conn.fetchrow(
                f"SELECT COALESCE(sum(octet_length(entry::text)),0) AS bytes, "
                f"bool_or(entry->>'type'='kotoyose_native_session_unavailable') AS disabled FROM {self._table} "
                "WHERE project_key=$1 AND session_id=$2", key['project_key'], key['session_id'])
            if state['disabled']:
                return
            if state['bytes'] + sum(len(json.dumps(e).encode()) for e in entries) > self.max_session_bytes:
                # 不完全なprefixをnative再開へ渡さない。通常のcodingは中断しない。
                entries = [{'type': 'kotoyose_native_session_unavailable'}]
            await self._append(conn, key, entries)

    async def _append(self, connection, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        subpath = key.get("subpath") or ""
        await connection.execute(
            f"""
            INSERT INTO {self._table} (project_key, session_id, subpath, entry, mtime)
            SELECT $1, $2, $3, e,
                   (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint
            FROM unnest($4::jsonb[]) WITH ORDINALITY AS t(e, ord)
            ORDER BY ord
            """,
            key["project_key"],
            key["session_id"],
            subpath,
            [json.dumps(e) for e in entries],
        )

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        if self.max_session_bytes is not None:
            allowed = await self._pool.fetchval(
                f"SELECT COALESCE(sum(octet_length(entry::text)),0)<=$3 AND "
                f"NOT COALESCE(bool_or(entry->>'type'='kotoyose_native_session_unavailable'),false) FROM {self._table} "
                "WHERE project_key=$1 AND session_id=$2", key['project_key'], key['session_id'], self.max_session_bytes)
            if not allowed:
                return None
        rows = await self._pool.fetch(
            f"""
            SELECT entry FROM {self._table}
            WHERE project_key = $1 AND session_id = $2 AND subpath = $3
            ORDER BY seq
            """,
            key["project_key"],
            key["session_id"],
            key.get("subpath") or "",
        )
        if not rows:
            return None
        out: list[SessionStoreEntry] = []
        for row in rows:
            v = row["entry"]
            out.append(json.loads(v) if isinstance(v, (str, bytes)) else v)
        return out

    async def list_sessions(
        self, project_key: str
    ) -> list[SessionStoreListEntry]:
        rows = await self._pool.fetch(
            f"""
            SELECT session_id, MAX(mtime) AS mtime FROM {self._table}
            WHERE project_key = $1 AND subpath = ''
            GROUP BY session_id
            """,
            project_key,
        )
        return [
            {"session_id": r["session_id"], "mtime": int(r["mtime"])}
            for r in rows
        ]

    async def delete(self, key: SessionKey) -> None:
        # NOTE: 厳密には SDK Protocol 上 subpath="" は「main transcript のみ削除」を
        # 意味するが、本実装は falsy 比較で main + subagent をすべて cascade 削除する
        # (upstream examples/postgres_session_store.py と同じ挙動)。SDK が明示的に
        # subpath="" を送る運用が現れたら subpath is not None への切替を検討する。
        subpath = key.get("subpath")
        if subpath:
            await self._pool.execute(
                f"""
                DELETE FROM {self._table}
                WHERE project_key = $1 AND session_id = $2 AND subpath = $3
                """,
                key["project_key"],
                key["session_id"],
                subpath,
            )
            return
        await self._pool.execute(
            f"""
            DELETE FROM {self._table}
            WHERE project_key = $1 AND session_id = $2
            """,
            key["project_key"],
            key["session_id"],
        )

    async def list_subkeys(
        self, key: SessionListSubkeysKey
    ) -> list[str]:
        rows = await self._pool.fetch(
            f"""
            SELECT DISTINCT subpath FROM {self._table}
            WHERE project_key = $1 AND session_id = $2 AND subpath <> ''
            """,
            key["project_key"],
            key["session_id"],
        )
        return [r["subpath"] for r in rows]


# ---------------------------------------------------------------------------
# Module-level helpers — placed after PostgresSessionStore to keep
# definition order readable; these are called lazily, never at import time.
# ---------------------------------------------------------------------------


def _pool_provider() -> asyncpg.Pool | None:
    """kotoyose.db._pool を取得する間接化ヘルパ。テストでパッチしやすい。"""
    from . import db

    return db._pool


def get_default_store() -> PostgresSessionStore | None:
    """db._pool が利用可能なら共有 PostgresSessionStore インスタンスを返す。"""
    pool = _pool_provider()
    if pool is None:
        return None
    return PostgresSessionStore(pool=pool)
