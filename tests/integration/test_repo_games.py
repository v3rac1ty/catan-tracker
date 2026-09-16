"""Behavior tests for `catan_bot.db.repositories.games`."""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime

import asyncpg
import pytest

from catan_bot.db.repositories import games, players
from catan_bot.domain.scoring import PlayerScore, ScoreEntry

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

PLAYED_ON = date(2026, 3, 1)


async def test_create_game_and_get_game_round_trips_participants(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2, 3])

    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2, 3])

    assert game.status == "pending"
    assert game.reported_by == 1
    assert game.played_on == PLAYED_ON

    fetched = await games.get_game(app_conn, guild_id, game.game_id)
    assert fetched is not None
    assert fetched.winner_id == 1
    assert set(fetched.loser_ids) == {2, 3}


async def test_get_game_returns_active_roster_in_deterministic_order(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2, 3, 4])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [4, 3, 2])

    fetched = await games.get_game(app_conn, guild_id, game.game_id)

    assert fetched is not None
    assert fetched.winner_id == 1
    assert fetched.loser_ids == (2, 3, 4)


async def test_create_game_with_generator_loser_ids_succeeds_with_all_participants(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """N1 regression: `loser_ids` must be materialized exactly once -- a
    generator previously raised `TypeError` (`len()` on an already-exhausted
    generator) instead of succeeding with every participant stored."""
    await players.ensure_players(app_conn, guild_id, [1, 2, 3])
    loser_ids = (uid for uid in [2, 3])

    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, loser_ids)

    fetched = await games.get_game(app_conn, guild_id, game.game_id)
    assert fetched is not None
    assert fetched.winner_id == 1
    assert set(fetched.loser_ids) == {2, 3}


async def test_create_game_with_unregistered_player_raises_fk_violation(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    # Player 2 was never ensure_players'd.
    await players.ensure_players(app_conn, guild_id, [1])

    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])

    # The transaction rolled back: no partial game row survives.
    recent = await games.list_recent_games(app_conn, guild_id, 10)
    assert recent == []


async def test_get_game_returns_none_for_unknown_game(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    assert await games.get_game(app_conn, guild_id, 999_999) is None


async def test_set_game_message_updates_channel_and_message_id(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])

    updated = await games.set_game_message(app_conn, guild_id, game.game_id, 111, 222)

    assert updated is not None
    assert updated.channel_id == 111
    assert updated.message_id == 222


async def test_set_game_message_unknown_game_returns_none(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    assert await games.set_game_message(app_conn, guild_id, 999_999, 1, 1) is None


# ---------------------------------------------------------------------------
# confirm_game
# ---------------------------------------------------------------------------


async def test_confirm_game_by_participant_succeeds(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])

    result = await games.confirm_game(app_conn, guild_id, game.game_id, 2)

    assert result == "confirmed"
    confirmed = await games.get_game(app_conn, guild_id, game.game_id)
    assert confirmed is not None
    assert confirmed.game.status == "confirmed"
    assert confirmed.game.confirmed_by == 2
    assert confirmed.game.confirmed_at is not None


async def test_confirm_game_by_reporter_is_refused(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])

    result = await games.confirm_game(app_conn, guild_id, game.game_id, 1)

    assert result == "reporter_cannot_confirm"


async def test_confirm_game_by_non_participant_is_refused(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2, 3])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])

    result = await games.confirm_game(app_conn, guild_id, game.game_id, 3)

    assert result == "not_participant"


async def test_confirm_game_by_participant_of_a_different_game_is_not_participant(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """M1: guards `_CONFIRM_GAME_SQL`'s `EXISTS (... WHERE game_id = $1 ...
    AND user_id = $3)` -- a user who participates in game Y must not be
    treated as a participant of a *different* game X just because they're a
    participant of *some* game in the guild."""
    await players.ensure_players(app_conn, guild_id, [1, 2, 3, 4, 5])
    game_x = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2, 3])
    await games.create_game(app_conn, guild_id, None, PLAYED_ON, 4, 4, [5])

    result = await games.confirm_game(app_conn, guild_id, game_x.game_id, 4)

    assert result == "not_participant"
    unchanged = await games.get_game(app_conn, guild_id, game_x.game_id)
    assert unchanged is not None
    assert unchanged.game.status == "pending"


async def test_confirm_game_already_confirmed_is_refused(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])
    await games.confirm_game(app_conn, guild_id, game.game_id, 2)

    result = await games.confirm_game(app_conn, guild_id, game.game_id, 2)

    assert result == "not_pending"


