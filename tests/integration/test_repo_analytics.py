"""Behavior tests for `catan_bot.db.repositories.analytics`."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta

import asyncpg
import pytest

from catan_bot.db.models import Game
from catan_bot.db.repositories import analytics, games, players, seasons
from catan_bot.domain.participation import ParticipationRecord
from catan_bot.domain.scoring import PlayerScore, ScoreEntry

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

STARTS_ON = date(2026, 1, 1)
ENDS_ON = date(2026, 12, 31)
ENDS_AT = datetime(2027, 1, 1, tzinfo=UTC)


async def _new_season(conn: asyncpg.Connection, guild_id: int, name: str) -> int:
    season = await seasons.create_season(conn, guild_id, name, STARTS_ON, ENDS_ON, ENDS_AT, 1, 1)
    return season.season_id


def _score(user_id: int, settlements: int, cities: int, road: int = 0) -> PlayerScore:
    total = settlements + cities + road
    return PlayerScore(
        user_id=user_id,
        total_points=total,
        breakdown=(
            ScoreEntry("settlements", settlements),
            ScoreEntry("cities", cities),
            ScoreEntry("longest_road", road),
        ),
    )


async def _confirmed(
    conn: asyncpg.Connection,
    guild_id: int,
    played_on: date,
    winner: int,
    losers: list[int],
    *,
    season_id: int | None = None,
    **kwargs: object,
) -> Game:
    game = await games.create_game(
        conn,
        guild_id,
        season_id,
        played_on,
        winner,
        winner,
        losers,
        **kwargs,  # type: ignore[arg-type]
    )
    assert await games.confirm_game(conn, guild_id, game.game_id, losers[0]) == "confirmed"
    return game


async def test_empty_guild_returns_no_records(app_conn: asyncpg.Connection, guild_id: int) -> None:
    assert await analytics.list_participations(app_conn, guild_id) == []


async def test_only_confirmed_games_and_active_participants_are_returned(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2, 3])
    confirmed = await _confirmed(app_conn, guild_id, date(2026, 3, 1), 1, [2, 3])
    await games.create_game(app_conn, guild_id, None, date(2026, 3, 1), 1, 1, [2])  # pending
    rejected = await games.create_game(app_conn, guild_id, None, date(2026, 3, 1), 1, 1, [2])
    assert await games.reject_game(app_conn, guild_id, rejected.game_id, 2) == "rejected"
    voided = await _confirmed(app_conn, guild_id, date(2026, 3, 1), 1, [2])
    assert await games.void_game(app_conn, guild_id, voided.game_id, 1, "oops") == "voided"

    records = await analytics.list_participations(app_conn, guild_id)

    assert {r.game_id for r in records} == {confirmed.game_id}
    assert [(r.user_id, r.is_winner) for r in records] == [(1, True), (2, False), (3, False)]
    assert all(r.player_count == 3 for r in records)


async def test_inactive_participant_is_excluded_and_player_count_shrinks(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2, 3])
    game = await _confirmed(app_conn, guild_id, date(2026, 3, 2), 1, [2, 3])

    result = await games.update_confirmed_game(
        app_conn,
        guild_id,
        game.game_id,
        expected_revision=0,
        updated_by=9,
        reason=None,
        played_on=date(2026, 3, 2),
        winner_id=1,
        loser_ids=[2],
        game_type="normal",
        extension_5_6=False,
        scenario=None,
        target_points=None,
        played_at=None,
        played_timezone=None,
        scores=None,
    )
    assert not isinstance(result, str)

    records = await analytics.list_participations(app_conn, guild_id)

    assert [(r.game_id, r.user_id, r.player_count) for r in records] == [
        (game.game_id, 1, 2),
        (game.game_id, 2, 2),
    ]


async def test_record_fields_scores_and_breakdown_are_decoded(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2, 3])
    played_at = datetime(2026, 3, 1, 20, 30, tzinfo=UTC)
    scored = await _confirmed(
        app_conn,
        guild_id,
        date(2026, 3, 1),
        1,
        [2],
        game_type="seafarers",
        extension_5_6=True,
        target_points=12,
        played_at=played_at,
        played_timezone="America/Chicago",
        scores=[_score(1, 4, 6, 2), _score(2, 3, 4)],
    )
    unscored = await _confirmed(app_conn, guild_id, date(2026, 3, 2), 3, [2])
    # Partial collection: only the winner has submitted a score.
    partial = await _confirmed(app_conn, guild_id, date(2026, 3, 3), 1, [2])
    assert await games.set_player_score(app_conn, guild_id, partial.game_id, 1, _score(1, 5, 5))

    records = await analytics.list_participations(app_conn, guild_id)

    assert records[0] == ParticipationRecord(
        game_id=scored.game_id,
        played_on=date(2026, 3, 1),
        played_at=played_at,
        game_type="seafarers",
        extension_5_6=True,
        target_points=12,
        player_count=2,
        user_id=1,
        is_winner=True,
        total_points=12,
        breakdown={"settlements": 4, "cities": 6, "longest_road": 2},
        season_id=None,
        played_timezone="America/Chicago",
    )
    assert isinstance(records[0].breakdown, dict)
    assert records[1].user_id == 2
    assert (records[1].total_points, records[1].breakdown) == (
        7,
        {"settlements": 3, "cities": 4, "longest_road": 0},
    )
    by_key = {(r.game_id, r.user_id): r for r in records}
    for user_id in (2, 3):
        row = by_key[(unscored.game_id, user_id)]
        assert (row.total_points, row.breakdown) == (None, None)
        assert (row.game_type, row.extension_5_6, row.target_points) == ("normal", False, None)
        assert row.played_at is None
        assert (row.season_id, row.played_timezone) == (None, None)
    assert by_key[(partial.game_id, 1)].breakdown == {
        "settlements": 5,
        "cities": 5,
        "longest_road": 0,
    }
    assert by_key[(partial.game_id, 2)].breakdown is None
    assert by_key[(partial.game_id, 2)].total_points is None


async def test_season_id_and_played_timezone_are_mapped_per_game(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    season_id = await _new_season(app_conn, guild_id, "One")
    timed = await _confirmed(
        app_conn,
        guild_id,
        date(2026, 3, 1),
        1,
        [2],
        season_id=season_id,
        played_at=datetime(2026, 3, 1, 2, 0, tzinfo=UTC),
        played_timezone="Asia/Tokyo",
    )
    plain = await _confirmed(app_conn, guild_id, date(2026, 3, 2), 2, [1])

    records = await analytics.list_participations(app_conn, guild_id)

    by_game = {r.game_id: (r.season_id, r.played_timezone) for r in records}
    assert by_game == {timed.game_id: (season_id, "Asia/Tokyo"), plain.game_id: (None, None)}
    # Both participants of a game carry the game's values.
    assert {(r.season_id, r.played_timezone) for r in records if r.game_id == timed.game_id} == {
        (season_id, "Asia/Tokyo")
    }


async def test_ordering_is_chronological_with_null_played_at_first_then_game_id(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    base = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    # Creation order deliberately differs from the expected chronological order.
    later_day = await _confirmed(app_conn, guild_id, date(2026, 3, 2), 1, [2])
    timed_late = await _confirmed(
        app_conn,
        guild_id,
        date(2026, 3, 1),
        1,
        [2],
        played_at=base + timedelta(hours=2),
        played_timezone="UTC",
    )
    timed_early = await _confirmed(
        app_conn,
        guild_id,
        date(2026, 3, 1),
        1,
        [2],
        played_at=base,
        played_timezone="UTC",
    )
    unknown_a = await _confirmed(app_conn, guild_id, date(2026, 3, 1), 1, [2])
    unknown_b = await _confirmed(app_conn, guild_id, date(2026, 3, 1), 1, [2])
    earliest = await _confirmed(app_conn, guild_id, date(2026, 2, 28), 1, [2])
    tie_a = await _confirmed(
        app_conn, guild_id, date(2026, 3, 1), 1, [2], played_at=base, played_timezone="UTC"
    )

    records = await analytics.list_participations(app_conn, guild_id)

    expected_games = [
        earliest,
        unknown_a,
        unknown_b,
        timed_early,
        tie_a,
        timed_late,
        later_day,
    ]
    assert [(r.game_id, r.user_id) for r in records] == [
        (g.game_id, user_id) for g in expected_games for user_id in (1, 2)
    ]


async def test_filters_by_season_and_game_type(app_conn: asyncpg.Connection, guild_id: int) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    season_one = await _new_season(app_conn, guild_id, "One")
    assert await seasons.cancel_active_season(app_conn, guild_id) is not None
    season_two = await _new_season(app_conn, guild_id, "Two")
    a = await _confirmed(app_conn, guild_id, date(2026, 3, 1), 1, [2], season_id=season_one)
    b = await _confirmed(
        app_conn,
        guild_id,
        date(2026, 3, 2),
        1,
        [2],
        season_id=season_two,
        game_type="cities_knights",
    )
    c = await _confirmed(
        app_conn, guild_id, date(2026, 3, 3), 2, [1], season_id=season_two, game_type="seafarers"
    )
    d = await _confirmed(app_conn, guild_id, date(2026, 3, 4), 1, [2])  # no season

    async def ids(**kwargs: int | str | None) -> list[int]:
        rows = await analytics.list_participations(app_conn, guild_id, **kwargs)  # type: ignore[arg-type]
        return sorted({r.game_id for r in rows})

    assert await ids() == sorted([a.game_id, b.game_id, c.game_id, d.game_id])
    assert await ids(season_id=season_one) == [a.game_id]
    assert await ids(season_id=season_two) == sorted([b.game_id, c.game_id])
    assert await ids(game_type="normal") == sorted([a.game_id, d.game_id])
    assert await ids(game_type="cities_knights") == [b.game_id]
    assert await ids(season_id=season_two, game_type="seafarers") == [c.game_id]
    assert await ids(season_id=season_one, game_type="seafarers") == []
    assert await ids(game_type="seafarers_cities_knights") == []
    assert await ids(season_id=season_two + 1000) == []


async def test_filtered_records_keep_the_full_active_player_count(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2, 3, 4])
    await _confirmed(app_conn, guild_id, date(2026, 3, 1), 1, [2, 3, 4], game_type="seafarers")
    await _confirmed(app_conn, guild_id, date(2026, 3, 2), 1, [2])

    seafarers = await analytics.list_participations(app_conn, guild_id, game_type="seafarers")

    assert [r.player_count for r in seafarers] == [4, 4, 4, 4]


async def test_guilds_are_isolated(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    await players.ensure_players(app_conn, other_guild_id, [1, 2, 3])
    mine = await _confirmed(app_conn, guild_id, date(2026, 3, 1), 1, [2])
    theirs = await _confirmed(app_conn, other_guild_id, date(2026, 3, 1), 3, [1, 2])

    mine_records = await analytics.list_participations(app_conn, guild_id)
    their_records = await analytics.list_participations(app_conn, other_guild_id)

    assert {r.game_id for r in mine_records} == {mine.game_id}
    assert len(mine_records) == 2
    assert {r.game_id for r in their_records} == {theirs.game_id}
    assert len(their_records) == 3
    # A season id from another guild can never leak that guild's games.
    other_season = await _new_season(app_conn, other_guild_id, "Theirs")
    assert await analytics.list_participations(app_conn, guild_id, season_id=other_season) == []


async def test_malformed_stored_breakdown_raises(
    app_conn: asyncpg.Connection, migrator_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await _confirmed(app_conn, guild_id, date(2026, 3, 1), 1, [2])
    await migrator_conn.execute(
        "UPDATE game_participants SET total_points = 5, "
        'score_breakdown = \'{"settlements": "five"}\'::jsonb '
        "WHERE game_id = $1 AND user_id = $2",
        game.game_id,
        1,
    )

    with pytest.raises(RuntimeError, match="invalid entries"):
        await analytics.list_participations(app_conn, guild_id)


@pytest.mark.parametrize("bad", ["bogus", "", "NORMAL", 1, True, ["normal"]])
async def test_bad_game_type_is_rejected(
    app_conn: asyncpg.Connection, guild_id: int, bad: object
) -> None:
    with pytest.raises(ValueError, match="game_type"):
        await analytics.list_participations(app_conn, guild_id, game_type=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "1"])
async def test_bad_ids_are_rejected(
    app_conn: asyncpg.Connection, guild_id: int, bad: object
) -> None:
    with pytest.raises(ValueError, match="guild_id"):
        await analytics.list_participations(app_conn, bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="season_id"):
        await analytics.list_participations(app_conn, guild_id, season_id=bad)  # type: ignore[arg-type]
