"""Schema constraint tests for `0001_init.sql`: CHECKs, UNIQUEs, defaults.

Runs as the `catan_app` role against `catan_test`, exercising the
least-privilege setup the same way the bot does.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime

import asyncpg
import pytest

from catan_bot.db.migrate import run_migrations

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

GUILD_ID = 1001


async def _insert_guild(conn: asyncpg.Connection, guild_id: int = GUILD_ID) -> None:
    await conn.execute("INSERT INTO guild_config (guild_id) VALUES ($1)", guild_id)


async def _insert_season(
    conn: asyncpg.Connection,
    *,
    guild_id: int = GUILD_ID,
    name: str = "Test Season",
    starts_on: date = date(2026, 1, 1),
    ends_on: date = date(2026, 6, 30),
    min_games: int = 2,
    status: str = "active",
    created_by: int = 1,
) -> int:
    ends_at = datetime(ends_on.year, ends_on.month, ends_on.day, tzinfo=UTC)
    row = await conn.fetchrow(
        """
        INSERT INTO seasons
            (guild_id, name, starts_on, ends_on, ends_at, min_games, status, created_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING season_id
        """,
        guild_id,
        name,
        starts_on,
        ends_on,
        ends_at,
        min_games,
        status,
        created_by,
    )
    return row["season_id"]


async def test_second_active_season_same_guild_raises_unique_violation(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)
    await _insert_season(app_conn, status="active")

    with pytest.raises(asyncpg.UniqueViolationError):
        await _insert_season(app_conn, name="Second Season", status="active")


async def test_second_winner_in_game_raises_unique_violation(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)
    await app_conn.execute(
        "INSERT INTO players (guild_id, user_id) VALUES ($1, $2), ($1, $3)",
        GUILD_ID,
        1,
        2,
    )
    game_row = await app_conn.fetchrow(
        "INSERT INTO games (guild_id, played_on, reported_by) VALUES ($1, $2, $3) "
        "RETURNING game_id",
        GUILD_ID,
        date(2026, 1, 5),
        1,
    )
    game_id = game_row["game_id"]

    await app_conn.execute(
        "INSERT INTO game_participants (game_id, user_id, guild_id, is_winner) "
        "VALUES ($1, $2, $3, true)",
        game_id,
        1,
        GUILD_ID,
    )

    with pytest.raises(asyncpg.UniqueViolationError):
        await app_conn.execute(
            "INSERT INTO game_participants (game_id, user_id, guild_id, is_winner) "
            "VALUES ($1, $2, $3, true)",
            game_id,
            2,
            GUILD_ID,
        )


async def test_guild_config_default_min_games_defaults_to_two(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)

    value = await app_conn.fetchval(
        "SELECT default_min_games FROM guild_config WHERE guild_id = $1", GUILD_ID
    )
    assert value == 2


async def test_seasons_min_games_defaults_to_two(app_conn: asyncpg.Connection) -> None:
    await _insert_guild(app_conn)

    row = await app_conn.fetchrow(
        """
        INSERT INTO seasons (guild_id, name, starts_on, ends_on, ends_at, status, created_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING min_games
        """,
        GUILD_ID,
        "Default Season",
        date(2026, 1, 1),
        date(2026, 6, 30),
        datetime(2026, 7, 1, tzinfo=UTC),
        "active",
        1,
    )
    assert row["min_games"] == 2


@pytest.mark.parametrize("bad_min_games", [0, 101])
async def test_min_games_out_of_range_raises_check_violation(
    app_conn: asyncpg.Connection, bad_min_games: int
) -> None:
    await _insert_guild(app_conn)

    with pytest.raises(asyncpg.CheckViolationError):
        await _insert_season(app_conn, min_games=bad_min_games)


async def test_confirmed_by_equal_reported_by_raises_check_violation(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)

    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(
            "INSERT INTO games (guild_id, played_on, reported_by, confirmed_by, status) "
            "VALUES ($1, $2, $3, $3, 'confirmed')",
            GUILD_ID,
            date(2026, 1, 5),
            1,
        )


async def test_invalid_game_status_raises_check_violation(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)

    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(
            "INSERT INTO games (guild_id, played_on, reported_by, status) VALUES ($1, $2, $3, $4)",
            GUILD_ID,
            date(2026, 1, 5),
            1,
            "not_a_real_status",
        )


@pytest.mark.parametrize("bad_name", ["", "x" * 101])
async def test_season_name_length_out_of_range_raises_check_violation(
    app_conn: asyncpg.Connection, bad_name: str
) -> None:
    await _insert_guild(app_conn)

    with pytest.raises(asyncpg.CheckViolationError):
        await _insert_season(app_conn, name=bad_name)


async def test_migrations_are_idempotent() -> None:
    migrator_dsn = os.environ["TEST_MIGRATOR_DATABASE_URL"]
    # The session-scoped `run_migrations` fixture already applied every
    # migration once; running it again here must be a no-op.
    newly_applied = await run_migrations(migrator_dsn)
    assert newly_applied == []
