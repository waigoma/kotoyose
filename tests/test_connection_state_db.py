"""connection_state の CHECK 制約が Python 側の許可値に追従することを本物の Postgres で確かめる。"""

import os
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest

from kotoyose.connection_state_store import VALID_AUTH_STATES, VALID_QUOTA_SOURCES, ConnectionStateStore


@pytest.fixture
async def pool():
    url = os.environ.get("KOTOYOSE_RUNTIME_IDENTITY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Isolated CI test database is not configured")
    assert urlparse(url).path.lstrip("/").startswith("ahs_execution_test"), "refusing a non-test database"
    database = "ahs_execution_test_connstate_" + uuid4().hex
    admin = await asyncpg.connect(url)
    created = None
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        created = await asyncpg.create_pool(urlparse(url)._replace(path="/" + database).geturl(), min_size=1, max_size=2)
        yield created
    finally:
        if created is not None:
            await created.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await admin.close()


async def test_initialize_widens_constraints_of_an_existing_table(pool):
    # S4b 時点の表 (login_required と claude_cli_usage を知らない CHECK) から始める。
    await pool.execute("""
        CREATE SCHEMA kotoyose;
        CREATE TABLE kotoyose.connection_state (
            connection TEXT PRIMARY KEY,
            auth TEXT NOT NULL CHECK (auth IN ('ok', 'expiring', 'expired', 'logged_out', 'unknown')),
            expires_at TIMESTAMPTZ, plan TEXT,
            quota JSONB NOT NULL DEFAULT '[]'::jsonb,
            quota_source TEXT CHECK (quota_source IS NULL OR quota_source IN ('official_rpc', 'official_stream', 'metered')),
            cli_version TEXT, checked_at TIMESTAMPTZ, last_error TEXT
        );
    """)
    store = ConnectionStateStore(pool)
    await store.initialize()
    await store.initialize()  # 2 回目も通る (再起動のたびに走る)

    for auth in sorted(VALID_AUTH_STATES):
        for source in sorted(VALID_QUOTA_SOURCES):
            await pool.execute(
                "INSERT INTO kotoyose.connection_state (connection, auth, quota_source) VALUES ($1, $2, $3)",
                f"{auth}-{source}", auth, source,
            )
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("INSERT INTO kotoyose.connection_state (connection, auth) VALUES ('x', 'bogus')")


@pytest.mark.parametrize("has_new_schema", [False, True])
async def test_startup_renames_only_when_new_schema_is_absent(pool, monkeypatch, has_new_schema):
    from kotoyose import db

    await pool.execute("""
        CREATE SCHEMA harness;
        CREATE TABLE harness.rename_probe (value text);
        INSERT INTO harness.rename_probe VALUES ('preserved');
    """)
    if has_new_schema:
        await pool.execute("CREATE SCHEMA kotoyose")
    async def create_pool(*args, **kwargs):
        return pool

    monkeypatch.setattr(db, "DATABASE_URL", "isolated-test")
    monkeypatch.setattr(db.asyncpg, "create_pool", create_pool)
    try:
        for _ in range(2):
            await db.init_db()
            assert await pool.fetchval("SELECT to_regclass('kotoyose.sessions') IS NOT NULL")
            assert await pool.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'harness')"
            ) is has_new_schema
            schema = "harness" if has_new_schema else "kotoyose"
            assert await pool.fetchval(f"SELECT value FROM {schema}.rename_probe") == "preserved"
    finally:
        db._pool = None
