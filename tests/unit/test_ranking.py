"""Unit tests for `catan_bot.domain.ranking`, including Hypothesis properties."""

from __future__ import annotations

from enum import IntEnum
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.ranking import PlayerStats, RankedPlayer, rank_players


class _IdEnum(IntEnum):
    PLAYER = 3  # an otherwise-valid positive id, as a non-plain-int type


# ---------------------------------------------------------------------------
# PlayerStats
# ---------------------------------------------------------------------------


def test_player_stats_losses() -> None:
    stats = PlayerStats(user_id=1, games=5, wins=2)
    assert stats.losses == 3


def test_player_stats_win_rate_is_exact_fraction() -> None:
    stats = PlayerStats(user_id=1, games=3, wins=1)
    assert stats.win_rate == Fraction(1, 3)


def test_player_stats_win_rate_zero_games_is_zero() -> None:
    stats = PlayerStats(user_id=1, games=0, wins=0)
    assert stats.win_rate == Fraction(0)


def test_player_stats_equivalent_fractions_compare_equal() -> None:
    a = PlayerStats(user_id=1, games=3, wins=1)
    b = PlayerStats(user_id=2, games=6, wins=2)
    assert a.win_rate == b.win_rate == Fraction(1, 3)


@pytest.mark.parametrize(
    ("user_id", "games", "wins"),
    [
        pytest.param(0, 1, 0, id="user-id-zero"),
        pytest.param(-1, 1, 0, id="user-id-negative"),
        pytest.param(1, -1, 0, id="games-negative"),
        pytest.param(1, 2, 3, id="wins-exceeds-games"),
        pytest.param(1, 2, -1, id="wins-negative"),
    ],
)
def test_player_stats_rejects_invalid_values(user_id: int, games: int, wins: int) -> None:
    with pytest.raises(DomainValidationError):
        PlayerStats(user_id=user_id, games=games, wins=wins)


def test_player_stats_rejects_bool_user_id() -> None:
    with pytest.raises(DomainValidationError):
        PlayerStats(user_id=True, games=1, wins=0)  # type: ignore[arg-type]


def test_player_stats_rejects_int_enum_user_id() -> None:
    # `type(x) is int` rejects an IntEnum even at an otherwise-valid,
    # in-range positive value.
    with pytest.raises(DomainValidationError):
        PlayerStats(user_id=_IdEnum.PLAYER, games=1, wins=0)  # type: ignore[arg-type]


def test_player_stats_rejects_non_int_games() -> None:
    with pytest.raises(DomainValidationError):
        PlayerStats(user_id=1, games=2.0, wins=0)  # type: ignore[arg-type]


