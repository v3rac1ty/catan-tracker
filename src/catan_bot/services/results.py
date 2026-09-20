"""Result dataclasses returned by the services layer.

These sit above `catan_bot.db.models` (repository rows) and
`catan_bot.domain` (pure computations): each one bundles a repository row
together with a domain computation over it, which is what a cog actually
needs to render a response.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from catan_bot.db.models import (
    Event,
    GameWithParticipants,
    ScoreRequestDeliveryStatus,
    Season,
    SeasonResultRow,
)
from catan_bot.domain.bet import BetOutcome
from catan_bot.domain.ranking import PlayerStats, RankedPlayer
from catan_bot.domain.scoring import GameRules, PlayerScore

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


@dataclass(frozen=True, slots=True)
class PreparedGameReport:
    """Validated, stable game metadata awaiting score-sheet submission.

    Preparation intentionally contains no database identity.  In particular,
    ``played_on`` is resolved before a Discord form is shown, so a submission
    after midnight cannot silently move a game to the next local day.
    """

    guild_id: int
    reporter_id: int
    winner_id: int
    loser_ids: tuple[int, ...]
    played_on: date
    played_at: datetime | None
    played_timezone: str | None
    rules: GameRules


@dataclass(frozen=True, slots=True)
class PlayerScoreState:
    """One participant's DM delivery/submission state for Phase 2's score collection.

    A thin, display-ready projection of `db.models.ScoreRequest` -- everything
    `formatting`'s progress field and `/game scores`'s access check need,
    without exposing DM channel/message ids or the re-prompt schedule that
    only the scheduler cares about.
    """

    user_id: int
    delivery_status: ScoreRequestDeliveryStatus
    submitted: bool


@dataclass(frozen=True, slots=True)
class ScoreCollectionStatus:
    """Per-participant score-collection progress for one game (Phase 2).

    Bundles the authoritative game/roster row with each participant's DM
    delivery/submission state, in the same user-id order `game_score_requests`
    is read back in. This is what both the public message's live progress
    field (`formatting`) and the nudge button (`views.game_confirm`) need to
    render "who's left" without querying the repository layer directly.
    """

    game: GameWithParticipants
    requests: tuple[PlayerScoreState, ...]

    @property
    def submitted_ids(self) -> tuple[int, ...]:
        return tuple(request.user_id for request in self.requests if request.submitted)

    @property
    def outstanding_ids(self) -> tuple[int, ...]:
        return tuple(request.user_id for request in self.requests if not request.submitted)

    @property
    def blocked_ids(self) -> tuple[int, ...]:
        return tuple(
            request.user_id
            for request in self.requests
            if not request.submitted and request.delivery_status == "blocked"
        )

    @property
    def complete(self) -> bool:
        return len(self.requests) > 0 and all(request.submitted for request in self.requests)


@dataclass(frozen=True, slots=True)
class PreparedGameUpdate:
    """Validated, immutable draft for editing one confirmed game.

    The original row and revision are retained as the optimistic-concurrency
    token. No database identity is created by preparation; the draft can be
    safely discarded if the editor abandons the Discord form.
    """

    guild_id: int
    game_id: int
    editor_id: int
    expected_revision: int
    original: GameWithParticipants
    winner_id: int
    loser_ids: tuple[int, ...]
    played_on: date
    played_at: datetime | None
    played_timezone: str | None
    rules: GameRules
    target_points_to_store: int | None
    initial_scores: tuple[PlayerScore, ...]
    update_reason: str | None
    season_id: int | None
