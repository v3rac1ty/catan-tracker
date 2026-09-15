"""Behavior tests for `catan_bot.db.repositories.games`."""

from __future__ import annotations

import os
from datetime import date

import asyncpg
import pytest

from catan_bot.db.repositories import games, players

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
