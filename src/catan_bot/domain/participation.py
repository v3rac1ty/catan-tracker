"""One player's result in one confirmed game -- the input to analytics."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class ParticipationRecord:
    game_id: int
    played_on: date
    played_at: datetime | None
    game_type: str  # one of scoring.GameType's values
    extension_5_6: bool
    target_points: int | None
    player_count: int  # active participants in this game
    user_id: int
    is_winner: bool
    total_points: int | None  # None = score not recorded
    breakdown: Mapping[str, int] | None  # source key -> points; None iff total_points is None
    season_id: int | None = None
    played_timezone: str | None = None  # IANA name `played_at` was entered in
