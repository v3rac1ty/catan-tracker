"""Pure leaderboard ranking with exact win-rate ties via `fractions.Fraction`.

Floats never enter this module: 1/3 and 2/6 must compare equal exactly, with
no rounding surprises, since a tied bet payout hinges on it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction

from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.validation import validate_min_games


@dataclass(frozen=True, slots=True)
class PlayerStats:
    """One player's raw confirmed-game tally, before ranking is applied."""

    user_id: int
    games: int
    wins: int

    def __post_init__(self) -> None:
        # Exact-type check (not `isinstance`): a `bool`, an `IntEnum`, or
        # any other `int` subclass is rejected here too, for the same
        # reason as `_validate_player_id` in `validation.py`.
        if type(self.user_id) is not int:
            raise DomainValidationError("A player id must be a whole number.")
        if type(self.games) is not int or type(self.wins) is not int:
            raise DomainValidationError("Games and wins must be whole numbers.")
        if self.user_id <= 0:
            raise DomainValidationError("A player id must be a positive number.")
        if self.games < 0:
            raise DomainValidationError("Games played can't be negative.")
        if not (0 <= self.wins <= self.games):
            raise DomainValidationError("Wins must be between 0 and games played.")

    @property
    def losses(self) -> int:
        return self.games - self.wins

    @property
    def win_rate(self) -> Fraction:
        if self.games == 0:
            return Fraction(0)
        return Fraction(self.wins, self.games)


@dataclass(frozen=True, slots=True)
class RankedPlayer:
    """One player's position on a leaderboard, after ranking and eligibility."""

    user_id: int
    games: int
    wins: int
    win_rate: Fraction
    rank: int
    eligible: bool


def _sort_key(stats: PlayerStats, *, min_games: int) -> tuple[bool, Fraction, int, int, int]:
    eligible = stats.games >= min_games
    # `user_id` only breaks ties for a deterministic display order; it plays
    # no role in eligibility, win rate, wins, or games.
    return (not eligible, -stats.win_rate, -stats.wins, -stats.games, stats.user_id)


def rank_players(stats: Iterable[PlayerStats], *, min_games: int) -> list[RankedPlayer]:
    """Rank players by (win rate desc, wins desc, games desc), eligible first.

    Uses standard competition ranking (1, 1, 3, ...) over the whole ordered
    list. A tie shares a rank only when win rate, wins, and games are all
    equal, which -- since eligibility is a deterministic function of
    `games` -- can never span the eligible/ineligible boundary: the first
    ineligible player's rank is always at least the last eligible rank + 1.
    """
    validate_min_games(min_games)
    stats_list = list(stats)

    ids = [s.user_id for s in stats_list]
    if len(set(ids)) != len(ids):
        raise DomainValidationError("The same player can't appear twice in the standings.")

    ordered = sorted(stats_list, key=lambda s: _sort_key(s, min_games=min_games))

    ranked: list[RankedPlayer] = []
    previous_tie_key: tuple[bool, Fraction, int, int] | None = None
    current_rank = 0
    for position, s in enumerate(ordered, start=1):
        eligible = s.games >= min_games
        tie_key = (eligible, s.win_rate, s.wins, s.games)
        if tie_key != previous_tie_key:
            current_rank = position
        ranked.append(
            RankedPlayer(
                user_id=s.user_id,
                games=s.games,
                wins=s.wins,
                win_rate=s.win_rate,
                rank=current_rank,
                eligible=eligible,
            )
        )
        previous_tie_key = tie_key
    return ranked
