"""Leaderboards and per-player stats."""

from __future__ import annotations

from typing import get_args

import asyncpg

from catan_bot.db.repositories import guilds, seasons
from catan_bot.domain.ranking import rank_players
from catan_bot.services.results import Leaderboard, LeaderboardScope, PlayerStatsView

_VALID_SCOPES = frozenset(get_args(LeaderboardScope))


async def leaderboard(pool: asyncpg.Pool, guild_id: int, scope: LeaderboardScope) -> Leaderboard:
    if scope not in _VALID_SCOPES:
        # Fixed message: never echo `repr(scope)` back (I2 audit finding --
        # an unbounded, caller-supplied value reflected into an exception
        # message is a needless leak, even for a ValueError callers aren't
        # expected to show verbatim to a user).
        raise ValueError("invalid leaderboard scope")

    async with pool.acquire() as conn:
        config = await guilds.ensure_guild(conn, guild_id)

        if scope == "season":
            active = await seasons.get_active_season(conn, guild_id)
            if active is None:
                return Leaderboard(
                    scope=scope, season=None, min_games=config.default_min_games, ranked=[]
                )
            stats = await seasons.season_player_stats(conn, guild_id, active.season_id)
            ranked = rank_players(stats, min_games=active.min_games)
            return Leaderboard(
                scope=scope, season=active, min_games=active.min_games, ranked=ranked
            )

        stats = await seasons.all_time_player_stats(conn, guild_id)
        ranked = rank_players(stats, min_games=config.default_min_games)
        return Leaderboard(
            scope=scope, season=None, min_games=config.default_min_games, ranked=ranked
        )


async def player_stats(pool: asyncpg.Pool, guild_id: int, user_id: int) -> PlayerStatsView:
    async with pool.acquire() as conn:
        await guilds.ensure_guild(conn, guild_id)
        season_stats, all_time_stats = await seasons.player_stats(conn, guild_id, user_id)
        return PlayerStatsView(user_id=user_id, season=season_stats, all_time=all_time_stats)
