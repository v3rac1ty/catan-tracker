"""Privilege tests: `catan_app` is least-privilege (SELECT/INSERT/UPDATE only).

Also proves asyncpg's extended (prepared-statement) protocol rejects stacked
("`;`-separated") queries outright, which is one of the SQLi defenses listed
in DESIGN.md (Postgres itself allows stacked queries via the simple protocol;
asyncpg never uses that protocol for parameterized calls).
"""

from __future__ import annotations

import os
import urllib.parse

import asyncpg
import pytest

from catan_bot.db.pool import create_pool

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]


async def test_drop_table_raises_insufficient_privilege(app_conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("DROP TABLE games")


async def test_truncate_raises_insufficient_privilege(app_conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("TRUNCATE TABLE games")


async def test_delete_raises_insufficient_privilege(app_conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("DELETE FROM games")


async def test_game_update_audit_cannot_be_changed_or_deleted(
    app_conn: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("UPDATE game_updates SET reason = NULL")
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("DELETE FROM game_updates")


async def test_create_table_raises_insufficient_privilege(app_conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("CREATE TABLE privilege_probe (id INT)")


async def test_alter_table_raises_insufficient_privilege(app_conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("ALTER TABLE games ADD COLUMN privilege_probe INT")


async def test_copy_to_program_raises_insufficient_privilege(
    app_conn: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("COPY (SELECT 1) TO PROGRAM 'id'")


async def test_cannot_select_schema_migrations(app_conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.fetch("SELECT version FROM schema_migrations")


async def test_cannot_insert_schema_migrations(app_conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute(
            "INSERT INTO schema_migrations (version) VALUES ($1)", "9999_fake.sql"
        )


@pytest.mark.parametrize("maintenance_db", ["postgres", "template1"])
async def test_cannot_connect_to_maintenance_database(maintenance_db: str) -> None:
    # catan_app can only CONNECT to `catan`/`catan_test` (db/roles.sql
    # revokes CONNECT on `postgres` and `template1` from PUBLIC).
    app_dsn = os.environ["TEST_DATABASE_URL"]
    parts = urllib.parse.urlsplit(app_dsn)
    maintenance_dsn = urllib.parse.urlunsplit(parts._replace(path=f"/{maintenance_db}"))

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await asyncpg.connect(maintenance_dsn)


async def test_statement_timeout_is_five_seconds(app_conn: asyncpg.Connection) -> None:
    value = await app_conn.fetchval("SHOW statement_timeout")
    assert value == "5s"


async def test_pool_connections_reset_statement_timeout_on_release() -> None:
    """Every pooled connection must keep the 5s statement_timeout, even after
    a query changes it. `create_pool()` sets it via `server_settings` at
    connection startup (source='client'), not the role-level ALTER ROLE
    default -- proven below by checking `pg_settings.source`. asyncpg's pool
    also runs a RESET on release, which restores that startup value; this is
    proven by checking `pg_backend_pid()` stays the same physical connection
    across acquire/release/re-acquire (pool.py's min_size=1 makes this the
    only connection in play as long as just one is held at a time).
    """
    dsn = os.environ["TEST_DATABASE_URL"]
    pool = await create_pool(dsn)
    try:
        async with pool.acquire() as conn:
            # A mispointed TEST_DATABASE_URL must never let this pool run
            # against a real database.
            db_name = await conn.fetchval("SELECT pg_catalog.current_database()")
            assert db_name == "catan_test", (
                f"refusing to run against database {db_name!r}; check TEST_DATABASE_URL"
            )

            assert await conn.fetchval("SHOW statement_timeout") == "5s"
            source = await conn.fetchval(
                "SELECT source FROM pg_settings WHERE name = 'statement_timeout'"
            )
            assert source == "client"
            pid_before = await conn.fetchval("SELECT pg_backend_pid()")

        async with pool.acquire() as conn:
            await conn.execute("SET statement_timeout = 0")
            assert await conn.fetchval("SHOW statement_timeout") == "0"

        async with pool.acquire() as conn:
            pid_after = await conn.fetchval("SELECT pg_backend_pid()")
            assert pid_after == pid_before
            assert await conn.fetchval("SHOW statement_timeout") == "5s"
    finally:
        await pool.close()


async def test_stacked_query_without_args_raises_syntax_error(
    app_conn: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.PostgresSyntaxError, match="cannot insert multiple commands"):
        await app_conn.fetch("SELECT 1; SELECT 2")


async def test_stacked_query_with_arg_raises_syntax_error(app_conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.PostgresSyntaxError, match="cannot insert multiple commands"):
        await app_conn.fetch("SELECT $1; SELECT 2", 1)


async def test_execute_without_args_allows_stacked_queries_via_simple_protocol(
    app_conn: asyncpg.Connection,
) -> None:
    """`Connection.execute()` switches to the *simple* query protocol when it
    is called with no bind arguments, and the simple protocol (like a raw
    psql script) happily runs multiple `;`-separated statements. The two
    tests above only reject stacked queries because `fetch()` always uses
    the extended/prepared protocol. This is exactly why "no arguments" is
    not a safe way to build a query: every SQL string in this codebase must
    be a fixed constant with no internal `;`, regardless of which method or
    how many bind arguments are used.
    """
    result = await app_conn.execute("SELECT 1; SELECT 2")
    assert isinstance(result, str)


async def test_insert_select_update_succeed(app_conn: asyncpg.Connection) -> None:
    guild_id = 2001

    await app_conn.execute("INSERT INTO guild_config (guild_id) VALUES ($1)", guild_id)

    timezone_value = await app_conn.fetchval(
        "SELECT timezone FROM guild_config WHERE guild_id = $1", guild_id
    )
    assert timezone_value == "UTC"

    await app_conn.execute(
        "UPDATE guild_config SET timezone = $1 WHERE guild_id = $2",
        "America/Chicago",
        guild_id,
    )

    updated_value = await app_conn.fetchval(
        "SELECT timezone FROM guild_config WHERE guild_id = $1", guild_id
    )
    assert updated_value == "America/Chicago"
