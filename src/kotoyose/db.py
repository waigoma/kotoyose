"""
Postgres セッション永続化モジュール。

kotoyose.sessions テーブルへの CRUD を担当する。
DATABASE_URL が未設定の場合は全操作が no-op になり、
インメモリセッションのみで動作する。

テーブル定義 (初回起動時に自動作成):
    CREATE SCHEMA IF NOT EXISTS kotoyose;
    CREATE TABLE IF NOT EXISTS kotoyose.sessions (
        conversation_key  TEXT PRIMARY KEY,
        claude_session_id TEXT NOT NULL,
        provider          TEXT NOT NULL DEFAULT 'claude',
        runtime_session_id TEXT,
        model             TEXT NOT NULL,
        created_at        TIMESTAMPTZ DEFAULT NOW(),
        last_used_at      TIMESTAMPTZ DEFAULT NOW(),
        metadata          JSONB DEFAULT '{}'::jsonb
    );
"""

from __future__ import annotations

import asyncpg

from .config import DATABASE_URL
from .logging_setup import log

_pool: asyncpg.Pool | None = None


async def init_db() -> None:
    """接続プールを作成してテーブルを初期化する。起動時に1回呼ぶ。"""
    global _pool
    if DATABASE_URL is None:
        log.warning("db.skipped", reason="DATABASE_URL not set — in-memory only")
        return

    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with _pool.acquire() as conn:
        await conn.execute("""
            SELECT pg_advisory_xact_lock(hashtextextended('kotoyose-schema', 0));
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'harness')
                   AND NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'kotoyose') THEN
                    ALTER SCHEMA harness RENAME TO kotoyose;
                END IF;
            END
            $$;
            CREATE SCHEMA IF NOT EXISTS kotoyose;
            CREATE TABLE IF NOT EXISTS kotoyose.sessions (
                conversation_key  TEXT PRIMARY KEY,
                claude_session_id TEXT NOT NULL,
                provider          TEXT NOT NULL DEFAULT 'claude',
                runtime_session_id TEXT,
                model             TEXT NOT NULL,
                created_at        TIMESTAMPTZ DEFAULT NOW(),
                last_used_at      TIMESTAMPTZ DEFAULT NOW(),
                metadata          JSONB DEFAULT '{}'::jsonb
            );
            CREATE INDEX IF NOT EXISTS sessions_last_used
                ON kotoyose.sessions (last_used_at DESC);

            CREATE TABLE IF NOT EXISTS kotoyose.interactions (
                id                BIGSERIAL PRIMARY KEY,
                request_id        TEXT NOT NULL UNIQUE,
                conversation_key  TEXT,
                claude_session_id TEXT,
                provider          TEXT NOT NULL DEFAULT 'claude',
                runtime_session_id TEXT,
                model             TEXT NOT NULL,
                source            TEXT,
                received_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at      TIMESTAMPTZ,
                duration_ms       INTEGER,
                prompt            TEXT NOT NULL,
                response          TEXT,
                prompt_chars      INTEGER NOT NULL,
                response_chars    INTEGER,
                tokens_in         INTEGER,
                tokens_out        INTEGER,
                tokens_source     TEXT,
                streamed          BOOLEAN NOT NULL DEFAULT FALSE,
                session_event     TEXT,
                status            TEXT NOT NULL CHECK (status IN ('completed', 'failed', 'in_progress')),
                error_message     TEXT,
                metadata          JSONB NOT NULL DEFAULT '{}'::jsonb
            );
            CREATE INDEX IF NOT EXISTS idx_interactions_received_at
                ON kotoyose.interactions (received_at DESC);
            CREATE INDEX IF NOT EXISTS idx_interactions_conv_key
                ON kotoyose.interactions (conversation_key, received_at DESC);
            CREATE INDEX IF NOT EXISTS idx_interactions_model
                ON kotoyose.interactions (model, received_at DESC);
            CREATE INDEX IF NOT EXISTS idx_interactions_source
                ON kotoyose.interactions (source, received_at DESC);

            CREATE TABLE IF NOT EXISTS kotoyose.interaction_events (
                id             BIGSERIAL PRIMARY KEY,
                interaction_id BIGINT NOT NULL
                               REFERENCES kotoyose.interactions(id) ON DELETE CASCADE,
                seq            INT NOT NULL,
                ts             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                kind           TEXT NOT NULL,
                tool_name      TEXT,
                summary        TEXT,
                payload        JSONB
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_interaction_events_interaction_seq
                ON kotoyose.interaction_events (interaction_id, seq);

            -- NOTE: must stay in sync with src/kotoyose/session_store.py PostgresSessionStore.create_schema
            CREATE TABLE IF NOT EXISTS kotoyose.claude_session_store (
                project_key  TEXT NOT NULL,
                session_id   TEXT NOT NULL,
                subpath      TEXT NOT NULL DEFAULT '',
                seq          BIGSERIAL,
                entry        JSONB NOT NULL,
                mtime        BIGINT NOT NULL,
                PRIMARY KEY (project_key, session_id, subpath, seq)
            );
            CREATE INDEX IF NOT EXISTS claude_session_store_list_idx
                ON kotoyose.claude_session_store (project_key, session_id)
                WHERE subpath = '';
        """)
        # 既存 DB への migration: claude_session_id が無い場合に追加
        await conn.execute("""
            ALTER TABLE kotoyose.interactions
            ADD COLUMN IF NOT EXISTS claude_session_id TEXT
        """)
        await conn.execute("""
            ALTER TABLE kotoyose.sessions
            ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'claude'
        """)
        await conn.execute("""
            ALTER TABLE kotoyose.sessions
            ADD COLUMN IF NOT EXISTS runtime_session_id TEXT
        """)
        await conn.execute("""
            ALTER TABLE kotoyose.interactions
            ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'claude'
        """)
        await conn.execute("""
            ALTER TABLE kotoyose.interactions
            ADD COLUMN IF NOT EXISTS runtime_session_id TEXT
        """)
        await conn.execute("""
            UPDATE kotoyose.sessions
            SET runtime_session_id = claude_session_id
            WHERE runtime_session_id IS NULL
        """)
        await conn.execute("""
            UPDATE kotoyose.interactions
            SET runtime_session_id = claude_session_id
            WHERE runtime_session_id IS NULL
              AND claude_session_id IS NOT NULL
        """)
        await conn.execute("ALTER TABLE kotoyose.interactions ADD COLUMN IF NOT EXISTS api_key_name TEXT")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_interactions_key_session ON kotoyose.interactions (conversation_key, api_key_name, received_at)")
        # soft-delete 用 deleted_at + 部分インデックス
        await conn.execute("""
            ALTER TABLE kotoyose.interactions
            ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_interactions_visible_session
                ON kotoyose.interactions (conversation_key, claude_session_id, received_at DESC)
                WHERE deleted_at IS NULL
        """)
        await conn.execute("""
            DELETE FROM kotoyose.interaction_events
            WHERE ts < NOW() - INTERVAL '14 days'
        """)
        from .api_key_store import ApiKeyStore
        from .connection_state_store import ConnectionStateStore
        from .secret_store import SecretStore

        await ApiKeyStore(conn).initialize()
        await SecretStore(conn).initialize()
        await ConnectionStateStore(conn).initialize()
    log.info("db.ready")


