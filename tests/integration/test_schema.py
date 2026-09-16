"""Schema constraint tests for `0001_init.sql`: CHECKs, UNIQUEs, FKs, defaults.

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
OTHER_GUILD_ID = 1002


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


async def test_cross_guild_season_reference_raises_fk_violation(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)
    await _insert_guild(app_conn, guild_id=OTHER_GUILD_ID)
    season_id = await _insert_season(app_conn, guild_id=GUILD_ID)

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await app_conn.execute(
            "INSERT INTO games (guild_id, season_id, played_on, reported_by) "
            "VALUES ($1, $2, $3, $4)",
            OTHER_GUILD_ID,
            season_id,
            date(2026, 1, 5),
            1,
        )


async def test_participant_guild_mismatch_raises_fk_violation(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)
    await _insert_guild(app_conn, guild_id=OTHER_GUILD_ID)
    await app_conn.execute(
        "INSERT INTO players (guild_id, user_id) VALUES ($1, $2)", OTHER_GUILD_ID, 1
    )
    game_row = await app_conn.fetchrow(
        "INSERT INTO games (guild_id, played_on, reported_by) VALUES ($1, $2, $3) "
        "RETURNING game_id",
        GUILD_ID,
        date(2026, 1, 5),
        1,
    )
    game_id = game_row["game_id"]

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await app_conn.execute(
            "INSERT INTO game_participants (game_id, user_id, guild_id, is_winner) "
            "VALUES ($1, $2, $3, false)",
            game_id,
            1,
            OTHER_GUILD_ID,
        )


async def test_guild_config_default_min_games_defaults_to_two(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)

    value = await app_conn.fetchval(
        "SELECT default_min_games FROM guild_config WHERE guild_id = $1", GUILD_ID
    )
    assert value == 2


async def test_guild_config_player_role_defaults_to_null(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)

    value = await app_conn.fetchval(
        "SELECT player_role_id FROM guild_config WHERE guild_id = $1", GUILD_ID
    )
    assert value is None


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

    # confirmed_at is set so this only trips the confirmed_by<>reported_by
    # check, not the separate "confirmed requires confirmed_by/confirmed_at"
    # check below -- keeping the two assertions unambiguous.
    with pytest.raises(asyncpg.CheckViolationError) as exc_info:
        await app_conn.execute(
            "INSERT INTO games "
            "(guild_id, played_on, reported_by, confirmed_by, confirmed_at, status) "
            "VALUES ($1, $2, $3, $3, now(), 'confirmed')",
            GUILD_ID,
            date(2026, 1, 5),
            1,
        )

    assert exc_info.value.constraint_name == "games_confirmed_by_ne_reported_by"


async def test_confirmed_without_confirmed_by_raises_named_check(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)

    with pytest.raises(asyncpg.CheckViolationError) as exc_info:
        await app_conn.execute(
            "INSERT INTO games (guild_id, played_on, reported_by, status) "
            "VALUES ($1, $2, $3, 'confirmed')",
            GUILD_ID,
            date(2026, 1, 5),
            1,
        )

    assert exc_info.value.constraint_name == "games_confirmed_requires_fields"


async def test_voided_without_voided_by_raises_named_check(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)

    with pytest.raises(asyncpg.CheckViolationError) as exc_info:
        await app_conn.execute(
            "INSERT INTO games (guild_id, played_on, reported_by, status) "
            "VALUES ($1, $2, $3, 'voided')",
            GUILD_ID,
            date(2026, 1, 5),
            1,
        )

    assert exc_info.value.constraint_name == "games_voided_requires_fields"


async def test_rejected_without_rejected_by_raises_named_check(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)

    with pytest.raises(asyncpg.CheckViolationError) as exc_info:
        await app_conn.execute(
            "INSERT INTO games (guild_id, played_on, reported_by, status) "
            "VALUES ($1, $2, $3, 'rejected')",
            GUILD_ID,
            date(2026, 1, 5),
            1,
        )

    assert exc_info.value.constraint_name == "games_rejected_requires_fields"


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


async def test_season_results_wins_greater_than_games_raises_check_violation(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)
    season_id = await _insert_season(app_conn)
    await app_conn.execute("INSERT INTO players (guild_id, user_id) VALUES ($1, $2)", GUILD_ID, 1)

    with pytest.raises(asyncpg.CheckViolationError) as exc_info:
        await app_conn.execute(
            "INSERT INTO season_results "
            "(season_id, guild_id, user_id, rank, games, wins, eligible) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            season_id,
            GUILD_ID,
            1,
            1,
            2,
            5,
            True,
        )

    assert exc_info.value.constraint_name == "season_results_wins_le_games"


async def test_season_results_guild_mismatch_raises_fk_violation(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)
    await _insert_guild(app_conn, guild_id=OTHER_GUILD_ID)
    season_id = await _insert_season(app_conn, guild_id=GUILD_ID)
    await app_conn.execute(
        "INSERT INTO players (guild_id, user_id) VALUES ($1, $2)", OTHER_GUILD_ID, 1
    )

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await app_conn.execute(
            "INSERT INTO season_results "
            "(season_id, guild_id, user_id, rank, games, wins, eligible) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            season_id,
            OTHER_GUILD_ID,
            1,
            1,
            2,
            1,
            True,
        )


async def test_update_season_guild_id_with_existing_results_raises_fk_violation(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)
    await _insert_guild(app_conn, guild_id=OTHER_GUILD_ID)
    season_id = await _insert_season(app_conn, guild_id=GUILD_ID)
    await app_conn.execute("INSERT INTO players (guild_id, user_id) VALUES ($1, $2)", GUILD_ID, 1)
    await app_conn.execute(
        "INSERT INTO season_results "
        "(season_id, guild_id, user_id, rank, games, wins, eligible) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
        season_id,
        GUILD_ID,
        1,
        1,
        2,
        1,
        True,
    )

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await app_conn.execute(
            "UPDATE seasons SET guild_id = $1 WHERE season_id = $2",
            OTHER_GUILD_ID,
            season_id,
        )


async def test_season_results_user_not_a_player_raises_fk_violation(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)
    season_id = await _insert_season(app_conn)
    # User 999 was never inserted into `players` for GUILD_ID.

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await app_conn.execute(
            "INSERT INTO season_results "
            "(season_id, guild_id, user_id, rank, games, wins, eligible) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            season_id,
            GUILD_ID,
            999,
            1,
            2,
            1,
            True,
        )


@pytest.mark.parametrize("bad_name", ["", "x" * 101])
async def test_season_name_length_out_of_range_raises_check_violation(
    app_conn: asyncpg.Connection, bad_name: str
) -> None:
    await _insert_guild(app_conn)

    with pytest.raises(asyncpg.CheckViolationError):
        await _insert_season(app_conn, name=bad_name)


async def test_game_scoring_metadata_defaults_and_time_pair_constraint(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)
    row = await app_conn.fetchrow(
        "INSERT INTO games (guild_id, played_on, reported_by) VALUES ($1, $2, $3) "
        "RETURNING game_type, extension_5_6, target_points, played_at, played_timezone",
        GUILD_ID,
        date(2026, 1, 5),
        1,
    )
    assert row["game_type"] == "normal"
    assert row["extension_5_6"] is False
    assert row["target_points"] is None
    assert row["played_at"] is None
    assert row["played_timezone"] is None

    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(
            "INSERT INTO games (guild_id, played_on, reported_by, played_at) "
            "VALUES ($1, $2, $3, $4)",
            GUILD_ID,
            date(2026, 1, 6),
            1,
            datetime(2026, 1, 6, tzinfo=UTC),
        )


async def test_game_update_columns_and_active_winner_constraint(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)
    await app_conn.execute(
        "INSERT INTO players (guild_id, user_id) VALUES ($1, $2), ($1, $3)",
        GUILD_ID,
        1,
        2,
    )
    game_id = await app_conn.fetchval(
        "INSERT INTO games (guild_id, played_on, reported_by) VALUES ($1, $2, $3) "
        "RETURNING game_id",
        GUILD_ID,
        date(2026, 1, 7),
        1,
    )
    defaults = await app_conn.fetchrow(
        "SELECT revision, updated_by, updated_at, update_reason FROM games WHERE game_id = $1",
        game_id,
    )
    assert tuple(defaults.values()) == (0, None, None, None)
    await app_conn.execute(
        "INSERT INTO game_participants (game_id, user_id, guild_id, is_winner) "
        "VALUES ($1, $2, $3, true)",
        game_id,
        1,
        GUILD_ID,
    )
    await app_conn.execute(
        "INSERT INTO game_participants (game_id, user_id, guild_id, is_winner, is_active) "
        "VALUES ($1, $2, $3, true, false)",
        game_id,
        2,
        GUILD_ID,
    )
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute("UPDATE games SET updated_by = $1 WHERE game_id = $2", 1, game_id)


async def test_game_participant_scores_must_be_an_object_and_paired(
    app_conn: asyncpg.Connection,
) -> None:
    await _insert_guild(app_conn)
    await app_conn.execute("INSERT INTO players (guild_id, user_id) VALUES ($1, $2)", GUILD_ID, 1)
    game_id = await app_conn.fetchval(
        "INSERT INTO games (guild_id, played_on, reported_by) VALUES ($1, $2, $3) "
        "RETURNING game_id",
        GUILD_ID,
        date(2026, 1, 5),
        1,
    )

    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(
            "INSERT INTO game_participants (game_id, user_id, guild_id, total_points) "
            "VALUES ($1, $2, $3, $4)",
            game_id,
            1,
            GUILD_ID,
            10,
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await app_conn.execute(
            "INSERT INTO game_participants "
            "(game_id, user_id, guild_id, total_points, score_breakdown) "
            "VALUES ($1, $2, $3, $4, $5::jsonb)",
            game_id,
            1,
            GUILD_ID,
            10,
            "[]",
        )


async def test_migrations_are_idempotent() -> None:
    migrator_dsn = os.environ["TEST_MIGRATOR_DATABASE_URL"]
    # The session-scoped `run_migrations` fixture already applied every
    # migration once; running it again here must be a no-op.
    newly_applied = await run_migrations(migrator_dsn)
    assert newly_applied == []
