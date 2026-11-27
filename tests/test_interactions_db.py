"""kotoyose.interactions テーブル DDL とテーブル操作のテスト。"""

import json
from enum import Enum
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kotoyose import db


@pytest.fixture(autouse=True)
def reset_db_pool():
    """各テスト後に db._pool をリセットしてテスト間の状態漏洩を防ぐ。"""
    yield
    db._pool = None


@pytest.mark.asyncio
async def test_init_db_creates_interactions_table():
    """init_db() の execute で interactions テーブル作成 SQL が含まれる。"""
    fake_conn = AsyncMock()
    fake_pool = MagicMock()
    fake_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("kotoyose.db.DATABASE_URL", "postgresql://fake/url"),
        patch("kotoyose.db.asyncpg.create_pool", new=AsyncMock(return_value=fake_pool)),
    ):
        await db.init_db()

    assert fake_conn.execute.await_count >= 1
    sql_executed = " ".join(call.args[0] for call in fake_conn.execute.await_args_list)
    assert "kotoyose.interactions" in sql_executed
    assert "CREATE TABLE IF NOT EXISTS kotoyose.session_titles" in sql_executed
    assert "request_id" in sql_executed
    assert "prompt" in sql_executed
    assert "response" in sql_executed
    assert "tokens_in" in sql_executed
    assert "status" in sql_executed
    # インデックス DDL も実行されること
    assert "idx_interactions_received_at" in sql_executed
    assert "kotoyose.interaction_events" in sql_executed
    assert "idx_interaction_events_interaction_seq" in sql_executed
    assert "INTERVAL '14 days'" in sql_executed


@pytest.mark.asyncio
async def test_init_db_prunes_only_old_interaction_events():
    """14 日超でも interaction は残し、期限切れ event だけを削除する。"""
    state = {
        "interactions": [{"id": 1, "age_days": 30}],
        "interaction_events": [
            {"id": 10, "interaction_id": 1, "age_days": 30},
            {"id": 11, "interaction_id": 1, "age_days": 1},
        ],
    }

    async def execute(sql):
        normalized = " ".join(sql.split())
        if normalized.startswith("DELETE FROM kotoyose.interactions "):
            state["interactions"].clear()
            state["interaction_events"].clear()
        elif normalized.startswith("DELETE FROM kotoyose.interaction_events "):
            state["interaction_events"] = [
                event for event in state["interaction_events"] if event["age_days"] <= 14
            ]

    fake_conn = MagicMock()
    fake_conn.execute = AsyncMock(side_effect=execute)
    fake_pool = MagicMock()
    fake_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    fake_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("kotoyose.db.DATABASE_URL", "postgresql://fake/url"),
        patch("kotoyose.db.asyncpg.create_pool", new=AsyncMock(return_value=fake_pool)),
    ):
        await db.init_db()

    assert state["interactions"] == [{"id": 1, "age_days": 30}]
    assert state["interaction_events"] == [
        {"id": 11, "interaction_id": 1, "age_days": 1}
    ]
    prune_sql = [
        call.args[0]
        for call in fake_conn.execute.await_args_list
        if "DELETE FROM kotoyose." in call.args[0]
    ]
    assert len(prune_sql) == 1
    assert "DELETE FROM kotoyose.interaction_events" in prune_sql[0]
    assert "WHERE ts < NOW() - INTERVAL '14 days'" in prune_sql[0]


@pytest.mark.asyncio
async def test_record_event_swallows_db_failure():
    """event DB が不調でも runtime 応答側へ例外を返さない。"""
    fake_pool = MagicMock()
    fake_pool.execute = AsyncMock(side_effect=RuntimeError("event table missing"))

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        await interactions.record_event(42, 0, "tool_use", "Bash", "pytest", {})


@pytest.mark.asyncio
async def test_record_event_serializes_nested_sdk_enum():
    """SDK 固有 Enum を含む payload も JSON 値へ正規化して保存する。"""

    class CommandSource(Enum):
        EXEC = "exec"

    fake_pool = MagicMock()
    fake_pool.execute = AsyncMock(return_value="INSERT 1")

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        await interactions.record_event(
            42,
            0,
            "tool_result",
            "Bash",
            "ok",
            {"metadata": {"source": CommandSource.EXEC}},
        )

    payload_json = fake_pool.execute.await_args.args[-1]
    assert json.loads(payload_json) == {"metadata": {"source": "exec"}}


