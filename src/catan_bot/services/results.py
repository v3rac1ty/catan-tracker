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
    """One reminder ready to send and its configured guild role.

    ``user_ids`` remains as a deprecated constructor-compatible field for
    callers compiled against the pre-role API.  Delivery deliberately ignores
    it: reminders may only mention the configured player role.
    """

    event: Event
    offset_minutes: int
    player_role_id: int | None = None
    user_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class EventCreation:
    """An event together with the role snapshot for its announcement.

    The role is read in the same transaction as event creation.  That makes
    the outbound announcement deterministic if an administrator changes the
    notification role while a create command is in flight.  Reminder delivery
    intentionally reads the current role instead.
    """

    event: Event
    player_role_id: int | None