def test_player_stats_rejects_non_int_wins() -> None:
    with pytest.raises(DomainValidationError):
        PlayerStats(user_id=1, games=2, wins=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# rank_players: basic ordering and eligibility
# ---------------------------------------------------------------------------


def test_rank_players_empty_input() -> None:
    assert rank_players([], min_games=2) == []


def test_rank_players_orders_by_win_rate_desc() -> None:
    stats = [
        PlayerStats(user_id=1, games=4, wins=1),  # 0.25
        PlayerStats(user_id=2, games=4, wins=3),  # 0.75
        PlayerStats(user_id=3, games=4, wins=2),  # 0.5
    ]
    ranked = rank_players(stats, min_games=2)
    assert [p.user_id for p in ranked] == [2, 3, 1]
    assert [p.rank for p in ranked] == [1, 2, 3]


def test_rank_players_default_min_games_two_makes_single_game_player_ineligible() -> None:
    stats = [
        PlayerStats(user_id=1, games=1, wins=1),  # 100% but only 1 game
        PlayerStats(user_id=2, games=4, wins=2),  # 50% over 4 games
    ]
    ranked = rank_players(stats, min_games=2)
    by_id = {p.user_id: p for p in ranked}
    assert by_id[1].eligible is False
    assert by_id[2].eligible is True
    # Eligible players are listed first regardless of win rate.
    assert [p.user_id for p in ranked] == [2, 1]


def test_rank_players_eligible_group_precedes_ineligible_even_with_lower_win_rate() -> None:
    stats = [
        PlayerStats(user_id=1, games=1, wins=1),  # ineligible, 100%
        PlayerStats(user_id=2, games=10, wins=1),  # eligible, 10%
    ]
    ranked = rank_players(stats, min_games=2)
    assert [p.user_id for p in ranked] == [2, 1]
    assert ranked[0].eligible is True
    assert ranked[1].eligible is False


def test_rank_players_tiebreak_by_wins_when_win_rate_equal_but_wins_differ() -> None:
    # 1/3 vs 2/6: equal win rate, but wins differ (1 vs 2), so NOT a full tie.
    stats = [
        PlayerStats(user_id=1, games=3, wins=1),
        PlayerStats(user_id=2, games=6, wins=2),
    ]
    ranked = rank_players(stats, min_games=2)
    assert ranked[0].win_rate == ranked[1].win_rate == Fraction(1, 3)
    # More wins ranks higher despite the identical win rate.
    assert [p.user_id for p in ranked] == [2, 1]
    assert ranked[0].rank == 1
    assert ranked[1].rank == 2


def test_rank_players_full_tie_shares_rank_and_next_rank_skips() -> None:
    stats = [
        PlayerStats(user_id=1, games=4, wins=2),
        PlayerStats(user_id=2, games=4, wins=2),
        PlayerStats(user_id=3, games=4, wins=1),
    ]
    ranked = rank_players(stats, min_games=2)
    by_id = {p.user_id: p for p in ranked}
    assert by_id[1].rank == 1
    assert by_id[2].rank == 1
    assert by_id[3].rank == 3  # skips rank 2


def test_rank_players_user_id_is_deterministic_tiebreak_only() -> None:
    stats = [
        PlayerStats(user_id=9, games=4, wins=2),
        PlayerStats(user_id=3, games=4, wins=2),
    ]
    ranked = rank_players(stats, min_games=2)
    # Fully tied on rate/wins/games: lower user_id sorts first for display,
    # but both still share rank 1.
    assert [p.user_id for p in ranked] == [3, 9]
    assert ranked[0].rank == ranked[1].rank == 1


def test_rank_players_eligible_ineligible_boundary_never_shares_a_rank() -> None:
    stats = [
        PlayerStats(user_id=1, games=4, wins=2),
        PlayerStats(user_id=2, games=4, wins=2),
        PlayerStats(user_id=3, games=1, wins=1),
    ]
    ranked = rank_players(stats, min_games=2)
    eligible_ranks = [p.rank for p in ranked if p.eligible]
    ineligible_ranks = [p.rank for p in ranked if not p.eligible]
    assert max(eligible_ranks) < min(ineligible_ranks)


def test_rank_players_rejects_duplicate_user_ids() -> None:
    stats = [
        PlayerStats(user_id=1, games=4, wins=2),
        PlayerStats(user_id=1, games=3, wins=1),
    ]
    with pytest.raises(DomainValidationError):
        rank_players(stats, min_games=2)


def test_rank_players_rejects_invalid_min_games() -> None:
    stats = [PlayerStats(user_id=1, games=4, wins=2)]
    with pytest.raises(DomainValidationError):
        rank_players(stats, min_games=0)


def test_rank_players_rejects_min_games_over_100() -> None:
    stats = [PlayerStats(user_id=1, games=4, wins=2)]
    with pytest.raises(DomainValidationError):
        rank_players(stats, min_games=101)


def test_rank_players_kills_games_ascending_mutant() -> None:
    # Equal win_rate (0) and equal wins (0), but different games. Per the
    # spec's tiebreak order (win rate desc, wins desc, games desc), MORE
    # games ranks *first*. A mutant that flipped the games tiebreak to
    # ascending order would rank these the other way around.
    stats = [
        PlayerStats(user_id=1, games=3, wins=0),
        PlayerStats(user_id=2, games=5, wins=0),
    ]
    ranked = rank_players(stats, min_games=1)
    assert [p.user_id for p in ranked] == [2, 1]
    assert ranked[0].rank == 1
    assert ranked[1].rank == 2


def test_rank_players_kills_ties_ignore_games_mutant() -> None:
    # Same setup as the mutant above, phrased as a "do these share a rank"
    # check: a mutant that dropped `games` from the tie-key (while still
    # using it for *ordering*) would wrongly give both of these rank 1.
    stats = [
        PlayerStats(user_id=1, games=3, wins=0),
        PlayerStats(user_id=2, games=5, wins=0),
    ]
    ranked = rank_players(stats, min_games=1)
    ranks = {p.user_id: p.rank for p in ranked}
    assert ranks[1] != ranks[2]


def test_rank_players_all_ineligible_still_ranked_among_themselves() -> None:
    stats = [
        PlayerStats(user_id=1, games=1, wins=1),
        PlayerStats(user_id=2, games=1, wins=0),
    ]
    ranked = rank_players(stats, min_games=5)
    assert all(not p.eligible for p in ranked)
    assert [p.user_id for p in ranked] == [1, 2]
    assert [p.rank for p in ranked] == [1, 2]


# ---------------------------------------------------------------------------
# Hypothesis property tests
# ---------------------------------------------------------------------------


@st.composite
def _player_stats(draw: st.DrawFn) -> PlayerStats:
    # Draw `wins` dependent on `games` so every generated value is valid
    # *before* construction -- PlayerStats.__post_init__ raises immediately
    # on an invalid combination, which a post-hoc `.filter()` can't rescue.
    user_id = draw(st.integers(min_value=1, max_value=10_000))
    games = draw(st.integers(min_value=0, max_value=50))
    wins = draw(st.integers(min_value=0, max_value=games))
    return PlayerStats(user_id=user_id, games=games, wins=wins)


_player_stats_strategy = _player_stats()


def _distinct_by_user_id(stats_list: list[PlayerStats]) -> list[PlayerStats]:
    seen: set[int] = set()
    out: list[PlayerStats] = []
    for s in stats_list:
        if s.user_id not in seen:
            seen.add(s.user_id)
            out.append(s)
    return out


@given(
    stats_list=st.lists(_player_stats_strategy, min_size=0, max_size=15),
    min_games=st.integers(min_value=1, max_value=10),
)
def test_property_result_is_a_permutation_of_the_input(
    stats_list: list[PlayerStats], min_games: int
) -> None:
    stats_list = _distinct_by_user_id(stats_list)
    ranked = rank_players(stats_list, min_games=min_games)
    assert {p.user_id for p in ranked} == {s.user_id for s in stats_list}
    assert len(ranked) == len(stats_list)


@given(
    stats_list=st.lists(_player_stats_strategy, min_size=0, max_size=15),
    min_games=st.integers(min_value=1, max_value=10),
)
def test_property_ranks_are_non_decreasing_along_the_list(
    stats_list: list[PlayerStats], min_games: int
) -> None:
    stats_list = _distinct_by_user_id(stats_list)
    ranked = rank_players(stats_list, min_games=min_games)
    ranks = [p.rank for p in ranked]
    assert ranks == sorted(ranks)


@given(
    stats_list=st.lists(_player_stats_strategy, min_size=0, max_size=15),
    min_games=st.integers(min_value=1, max_value=10),
)
def test_property_equal_tuple_and_eligibility_implies_equal_rank(
    stats_list: list[PlayerStats], min_games: int
) -> None:
    stats_list = _distinct_by_user_id(stats_list)
    ranked = rank_players(stats_list, min_games=min_games)
    by_key: dict[tuple[bool, Fraction, int, int], set[int]] = {}
    for p in ranked:
        key = (p.eligible, p.win_rate, p.wins, p.games)
        by_key.setdefault(key, set()).add(p.rank)
    for ranks_for_key in by_key.values():
        assert len(ranks_for_key) == 1


@st.composite
def _stats_list_with_a_permutation(
    draw: st.DrawFn,
) -> tuple[list[PlayerStats], list[PlayerStats]]:
    """A list plus one of its permutations, drawn without `random` (S311)."""
    base = draw(
        st.lists(_player_stats_strategy, min_size=0, max_size=15, unique_by=lambda s: s.user_id)
    )
    shuffled = draw(st.permutations(base))
    return base, list(shuffled)


@given(
    lists=_stats_list_with_a_permutation(),
    min_games=st.integers(min_value=1, max_value=10),
)
def test_property_output_independent_of_input_order(
    lists: tuple[list[PlayerStats], list[PlayerStats]], min_games: int
) -> None:
    stats_list, shuffled = lists
    assert rank_players(stats_list, min_games=min_games) == rank_players(
        shuffled, min_games=min_games
    )


def test_ranked_player_is_frozen_dataclass() -> None:
    player = RankedPlayer(
        user_id=1, games=2, wins=1, win_rate=Fraction(1, 2), rank=1, eligible=True
    )
    with pytest.raises(AttributeError):
        player.rank = 2  # type: ignore[misc]