@pytest.mark.asyncio
async def test_list_interaction_events_applies_after_seq():
    """蓄積取得は after_seq を SQL へ渡し、seq 昇順を保証する。"""
    fake_pool = MagicMock()
    fake_pool.fetch = AsyncMock(return_value=[{"seq": 3, "kind": "text"}])

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        rows = await interactions.list_interaction_events(42, after_seq=2)

    assert rows == [{"seq": 3, "kind": "text"}]
    sql, interaction_id, after_seq = fake_pool.fetch.await_args.args
    assert "seq > $2" in sql
    assert "ORDER BY seq ASC" in sql
    assert (interaction_id, after_seq) == (42, 2)


@pytest.mark.asyncio
async def test_record_received_returns_id_when_pool_available():
    """record_received は INSERT して id を返す。"""
    fake_pool = MagicMock()
    fake_pool.fetchrow = AsyncMock(return_value={"id": 42})

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        new_id = await interactions.record_received(
            request_id="req_abc",
            conversation_key="key:1",
            model="kotoyose-chat",
            source="discord",
            prompt="ごきげんよう",
            streamed=False,
        )
    assert new_id == 42
    assert fake_pool.fetchrow.await_count == 1
    sql, *args = fake_pool.fetchrow.await_args.args
    assert "INSERT INTO kotoyose.interactions" in sql
    assert "RETURNING id" in sql
    assert "req_abc" in args
    assert "kotoyose-chat" in args


@pytest.mark.asyncio
async def test_native_record_received_uses_request_id_for_idempotency():
    from kotoyose import interactions

    fake_pool = MagicMock()
    fake_pool.fetchrow = AsyncMock(side_effect=[{"id": 42}, None])
    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        ids = [await interactions.record_received(
            request_id="native-agent:attempt-1:3", conversation_key="attempt:attempt-1",
            model="grok-test", source="native-agent", prompt="task", streamed=True,
            provider="cursor", idempotent=True,
        ) for _ in range(2)]
    assert ids == [42, None]
    sql = fake_pool.fetchrow.await_args.args[0]
    assert "ON CONFLICT (request_id) DO NOTHING" in sql


@pytest.mark.asyncio
async def test_record_received_returns_none_when_pool_missing():
    """DB プール無し (DATABASE_URL 未設定) なら None を返す (no-op)。"""
    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=None):
        new_id = await interactions.record_received(
            request_id="req_abc",
            conversation_key=None,
            model="x",
            source=None,
            prompt="hi",
            streamed=False,
        )
    assert new_id is None


@pytest.mark.asyncio
async def test_record_completed_updates_row():
    """record_completed は UPDATE を発行する。"""
    fake_pool = MagicMock()
    fake_pool.execute = AsyncMock(return_value="UPDATE 1")

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        await interactions.record_completed(
            interaction_id=42,
            response="お答え",
            tokens_in=10,
            tokens_out=5,
            tokens_source="sdk",
            session_event="cache_hit",
        )
    sql, *args = fake_pool.execute.await_args.args
    assert "UPDATE kotoyose.interactions" in sql
    assert "status = 'completed'" in sql
    assert 42 in args
    assert "お答え" in args


@pytest.mark.asyncio
async def test_record_failed_updates_row_with_error():
    """record_failed は status='failed' と error_message を埋める。"""
    fake_pool = MagicMock()
    fake_pool.execute = AsyncMock(return_value="UPDATE 1")

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        await interactions.record_failed(
            interaction_id=99,
            error_message="connection refused",
        )
    sql, *args = fake_pool.execute.await_args.args
    assert "UPDATE kotoyose.interactions" in sql
    assert "status = 'failed'" in sql
    assert 99 in args
    assert "connection refused" in args


@pytest.mark.asyncio
async def test_list_interactions_filters():
    """list_interactions は WHERE 句でフィルタを適用する。"""
    fake_pool = MagicMock()
    fake_pool.fetch = AsyncMock(return_value=[])
    fake_pool.fetchval = AsyncMock(return_value=0)

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        await interactions.list_interactions(
            limit=20, offset=0,
            model="kotoyose-chat", source="discord", status="completed",
            conversation_key=None, since=None, until=None,
        )
    sql, *args = fake_pool.fetch.await_args.args
    assert "model = $" in sql
    assert "source = $" in sql
    assert "status = $" in sql
    assert "ORDER BY received_at DESC" in sql
    assert "LIMIT" in sql