async def close_db() -> None:
    """接続プールを閉じる。シャットダウン時に呼ぶ。"""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def upsert_session(
    conversation_key: str,
    claude_session_id: str,
    model: str,
    provider: str = "claude",
    runtime_session_id: str | None = None,
    session_identity: str | None = None,
) -> None:
    """セッション情報を upsert する。DB 未設定時は no-op。"""
    if _pool is None:
        return
    effective_runtime_id = runtime_session_id or claude_session_id
    await _pool.execute(
        """
        INSERT INTO kotoyose.sessions
            (conversation_key, claude_session_id, provider, runtime_session_id, model, last_used_at, metadata)
        VALUES (
            $1, $2, $3, $4, $5, NOW(),
            CASE WHEN $6::text IS NULL THEN '{}'::jsonb
                 ELSE jsonb_build_object('session_identity', $6::text)
            END
        )
        ON CONFLICT (conversation_key)
        DO UPDATE SET
            claude_session_id = EXCLUDED.claude_session_id,
            provider          = EXCLUDED.provider,
            runtime_session_id = EXCLUDED.runtime_session_id,
            model             = EXCLUDED.model,
            metadata          = CASE
                WHEN $6::text IS NULL THEN COALESCE(kotoyose.sessions.metadata, '{}'::jsonb)
                ELSE jsonb_set(
                    COALESCE(kotoyose.sessions.metadata, '{}'::jsonb),
                    '{session_identity}',
                    to_jsonb($6::text),
                    true
                )
            END,
            last_used_at      = NOW()
        """,
        conversation_key,
        claude_session_id,
        provider,
        effective_runtime_id,
        model,
        session_identity,
    )


async def delete_session(conversation_key: str) -> bool:
    """kotoyose.sessions の 1 行を削除する。

    削除に成功したら True、対象行が存在しなければ False を返す。
    DB 未設定時は no-op で False。
    """
    if _pool is None:
        return False
    result = await _pool.execute(
        "DELETE FROM kotoyose.sessions WHERE conversation_key = $1",
        conversation_key,
    )
    return result.endswith(" 1")  # asyncpg は "DELETE 1" / "DELETE 0" を返す


async def lookup_session(
    conversation_key: str,
    provider: str | None = "claude",
    model: str | None = None,
    session_identity: str | None = None,
) -> str | None:
    """conversation_key から runtime session id を引く。無ければ None。

    UPDATE ... RETURNING で lookup と last_used_at 更新を1クエリに統合し、
    TOCTOU (time-of-check/time-of-use) の競合を回避する。
    """
    if _pool is None:
        return None
    row = await _pool.fetchrow(
        """
        UPDATE kotoyose.sessions
        SET last_used_at = NOW()
        WHERE conversation_key = $1
          AND ($2::text IS NULL OR provider = $2)
          AND ($3::text IS NULL OR model = $3)
          AND ($4::text IS NULL OR metadata->>'session_identity' = $4)
        RETURNING claude_session_id
        """,
        conversation_key,
        provider,
        model,
        session_identity,
    )
    return row["claude_session_id"] if row else None


async def lookup_runtime_session(
    conversation_key: str,
    provider: str = "claude",
    model: str | None = None,
    session_identity: str | None = None,
) -> str | None:
    """conversation_key / provider から provider 固有 session id を引く。

    移行期間中は runtime_session_id が NULL の既存 Claude 行に対して
    claude_session_id を fallback として返す。
    """
    if _pool is None:
        return None
    row = await _pool.fetchrow(
        """
        UPDATE kotoyose.sessions
        SET last_used_at = NOW()
        WHERE conversation_key = $1
          AND provider = $2
          AND ($3::text IS NULL OR model = $3)
          AND ($4::text IS NULL OR metadata->>'session_identity' = $4)
        RETURNING COALESCE(runtime_session_id, claude_session_id) AS runtime_session_id
        """,
        conversation_key,
        provider,
        model,
        session_identity,
    )
    return row["runtime_session_id"] if row else None