async def test_confirm_rejected_game_is_refused(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])
    await games.reject_game(app_conn, guild_id, game.game_id, 2)

    assert await games.confirm_game(app_conn, guild_id, game.game_id, 2) == "not_pending"


async def test_confirm_voided_game_is_refused(app_conn: asyncpg.Connection, guild_id: int) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])
    await games.void_game(app_conn, guild_id, game.game_id, 999, "test void")

    assert await games.confirm_game(app_conn, guild_id, game.game_id, 2) == "not_pending"


async def test_confirm_game_not_found(app_conn: asyncpg.Connection, guild_id: int) -> None:
    result = await games.confirm_game(app_conn, guild_id, 999_999, 1)
    assert result == "not_found"


# ---------------------------------------------------------------------------
# reject_game
# ---------------------------------------------------------------------------


async def test_reject_game_by_participant_succeeds(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])

    result = await games.reject_game(app_conn, guild_id, game.game_id, 2)

    assert result == "rejected"
    rejected = await games.get_game(app_conn, guild_id, game.game_id)
    assert rejected is not None
    assert rejected.game.status == "rejected"
    assert rejected.game.rejected_by == 2


async def test_reject_game_by_reporter_succeeds(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])

    result = await games.reject_game(app_conn, guild_id, game.game_id, 1)

    assert result == "rejected"


async def test_reject_game_by_non_participant_is_refused(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2, 3])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])

    result = await games.reject_game(app_conn, guild_id, game.game_id, 3)

    assert result == "not_participant"


async def test_reject_game_by_participant_of_a_different_game_is_not_participant(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """M1: guards `_REJECT_GAME_SQL`'s `EXISTS (... WHERE game_id = $1 ...
    AND user_id = $3)`, the same way as confirm_game above."""
    await players.ensure_players(app_conn, guild_id, [1, 2, 3, 4, 5])
    game_x = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2, 3])
    await games.create_game(app_conn, guild_id, None, PLAYED_ON, 4, 4, [5])

    result = await games.reject_game(app_conn, guild_id, game_x.game_id, 4)

    assert result == "not_participant"
    unchanged = await games.get_game(app_conn, guild_id, game_x.game_id)
    assert unchanged is not None
    assert unchanged.game.status == "pending"


async def test_reject_game_by_reporter_who_is_not_a_participant_retracts_own_report(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """L1: a reporter who is *not* a participant of their own reported game
    can still retract it -- `_REJECT_GAME_SQL`'s `reported_by = $3` branch
    must succeed independently of the participant `EXISTS` check (see
    `reject_game`'s docstring: "participants, or the reporter retracting
    their own report")."""
    await players.ensure_players(app_conn, guild_id, [1, 2, 3])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 3, 1, [2])

    result = await games.reject_game(app_conn, guild_id, game.game_id, 3)

    assert result == "rejected"


async def test_reject_game_by_non_participant_non_reporter_is_refused(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """L1 counterpart: someone who is neither a participant nor the
    reporter still gets `not_participant`, proving the reporter-retract
    exception doesn't swallow every caller."""
    await players.ensure_players(app_conn, guild_id, [1, 2, 3, 4])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 3, 1, [2])

    result = await games.reject_game(app_conn, guild_id, game.game_id, 4)

    assert result == "not_participant"


async def test_reject_confirmed_game_is_refused(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])
    await games.confirm_game(app_conn, guild_id, game.game_id, 2)

    assert await games.reject_game(app_conn, guild_id, game.game_id, 2) == "not_pending"


async def test_reject_game_not_found(app_conn: asyncpg.Connection, guild_id: int) -> None:
    assert await games.reject_game(app_conn, guild_id, 999_999, 1) == "not_found"


# ---------------------------------------------------------------------------
# void_game
# ---------------------------------------------------------------------------


async def test_void_pending_game_succeeds(app_conn: asyncpg.Connection, guild_id: int) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])

    result = await games.void_game(app_conn, guild_id, game.game_id, 999, "bad report")

    assert result == "voided"
    voided = await games.get_game(app_conn, guild_id, game.game_id)
    assert voided is not None
    assert voided.game.status == "voided"
    assert voided.game.voided_by == 999
    assert voided.game.void_reason == "bad report"


async def test_void_confirmed_game_keeps_confirmation_fields(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])
    await games.confirm_game(app_conn, guild_id, game.game_id, 2)

    result = await games.void_game(app_conn, guild_id, game.game_id, 999, None)

    assert result == "voided"
    voided = await games.get_game(app_conn, guild_id, game.game_id)
    assert voided is not None
    assert voided.game.status == "voided"
    # Confirmation fields remain as an audit trail.
    assert voided.game.confirmed_by == 2
    assert voided.game.confirmed_at is not None