@pytest.mark.asyncio
async def test_list_interactions_adds_completed_output_throughput():
    """完了行だけに E2E output tok/s を付与し、0ms は未計測扱いにする。"""
    fake_pool = MagicMock()
    fake_pool.fetch = AsyncMock(return_value=[
        {"id": 1, "status": "completed", "tokens_out": 25, "duration_ms": 2000},
        {"id": 2, "status": "in_progress", "tokens_out": 25, "duration_ms": 2000},
        {"id": 3, "status": "completed", "tokens_out": 25, "duration_ms": 0},
        {"id": 4, "status": "completed", "tokens_out": 0, "duration_ms": 2000},
    ])
    fake_pool.fetchval = AsyncMock(return_value=4)

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        result = await interactions.list_interactions()

    assert result["items"][0]["output_tokens_per_second"] == 12.5
    assert result["items"][1]["output_tokens_per_second"] is None
    assert result["items"][2]["output_tokens_per_second"] is None
    assert result["items"][3]["output_tokens_per_second"] == 0.0


@pytest.mark.asyncio
async def test_get_interaction_returns_full_row():
    """get_interaction は単一行を fetchrow で返す。"""
    fake_pool = MagicMock()
    fake_pool.fetchrow = AsyncMock(return_value={"id": 7, "prompt": "hi"})

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        row = await interactions.get_interaction(7)
    assert row["id"] == 7
    sql, *args = fake_pool.fetchrow.await_args.args
    assert "WHERE id = $1" in sql


@pytest.mark.asyncio
async def test_list_sessions_groups_by_conversation_key():
    """list_sessions は conversation_key で GROUP BY する。"""
    fake_pool = MagicMock()
    fake_pool.fetch = AsyncMock(return_value=[])
    fake_pool.fetchval = AsyncMock(return_value=0)

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        await interactions.list_sessions(limit=20, offset=0)
    sql, *args = fake_pool.fetch.await_args.args
    assert "GROUP BY kotoyose.interactions.conversation_key" in sql
    assert "MAX(received_at)" in sql


@pytest.mark.asyncio
async def test_get_session_turns_returns_ordered_list():
    """get_session_turns は受信時刻の昇順で返す。"""
    fake_pool = MagicMock()
    fake_pool.fetch = AsyncMock(return_value=[])

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        await interactions.get_session_turns("channel:123")
    sql, *args = fake_pool.fetch.await_args.args
    assert "ORDER BY received_at ASC" in sql
    assert "channel:123" in args


@pytest.mark.asyncio
async def test_get_session_turns_filters_by_runtime_session_and_provider():
    """非 Claude の詳細表示は runtime session ID と provider で 1 行群に絞る。"""
    fake_pool = MagicMock()
    fake_pool.fetch = AsyncMock(return_value=[])

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        await interactions.get_session_turns(
            "task:42", runtime_session_id="thread_codex", provider="codex"
        )

    sql, *args = fake_pool.fetch.await_args.args
    assert "COALESCE(runtime_session_id, claude_session_id) IS NOT DISTINCT FROM $2" in sql
    assert "($3::text IS NULL OR provider = $3)" in sql
    assert args == ["task:42", "thread_codex", "codex"]


@pytest.mark.asyncio
async def test_get_session_turns_filters_legacy_rows_by_provider():
    """ID 未記録の legacy 行でも provider を指定した詳細表示は混在しない。"""
    fake_pool = MagicMock()
    fake_pool.fetch = AsyncMock(return_value=[])

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        await interactions.get_session_turns("task:42", provider="claude")

    sql, *args = fake_pool.fetch.await_args.args
    assert "COALESCE(runtime_session_id, claude_session_id) IS NOT DISTINCT FROM $2" in sql
    assert "($3::text IS NULL OR provider = $3)" in sql
    assert args == ["task:42", None, "claude"]


