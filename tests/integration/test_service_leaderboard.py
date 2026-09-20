"""Integration tests for `catan_bot.services.leaderboard_service`.

Covers what only makes sense against real SQL: the local-time/local-date due
check across a non-UTC timezone (and across that timezone's local day
boundary), the claim preventing a double-post, `leaderboard_after_game`'s
mode/channel gating, and movement being computed from -- and then
persisted as -- `guild_config.leaderboard_last_ranking`.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, time

import asyncpg
import pytest

from catan_bot.domain.validation import ParticipantRef
from catan_bot.services import config_service, game_service, leaderboard_service
from catan_bot.services.context import Actor

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

CHANNEL_ID = 555_000
REPORT_NOW = datetime(2026, 9, 14, 18, 0, tzinfo=UTC)


def _actor(user_id: int, *, admin: bool = False) -> Actor:
    return Actor(user_id=user_id, has_manage_guild=admin, role_ids=frozenset())


def _ref(user_id: int) -> ParticipantRef:
    return ParticipantRef(user_id=user_id, is_bot=False)


async def _confirmed_game(
    pool: asyncpg.Pool, guild_id: int, winner: int, loser: int, *, date_text: str
) -> int:
    """Report a game (reported by the winner) and confirm it via the loser."""
    created = await game_service.report_game(
        pool,
        guild_id,
        _actor(winner),
        winner=_ref(winner),
        losers=[_ref(loser)],
        date_text=date_text,
        now=REPORT_NOW,
    )
    await game_service.confirm_game(pool, guild_id, created.game.game_id, _actor(loser))
    return created.game.game_id


async def _configure_daily(
    pool: asyncpg.Pool, guild_id: int, *, daily_time: time = time(22, 0)
) -> None:
    await config_service.set_timezone(pool, guild_id, _actor(1, admin=True), "America/Chicago")
    await config_service.set_leaderboard_settings(
        pool,
        guild_id,
        _actor(1, admin=True),
        mode="daily",
        channel_id=CHANNEL_ID,
        scope="all_time",
        daily_time=daily_time,
    )


# ---------------------------------------------------------------------------
# due_daily_leaderboards: local-time due check, across a non-UTC timezone.
# ---------------------------------------------------------------------------


async def test_due_daily_leaderboards_waits_for_configured_local_time(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await _configure_daily(pool, guild_id, daily_time=time(22, 0))
    await _confirmed_game(pool, guild_id, winner=10, loser=20, date_text="2026-09-14")

    # America/Chicago is UTC-5 in September (CDT): 2026-09-15 02:00 UTC is
    # 2026-09-14 21:00 local -- one hour before the configured 22:00 post
    # time, so nothing should be due yet.
    before_threshold = datetime(2026, 9, 15, 2, 0, tzinfo=UTC)
    assert await leaderboard_service.due_daily_leaderboards(pool, before_threshold, 50) == []

    # 2026-09-15 03:05 UTC is 2026-09-14 22:05 local -- five minutes past
    # the configured time, on the same local day the game was played.
    after_threshold = datetime(2026, 9, 15, 3, 5, tzinfo=UTC)
    posts = await leaderboard_service.due_daily_leaderboards(pool, after_threshold, 50)

    assert len(posts) == 1
    assert posts[0].guild_id == guild_id
    assert posts[0].channel_id == CHANNEL_ID
    assert {game.winner_id for game in posts[0].games} == {10}


async def test_due_daily_leaderboards_local_day_boundary_separates_digests(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    """A moment just before local midnight sees the day's game; a moment
    just after sees a fresh (empty) local day and posts nothing."""
    await _configure_daily(pool, guild_id, daily_time=time(22, 0))
    await _confirmed_game(pool, guild_id, winner=10, loser=20, date_text="2026-09-14")

    # 2026-09-15 04:30 UTC = 2026-09-14 23:30 America/Chicago: still the
    # local day the game was played on, and past the 22:00 post time.
    just_before_midnight = datetime(2026, 9, 15, 4, 30, tzinfo=UTC)
    posts = await leaderboard_service.due_daily_leaderboards(pool, just_before_midnight, 50)
    assert len(posts) == 1
    assert date(2026, 9, 14) in {game.game.played_on for game in posts[0].games}

    # 2026-09-15 05, 06 UTC = 2026-09-15 00:xx America/Chicago: the local
    # calendar day has now rolled over. No game was played *on* the 15th,
    # so nothing is due for that new local day -- proving the digest is
    # scoped to one local day at a time, not "since the last post".
    just_after_midnight = datetime(2026, 9, 15, 6, 0, tzinfo=UTC)
    assert await leaderboard_service.due_daily_leaderboards(pool, just_after_midnight, 50) == []


# ---------------------------------------------------------------------------
# due_daily_leaderboards: the claim is at-most-once per local day.
# ---------------------------------------------------------------------------


async def test_due_daily_leaderboards_claim_prevents_a_double_post(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await _configure_daily(pool, guild_id, daily_time=time(22, 0))
    await _confirmed_game(pool, guild_id, winner=10, loser=20, date_text="2026-09-14")
    due_now = datetime(2026, 9, 15, 3, 5, tzinfo=UTC)  # 22:05 local

    first = await leaderboard_service.due_daily_leaderboards(pool, due_now, 50)
    # A second sweep at the same instant -- simulating a restart-style
    # repeat tick, or a second concurrent scheduler process -- must not
    # claim (and therefore must not post) the same guild's digest again.
    second = await leaderboard_service.due_daily_leaderboards(pool, due_now, 50)

    assert len(first) == 1
    assert second == []


async def test_due_daily_leaderboards_skips_quiet_local_day_without_claiming(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    """No confirmed games on the local day -> nothing is claimed, so a game
    confirmed *later* the same day is still picked up by a later check."""
    await _configure_daily(pool, guild_id, daily_time=time(9, 0))
    due_now = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)  # 10:00 local, quiet

    assert await leaderboard_service.due_daily_leaderboards(pool, due_now, 50) == []

    await _confirmed_game(pool, guild_id, winner=10, loser=20, date_text="2026-09-14")
    later_same_day = datetime(2026, 9, 14, 16, 0, tzinfo=UTC)  # 11:00 local
    posts = await leaderboard_service.due_daily_leaderboards(pool, later_same_day, 50)

    assert len(posts) == 1


# ---------------------------------------------------------------------------
# leaderboard_after_game: mode/channel gating.
# ---------------------------------------------------------------------------


async def test_leaderboard_after_game_is_none_when_mode_is_off(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    assert await leaderboard_service.leaderboard_after_game(pool, guild_id) is None


async def test_leaderboard_after_game_is_none_in_daily_mode(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await _configure_daily(pool, guild_id)
    assert await leaderboard_service.leaderboard_after_game(pool, guild_id) is None


async def test_leaderboard_after_game_is_none_without_a_channel_configured(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await config_service.set_leaderboard_settings(
        pool,
        guild_id,
        _actor(1, admin=True),
        mode="per_game",
        channel_id=None,
        scope="season",
        daily_time=time(22, 0),
    )
    assert await leaderboard_service.leaderboard_after_game(pool, guild_id) is None


async def test_leaderboard_after_game_returns_a_post_in_per_game_mode(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await config_service.set_leaderboard_settings(
        pool,
        guild_id,
        _actor(1, admin=True),
        mode="per_game",
        channel_id=CHANNEL_ID,
        scope="all_time",
        daily_time=time(22, 0),
    )
    await _confirmed_game(pool, guild_id, winner=10, loser=20, date_text="2026-09-14")

    post = await leaderboard_service.leaderboard_after_game(pool, guild_id)

    assert post is not None
    assert post.guild_id == guild_id
    assert post.channel_id == CHANNEL_ID
    assert post.games == ()  # per-game posts never carry a "today's results" section
    assert {p.user_id for p in post.board.ranked} == {10, 20}


# ---------------------------------------------------------------------------
# Movement: computed from, then persisted as, leaderboard_last_ranking.
# ---------------------------------------------------------------------------


async def test_leaderboard_after_game_movement_reflects_and_updates_stored_ranking(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await config_service.set_leaderboard_settings(
        pool,
        guild_id,
        _actor(1, admin=True),
        mode="per_game",
        channel_id=CHANNEL_ID,
        scope="all_time",
        daily_time=time(22, 0),
    )

    # First confirmed game: no board has ever been posted before, so every
    # ranked player must show up as "new".
    await _confirmed_game(pool, guild_id, winner=10, loser=20, date_text="2026-09-14")
    first_post = await leaderboard_service.leaderboard_after_game(pool, guild_id)
    assert first_post is not None
    assert {m.direction for m in first_post.movements} == {"new"}

    after_first = await config_service.get_config(pool, guild_id)
    assert after_first.leaderboard_last_ranking is not None
    assert set(after_first.leaderboard_last_ranking) == {10, 20}

    # A second confirmed game brings in a third player (30) without
    # changing 10 or 20's relative order (default_min_games=2: 10 is now
    # eligible at 2-0, while 20 and 30 are both still ineligible 1-game
    # entries tied at 0 wins, tie-broken by user_id) -- so the previously
    # posted [10, 20] ranking is exactly what movement is computed against:
    # 10 and 20 keep their positions ("unchanged"), and only 30 is "new".
    await _confirmed_game(pool, guild_id, winner=10, loser=30, date_text="2026-09-14")
    second_post = await leaderboard_service.leaderboard_after_game(pool, guild_id)
    assert second_post is not None
    by_id = {m.user_id: m for m in second_post.movements}
    assert by_id[10].direction == "unchanged"
    assert by_id[20].direction == "unchanged"
    assert by_id[30].direction == "new"

    after_second = await config_service.get_config(pool, guild_id)
    assert after_second.leaderboard_last_ranking == tuple(
        p.user_id for p in second_post.board.ranked
    )
    assert after_second.leaderboard_last_ranking == (10, 20, 30)