async def test_void_rejected_game_is_refused(app_conn: asyncpg.Connection, guild_id: int) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])
    await games.reject_game(app_conn, guild_id, game.game_id, 2)

    result = await games.void_game(app_conn, guild_id, game.game_id, 999, "too late")

    assert result == "not_pending_or_confirmed"


async def test_void_already_voided_game_is_refused(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])
    await games.void_game(app_conn, guild_id, game.game_id, 999, "first void")

    result = await games.void_game(app_conn, guild_id, game.game_id, 999, "second void")

    assert result == "not_pending_or_confirmed"


async def test_void_game_not_found(app_conn: asyncpg.Connection, guild_id: int) -> None:
    assert await games.void_game(app_conn, guild_id, 999_999, 999, "n/a") == "not_found"


# ---------------------------------------------------------------------------
# list_recent_games / list_recent_games_for_player
# ---------------------------------------------------------------------------


async def test_list_recent_games_orders_newest_first_and_respects_limit(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    first = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])
    second = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])

    listed = await games.list_recent_games(app_conn, guild_id, 10)
    assert [g.game_id for g in listed] == [second.game_id, first.game_id]

    limited = await games.list_recent_games(app_conn, guild_id, 1)
    assert [g.game_id for g in limited] == [second.game_id]


async def test_list_recent_games_for_player_only_includes_their_games(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2, 3])
    involving_1 = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])
    await games.create_game(app_conn, guild_id, None, PLAYED_ON, 2, 2, [3])

    listed = await games.list_recent_games_for_player(app_conn, guild_id, 1, 10)

    assert [g.game_id for g in listed] == [involving_1.game_id]


async def test_create_game_round_trips_rules_time_and_scores(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    scores = (
        PlayerScore(
            user_id=1,
            total_points=10,
            breakdown=(
                ScoreEntry("settlements", 4),
                ScoreEntry("cities", 4),
                ScoreEntry("longest_road", 2),
                ScoreEntry("largest_army", 0),
                ScoreEntry("vp_cards", 0),
            ),
        ),
        PlayerScore(
            user_id=2,
            total_points=8,
            breakdown=(
                ScoreEntry("settlements", 4),
                ScoreEntry("cities", 4),
                ScoreEntry("longest_road", 0),
                ScoreEntry("largest_army", 0),
                ScoreEntry("vp_cards", 0),
            ),
        ),
    )
    played_at = datetime(2026, 3, 1, 20, 30, tzinfo=UTC)

    created = await games.create_game(
        app_conn,
        guild_id,
        None,
        PLAYED_ON,
        1,
        1,
        [2],
        game_type="normal",
        extension_5_6=False,
        scenario="League night",
        target_points=10,
        played_at=played_at,
        played_timezone="America/Chicago",
        scores=scores,
    )

    assert created.scenario == "League night"
    assert created.played_at == played_at
    fetched = await games.get_game(app_conn, guild_id, created.game_id)
    assert fetched is not None
    assert fetched.game.game_type == "normal"
    assert fetched.game.target_points == 10
    assert fetched.game.played_timezone == "America/Chicago"
    assert fetched.scores == scores


async def test_absent_scores_stay_null_while_explicit_zero_scores_round_trip(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    absent = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2], scores=[])
    zero_scores = (
        PlayerScore(user_id=1, total_points=0, breakdown=(ScoreEntry("settlements", 0),)),
        PlayerScore(user_id=2, total_points=0, breakdown=(ScoreEntry("settlements", 0),)),
    )
    recorded = await games.create_game(
        app_conn, guild_id, None, PLAYED_ON, 1, 1, [2], scores=zero_scores
    )

    absent_rows = await app_conn.fetch(
        "SELECT total_points, score_breakdown FROM game_participants WHERE game_id = $1",
        absent.game_id,
    )
    recorded_rows = await app_conn.fetch(
        "SELECT total_points, score_breakdown FROM game_participants WHERE game_id = $1",
        recorded.game_id,
    )
    assert all(
        row["total_points"] is None and row["score_breakdown"] is None for row in absent_rows
    )
    assert all(
        row["total_points"] == 0 and row["score_breakdown"] is not None
        for row in recorded_rows
    )
    absent_fetched = await games.get_game(app_conn, guild_id, absent.game_id)
    recorded_fetched = await games.get_game(app_conn, guild_id, recorded.game_id)
    assert absent_fetched is not None
    assert recorded_fetched is not None
    assert absent_fetched.scores == ()
    assert recorded_fetched.scores == zero_scores