@pytest.mark.asyncio
async def test_soft_delete_session_marks_rows():
    """soft_delete_session は UPDATE で deleted_at を設定し件数を返す。"""
    fake_pool = MagicMock()
    fake_pool.execute = AsyncMock(return_value="UPDATE 3")

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        n = await interactions.soft_delete_session("example-client:default", "sess_xyz")

    assert n == 3
    sql, *args = fake_pool.execute.await_args.args
    assert "UPDATE kotoyose.interactions" in sql
    assert "SET deleted_at = NOW()" in sql
    assert "IS NOT DISTINCT FROM" in sql  # NULL-safe match
    assert "deleted_at IS NULL" in sql  # 冪等性のための既存削除済み除外
    assert "example-client:default" in args
    assert "sess_xyz" in args


@pytest.mark.asyncio
async def test_soft_delete_session_uses_runtime_session_and_provider():
    """非 Claude 削除は NULL の Claude ID 群ではなく対象 runtime session だけを選ぶ。"""
    fake_pool = MagicMock()
    fake_pool.execute = AsyncMock(return_value="UPDATE 1")

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        await interactions.soft_delete_session("task:42", "thread_codex", provider="codex")

    sql, *args = fake_pool.execute.await_args.args
    assert "COALESCE(runtime_session_id, claude_session_id) IS NOT DISTINCT FROM $2" in sql
    assert "($3::text IS NULL OR provider = $3)" in sql
    assert args == ["task:42", "thread_codex", "codex"]


@pytest.mark.asyncio
async def test_soft_delete_session_with_null_session_id():
    """session_id=None なら legacy の ID 未記録行を対象にできる。"""
    fake_pool = MagicMock()
    fake_pool.execute = AsyncMock(return_value="UPDATE 1")

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        n = await interactions.soft_delete_session("legacy:key", None)

    assert n == 1
    _, *args = fake_pool.execute.await_args.args
    assert args[0] == "legacy:key"
    assert args[1] is None


@pytest.mark.asyncio
async def test_soft_delete_session_idempotent_returns_zero():
    """既に削除済みの行のみ対象なら UPDATE 0 → 0 を返す。"""
    fake_pool = MagicMock()
    fake_pool.execute = AsyncMock(return_value="UPDATE 0")

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        n = await interactions.soft_delete_session("example-client:default", "sess_xyz")

    assert n == 0


@pytest.mark.asyncio
async def test_soft_delete_session_no_pool_returns_zero():
    """DB プール無しなら 0 を返す (no-op)。"""
    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=None):
        n = await interactions.soft_delete_session("any:key", "sid")
    assert n == 0


@pytest.mark.asyncio
async def test_list_sessions_includes_is_active_subquery():
    """list_sessions は kotoyose.sessions と照合する is_active EXISTS subquery を含む。"""
    fake_pool = MagicMock()
    fake_pool.fetch = AsyncMock(return_value=[])
    fake_pool.fetchval = AsyncMock(return_value=0)

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        await interactions.list_sessions(limit=10, offset=0)

    sql, *args = fake_pool.fetch.await_args.args
    assert "is_active" in sql
    assert "FROM kotoyose.sessions s" in sql
    # 同じ conversation_key + provider + runtime session ID のみ active 扱い
    assert "s.conversation_key = kotoyose.interactions.conversation_key" in sql
    assert "s.provider = kotoyose.interactions.provider" in sql
    assert "COALESCE(s.runtime_session_id, s.claude_session_id)" in sql


@pytest.mark.asyncio
async def test_list_sessions_filters_deleted_by_default():
    """list_sessions は deleted_at IS NULL でフィルタする (既定)。"""
    fake_pool = MagicMock()
    fake_pool.fetch = AsyncMock(return_value=[])
    fake_pool.fetchval = AsyncMock(return_value=0)

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        await interactions.list_sessions(limit=20, offset=0)

    sql, *args = fake_pool.fetch.await_args.args
    assert "deleted_at IS NULL" in sql


@pytest.mark.asyncio
async def test_list_sessions_include_deleted_disables_filter():
    """include_deleted=True で削除済みも含む。"""
    fake_pool = MagicMock()
    fake_pool.fetch = AsyncMock(return_value=[])
    fake_pool.fetchval = AsyncMock(return_value=0)

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        await interactions.list_sessions(limit=20, offset=0, include_deleted=True)

    sql, *args = fake_pool.fetch.await_args.args
    assert "deleted_at IS NULL" not in sql


