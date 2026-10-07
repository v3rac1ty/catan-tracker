"""Per-player, group-meta and head-to-head insights over confirmed games."""

from __future__ import annotations

from typing import get_args

import asyncpg

from catan_bot.db.models import Season
from catan_bot.db.repositories import analytics as analytics_repo
from catan_bot.db.repositories import guilds, seasons
from catan_bot.domain import analytics as domain_analytics
from catan_bot.domain.participation import ParticipationRecord
from catan_bot.services.results import (
    ChartInsightsView,
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
    """Resolve scope and a single game type; fetch only that type's participation rows.

    Insights never mix game types: the scope's rows are fetched once without a
    type filter, per-type game counts are taken, the type is the caller's choice
    (else the most-played one) and the rows are narrowed to it in memory.
    """
    await guilds.ensure_guild(conn, guild_id)
    season: Season | None = None
    if scope == "season":
        season = await seasons.get_active_season(conn, guild_id)
        if season is None:
            flt = InsightsFilter(scope=scope, season=None, game_type=game_type)
            return flt, []
        records = await analytics_repo.list_participations(
            conn, guild_id, season_id=season.season_id
        )
    else:
        records = await analytics_repo.list_participations(conn, guild_id)

    counts = domain_analytics.games_by_type(records)
    defaulted = False
    if game_type is None:
        game_type = domain_analytics.most_played_game_type(counts)
        defaulted = game_type is not None
    flt = InsightsFilter(
        scope=scope,
        season=season,
        game_type=game_type,
        available_game_types=counts,
        game_type_defaulted=defaulted,
    )
    return flt, [record for record in records if record.game_type == game_type]


def _require_valid(scope: object, game_type: object) -> None:
    _require_scope(scope)
    if game_type is not None and game_type not in domain_analytics.GAME_TYPE_ORDER:
        # Fixed message, like the scope check: never echo the caller's value.
        raise ValueError("invalid insights game type")


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
    _require_valid(scope, game_type)
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
    _require_valid(scope, game_type)
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
    _require_valid(scope, game_type)
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


async def chart_insights(
    pool: asyncpg.Pool,
    guild_id: int,
    *,
    scope: InsightsScope = "all_time",
    game_type: str | None = None,
) -> ChartInsightsView:
    _require_valid(scope, game_type)
    async with pool.acquire() as conn:
        flt, records = await _load(conn, guild_id, scope, game_type)
    return ChartInsightsView(
        filter=flt,
        meta=domain_analytics.meta_summary(records),
        players=domain_analytics.player_summaries(records),
        head_to_head=domain_analytics.head_to_head(records),
        timeline=domain_analytics.win_rate_timeline(records),
    )
