"""Unit tests for `catan_bot.domain.bet`, including Hypothesis properties."""

from __future__ import annotations

from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from catan_bot.domain.bet import BetOutcome, outcome_for, resolve_bet
from catan_bot.domain.ranking import RankedPlayer


def _rp(user_id: int, rank: int, *, eligible: bool = True) -> RankedPlayer:
    return RankedPlayer(
        user_id=user_id, games=10, wins=5, win_rate=Fraction(1, 2), rank=rank, eligible=eligible
    )


# ---------------------------------------------------------------------------
# resolve_bet: duplicate user_id is a caller bug, not a user input problem
# ---------------------------------------------------------------------------


def test_resolve_bet_rejects_duplicate_user_id_with_value_error() -> None:
    # A duplicate here means the ranking step is broken -- that's a plain
    # ValueError (a caller bug), never a DomainValidationError.
    ranked = [_rp(1, rank=1), _rp(1, rank=2)]
    with pytest.raises(ValueError, match="duplicate"):
        resolve_bet(ranked)


def test_resolve_bet_rejects_duplicate_even_when_one_copy_is_ineligible() -> None:
    ranked = [_rp(1, rank=1), _rp(1, rank=2, eligible=False)]
    with pytest.raises(ValueError, match="duplicate"):
        resolve_bet(ranked)


# ---------------------------------------------------------------------------
# resolve_bet: fewer than 2 eligible
# ---------------------------------------------------------------------------


def test_resolve_bet_no_eligible_players() -> None:
    outcome = resolve_bet([])
    assert outcome.status == "no_bet"
    assert outcome.reason == "fewer_than_two_eligible"
    assert outcome.payees == ()
    assert outcome.payers == ()


def test_resolve_bet_exactly_one_eligible_player() -> None:
    ranked = [_rp(1, rank=1), _rp(2, rank=2, eligible=False)]
    outcome = resolve_bet(ranked)
    assert outcome.status == "no_bet"
    assert outcome.reason == "fewer_than_two_eligible"


# ---------------------------------------------------------------------------
# resolve_bet: distinct ranks
# ---------------------------------------------------------------------------


def test_resolve_bet_two_eligible_distinct_ranks() -> None:
    ranked = [_rp(1, rank=1), _rp(2, rank=2)]
    outcome = resolve_bet(ranked)
    assert outcome.status == "resolved"
    assert outcome.payees == (1,)
    assert outcome.payers == (2,)
    assert outcome.reason is None


def test_resolve_bet_does_not_assume_sorted_input() -> None:
    ranked = [_rp(2, rank=2), _rp(1, rank=1), _rp(3, rank=3)]
    outcome = resolve_bet(ranked)
    assert outcome.payees == (1,)
    assert outcome.payers == (3,)


# ---------------------------------------------------------------------------
# resolve_bet: ties
# ---------------------------------------------------------------------------


def test_resolve_bet_all_tied_is_no_bet() -> None:
    ranked = [_rp(1, rank=1), _rp(2, rank=1), _rp(3, rank=1)]
    outcome = resolve_bet(ranked)
    assert outcome.status == "no_bet"
    assert outcome.reason == "all_tied"
    assert outcome.payees == ()
    assert outcome.payers == ()


def test_resolve_bet_ties_at_top_only() -> None:
    ranked = [_rp(1, rank=1), _rp(2, rank=1), _rp(3, rank=3)]
    outcome = resolve_bet(ranked)
    assert outcome.status == "resolved"
    assert outcome.payees == (1, 2)
    assert outcome.payers == (3,)


def test_resolve_bet_ties_at_bottom_only() -> None:
    ranked = [_rp(1, rank=1), _rp(2, rank=2), _rp(3, rank=2)]
    outcome = resolve_bet(ranked)
    assert outcome.status == "resolved"
    assert outcome.payees == (1,)
    assert outcome.payers == (2, 3)


def test_resolve_bet_ties_at_both_top_and_bottom() -> None:
    ranked = [
        _rp(1, rank=1),
        _rp(2, rank=1),
        _rp(3, rank=3),
        _rp(4, rank=3),
    ]
    outcome = resolve_bet(ranked)
    assert outcome.status == "resolved"
    assert outcome.payees == (1, 2)
    assert outcome.payers == (3, 4)


def test_resolve_bet_payees_sorted_by_user_id() -> None:
    ranked = [_rp(30, rank=1), _rp(10, rank=1), _rp(20, rank=3)]
    outcome = resolve_bet(ranked)
    assert outcome.payees == (10, 30)