@pytest.mark.asyncio
async def test_list_sessions_searches_server_side_and_escapes_like():
    """list_sessions(q=...) は source_machine / origin_hint を含めて SQL ILIKE で検索し、ワイルドカードをエスケープする。"""
    fake_pool = MagicMock()
    fake_pool.fetch = AsyncMock(return_value=[])
    fake_pool.fetchval = AsyncMock(return_value=0)

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        await interactions.list_sessions(limit=100, offset=0, q="win_lut%1")

    sql, *args = fake_pool.fetch.await_args.args
    assert "metadata->>'source_machine'" in sql
    assert "metadata->>'origin_hint'" in sql
    assert "ILIKE $1 ESCAPE" in sql
    assert args == ["%win\\_lut\\%1%"]
    count_sql, *count_args = fake_pool.fetchval.await_args.args
    assert "ILIKE $1 ESCAPE" in count_sql
    assert count_args == ["%win\\_lut\\%1%"]


@pytest.mark.asyncio
async def test_get_session_turns_filters_deleted_by_default():
    """get_session_turns は deleted_at IS NULL でフィルタする (既定)。"""
    fake_pool = MagicMock()
    fake_pool.fetch = AsyncMock(return_value=[])

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        await interactions.get_session_turns("k", "sid")

    sql, *args = fake_pool.fetch.await_args.args
    assert "deleted_at IS NULL" in sql


@pytest.mark.asyncio
async def test_compute_stats_returns_24h_and_7d():
    """compute_stats は 24h / 7d の集計を返す (空 DB ならゼロ)。"""
    fake_pool = MagicMock()
    fake_pool.fetchrow = AsyncMock(return_value={
        "total": 0, "completed": 0, "failed": 0,
        "tokens_in": 0, "tokens_out": 0, "avg_duration_ms": None,
    })
    fake_pool.fetch = AsyncMock(return_value=[])

    from kotoyose import interactions

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        stats = await interactions.compute_stats()
    assert "past_24h" in stats
    assert "past_7d" in stats
    assert stats["past_24h"]["total_requests"] == 0


@pytest.mark.parametrize(('prompt', 'expected'), [
    (None, None), (' \n\t', None), ('\n  Fix  the\tbug\nignored', 'Fix the bug'),
    ('a' * 81, 'a' * 79 + '…'), ('a' * 80, 'a' * 80),
])
def test_prompt_title(prompt, expected):
    from kotoyose.interactions import _summarize_prompt_title
    assert _summarize_prompt_title(prompt) == expected


@pytest.mark.asyncio
async def test_session_title_storage():
    from kotoyose.interactions import get_session_title, set_session_title
    pool = AsyncMock()
    pool.fetchval.return_value = 'Saved'
    with patch('kotoyose.interactions._get_pool', return_value=pool):
        assert await set_session_title('conversation', ' Saved\n title ') == 'Saved title'
        assert pool.execute.call_args.args[1:] == ('conversation', 'Saved title')
        assert 'ON CONFLICT' in pool.execute.call_args.args[0]
        assert await get_session_title('conversation') == 'Saved'
        assert await set_session_title('conversation', ' \n ') is None
        assert 'DELETE FROM' in pool.execute.call_args.args[0]
    with patch('kotoyose.interactions._get_pool', return_value=None):
        assert await get_session_title('conversation') is None
        assert await set_session_title('conversation', 'Title') is None


@pytest.mark.asyncio
@pytest.mark.parametrize(('custom', 'native', 'expected'), [
    ('Custom', 'Native', 'Custom'), (None, 'Native', 'Native'), (None, None, 'First prompt'),
])
async def test_session_titles_priority_and_search(custom, native, expected):
    from kotoyose.interactions import list_sessions
    pool = AsyncMock()
    pool.fetch.return_value = [dict(custom_title=custom, native_title=native, first_prompt='First  prompt\nnext')]
    pool.fetchval.return_value = 1
    with patch('kotoyose.interactions._get_pool', return_value=pool):
        result = await list_sessions(q='Title_%')
    assert result['items'] == [dict(title=expected, custom_title=custom)]
    for call in [pool.fetch.call_args, pool.fetchval.call_args]:
        sql = call.args[0]
        assert 'LEFT JOIN kotoyose.session_titles st' in sql
        assert "COALESCE(st.title, '') ILIKE" in sql
        assert "COALESCE(metadata->>'session_title', '') ILIKE" in sql
        assert "LEFT(COALESCE(prompt, ''), 300) ILIKE" in sql
        assert call.args[1:] == ('%Title\\_\\%%',)
