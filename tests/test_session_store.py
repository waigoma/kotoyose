"""kotoyose.claude_session_store テーブル DDL と PostgresSessionStore のテスト。

実 Postgres conformance は dev 環境で別途実行する想定:

    import asyncpg
    from claude_agent_sdk.testing import run_session_store_conformance
    from kotoyose.session_store import PostgresSessionStore
    pool = await asyncpg.create_pool("postgresql://...")
    store = PostgresSessionStore(pool=pool)
    await store.create_schema()
    await run_session_store_conformance(lambda: store)
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kotoyose import db
from kotoyose.config import build_options


@pytest.fixture(autouse=True)
def reset_db_pool():
    """各テスト後に db._pool をリセットしてテスト間の状態漏洩を防ぐ。"""
    yield
    db._pool = None


@pytest.mark.asyncio
async def test_init_db_creates_claude_session_store_table():
    """init_db() の execute で claude_session_store テーブル作成 SQL が含まれる。"""
    fake_conn = AsyncMock()
    fake_pool = MagicMock()
    fake_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("kotoyose.db.DATABASE_URL", "postgresql://fake/url"),
        patch("kotoyose.db.asyncpg.create_pool", new=AsyncMock(return_value=fake_pool)),
    ):
        await db.init_db()

    sql_executed = " ".join(call.args[0] for call in fake_conn.execute.await_args_list)
    # 列・キーワードの存在確認
    assert "kotoyose.claude_session_store" in sql_executed
    assert "project_key" in sql_executed
    assert "session_id" in sql_executed
    assert "subpath" in sql_executed
    assert "entry" in sql_executed
    assert "mtime" in sql_executed
    assert "JSONB" in sql_executed.upper()
    assert "BIGSERIAL" in sql_executed.upper()

    # 構造の不変条件 (空白を正規化してから比較し DDL 体裁の些細な変更には頑健にする)
    normalized = " ".join(sql_executed.split())
    assert "PRIMARY KEY (project_key, session_id, subpath, seq)" in normalized
    assert "subpath TEXT NOT NULL DEFAULT ''" in normalized
    assert "claude_session_store_list_idx" in normalized
    assert "WHERE subpath = ''" in normalized


from kotoyose.session_store import PostgresSessionStore


@pytest.fixture
def fake_pool():
    pool = MagicMock()
    pool.execute = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    return pool


@pytest.mark.asyncio
async def test_postgres_session_store_create_schema_uses_kotoyose_schema(fake_pool):
    """create_schema() で kotoyose schema 配下のテーブルを CREATE する SQL が発行される。"""
    store = PostgresSessionStore(pool=fake_pool)
    await store.create_schema()

    args = fake_pool.execute.await_args_list
    assert len(args) >= 1
    sql = args[0].args[0]
    assert "kotoyose.claude_session_store" in sql
    assert "CREATE TABLE IF NOT EXISTS" in sql


@pytest.mark.asyncio
async def test_postgres_session_store_append_inserts_entries(fake_pool):
    """append() は INSERT ... SELECT FROM unnest() の形で 1 RTT で書く。"""
    store = PostgresSessionStore(pool=fake_pool)
    key = {"project_key": "proj1", "session_id": "sess1"}
    entries = [{"type": "user", "content": "hi"}, {"type": "assistant", "content": "hello"}]

    await store.append(key, entries)

    fake_pool.execute.assert_awaited_once()
    sql = fake_pool.execute.await_args.args[0]
    assert "INSERT INTO kotoyose.claude_session_store" in sql
    assert "unnest" in sql.lower()
    # entries が JSON 文字列化されて第 4 引数で渡されている
    json_args = fake_pool.execute.await_args.args[4]
    assert json_args == [json.dumps(e) for e in entries]


@pytest.mark.asyncio
async def test_postgres_session_store_append_empty_is_noop(fake_pool):
    """空リストでは SQL を発行しない。"""
    store = PostgresSessionStore(pool=fake_pool)
    await store.append({"project_key": "p", "session_id": "s"}, [])
    fake_pool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_postgres_session_store_load_returns_none_for_unknown(fake_pool):
    """load() は行が無ければ None を返す。"""
    fake_pool.fetch = AsyncMock(return_value=[])
    store = PostgresSessionStore(pool=fake_pool)
    result = await store.load({"project_key": "p", "session_id": "missing"})
    assert result is None


@pytest.mark.asyncio
async def test_postgres_session_store_load_orders_by_seq(fake_pool):
    """load() は ORDER BY seq で行を取得する。"""
    fake_pool.fetch = AsyncMock(return_value=[
        {"entry": json.dumps({"type": "user", "content": "first"})},
        {"entry": json.dumps({"type": "user", "content": "second"})},
    ])
    store = PostgresSessionStore(pool=fake_pool)
    result = await store.load({"project_key": "p", "session_id": "s"})

    sql = fake_pool.fetch.await_args.args[0]
    assert "ORDER BY seq" in sql
    assert result == [
        {"type": "user", "content": "first"},
        {"type": "user", "content": "second"},
    ]


def test_postgres_session_store_rejects_invalid_table_name():
    """SQL injection を防ぐため、table 名は識別子文字のみ受け付ける。"""
    with pytest.raises(ValueError, match="must match"):
        PostgresSessionStore(pool=MagicMock(), table="users; DROP TABLE x")


def test_postgres_session_store_requires_pool():
    with pytest.raises(ValueError, match="requires 'pool'"):
        PostgresSessionStore(pool=None)


@pytest.mark.asyncio
async def test_postgres_session_store_delete_with_subpath_targets_only_that_row(fake_pool):
    """subpath を指定した delete は、その subpath だけを WHERE 句で絞り込む。"""
    store = PostgresSessionStore(pool=fake_pool)
    await store.delete({
        "project_key": "p",
        "session_id": "s",
        "subpath": "subagents/agent-abc",
    })

    fake_pool.execute.assert_awaited_once()
    sql = fake_pool.execute.await_args.args[0]
    assert "DELETE FROM kotoyose.claude_session_store" in sql
    assert "subpath = $3" in sql
    args = fake_pool.execute.await_args.args
    assert args[1] == "p"
    assert args[2] == "s"
    assert args[3] == "subagents/agent-abc"


@pytest.mark.asyncio
async def test_postgres_session_store_delete_without_subpath_cascades(fake_pool):
    """subpath 無しの delete は session 全体を cascade 削除する (subpath WHERE 無し)。"""
    store = PostgresSessionStore(pool=fake_pool)
    await store.delete({"project_key": "p", "session_id": "s"})

    fake_pool.execute.assert_awaited_once()
    sql = fake_pool.execute.await_args.args[0]
    assert "DELETE FROM kotoyose.claude_session_store" in sql
    assert "subpath" not in sql  # WHERE 句に subpath が含まれない (cascade)
    args = fake_pool.execute.await_args.args
    assert args[1] == "p"
    assert args[2] == "s"


@pytest.mark.asyncio
async def test_postgres_session_store_list_sessions_filters_main_transcripts(fake_pool):
    """list_sessions は subpath = '' (main transcript) のみを集計する。"""
    fake_pool.fetch = AsyncMock(return_value=[
        {"session_id": "s1", "mtime": 1700000000000},
        {"session_id": "s2", "mtime": 1700000001000},
    ])
    store = PostgresSessionStore(pool=fake_pool)
    result = await store.list_sessions("proj1")

    sql = fake_pool.fetch.await_args.args[0]
    assert "subpath = ''" in sql
    assert "GROUP BY session_id" in sql
    assert result == [
        {"session_id": "s1", "mtime": 1700000000000},
        {"session_id": "s2", "mtime": 1700000001000},
    ]


@pytest.mark.asyncio
async def test_postgres_session_store_list_subkeys_excludes_main_transcript(fake_pool):
    """list_subkeys は subpath <> '' で main transcript を除外する。"""
    fake_pool.fetch = AsyncMock(return_value=[
        {"subpath": "subagents/agent-abc"},
        {"subpath": "subagents/agent-def"},
    ])
    store = PostgresSessionStore(pool=fake_pool)
    result = await store.list_subkeys({"project_key": "p", "session_id": "s"})

    sql = fake_pool.fetch.await_args.args[0]
    assert "subpath <> ''" in sql
    assert "DISTINCT subpath" in sql
    assert result == ["subagents/agent-abc", "subagents/agent-def"]


# ---------------------------------------------------------------------------
# Task 3: build_options() session_store 注入テスト
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("configured_chat_alias")
def test_build_options_passes_session_store_when_pool_available():
    """db._pool が利用可能ならば build_options() が ClaudeAgentOptions に session_store を注入する。"""
    fake_pool = MagicMock()
    with patch("kotoyose.session_store._pool_provider", return_value=fake_pool):
        options = build_options("kotoyose-chat")
    assert options.session_store is not None


@pytest.mark.usefixtures("configured_chat_alias")
def test_build_options_session_store_none_when_pool_missing():
    """db._pool が None なら session_store も None (現状互換動作)。"""
    with patch("kotoyose.session_store._pool_provider", return_value=None):
        options = build_options("kotoyose-chat")
    assert options.session_store is None


# ---------------------------------------------------------------------------
# Task 6: app.py 起動時の session_store sanity ログテスト
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_app_startup_logs_session_store_state(capsys):
    """init_db 後、session_store が利用可能か起動ログに残す。"""
    from kotoyose import app as app_module

    fake_pool = MagicMock()
    with (
        patch("kotoyose.db._pool", fake_pool),
        patch("kotoyose.app.init_db", new=AsyncMock()),
        patch("kotoyose.app.close_db", new=AsyncMock()),
        patch("kotoyose.app.evict_expired_sessions", new=AsyncMock()),
    ):
        # lifespan の startup フェーズだけ走らせる
        async with app_module.lifespan(app_module.app):
            pass

    out = capsys.readouterr().out
    assert "session_store" in out.lower()
