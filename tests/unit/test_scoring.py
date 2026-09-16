from __future__ import annotations

import pytest

from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.scoring import (
    GameRules,
    PlayerScore,
    ScoreEntry,
    build_rules,
    score_pages,
    score_sources,
    validate_game_scores,
    validate_player_score,
)


def row(user_id: int, rules: GameRules, **values: int) -> PlayerScore:
    sources = score_sources(rules)
    breakdown = tuple(ScoreEntry(source.key, values.get(source.key, 0)) for source in sources)
    return PlayerScore(user_id, sum(entry.points for entry in breakdown), breakdown)


def test_source_catalogs_follow_game_type() -> None:
    normal = build_rules("normal")
    assert [source.key for source in score_sources(normal)] == [
        "settlements",
        "cities",
        "longest_road",
        "largest_army",
        "vp_cards",
    ]
    seafarers = build_rules("seafarers", target_points=12)
    assert "longest_trade_route" in [source.key for source in score_sources(seafarers)]
    assert "scenario_points" in [source.key for source in score_sources(seafarers)]
    knights = build_rules("cities_knights")
    knight_keys = [source.key for source in score_sources(knights)]
    assert "largest_army" not in knight_keys
    assert "vp_cards" not in knight_keys
    assert {"metropolis_bonus", "defender_of_catan", "merchant", "constitution", "printer"} <= set(
        knight_keys
    )


def test_rules_defaults_and_explicit_target_requirements() -> None:
    assert build_rules("normal").target_points == 10
    assert build_rules("cities_knights").target_points == 13
    with pytest.raises(DomainValidationError, match="require a target"):
        build_rules("seafarers")
    with pytest.raises(DomainValidationError, match="require a target"):
        build_rules("normal", scenario="Fog Islands")
    assert build_rules("normal", scenario="Fog Islands", target_points=12).target_points == 12


def test_five_and_six_player_games_require_extension() -> None:
    with pytest.raises(DomainValidationError, match="5 or 6"):
        build_rules("normal", player_count=5)
    rules = build_rules("normal", extension_5_6=True, player_count=5)
    scores = [row(i, rules, settlements=10) for i in range(1, 6)]
    assert validate_game_scores(rules, scores, range(1, 6), winner_id=1)


def test_player_row_requires_exact_columns_and_sum() -> None:
    rules = build_rules("normal")
    with pytest.raises(DomainValidationError, match="missing"):
        validate_player_score(PlayerScore(1, 1, (ScoreEntry("cities", 1),)), rules)
    bad = row(1, rules, settlements=1)
    bad = PlayerScore(bad.user_id, bad.total_points + 1, bad.breakdown)
    with pytest.raises(DomainValidationError, match="sum"):
        validate_player_score(bad, rules)
    bad_key = PlayerScore(
        1,
        0,
        tuple(ScoreEntry(source.key, 0) for source in score_sources(rules))[:-1]
        + (ScoreEntry("bogus", 0),),
    )
    with pytest.raises(DomainValidationError, match="isn't valid"):
        validate_player_score(bad_key, rules)


def test_even_city_and_metropolis_points_and_consistency() -> None:
    normal = build_rules("normal")
    with pytest.raises(DomainValidationError, match="even"):
        validate_player_score(row(1, normal, cities=1), normal)
    knights = build_rules("cities_knights")
    with pytest.raises(DomainValidationError, match="exceed"):
        validate_player_score(row(1, knights, cities=2, metropolis_bonus=4), knights)


def test_exclusive_awards_are_shared_across_rows() -> None:
    rules = build_rules("normal")
    first = row(1, rules, settlements=8, longest_road=2)
    second = row(2, rules, settlements=8, largest_army=2)
    assert validate_game_scores(rules, [first, second], [1, 2], winner_id=1)
    duplicate_award = row(2, rules, settlements=8, longest_road=2)
    with pytest.raises(DomainValidationError, match="Only one"):
        validate_game_scores(rules, [first, duplicate_award], [1, 2], winner_id=1)


def test_cities_knights_award_semantics() -> None:
    rules = build_rules("cities_knights")
    merchant = next(source for source in score_sources(rules) if source.key == "merchant")
    defender = next(source for source in score_sources(rules) if source.key == "defender_of_catan")
    constitution = next(source for source in score_sources(rules) if source.key == "constitution")
    assert merchant.fixed_points == 1 and merchant.exclusive
    assert defender.fixed_points is None and not defender.exclusive
    assert constitution.fixed_points == 1 and constitution.exclusive
    first = row(
        1, rules, settlements=8, cities=2, metropolis_bonus=2, merchant=1, defender_of_catan=3
    )
    second = row(2, rules, settlements=8, defender_of_catan=2)
    assert validate_game_scores(rules, [first, second], [1, 2], winner_id=1)
    with pytest.raises(DomainValidationError, match="either 0 or 1"):
        validate_player_score(row(1, rules, settlements=8, merchant=2), rules)


def test_stored_point_values_are_limited_to_schema_range() -> None:
    rules = build_rules("normal")
    with pytest.raises(DomainValidationError, match="99"):
        ScoreEntry("vp_cards", 100)
    with pytest.raises(DomainValidationError, match="99"):
        PlayerScore(1, 100, tuple(ScoreEntry(source.key, 0) for source in score_sources(rules)))
    with pytest.raises(DomainValidationError, match="99"):
        build_rules("normal", target_points=100)
    # IDs use BIGINT semantics independently from point columns.
    assert ScoreEntry("vp_cards", 0).points == 0
    assert row(2**63 - 1, rules).user_id == 2**63 - 1


def test_winner_reaches_target_but_loser_may_also_reach_it() -> None:
    rules = build_rules("normal")
    scores = [row(1, rules, settlements=10), row(2, rules, settlements=10)]
    assert validate_game_scores(rules, scores, participants=[1, 2], winner_id=1) == tuple(scores)
    with pytest.raises(DomainValidationError, match="winner must reach"):
        validate_game_scores(
            rules, [row(1, rules, settlements=9), row(2, rules, settlements=9)], [1, 2], winner_id=1
        )


def test_absent_score_sheet_is_valid_and_partial_sheet_is_not() -> None:
    rules = build_rules("normal")
    assert validate_game_scores(rules, None, participants=[1, 2]) == ()
    assert validate_game_scores(rules, [], participants=[1, 2], winner_id=999) == ()
    with pytest.raises(DomainValidationError, match="exactly one"):
        validate_game_scores(rules, [row(1, rules, settlements=10)], [1, 2], winner_id=1)


def test_pages_are_stable_and_score_entries_are_immutable() -> None:
    rules = build_rules("cities_knights")
    pages = score_pages(rules, columns_per_page=3)
    assert [source.key for source in pages[0]] == ["settlements", "cities", "longest_road"]
    with pytest.raises((AttributeError, TypeError)):
        pages[0][0].key = "other"  # type: ignore[misc]
