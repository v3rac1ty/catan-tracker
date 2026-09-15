"""Behavior tests for `catan_bot.db.repositories.players`."""

from __future__ import annotations

import os

import asyncpg
import pytest

from catan_bot.db.repositories import players

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]


async def _player_user_ids(conn: asyncpg.Connection, guild_id: int) -> set[int]:
    rows = await conn.fetch("SELECT user_id FROM players WHERE guild_id = $1", guild_id)
    return {row["user_id"] for row in rows}


async def test_ensure_players_inserts_new_players(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2, 3])

    assert await _player_user_ids(app_conn, guild_id) == {1, 2, 3}


async def test_ensure_players_with_generator_stores_all_players(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """N1 regression: `user_ids` must be materialized exactly once, so a
    generator (previously consumed by validation, then exhausted by the
    time the SQL call ran) doesn't silently register zero players."""
    user_ids = (uid for uid in [1, 2, 3])

    await players.ensure_players(app_conn, guild_id, user_ids)

    assert await _player_user_ids(app_conn, guild_id) == {1, 2, 3}


async def test_ensure_players_is_idempotent(app_conn: asyncpg.Connection, guild_id: int) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    # Re-registering an overlapping set must not raise a unique violation.
    await players.ensure_players(app_conn, guild_id, [2, 3])

    assert await _player_user_ids(app_conn, guild_id) == {1, 2, 3}


async def test_ensure_players_empty_sequence_is_a_noop(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [])

    assert await _player_user_ids(app_conn, guild_id) == set()


async def test_ensure_players_scopes_by_guild(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1])
    await players.ensure_players(app_conn, other_guild_id, [1])

    assert await _player_user_ids(app_conn, guild_id) == {1}
    assert await _player_user_ids(app_conn, other_guild_id) == {1}
