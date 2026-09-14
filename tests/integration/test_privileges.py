"""Privilege tests: `catan_app` is least-privilege (SELECT/INSERT/UPDATE only).

Also proves asyncpg's extended (prepared-statement) protocol rejects stacked
("`;`-separated") queries outright, which is one of the SQLi defenses listed
in DESIGN.md (Postgres itself allows stacked queries via the simple protocol;
asyncpg never uses that protocol for parameterized calls).
"""

from __future__ import annotations

import os

import asyncpg
import pytest

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


async def test_statement_timeout_is_five_seconds(app_conn: asyncpg.Connection) -> None:
    value = await app_conn.fetchval("SHOW statement_timeout")
    assert value == "5s"


async def test_stacked_query_without_args_raises_syntax_error(
    app_conn: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.PostgresSyntaxError):
        await app_conn.fetch("SELECT 1; SELECT 2")


async def test_stacked_query_with_arg_raises_syntax_error(app_conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.PostgresSyntaxError):
        await app_conn.fetch("SELECT $1; SELECT 2", 1)


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
