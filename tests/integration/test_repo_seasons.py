"""Behavior tests for `catan_bot.db.repositories.seasons`."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta

import asyncpg
import pytest

from catan_bot.db.errors import ActiveSeasonExistsError
from catan_bot.db.models import SeasonResultRow
from catan_bot.db.repositories import games, players, seasons

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

UNKNOWN_GUILD_ID = 999_999_998
STARTS_ON = date(2026, 1, 1)
ENDS_ON = date(2026, 12, 31)
ENDS_AT = datetime(2027, 1, 1, tzinfo=UTC)


async def _create_season(
    conn: asyncpg.Connection,
    guild_id: int,
    *,
    name: str = "Season One",
    starts_on: date = STARTS_ON,
    ends_on: date = ENDS_ON,
    ends_at: datetime = ENDS_AT,
    min_games: int = 2,
    created_by: int = 1,
):
    return await seasons.create_season(
        conn, guild_id, name, starts_on, ends_on, ends_at, min_games, created_by
    )


async def test_get_active_season_returns_none_when_no_season(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    assert await seasons.get_active_season(app_conn, guild_id) is None


async def test_create_season_becomes_the_active_season(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    created = await _create_season(app_conn, guild_id)

    assert created.status == "active"
    active = await seasons.get_active_season(app_conn, guild_id)
    assert active is not None
    assert active.season_id == created.season_id


async def test_stats_ignore_inactive_game_participants(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    season = await _create_season(app_conn, guild_id)
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(
        app_conn, guild_id, season.season_id, date(2026, 2, 1), 1, 1, [2]
    )
    assert await games.confirm_game(app_conn, guild_id, game.game_id, 2) == "confirmed"
    await app_conn.execute(
        "UPDATE game_participants SET is_active = false, is_winner = false "
        "WHERE game_id = $1 AND user_id = $2",
        game.game_id,
        2,
    )
    assert await seasons.season_player_stats(app_conn, guild_id, season.season_id) == [
        seasons.PlayerStats(user_id=1, games=1, wins=1)
    ]
    player_season, player_all_time = await seasons.player_stats(app_conn, guild_id, 2)
    assert player_season is not None
    assert (player_season.games, player_all_time.games) == (0, 0)


async def test_create_season_second_active_raises_active_season_exists_error(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await _create_season(app_conn, guild_id)

    with pytest.raises(ActiveSeasonExistsError) as exc_info:
        await _create_season(app_conn, guild_id, name="Season Two")

    assert exc_info.value.guild_id == guild_id
    # Only the first season exists.
    all_seasons = await seasons.list_seasons(app_conn, guild_id, 10)
    assert len(all_seasons) == 1


async def test_create_season_same_name_different_guilds_both_succeed(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    a = await _create_season(app_conn, guild_id, name="Shared Name")
    b = await _create_season(app_conn, other_guild_id, name="Shared Name")
    assert a.season_id != b.season_id


async def test_set_active_min_games_updates_active_season(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await _create_season(app_conn, guild_id, min_games=2)

    updated = await seasons.set_active_min_games(app_conn, guild_id, 4)

    assert updated is not None
    assert updated.min_games == 4


async def test_set_active_min_games_returns_none_without_active_season(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    assert await seasons.set_active_min_games(app_conn, guild_id, 4) is None


async def test_set_active_end_updates_active_season(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await _create_season(app_conn, guild_id)
    new_ends_on = date(2026, 6, 30)
    new_ends_at = datetime(2026, 7, 1, tzinfo=UTC)

    updated = await seasons.set_active_end(app_conn, guild_id, new_ends_on, new_ends_at)

    assert updated is not None
    assert updated.ends_on == new_ends_on
    assert updated.ends_at == new_ends_at


async def test_set_active_end_returns_none_without_active_season(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    assert await seasons.set_active_end(app_conn, guild_id, ENDS_ON, ENDS_AT) is None


async def test_set_active_end_before_start_raises_check_violation(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await _create_season(app_conn, guild_id)
    with pytest.raises(asyncpg.CheckViolationError):
        await seasons.set_active_end(app_conn, guild_id, date(2025, 1, 1), ENDS_AT)


async def test_cancel_active_season_marks_cancelled(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    created = await _create_season(app_conn, guild_id)

    cancelled = await seasons.cancel_active_season(app_conn, guild_id)

    assert cancelled is not None
    assert cancelled.season_id == created.season_id
    assert cancelled.status == "cancelled"
    assert await seasons.get_active_season(app_conn, guild_id) is None
    # A new active season can now be created (the unique index only blocks
    # a second *active* row).
    again = await _create_season(app_conn, guild_id, name="After Cancel")
    assert again.status == "active"


async def test_cancel_active_season_returns_none_without_active_season(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    assert await seasons.cancel_active_season(app_conn, guild_id) is None


async def test_list_seasons_orders_newest_first_and_respects_limit(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    first = await _create_season(app_conn, guild_id, name="First")
    await seasons.cancel_active_season(app_conn, guild_id)
    second = await _create_season(app_conn, guild_id, name="Second")

    listed = await seasons.list_seasons(app_conn, guild_id, 10)
    assert [s.season_id for s in listed] == [second.season_id, first.season_id]

    limited = await seasons.list_seasons(app_conn, guild_id, 1)
    assert len(limited) == 1
    assert limited[0].season_id == second.season_id


async def test_get_season_returns_none_for_unknown_season(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    assert await seasons.get_season(app_conn, guild_id, 999_999) is None


async def test_get_season_returns_none_for_wrong_guild(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    created = await _create_season(app_conn, guild_id)
    assert await seasons.get_season(app_conn, other_guild_id, created.season_id) is None


# ---------------------------------------------------------------------------
# Stats: only confirmed games count; pending/rejected/voided are excluded.
# ---------------------------------------------------------------------------


async def _play_game(
    conn: asyncpg.Connection,
    guild_id: int,
    season_id: int | None,
    winner: int,
    loser: int,
    *,
    status: str,
    reported_by: int | None = None,
) -> None:
    """Create a game between `winner`/`loser` and drive it to `status`."""
    reporter = reported_by if reported_by is not None else winner
    game = await games.create_game(
        conn, guild_id, season_id, date(2026, 2, 1), reporter, winner, [loser]
    )
    if status == "pending":
        return
    if status == "confirmed":
        confirmer = loser if reporter == winner else winner
        result = await games.confirm_game(conn, guild_id, game.game_id, confirmer)
        assert result == "confirmed"
    elif status == "rejected":
        result = await games.reject_game(conn, guild_id, game.game_id, loser)
        assert result == "rejected"
    elif status == "voided":
        result = await games.void_game(conn, guild_id, game.game_id, 1, "test void")
        assert result == "voided"
    else:  # pragma: no cover -- test-arg guard, not reachable with valid callers.
        raise ValueError(status)


async def test_season_stats_count_only_confirmed_games(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    season = await _create_season(app_conn, guild_id)
    await players.ensure_players(app_conn, guild_id, [1, 2])

    await _play_game(app_conn, guild_id, season.season_id, 1, 2, status="confirmed")
    await _play_game(app_conn, guild_id, season.season_id, 1, 2, status="pending")
    await _play_game(app_conn, guild_id, season.season_id, 1, 2, status="rejected")
    await _play_game(app_conn, guild_id, season.season_id, 1, 2, status="voided")

    stats = {
        s.user_id: s
        for s in await seasons.season_player_stats(app_conn, guild_id, season.season_id)
    }
    assert stats[1].games == 1
    assert stats[1].wins == 1
    assert stats[2].games == 1
    assert stats[2].wins == 0


async def test_all_time_stats_count_only_confirmed_games_and_span_seasons(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    season = await _create_season(app_conn, guild_id)
    await players.ensure_players(app_conn, guild_id, [1, 2])

    await _play_game(app_conn, guild_id, season.season_id, 1, 2, status="confirmed")
    await _play_game(app_conn, guild_id, None, 2, 1, status="confirmed")  # seasonless game
    await _play_game(app_conn, guild_id, season.season_id, 1, 2, status="pending")

    stats = {s.user_id: s for s in await seasons.all_time_player_stats(app_conn, guild_id)}
    assert stats[1].games == 2
    assert stats[1].wins == 1
    assert stats[2].games == 2
    assert stats[2].wins == 1


async def test_season_vs_all_time_stats_differ(app_conn: asyncpg.Connection, guild_id: int) -> None:
    season = await _create_season(app_conn, guild_id)
    await players.ensure_players(app_conn, guild_id, [1, 2])

    await _play_game(app_conn, guild_id, season.season_id, 1, 2, status="confirmed")
    await _play_game(app_conn, guild_id, None, 1, 2, status="confirmed")

    season_stats = {
        s.user_id: s
        for s in await seasons.season_player_stats(app_conn, guild_id, season.season_id)
    }
    all_time_stats = {s.user_id: s for s in await seasons.all_time_player_stats(app_conn, guild_id)}
    assert season_stats[1].games == 1
    assert all_time_stats[1].games == 2


async def test_player_stats_season_is_none_without_active_season(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    await _play_game(app_conn, guild_id, None, 1, 2, status="confirmed")

    season, all_time = await seasons.player_stats(app_conn, guild_id, 1)
    assert season is None
    assert all_time.games == 1
    assert all_time.wins == 1


async def test_player_stats_season_zero_games_when_active_but_unplayed(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await _create_season(app_conn, guild_id)
    await players.ensure_players(app_conn, guild_id, [1])

    season, all_time = await seasons.player_stats(app_conn, guild_id, 1)
    assert season is not None
    assert season.games == 0
    assert all_time.games == 0


async def test_player_stats_reflects_confirmed_games_in_active_season(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    season = await _create_season(app_conn, guild_id)
    await players.ensure_players(app_conn, guild_id, [1, 2])
    await _play_game(app_conn, guild_id, season.season_id, 1, 2, status="confirmed")

    season_stats, all_time_stats = await seasons.player_stats(app_conn, guild_id, 1)
    assert season_stats is not None
    assert season_stats.games == 1
    assert season_stats.wins == 1
    assert all_time_stats.games == 1


async def test_player_stats_season_and_all_time_each_count_only_confirmed_games(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """M4: `_SELECT_SEASON_PLAYER_STATS_FOR_USER_SQL` and
    `_SELECT_ALL_TIME_PLAYER_STATS_FOR_USER_SQL` both guard on
    `g.status = 'confirmed'` -- for one user with a pending, a rejected, a
    voided, a confirmed seasonless, and a confirmed old-season game besides
    the two confirmed active-season games, `season` must count exactly the
    2 active-season ones and `all_time` exactly the 4 confirmed ones."""
    await players.ensure_players(app_conn, guild_id, [1, 2])

    old_season = await _create_season(app_conn, guild_id, name="Old")
    await _play_game(app_conn, guild_id, old_season.season_id, 2, 1, status="confirmed")  # loss
    await seasons.cancel_active_season(app_conn, guild_id)

    active_season = await _create_season(app_conn, guild_id, name="Active")
    await _play_game(app_conn, guild_id, active_season.season_id, 1, 2, status="confirmed")  # win
    await _play_game(app_conn, guild_id, active_season.season_id, 2, 1, status="confirmed")  # loss
    await _play_game(app_conn, guild_id, active_season.season_id, 1, 2, status="pending")
    await _play_game(app_conn, guild_id, active_season.season_id, 1, 2, status="rejected")
    await _play_game(app_conn, guild_id, active_season.season_id, 1, 2, status="voided")
    await _play_game(app_conn, guild_id, None, 1, 2, status="confirmed")  # seasonless win

    season_stats, all_time_stats = await seasons.player_stats(app_conn, guild_id, 1)

    assert season_stats is not None
    assert season_stats.games == 2
    assert season_stats.wins == 1

    assert all_time_stats.games == 4
    assert all_time_stats.wins == 2


# ---------------------------------------------------------------------------
# M3: prior-state guards -- once a season is completed, nothing may mutate
# it again.
# ---------------------------------------------------------------------------


async def test_setters_on_completed_season_return_none_and_do_not_mutate(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """M3: `_UPDATE_ACTIVE_MIN_GAMES_SQL`, `_UPDATE_ACTIVE_END_SQL`, and
    `_CANCEL_ACTIVE_SEASON_SQL` all guard on `status = 'active'` -- once
    `complete_season` has run, none of them may touch the season again."""
    season = await _create_season(app_conn, guild_id)
    await seasons.complete_season(app_conn, guild_id, season.season_id, [])
    before = await seasons.get_season(app_conn, guild_id, season.season_id)
    assert before is not None
    assert before.status == "completed"

    assert await seasons.set_active_min_games(app_conn, guild_id, 9) is None
    assert (
        await seasons.set_active_end(
            app_conn, guild_id, date(2026, 6, 1), datetime(2026, 6, 2, tzinfo=UTC)
        )
        is None
    )
    assert await seasons.cancel_active_season(app_conn, guild_id) is None

    after = await seasons.get_season(app_conn, guild_id, season.season_id)
    assert after is not None
    assert after.min_games == before.min_games
    assert after.ends_on == before.ends_on
    assert after.ends_at == before.ends_at
    assert after.status == before.status
    assert after.resolved_at == before.resolved_at


# ---------------------------------------------------------------------------
# Scheduler queries.
# ---------------------------------------------------------------------------


async def test_lock_due_seasons_returns_due_and_skips_future(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    due = await _create_season(
        app_conn, guild_id, ends_on=date(2026, 5, 1), ends_at=now - timedelta(days=1)
    )
    await _create_season(
        app_conn, other_guild_id, ends_on=date(2027, 1, 1), ends_at=now + timedelta(days=200)
    )

    async with app_conn.transaction():
        locked = await seasons.lock_due_seasons(app_conn, now)

    assert [s.season_id for s in locked] == [due.season_id]


async def test_complete_season_success_and_second_call_is_idempotent(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    season = await _create_season(app_conn, guild_id)
    await players.ensure_players(app_conn, guild_id, [1, 2])
    results = [
        SeasonResultRow(user_id=1, rank=1, games=2, wins=2, eligible=True, outcome="payee"),
        SeasonResultRow(user_id=2, rank=2, games=2, wins=0, eligible=True, outcome="payer"),
    ]

    first_call = await seasons.complete_season(app_conn, guild_id, season.season_id, results)
    assert first_call is True

    completed = await seasons.get_season(app_conn, guild_id, season.season_id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.resolved_at is not None

    stored = await seasons.get_season_results(app_conn, guild_id, season.season_id)
    assert {r.user_id for r in stored} == {1, 2}

    second_call = await seasons.complete_season(app_conn, guild_id, season.season_id, results)
    assert second_call is False

    # No duplicate rows from the second call.
    stored_again = await seasons.get_season_results(app_conn, guild_id, season.season_id)
    assert len(stored_again) == 2


async def test_complete_season_not_active_returns_false(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    result = await seasons.complete_season(app_conn, guild_id, 999_999, [])
    assert result is False


async def test_list_unannounced_completed_and_mark_announced(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    season = await _create_season(app_conn, guild_id)
    await seasons.complete_season(app_conn, guild_id, season.season_id, [])

    unannounced = await seasons.list_unannounced_completed(app_conn, 10)
    assert season.season_id in {s.season_id for s in unannounced}

    await seasons.mark_announced(app_conn, guild_id, season.season_id)

    unannounced_after = await seasons.list_unannounced_completed(app_conn, 10)
    assert season.season_id not in {s.season_id for s in unannounced_after}

    announced = await seasons.get_season(app_conn, guild_id, season.season_id)
    assert announced is not None
    assert announced.announced_at is not None


async def test_get_season_results_orders_by_rank(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    season = await _create_season(app_conn, guild_id)
    await players.ensure_players(app_conn, guild_id, [1, 2, 3])
    results = [
        SeasonResultRow(user_id=3, rank=2, games=1, wins=0, eligible=True, outcome="payer"),
        SeasonResultRow(user_id=1, rank=1, games=1, wins=1, eligible=True, outcome="payee"),
        SeasonResultRow(user_id=2, rank=2, games=1, wins=0, eligible=True, outcome="payer"),
    ]
    await seasons.complete_season(app_conn, guild_id, season.season_id, results)

    stored = await seasons.get_season_results(app_conn, guild_id, season.season_id)
    assert [r.user_id for r in stored] == [1, 2, 3]


# ---------------------------------------------------------------------------
# L4: scheduler guards. The lock-contention (SKIP LOCKED) test lives in
# test_races.py, alongside the other two-connection concurrency tests.
# ---------------------------------------------------------------------------


async def test_lock_due_seasons_excludes_completed_season_even_if_ends_at_passed(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """L4: `_LOCK_DUE_SEASONS_SQL` guards on `status = 'active'` -- a
    season that's already completed must never be locked/returned again,
    no matter how far in the past its `ends_at` is."""
    due_at = datetime(2026, 6, 1, tzinfo=UTC) - timedelta(days=10)
    season = await _create_season(app_conn, guild_id, ends_at=due_at)
    await seasons.complete_season(app_conn, guild_id, season.season_id, [])

    async with app_conn.transaction():
        locked = await seasons.lock_due_seasons(app_conn, datetime(2026, 6, 1, tzinfo=UTC))

    assert season.season_id not in {s.season_id for s in locked}


async def test_list_unannounced_completed_excludes_active_seasons(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """L4: `_LIST_UNANNOUNCED_COMPLETED_SQL` guards on `status =
    'completed'` -- a still-active season (whose `announced_at` is also
    NULL) must never show up here."""
    season = await _create_season(app_conn, guild_id)

    unannounced = await seasons.list_unannounced_completed(app_conn, 50)

    assert season.season_id not in {s.season_id for s in unannounced}


async def test_mark_announced_on_active_season_leaves_announced_at_null(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """L4: `_MARK_ANNOUNCED_SQL` guards on `status = 'completed'` -- calling
    it on a still-active season must not set `announced_at`."""
    season = await _create_season(app_conn, guild_id)

    await seasons.mark_announced(app_conn, guild_id, season.season_id)

    unchanged = await seasons.get_season(app_conn, guild_id, season.season_id)
    assert unchanged is not None
    assert unchanged.announced_at is None


async def test_lock_active_season_returns_guilds_active_season(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """L2: `lock_active_season` returns the same row as `get_active_season`,
    just row-locked -- see `test_races.py` for proof it actually blocks."""
    created = await _create_season(app_conn, guild_id)
    async with app_conn.transaction():
        locked = await seasons.lock_active_season(app_conn, guild_id)
    assert locked is not None
    assert locked.season_id == created.season_id


async def test_lock_active_season_returns_none_for_a_different_guild(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    await _create_season(app_conn, guild_id)
    async with app_conn.transaction():
        locked = await seasons.lock_active_season(app_conn, other_guild_id)
    assert locked is None


async def test_lock_active_season_returns_none_without_an_active_season(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    async with app_conn.transaction():
        locked = await seasons.lock_active_season(app_conn, guild_id)
    assert locked is None


async def test_lock_next_due_season_returns_the_lowest_due_season_id(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    """L3: due seasons across guilds are picked in `season_id` order."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    first = await _create_season(app_conn, guild_id, ends_at=now - timedelta(days=2))
    second = await _create_season(app_conn, other_guild_id, ends_at=now - timedelta(days=1))
    assert first.season_id < second.season_id

    async with app_conn.transaction():
        locked = await seasons.lock_next_due_season(app_conn, now, [])
    assert locked is not None
    assert locked.season_id == first.season_id