async def test_recent_history_orders_by_date_then_time_then_game_id_and_scopes_guild(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    await players.ensure_players(app_conn, other_guild_id, [1, 2])
    legacy = await games.create_game(app_conn, guild_id, None, date(2026, 3, 2), 1, 1, [2])
    earlier_time = await games.create_game(
        app_conn,
        guild_id,
        None,
        date(2026, 3, 2),
        1,
        1,
        [2],
        played_at=datetime(2026, 3, 3, 1, 0, tzinfo=UTC),
        played_timezone="America/Chicago",
    )
    later_time = await games.create_game(
        app_conn,
        guild_id,
        None,
        date(2026, 3, 2),
        1,
        1,
        [2],
        played_at=datetime(2026, 3, 3, 2, 0, tzinfo=UTC),
        played_timezone="America/Chicago",
    )
    same_time = await games.create_game(
        app_conn,
        guild_id,
        None,
        date(2026, 3, 2),
        1,
        1,
        [2],
        played_at=datetime(2026, 3, 3, 2, 0, tzinfo=UTC),
        played_timezone="America/Chicago",
    )
    older_day = await games.create_game(app_conn, guild_id, None, date(2026, 3, 1), 1, 1, [2])
    await games.create_game(app_conn, other_guild_id, None, date(2026, 3, 4), 1, 1, [2])

    listed = await games.list_recent_games(app_conn, guild_id, 10)

    assert [game.game_id for game in listed] == [
        same_time.game_id,
        later_time.game_id,
        earlier_time.game_id,
        legacy.game_id,
        older_day.game_id,
    ]


def _update_scores(*user_ids: int, zero: bool = False) -> tuple[PlayerScore, ...]:
    """Small complete persistence representations; domain validation is service-owned."""
    return tuple(
        PlayerScore(
            user_id=user_id,
            total_points=0 if zero else 10 - index,
            breakdown=(ScoreEntry("settlements", 0 if zero else 10 - index),),
        )
        for index, user_id in enumerate(user_ids)
    )


async def _confirmed_game(
    conn: asyncpg.Connection, guild_id: int, *, winner: int = 1, losers: list[int] | None = None
):
    losers = [2] if losers is None else losers
    game = await games.create_game(conn, guild_id, None, PLAYED_ON, winner, winner, losers)
    assert await games.confirm_game(conn, guild_id, game.game_id, losers[0]) == "confirmed"
    return game


async def test_update_confirmed_game_replaces_active_roster_and_appends_audit(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2, 3])
    game = await _confirmed_game(app_conn, guild_id)

    updated = await games.update_confirmed_game(
        app_conn,
        guild_id,
        game.game_id,
        expected_revision=0,
        updated_by=99,
        reason=None,
        played_on=date(2026, 3, 2),
        winner_id=3,
        loser_ids=[1],
        game_type="normal",
        extension_5_6=False,
        scenario=None,
        target_points=10,
        played_at=None,
        played_timezone=None,
        scores=None,
    )

    assert not isinstance(updated, str)
    assert updated.game.revision == 1
    assert updated.game.updated_by == 99
    assert updated.game.update_reason is None
    assert updated.winner_id == 3
    assert updated.loser_ids == (1,)
    assert updated.scores == ()
    # The removed player must disappear from player history, while retained
    # rows still read through the active-only participant filter.
    assert await games.list_recent_games_for_player(app_conn, guild_id, 2, 10) == []
    player_three_history = await games.list_recent_games_for_player(app_conn, guild_id, 3, 10)
    assert [g.game_id for g in player_three_history] == [game.game_id]
    inactive = await app_conn.fetchval(
        "SELECT is_active FROM game_participants WHERE game_id = $1 AND user_id = $2",
        game.game_id,
        2,
    )
    assert inactive is False
    audit = await app_conn.fetchrow(
        "SELECT revision, reason, before_snapshot, after_snapshot FROM game_updates "
        "WHERE game_id = $1",
        game.game_id,
    )
    assert audit["revision"] == 1
    assert audit["reason"] is None
    before = json.loads(audit["before_snapshot"])
    after = json.loads(audit["after_snapshot"])
    assert before["participants"][0]["user_id"] == 1
    assert {p["user_id"] for p in after["participants"]} == {1, 3}


