"""Integration tests for `catan_bot.services.stats_service`."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import asyncpg
import pytest

from catan_bot.domain.validation import ParticipantRef
from catan_bot.services import game_service, season_service, stats_service
from catan_bot.services.context import Actor

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

NOW = datetime(2026, 9, 14, 4, 30, tzinfo=UTC)


def _actor(user_id: int, *, admin: bool = False) -> Actor:
    return Actor(user_id=user_id, has_manage_guild=admin, role_ids=frozenset())


def _ref(user_id: int) -> ParticipantRef:
    return ParticipantRef(user_id=user_id, is_bot=False)


async def _confirmed_game(
    pool: asyncpg.Pool, guild_id: int, winner: int, loser: int, *, date_text: str
):
    created = await game_service.report_game(
        pool,
        guild_id,
        _actor(winner),
        winner=_ref(winner),
        losers=[_ref(loser)],
        date_text=date_text,
        now=NOW,
    )
    await game_service.confirm_game(pool, guild_id, created.game.game_id, _actor(loser))


async def test_leaderboard_rejects_unknown_scope(pool: asyncpg.Pool, guild_id: int) -> None:
    """I2: a fixed message -- never `repr(scope)` echoed back."""
    with pytest.raises(ValueError) as exc_info:
        await stats_service.leaderboard(pool, guild_id, "weekly")  # type: ignore[arg-type]
    assert str(exc_info.value) == "invalid leaderboard scope"
    assert "weekly" not in str(exc_info.value)


async def test_leaderboard_season_scope_without_active_season(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    board = await stats_service.leaderboard(pool, guild_id, "season")
    assert board.season is None
    assert board.ranked == []


async def test_leaderboard_season_scope_uses_active_seasons_min_games(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    season = await season_service.start_season(
        pool,
        guild_id,
        _actor(1, admin=True),
        name="S1",
        end_date_text="2026-12-31",
        start_date_text="2026-09-01",
        min_games=3,
        now=NOW,
    )
    await _confirmed_game(pool, guild_id, winner=1, loser=2, date_text="2026-09-10")

    board = await stats_service.leaderboard(pool, guild_id, "season")
    assert board.season is not None
    assert board.season.season_id == season.season_id
    assert board.min_games == 3
    ranked_by_id = {p.user_id: p for p in board.ranked}
    assert ranked_by_id[1].eligible is False  # only 1 game, needs 3


async def test_leaderboard_all_time_scope_uses_guild_default_min_games(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await _confirmed_game(pool, guild_id, winner=1, loser=2, date_text="2026-09-10")
    await _confirmed_game(pool, guild_id, winner=1, loser=2, date_text="2026-09-11")

    board = await stats_service.leaderboard(pool, guild_id, "all_time")
    assert board.season is None
    assert board.min_games == 2
    ranked_by_id = {p.user_id: p for p in board.ranked}
    assert ranked_by_id[1].wins == 2
    assert ranked_by_id[1].eligible is True


async def test_player_stats_season_none_without_active_season(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await _confirmed_game(pool, guild_id, winner=1, loser=2, date_text="2026-09-10")

    view = await stats_service.player_stats(pool, guild_id, 1)
    assert view.user_id == 1
    assert view.season is None
    assert view.all_time.games == 1
    assert view.all_time.wins == 1


async def test_player_stats_season_present_with_active_season(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await season_service.start_season(
        pool,
        guild_id,
        _actor(1, admin=True),
        name="S1",
        end_date_text="2026-12-31",
        start_date_text="2026-09-01",
        min_games=None,
        now=NOW,
    )
    await _confirmed_game(pool, guild_id, winner=1, loser=2, date_text="2026-09-10")

    view = await stats_service.player_stats(pool, guild_id, 1)
    assert view.season is not None
    assert view.season.games == 1
    assert view.season.wins == 1
