"""Per-player, group-meta and head-to-head insights over confirmed games."""

from __future__ import annotations

from typing import get_args

import asyncpg

from catan_bot.db.repositories import analytics as analytics_repo
from catan_bot.db.repositories import guilds, seasons
from catan_bot.domain import analytics as domain_analytics
from catan_bot.domain.participation import ParticipationRecord
from catan_bot.services.results import (
    HeadToHeadView,
    InsightsFilter,
    InsightsScope,
    MetaInsightsView,
    OpponentRecord,
    PlayerInsightsView,
)

_VALID_SCOPES = frozenset(get_args(InsightsScope))


async def _load(
    conn: asyncpg.Connection,
    guild_id: int,
    scope: InsightsScope,
    game_type: str | None,
) -> tuple[InsightsFilter, list[ParticipationRecord]]:
    """Resolve the scope to a filter and fetch the matching participation rows."""
    await guilds.ensure_guild(conn, guild_id)
    if scope == "season":
        active = await seasons.get_active_season(conn, guild_id)
        flt = InsightsFilter(scope=scope, season=active, game_type=game_type)
        if active is None:
            return flt, []
        records = await analytics_repo.list_participations(
            conn, guild_id, season_id=active.season_id, game_type=game_type
        )
        return flt, records
    flt = InsightsFilter(scope=scope, season=None, game_type=game_type)
    records = await analytics_repo.list_participations(conn, guild_id, game_type=game_type)
    return flt, records


def _require_scope(scope: object) -> None:
    if scope not in _VALID_SCOPES:
        # Fixed message: never echo the caller-supplied value back (see
        # stats_service.leaderboard).
        raise ValueError("invalid insights scope")


async def player_insights(
    pool: asyncpg.Pool,
    guild_id: int,
    user_id: int,
    *,
    scope: InsightsScope = "all_time",
    game_type: str | None = None,
) -> PlayerInsightsView:
    _require_scope(scope)
    async with pool.acquire() as conn:
        flt, records = await _load(conn, guild_id, scope, game_type)
    return PlayerInsightsView(filter=flt, summary=domain_analytics.player_summary(records, user_id))


async def meta_insights(
    pool: asyncpg.Pool,
    guild_id: int,
    *,
    scope: InsightsScope = "all_time",
    game_type: str | None = None,
) -> MetaInsightsView:
    _require_scope(scope)
    async with pool.acquire() as conn:
        flt, records = await _load(conn, guild_id, scope, game_type)
    return MetaInsightsView(
        filter=flt,
        meta=domain_analytics.meta_summary(records),
        players=domain_analytics.player_summaries(records),
    )


async def head_to_head_insights(
    pool: asyncpg.Pool,
    guild_id: int,
    user_id: int,
    *,
    scope: InsightsScope = "all_time",
    game_type: str | None = None,
) -> HeadToHeadView:
    _require_scope(scope)
    async with pool.acquire() as conn:
        flt, records = await _load(conn, guild_id, scope, game_type)
    opponents: list[OpponentRecord] = []
    for pair in domain_analytics.head_to_head(records):
        # Pairs are stored with player_a < player_b; normalize to the
        # subject's point of view.
        if pair.player_a == user_id:
            opponents.append(
                OpponentRecord(pair.player_b, pair.games_together, pair.a_wins, pair.b_wins)
            )
        elif pair.player_b == user_id:
            opponents.append(
                OpponentRecord(pair.player_a, pair.games_together, pair.b_wins, pair.a_wins)
            )
    opponents.sort(key=lambda item: (-item.games_together, item.opponent_id))
    return HeadToHeadView(filter=flt, user_id=user_id, opponents=opponents)
