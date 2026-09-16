"""Frozen dataclasses for rows read back from Postgres.

Repositories never return raw `asyncpg.Record` objects: every read function
in `db/repositories/` converts its result(s) into one of these before
returning, so callers get typed, immutable values instead of a dict-like
`Record` (which is easy to accidentally mutate-shaped code around, and
doesn't self-document its columns).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

GameStatus = Literal["pending", "confirmed", "rejected", "voided"]
SeasonStatus = Literal["active", "completed", "cancelled"]
EventStatus = Literal["scheduled", "cancelled", "completed"]
RsvpResponse = Literal["going", "maybe", "not_going"]
SeasonOutcome = Literal["payer", "payee"]

# The result of a guarded status-transition UPDATE (confirm_game,
# reject_game, void_game, cancel_event). Each function only ever returns a
# subset of these: its own success value, plus whichever failure reasons its
# WHERE clause can actually distinguish (see each function's docstring).
TransitionResult = Literal[
    "confirmed",
    "rejected",
    "voided",
    "cancelled",
    "not_found",
    "not_pending",
    "not_pending_or_confirmed",
    "not_scheduled",
    "reporter_cannot_confirm",
    "not_participant",
    "not_creator_or_admin",
]


@dataclass(frozen=True, slots=True)
class GuildConfig:
    guild_id: int
    timezone: str
    announce_channel_id: int | None
    admin_role_id: int | None
    default_min_games: int
    created_at: datetime
    updated_at: datetime
    # Optional role to mention for scheduled-event notifications.  Kept
    # nullable so existing guilds remain silent until explicitly configured.
    player_role_id: int | None = None


@dataclass(frozen=True, slots=True)
class Season:
    season_id: int
    guild_id: int
    name: str
    starts_on: date
    ends_on: date
    ends_at: datetime
    min_games: int
    status: SeasonStatus
    resolved_at: datetime | None
    announced_at: datetime | None
    created_by: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SeasonResultRow:
    """One player's frozen standing in a completed season.

    Carries no `season_id`/`guild_id`: those are always supplied as explicit
    arguments to the repository functions that read or write these (e.g.
    `complete_season`, `get_season_results`), matching how `PlayerStats`
    carries no guild/season context either.
    """

    user_id: int
    rank: int
    games: int
    wins: int
    eligible: bool
    outcome: SeasonOutcome | None


@dataclass(frozen=True, slots=True)
class Game:
    game_id: int
    guild_id: int
    season_id: int | None
    played_on: date
    status: GameStatus
    reported_by: int
    confirmed_by: int | None
    confirmed_at: datetime | None
    voided_by: int | None
    voided_at: datetime | None
    void_reason: str | None
    rejected_by: int | None
    rejected_at: datetime | None
    channel_id: int | None
    message_id: int | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class GameWithParticipants:
    game: Game
    winner_id: int
    loser_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Event:
    event_id: int
    guild_id: int
    title: str
    description: str | None
    location: str | None
    starts_at: datetime
    status: EventStatus
    created_by: int
    channel_id: int | None
    message_id: int | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RsvpCounts:
    going: int
    maybe: int
    not_going: int


@dataclass(frozen=True, slots=True)
class RsvpRoster:
    """The complete, guild-scoped RSVP roster for an event.

    IDs are grouped by response and are kept in repository order.  The count
    properties make this safe to pass to callers that only need exact totals,
    while retaining the IDs needed to render a useful event breakdown.
    """

    going: tuple[int, ...]
    maybe: tuple[int, ...]
    not_going: tuple[int, ...]

    @property
    def going_count(self) -> int:
        return len(self.going)

    @property
    def maybe_count(self) -> int:
        return len(self.maybe)

    @property
    def not_going_count(self) -> int:
        return len(self.not_going)

    @property
    def counts(self) -> RsvpCounts:
        return RsvpCounts(self.going_count, self.maybe_count, self.not_going_count)


@dataclass(frozen=True, slots=True)
class ClaimedReminder:
    """One reminder claimed by `events.claim_due_reminders`.

    The caller (the M4/M5 scheduler) uses `starts_at`/`remind_at` with
    `domain.reminders.classify_reminder` to decide send vs. skip-stale.
    """

    event_id: int
    guild_id: int
    channel_id: int | None
    title: str
    starts_at: datetime
    offset_minutes: int
    remind_at: datetime
    player_role_id: int | None = None