async def test_lock_next_due_season_skips_excluded_ids(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    first = await _create_season(app_conn, guild_id, ends_at=now - timedelta(days=2))
    second = await _create_season(app_conn, other_guild_id, ends_at=now - timedelta(days=1))

    async with app_conn.transaction():
        locked = await seasons.lock_next_due_season(app_conn, now, [first.season_id])
    assert locked is not None
    assert locked.season_id == second.season_id

    async with app_conn.transaction():
        # Excluding both leaves nothing.
        none_left = await seasons.lock_next_due_season(
            app_conn, now, [first.season_id, second.season_id]
        )
    assert none_left is None


async def test_lock_next_due_season_excludes_completed_and_not_yet_due_seasons(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    completed = await _create_season(app_conn, guild_id, ends_at=now - timedelta(days=2))
    await seasons.complete_season(app_conn, guild_id, completed.season_id, [])
    not_due = await _create_season(app_conn, other_guild_id, ends_at=now + timedelta(days=2))

    async with app_conn.transaction():
        locked = await seasons.lock_next_due_season(app_conn, now, [])
    assert locked is None
    assert not_due.status == "active"  # sanity: it exists, it's just not due yet


async def test_lock_next_due_season_empty_exclude_list_excludes_nothing(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    due = await _create_season(app_conn, guild_id, ends_at=now - timedelta(days=1))
    async with app_conn.transaction():
        locked = await seasons.lock_next_due_season(app_conn, now, [])
    assert locked is not None
    assert locked.season_id == due.season_id


async def test_mark_announced_twice_on_completed_season_keeps_first_timestamp(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """L4: `_MARK_ANNOUNCED_SQL` also guards on `announced_at IS NULL` --
    a second call must be a no-op rather than bumping the timestamp."""
    season = await _create_season(app_conn, guild_id)
    await seasons.complete_season(app_conn, guild_id, season.season_id, [])

    await seasons.mark_announced(app_conn, guild_id, season.season_id)
    first = await seasons.get_season(app_conn, guild_id, season.season_id)
    assert first is not None
    assert first.announced_at is not None

    await seasons.mark_announced(app_conn, guild_id, season.season_id)
    second = await seasons.get_season(app_conn, guild_id, season.season_id)

    assert second is not None
    assert second.announced_at == first.announced_at
