"""Pure bet resolution: who buys food for whom at season end.

Only eligible players ever pay or get paid. Ties at the top are all fed;
ties at the bottom all split the bill; if every eligible player is tied
with every other, there's no meaningful top/bottom, so it's no bet.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from catan_bot.domain.ranking import RankedPlayer

BetStatus = Literal["resolved", "no_bet"]
NoBetReason = Literal["fewer_than_two_eligible", "all_tied"]


@dataclass(frozen=True, slots=True)
class BetOutcome:
    status: BetStatus
    payees: tuple[int, ...]
    payers: tuple[int, ...]
    reason: NoBetReason | None


def resolve_bet(ranked: Sequence[RankedPlayer]) -> BetOutcome:
    """Resolve a season's standings into a bet outcome.

    Recomputes the top/bottom rank from `ranked` rather than assuming it's
    already sorted or that the first/last entries are the extremes.

    A duplicate `user_id` in `ranked` is a caller bug (the ranking step
    should never produce one), not a user input problem, so it's a plain
    `ValueError` rather than a `DomainValidationError`.
    """
    ids = [p.user_id for p in ranked]
    if len(set(ids)) != len(ids):
        raise ValueError("ranked contains duplicate user_id entries.")

    eligible = [p for p in ranked if p.eligible]
    if len(eligible) < 2:
        return BetOutcome(status="no_bet", payees=(), payers=(), reason="fewer_than_two_eligible")

    best_rank = min(p.rank for p in eligible)
    worst_rank = max(p.rank for p in eligible)
    top_group = frozenset(p.user_id for p in eligible if p.rank == best_rank)
    bottom_group = frozenset(p.user_id for p in eligible if p.rank == worst_rank)

    if top_group == bottom_group:
        return BetOutcome(status="no_bet", payees=(), payers=(), reason="all_tied")

    return BetOutcome(
        status="resolved",
        payees=tuple(sorted(top_group)),
        payers=tuple(sorted(bottom_group)),
        reason=None,
    )


def outcome_for(user_id: int, outcome: BetOutcome) -> Literal["payer", "payee"] | None:
    """The `season_results.outcome` value for one player, or `None` if uninvolved."""
    if user_id in outcome.payees:
        return "payee"
    if user_id in outcome.payers:
        return "payer"
    return None
