"""Behavior tests for `catan_bot.db.repositories.guilds`.

Runs as the least-privilege `catan_app` role against `catan_test`.
"""

from __future__ import annotations

import os

import asyncpg
import pytest

from catan_bot.db.repositories import guilds

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

UNKNOWN_GUILD_ID = 999_999_999


async def test_ensure_guild_creates_row_with_defaults(app_conn: asyncpg.Connection) -> None:
    gid = 700_001
    config = await guilds.ensure_guild(app_conn, gid)

    assert config.guild_id == gid
    assert config.timezone == "UTC"
    assert config.announce_channel_id is None
    assert config.admin_role_id is None
    assert config.default_min_games == 2
    assert config.created_at == config.updated_at


async def test_ensure_guild_is_idempotent_and_does_not_bump_updated_at(
    app_conn: asyncpg.Connection,
) -> None:
    gid = 700_002
    first = await guilds.ensure_guild(app_conn, gid)
    await guilds.set_timezone(app_conn, gid, "America/Chicago")

    second = await guilds.ensure_guild(app_conn, gid)

    # The second call must not overwrite the timezone change or bump
    # updated_at -- ON CONFLICT DO NOTHING never touches an existing row.
    assert second.timezone == "America/Chicago"
    assert second.created_at == first.created_at
    assert second.updated_at > first.updated_at  # from the set_timezone call, not ensure_guild

    before = second
    third = await guilds.ensure_guild(app_conn, gid)
    assert third.updated_at == before.updated_at


async def test_get_guild_returns_none_for_unknown_guild(app_conn: asyncpg.Connection) -> None:
    assert await guilds.get_guild(app_conn, UNKNOWN_GUILD_ID) is None


async def test_get_guild_returns_existing_row(app_conn: asyncpg.Connection, guild_id: int) -> None:
    found = await guilds.get_guild(app_conn, guild_id)
    assert found is not None
    assert found.guild_id == guild_id


async def test_set_timezone_updates_value_and_bumps_updated_at(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    before = await guilds.get_guild(app_conn, guild_id)
    assert before is not None

    updated = await guilds.set_timezone(app_conn, guild_id, "Europe/Paris")

    assert updated is not None
    assert updated.timezone == "Europe/Paris"
    assert updated.updated_at > before.updated_at
    assert updated.created_at == before.created_at


async def test_set_timezone_unknown_guild_returns_none(app_conn: asyncpg.Connection) -> None:
    assert await guilds.set_timezone(app_conn, UNKNOWN_GUILD_ID, "UTC") is None


async def test_set_announce_channel_sets_and_clears(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    set_result = await guilds.set_announce_channel(app_conn, guild_id, 123456)
    assert set_result is not None
    assert set_result.announce_channel_id == 123456

    cleared = await guilds.set_announce_channel(app_conn, guild_id, None)
    assert cleared is not None
    assert cleared.announce_channel_id is None


async def test_set_admin_role_sets_and_clears(app_conn: asyncpg.Connection, guild_id: int) -> None:
    set_result = await guilds.set_admin_role(app_conn, guild_id, 654321)
    assert set_result is not None
    assert set_result.admin_role_id == 654321

    cleared = await guilds.set_admin_role(app_conn, guild_id, None)
    assert cleared is not None
    assert cleared.admin_role_id is None


async def test_set_default_min_games_updates_value(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    updated = await guilds.set_default_min_games(app_conn, guild_id, 5)
    assert updated is not None
    assert updated.default_min_games == 5


async def test_set_default_min_games_out_of_range_raises_value_error(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """N3 (M3a re-audit): `min_games` is now validated by `_params.require_int`
    (range `1..100`, exactly matching `guild_config`'s own
    `default_min_games BETWEEN 1 AND 100` CHECK) before any SQL is built, so
    an out-of-range value is now rejected at the repository layer -- as a
    `ValueError`, never reaching Postgres to raise `CheckViolationError`."""
    with pytest.raises(ValueError, match="min_games"):
        await guilds.set_default_min_games(app_conn, guild_id, 0)


async def test_setters_return_none_for_unknown_guild(app_conn: asyncpg.Connection) -> None:
    assert await guilds.set_announce_channel(app_conn, UNKNOWN_GUILD_ID, 1) is None
    assert await guilds.set_admin_role(app_conn, UNKNOWN_GUILD_ID, 1) is None
    assert await guilds.set_default_min_games(app_conn, UNKNOWN_GUILD_ID, 3) is None


async def test_server_encoding_is_utf8(app_conn: asyncpg.Connection) -> None:
    """Postgres `char_length()` (used by every text CHECK in 0001_init.sql)
    must count the same units as Python's `len()`. That only holds if the
    server-side encoding is UTF8 (see `db/roles.sql`'s pinned
    `ENCODING 'UTF8' TEMPLATE template0`)."""
    encoding = await app_conn.fetchval("SHOW server_encoding")
    assert encoding == "UTF8"