def test_resolve_bet_payers_sorted_by_user_id() -> None:
    ranked = [_rp(1, rank=1), _rp(30, rank=2), _rp(10, rank=2)]
    outcome = resolve_bet(ranked)
    assert outcome.payers == (10, 30)


# ---------------------------------------------------------------------------
# resolve_bet: ineligible players never pay or get paid
# ---------------------------------------------------------------------------


def test_resolve_bet_ineligible_player_never_pays_even_if_ranked_lowest() -> None:
    ranked = [
        _rp(1, rank=1),
        _rp(2, rank=2),
        _rp(3, rank=99, eligible=False),  # would be "lowest" but isn't eligible
    ]
    outcome = resolve_bet(ranked)
    assert outcome.status == "resolved"
    assert outcome.payees == (1,)
    assert outcome.payers == (2,)
    assert 3 not in outcome.payers
    assert 3 not in outcome.payees


def test_resolve_bet_ineligible_player_never_gets_paid_even_if_ranked_highest() -> None:
    ranked = [
        _rp(1, rank=0, eligible=False),  # would be "highest" but isn't eligible
        _rp(2, rank=1),
        _rp(3, rank=2),
    ]
    outcome = resolve_bet(ranked)
    assert outcome.status == "resolved"
    assert outcome.payees == (2,)
    assert outcome.payers == (3,)
    assert 1 not in outcome.payees
    assert 1 not in outcome.payers


# ---------------------------------------------------------------------------
# outcome_for
# ---------------------------------------------------------------------------


def test_outcome_for_payee() -> None:
    outcome = BetOutcome(status="resolved", payees=(1,), payers=(2,), reason=None)
    assert outcome_for(1, outcome) == "payee"


def test_outcome_for_payer() -> None:
    outcome = BetOutcome(status="resolved", payees=(1,), payers=(2,), reason=None)
    assert outcome_for(2, outcome) == "payer"


def test_outcome_for_uninvolved_player() -> None:
    outcome = BetOutcome(status="resolved", payees=(1,), payers=(2,), reason=None)
    assert outcome_for(3, outcome) is None


def test_outcome_for_no_bet() -> None:
    outcome = BetOutcome(status="no_bet", payees=(), payers=(), reason="all_tied")
    assert outcome_for(1, outcome) is None


# ---------------------------------------------------------------------------
# Hypothesis property tests
# ---------------------------------------------------------------------------

_ranked_player_strategy = st.builds(
    RankedPlayer,
    user_id=st.integers(min_value=1, max_value=1000),
    games=st.integers(min_value=0, max_value=20),
    wins=st.integers(min_value=0, max_value=20),
    win_rate=st.sampled_from([Fraction(0), Fraction(1, 2), Fraction(1, 3), Fraction(2, 3)]),
    rank=st.integers(min_value=1, max_value=10),
    eligible=st.booleans(),
)


@given(
    ranked=st.lists(_ranked_player_strategy, min_size=0, max_size=12, unique_by=lambda p: p.user_id)
)
def test_property_payers_and_payees_disjoint_when_resolved(ranked: list[RankedPlayer]) -> None:
    outcome = resolve_bet(ranked)
    if outcome.status == "resolved":
        assert set(outcome.payees).isdisjoint(outcome.payers)


@given(
    ranked=st.lists(_ranked_player_strategy, min_size=0, max_size=12, unique_by=lambda p: p.user_id)
)
def test_property_payers_and_payees_are_subsets_of_eligible(ranked: list[RankedPlayer]) -> None:
    outcome = resolve_bet(ranked)
    eligible_ids = {p.user_id for p in ranked if p.eligible}
    assert set(outcome.payees) <= eligible_ids
    assert set(outcome.payers) <= eligible_ids


@given(
    ranked=st.lists(_ranked_player_strategy, min_size=0, max_size=12, unique_by=lambda p: p.user_id)
)
def test_property_no_bet_reason_matches_eligible_count_or_all_tied(
    ranked: list[RankedPlayer],
) -> None:
    outcome = resolve_bet(ranked)
    eligible = [p for p in ranked if p.eligible]
    if len(eligible) < 2:
        assert outcome.status == "no_bet"
        assert outcome.reason == "fewer_than_two_eligible"
    elif len({p.rank for p in eligible}) == 1:
        assert outcome.status == "no_bet"
        assert outcome.reason == "all_tied"
    else:
        assert outcome.status == "resolved"
