from __future__ import annotations

import pytest

from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.scoring import (
    GameRules,
    GameType,
    PlayerScore,
    ScoreEntry,
    build_player_score,
    build_rules,
    entry_fields,
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


# ---------------------------------------------------------------------------
# entry_fields
# ---------------------------------------------------------------------------

_ALL_GAME_TYPES: tuple[GameType, ...] = (
    "normal",
    "seafarers",
    "cities_knights",
    "seafarers_cities_knights",
)


@pytest.mark.parametrize("game_type", _ALL_GAME_TYPES)
@pytest.mark.parametrize("with_scenario", [False, True])
def test_entry_fields_splits_numeric_and_award_sources(
    game_type: GameType, with_scenario: bool
) -> None:
    rules = build_rules(
        game_type,
        scenario="Fog Islands" if with_scenario else None,
        target_points=12,
    )
    numeric, awards = entry_fields(rules)

    all_sources = score_sources(rules)
    assert {source.key for source in numeric} | {source.key for source in awards} == {
        source.key for source in all_sources
    }
    assert all(source.fixed_points is None for source in numeric)
    assert all(source.fixed_points is not None for source in awards)


@pytest.mark.parametrize("game_type", _ALL_GAME_TYPES)
@pytest.mark.parametrize("with_scenario", [False, True])
def test_entry_fields_numeric_and_award_counts_fit_ui_limits(
    game_type: GameType, with_scenario: bool
) -> None:
    """A Discord modal holds at most 5 text inputs, and awards are shown some
    other way (buttons/select) with headroom to spare -- verified by
    inspection across every game type, with and without a scenario, that
    numeric never exceeds 5 and awards never exceed 4."""
    rules = build_rules(
        game_type,
        scenario="Fog Islands" if with_scenario else None,
        target_points=12,
    )
    numeric, awards = entry_fields(rules)
    assert len(numeric) <= 5
    assert len(awards) <= 4


def test_entry_fields_rejects_non_game_rules() -> None:
    with pytest.raises(DomainValidationError, match="Game rules are required"):
        entry_fields("normal")  # type: ignore[arg-type]


def test_entry_fields_overflow_is_a_design_invariant_not_domain_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A catalog that broke the <=5-numeric-field invariant must fail loudly
    as a plain `RuntimeError` (a bug in the catalog itself), never dressed up
    as a `DomainValidationError` a cog might show back to a player."""
    import catan_bot.domain.scoring as scoring_module

    rules = build_rules("normal")

    def _six_numeric_sources(_rules: object, *, scenario: str | None = None) -> tuple[object, ...]:
        return tuple(
            scoring_module._source(f"numeric_{i}", f"Numeric {i}", "help", "board")
            for i in range(6)
        )

    monkeypatch.setattr(scoring_module, "score_sources", _six_numeric_sources)

    with pytest.raises(RuntimeError, match="single modal") as excinfo:
        entry_fields(rules)
    assert not isinstance(excinfo.value, DomainValidationError)


# ---------------------------------------------------------------------------
# build_player_score
# ---------------------------------------------------------------------------


def test_build_player_score_computes_total_and_zero_fills_unclaimed_awards() -> None:
    rules = build_rules("normal")
    score = build_player_score(
        rules,
        1,
        numeric={"settlements": 4, "cities": 4, "vp_cards": 1},
        awards=["longest_road"],
    )
    assert score.user_id == 1
    assert score.total_points == 11
    breakdown = {entry.key: entry.points for entry in score.breakdown}
    assert breakdown == {
        "settlements": 4,
        "cities": 4,
        "longest_road": 2,
        "largest_army": 0,
        "vp_cards": 1,
    }
    # Catalog order, not numeric-then-awards insertion order.
    assert [entry.key for entry in score.breakdown] == [
        source.key for source in score_sources(rules)
    ]


def test_build_player_score_accepts_no_claimed_awards() -> None:
    rules = build_rules("normal")
    score = build_player_score(
        rules, 1, numeric={"settlements": 8, "cities": 0, "vp_cards": 0}, awards=()
    )
    assert score.total_points == 8
    assert all(
        entry.points == 0
        for entry in score.breakdown
        if entry.key in ("longest_road", "largest_army")
    )


def test_build_player_score_rejects_unknown_numeric_key() -> None:
    rules = build_rules("normal")
    with pytest.raises(DomainValidationError, match="Unknown numeric"):
        build_player_score(
            rules,
            1,
            numeric={"settlements": 4, "cities": 4, "vp_cards": 0, "bogus": 1},
            awards=(),
        )


def test_build_player_score_rejects_missing_numeric_key() -> None:
    rules = build_rules("normal")
    with pytest.raises(DomainValidationError, match="Missing numeric"):
        build_player_score(rules, 1, numeric={"settlements": 4, "cities": 4}, awards=())


def test_build_player_score_rejects_unknown_award() -> None:
    rules = build_rules("normal")
    with pytest.raises(DomainValidationError, match="Unknown award"):
        build_player_score(
            rules,
            1,
            numeric={"settlements": 4, "cities": 4, "vp_cards": 0},
            awards=["merchant"],
        )


def test_build_player_score_rejects_duplicate_claimed_award() -> None:
    rules = build_rules("normal")
    with pytest.raises(DomainValidationError, match="same award"):
        build_player_score(
            rules,
            1,
            numeric={"settlements": 4, "cities": 4, "vp_cards": 0},
            awards=["longest_road", "longest_road"],
        )


def test_build_player_score_still_enforces_parity_and_other_rules() -> None:
    rules = build_rules("normal")
    with pytest.raises(DomainValidationError, match="even"):
        build_player_score(
            rules, 1, numeric={"settlements": 1, "cities": 1, "vp_cards": 0}, awards=()
        )


def test_build_player_score_rejects_non_mapping_numeric() -> None:
    rules = build_rules("normal")
    with pytest.raises(DomainValidationError, match="mapping"):
        build_player_score(rules, 1, numeric=["settlements"], awards=())  # type: ignore[arg-type]


def test_build_player_score_rejects_non_game_rules() -> None:
    with pytest.raises(DomainValidationError, match="Game rules are required"):
        build_player_score("normal", 1, numeric={}, awards=())  # type: ignore[arg-type]


def test_build_player_score_cities_knights_awards_use_fixed_points() -> None:
    rules = build_rules("cities_knights")
    score = build_player_score(
        rules,
        2,
        numeric={"settlements": 8, "cities": 2, "metropolis_bonus": 2, "defender_of_catan": 3},
        awards=["merchant", "constitution"],
    )
    breakdown = {entry.key: entry.points for entry in score.breakdown}
    assert breakdown["merchant"] == 1
    assert breakdown["constitution"] == 1
    assert breakdown["printer"] == 0
    assert breakdown["longest_road"] == 0
    assert score.total_points == 8 + 2 + 2 + 3 + 1 + 1


# ---------------------------------------------------------------------------
# validate_game_scores(allow_partial=True)
# ---------------------------------------------------------------------------


def test_allow_partial_accepts_zero_rows_and_a_subset() -> None:
    rules = build_rules("normal")
    assert validate_game_scores(rules, None, [1, 2], winner_id=1, allow_partial=True) == ()
    assert validate_game_scores(rules, [], [1, 2], winner_id=1, allow_partial=True) == ()

    only_loser = row(2, rules, settlements=8)
    result = validate_game_scores(rules, [only_loser], [1, 2], winner_id=1, allow_partial=True)
    assert result == (only_loser,)


def test_allow_partial_still_rejects_duplicate_or_nonparticipant_rows() -> None:
    rules = build_rules("normal")
    dup = row(1, rules, settlements=8)
    with pytest.raises(DomainValidationError, match="more than one score"):
        validate_game_scores(rules, [dup, dup], [1, 2], winner_id=1, allow_partial=True)

    stranger = row(3, rules, settlements=8)
    with pytest.raises(DomainValidationError, match="one of this game's participants"):
        validate_game_scores(rules, [stranger], [1, 2], winner_id=1, allow_partial=True)


def test_allow_partial_skips_winner_target_check_until_winners_row_present() -> None:
    rules = build_rules("normal")
    # Loser's row is present and short of target; fine, since the winner's
    # row (the one the target rule cares about) hasn't arrived yet.
    short_loser = row(2, rules, settlements=1)
    assert validate_game_scores(rules, [short_loser], [1, 2], winner_id=1, allow_partial=True) == (
        short_loser,
    )

    # Once the winner's own row is present and short, the rule bites.
    short_winner = row(1, rules, settlements=1)
    with pytest.raises(DomainValidationError, match="winner must reach"):
        validate_game_scores(rules, [short_winner], [1, 2], winner_id=1, allow_partial=True)


def test_allow_partial_still_enforces_exclusive_award_totals_across_present_rows() -> None:
    rules = build_rules("normal")
    first = row(1, rules, settlements=8, longest_road=2)
    duplicate_award = row(2, rules, settlements=8, longest_road=2)
    with pytest.raises(DomainValidationError, match="Only one"):
        validate_game_scores(
            rules, [first, duplicate_award], [1, 2], winner_id=1, allow_partial=True
        )


def test_allow_partial_false_default_matches_original_behavior_and_messages() -> None:
    rules = build_rules("normal")
    with pytest.raises(DomainValidationError, match="exactly one"):
        validate_game_scores(rules, [row(1, rules, settlements=10)], [1, 2], winner_id=1)
    # Identical call with the new kwarg explicitly False must behave the same.
    with pytest.raises(DomainValidationError, match="exactly one"):
        validate_game_scores(
            rules, [row(1, rules, settlements=10)], [1, 2], winner_id=1, allow_partial=False
        )


# ---------------------------------------------------------------------------
# validate_game_scores(enforce_winner_target=...)
# ---------------------------------------------------------------------------


def test_enforce_winner_target_defaults_true_so_no_existing_caller_changes_behavior() -> None:
    """Every call site that doesn't pass the new kwarg explicitly must see
    byte-for-byte the same outcome as before the kwarg existed."""
    rules = build_rules("normal")
    short_winner = row(1, rules, settlements=9)
    with pytest.raises(DomainValidationError, match="winner must reach"):
        validate_game_scores(rules, [short_winner], [1, 2], winner_id=1, allow_partial=True)
    # Explicitly passing True is identical to omitting the kwarg.
    with pytest.raises(DomainValidationError, match="winner must reach"):
        validate_game_scores(
            rules,
            [short_winner],
            [1, 2],
            winner_id=1,
            allow_partial=True,
            enforce_winner_target=True,
        )
    # Also still fires for a full, non-partial sheet.
    with pytest.raises(DomainValidationError, match="winner must reach"):
        validate_game_scores(
            rules, [short_winner, row(2, rules, settlements=9)], [1, 2], winner_id=1
        )


def test_enforce_winner_target_false_skips_only_that_one_check() -> None:
    """`enforce_winner_target=False` lets a below-target winner row save --
    the deadlock this exists to break -- while every other rule (per-row
    validation, exclusive-award limits, duplicate/nonparticipant rows) still
    applies exactly as with the flag on."""
    rules = build_rules("normal")
    short_winner = row(1, rules, settlements=8)
    result = validate_game_scores(
        rules, [short_winner], [1, 2], winner_id=1, allow_partial=True, enforce_winner_target=False
    )
    assert result == (short_winner,)

    # Exclusive-award totals are still enforced.
    first = row(1, rules, settlements=8, longest_road=2)
    duplicate_award = row(2, rules, settlements=8, longest_road=2)
    with pytest.raises(DomainValidationError, match="Only one"):
        validate_game_scores(
            rules,
            [first, duplicate_award],
            [1, 2],
            winner_id=1,
            allow_partial=True,
            enforce_winner_target=False,
        )

    # Duplicate/nonparticipant rows are still rejected.
    dup = row(1, rules, settlements=8)
    with pytest.raises(DomainValidationError, match="more than one score"):
        validate_game_scores(
            rules, [dup, dup], [1, 2], winner_id=1, allow_partial=True, enforce_winner_target=False
        )

    # Per-row validation (e.g. odd city count) still fires.
    with pytest.raises(DomainValidationError, match="even"):
        validate_game_scores(
            rules,
            [row(1, rules, settlements=8, cities=1)],
            [1, 2],
            winner_id=1,
            allow_partial=True,
            enforce_winner_target=False,
        )


def test_enforce_winner_target_false_then_true_models_claiming_an_award_next() -> None:
    """The exact deadlock this flag exists to break: a winner's numeric-only
    row is below target and saves fine with the check off (the per-row save
    path); a second save that adds a claimed award reaches target, and would
    also have passed with the check back on."""
    rules = build_rules("normal")
    numeric_only = row(1, rules, settlements=8)  # 8 < target of 10
    validate_game_scores(
        rules, [numeric_only], [1, 2], winner_id=1, allow_partial=True, enforce_winner_target=False
    )

    with_award = row(1, rules, settlements=8, longest_road=2)  # 10 >= target
    result = validate_game_scores(
        rules, [with_award], [1, 2], winner_id=1, allow_partial=True, enforce_winner_target=False
    )
    assert result == (with_award,)
    # Also passes with the check back on, since the winner now meets target.
    result = validate_game_scores(rules, [with_award], [1, 2], winner_id=1, allow_partial=True)
    assert result == (with_award,)