async def test_update_confirmed_game_reactivates_prior_player_and_preserves_zero_scores(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2, 3])
    game = await _confirmed_game(app_conn, guild_id)
    first = await games.update_confirmed_game(
        app_conn,
        guild_id,
        game.game_id,
        expected_revision=0,
        updated_by=9,
        reason="swap",
        played_on=PLAYED_ON,
        winner_id=3,
        loser_ids=[1],
        game_type="normal",
        extension_5_6=False,
        scenario=None,
        target_points=None,
        played_at=None,
        played_timezone=None,
        scores=None,
    )
    assert not isinstance(first, str)
    second = await games.update_confirmed_game(
        app_conn,
        guild_id,
        game.game_id,
        expected_revision=1,
        updated_by=9,
        reason="restore",
        played_on=PLAYED_ON,
        winner_id=1,
        loser_ids=[2],
        game_type="normal",
        extension_5_6=False,
        scenario=None,
        target_points=None,
        played_at=None,
        played_timezone=None,
        scores=_update_scores(1, 2, zero=True),
    )
    assert not isinstance(second, str)
    assert second.scores == _update_scores(1, 2, zero=True)
    count = await app_conn.fetchval(
        "SELECT COUNT(*) FROM game_participants WHERE game_id = $1 AND user_id = $2",
        game.game_id,
        2,
    )
    assert count == 1
    assert await app_conn.fetchval(
        "SELECT is_active FROM game_participants WHERE game_id = $1 AND user_id = $2",
        game.game_id,
        2,
    ) is True
    full = await games.update_confirmed_game(
        app_conn,
        guild_id,
        game.game_id,
        expected_revision=2,
        updated_by=9,
        reason=None,
        played_on=PLAYED_ON,
        winner_id=1,
        loser_ids=[2],
        game_type="normal",
        extension_5_6=False,
        scenario=None,
        target_points=None,
        played_at=None,
        played_timezone=None,
        scores=_update_scores(1, 2),
    )
    assert not isinstance(full, str)
    assert full.scores == _update_scores(1, 2)
    assert await app_conn.fetchval(
        "SELECT COUNT(*) FROM game_updates WHERE game_id = $1", game.game_id
    ) == 3


async def test_update_confirmed_game_guards_guild_status_and_revision(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    await players.ensure_players(app_conn, other_guild_id, [1, 2])
    game = await _confirmed_game(app_conn, guild_id)
    kwargs = dict(
        expected_revision=0,
        updated_by=9,
        reason=None,
        played_on=PLAYED_ON,
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
    foreign = await games.update_confirmed_game(app_conn, other_guild_id, game.game_id, **kwargs)
    assert foreign == "not_found"
    missing = await games.update_confirmed_game(app_conn, guild_id, 999_999, **kwargs)
    assert missing == "not_found"
    first = await games.update_confirmed_game(app_conn, guild_id, game.game_id, **kwargs)
    assert not isinstance(first, str)
    stale = await games.update_confirmed_game(app_conn, guild_id, game.game_id, **kwargs)
    assert stale == "stale"
    pending = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])
    not_confirmed = await games.update_confirmed_game(app_conn, guild_id, pending.game_id, **kwargs)
    assert not_confirmed == "not_confirmed"


async def test_update_confirmed_game_rolls_back_if_a_late_step_fails(
    app_conn: asyncpg.Connection, guild_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2, 3])
    game = await _confirmed_game(app_conn, guild_id)
    real_get_game = games.get_game

    async def fail_after_roster(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected failure")

    monkeypatch.setattr(games, "get_game", fail_after_roster)
    with pytest.raises(RuntimeError, match="injected failure"):
        await games.update_confirmed_game(
            app_conn, guild_id, game.game_id, expected_revision=0, updated_by=9, reason=None,
            played_on=PLAYED_ON, winner_id=3, loser_ids=[1], game_type="normal",
            extension_5_6=False, scenario=None, target_points=None, played_at=None,
            played_timezone=None, scores=None,
        )
    monkeypatch.setattr(games, "get_game", real_get_game)
    fetched = await games.get_game(app_conn, guild_id, game.game_id)
    assert fetched is not None
    assert fetched.game.revision == 0
    assert fetched.winner_id == 1
    assert fetched.loser_ids == (2,)
    assert await app_conn.fetchval(
        "SELECT COUNT(*) FROM game_updates WHERE game_id = $1", game.game_id
    ) == 0


async def test_inactive_participant_cannot_confirm_or_reject_pending_game(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])
    await app_conn.execute(
        "UPDATE game_participants SET is_active = false WHERE game_id = $1 AND user_id = $2",
        game.game_id,
        2,
    )
    assert await games.confirm_game(app_conn, guild_id, game.game_id, 2) == "not_participant"
    assert await games.reject_game(app_conn, guild_id, game.game_id, 2) == "not_participant"
