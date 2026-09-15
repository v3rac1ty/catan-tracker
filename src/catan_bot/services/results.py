"""Result dataclasses returned by the services layer.

These sit above `catan_bot.db.models` (repository rows) and
`catan_bot.domain` (pure computations): each one bundles a repository row
together with a domain computation over it, which is what a cog actually
needs to render a response.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from catan_bot.db.models import Event, Season, SeasonResultRow
from catan_bot.domain.bet import BetOutcome
from catan_bot.domain.ranking import PlayerStats, RankedPlayer

LeaderboardScope = Literal["season", "all_time"]


@dataclass(frozen=True, slots=True)
class SeasonResolution:
    """The outcome of resolving one season (`season_service._resolve`)."""

    season: Season
    ranked: list[RankedPlayer]
    outcome: BetOutcome
    completed: bool


@dataclass(frozen=True, slots=True)
class SeasonInfo:
    """The active season plus its live (not yet frozen) standings."""

    season: Season
    ranked: list[RankedPlayer]


@dataclass(frozen=True, slots=True)
class Announcement:
    """One completed-but-unannounced season, with its frozen results."""

    season: Season
    results: list[SeasonResultRow]


@dataclass(frozen=True, slots=True)
class Leaderboard:
    scope: LeaderboardScope
    season: Season | None
    min_games: int
    ranked: list[RankedPlayer]


@dataclass(frozen=True, slots=True)
class PlayerStatsView:
    user_id: int
    season: PlayerStats | None
    all_time: PlayerStats


@dataclass(frozen=True, slots=True)
class ReminderToSend:
    """One reminder ready to send: the event, its offset, and who to ping."""

    event: Event
    offset_minutes: int
    user_ids: tuple[int, ...]
