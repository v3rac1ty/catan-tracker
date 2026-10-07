"""Hand-calculated tests for the pure analytics engine."""

from __future__ import annotations

from datetime import date, datetime
from fractions import Fraction
from time import perf_counter
from zoneinfo import ZoneInfo

from catan_bot.domain.analytics import (
    GAME_TYPE_ORDER,
    TIME_OF_DAY_BUCKETS,
    AwardStat,
    HeadToHead,
    MatchupHighlights,
    RecordSplit,
    games_by_type,
    head_to_head,
    matchup_highlights,
    meta_summary,
    most_played_game_type,
    player_summaries,
    player_summary,
    win_rate_by_season,
    win_rate_timeline,
)
from catan_bot.domain.participation import ParticipationRecord


def row(
    game_id: int,
    day: date,
    user_id: int,
    *,
    game_type: str = "normal",
    winner: bool = False,
    points: int | None = 0,
    breakdown: dict[str, int] | None = None,
    players: int = 2,
    target: int | None = 10,
    played_at: datetime | None = None,
    played_timezone: str | None = None,
    season_id: int | None = None,
) -> ParticipationRecord:
    if points is None:
        breakdown = None
    elif breakdown is None:
        breakdown = {"settlements": points}
    return ParticipationRecord(
        game_id,
        day,
        played_at,
        game_type,
        False,
        target,
        players,
        user_id,
        winner,
        points,
        breakdown,
        season_id,
        played_timezone,
    )


def sample() -> list[ParticipationRecord]:
    return [
        row(
            1,
            date(2025, 1, 6),
            1,
            winner=True,
            points=10,
            breakdown={
                "settlements": 4,
                "cities": 2,
                "longest_road": 2,
                "largest_army": 2,
                "vp_cards": 0,
            },
        ),
        row(
            1,
            date(2025, 1, 6),
            2,
            points=8,
            breakdown={
                "settlements": 4,
                "cities": 2,
                "longest_road": 0,
                "largest_army": 0,
                "vp_cards": 2,
            },
        ),
        row(
            2,
            date(2025, 1, 7),
            1,
            game_type="seafarers",
            points=7,
            players=3,
            breakdown={
                "settlements": 3,
                "cities": 2,
                "longest_trade_route": 0,
                "largest_army": 0,
                "vp_cards": 2,
                "scenario_points": 0,
            },
        ),
        row(
            2,
            date(2025, 1, 7),
            2,
            game_type="seafarers",
            winner=True,
            points=9,
            players=3,
            breakdown={
                "settlements": 5,
                "cities": 2,
                "longest_trade_route": 2,
                "largest_army": 0,
                "vp_cards": 0,
                "scenario_points": 0,
            },
        ),
        row(2, date(2025, 1, 7), 3, game_type="seafarers", points=None, players=3),
        row(
            3,
            date(2025, 2, 3),
            1,
            game_type="cities_knights",
            winner=True,
            points=15,
            breakdown={
                "settlements": 3,
                "cities": 4,
                "longest_road": 2,
                "metropolis_bonus": 2,
                "defender_of_catan": 1,
                "merchant": 1,
                "constitution": 1,
                "printer": 1,
            },
        ),
        row(
            3,
            date(2025, 2, 3),
            2,
            game_type="cities_knights",
            points=14,
            breakdown={
                "settlements": 4,
                "cities": 4,
                "longest_road": 2,
                "metropolis_bonus": 2,
                "defender_of_catan": 1,
                "merchant": 1,
            },
        ),
        row(
            4,
            date(2025, 2, 4),
            1,
            winner=True,
            points=12,
            breakdown={
                "settlements": 4,
                "cities": 2,
                "longest_road": 2,
                "largest_army": 0,
                "vp_cards": 4,
            },
        ),
        row(
            4,
            date(2025, 2, 4),
            2,
            points=10,
            breakdown={
                "settlements": 4,
                "cities": 2,
                "longest_road": 0,
                "largest_army": 0,
                "vp_cards": 4,
            },
        ),
        row(
            4,
            date(2025, 2, 4),
            3,
            points=10,
            breakdown={
                "settlements": 4,
                "cities": 2,
                "longest_road": 0,
                "largest_army": 0,
                "vp_cards": 4,
            },
        ),
        row(
            5,
            date(2025, 3, 2),
            1,
            points=10,
            breakdown={
                "settlements": 4,
                "cities": 2,
                "longest_road": 2,
                "largest_army": 0,
                "vp_cards": 2,
            },
        ),
        row(
            5,
            date(2025, 3, 2),
            2,
            winner=True,
            points=12,
            breakdown={
                "settlements": 4,
                "cities": 2,
                "longest_road": 0,
                "largest_army": 2,
                "vp_cards": 4,
            },
        ),
    ]


def test_player_summary_scores_margins_awards_and_streaks() -> None:
    summary = player_summary(sample(), 1)
    assert (summary.games, summary.wins, summary.win_rate, summary.scored_games) == (
        5,
        3,
        Fraction(3, 5),
        5,
    )
    assert summary.avg_points == Fraction(54, 5)
    assert summary.avg_points_in_wins == Fraction(37, 3)
    assert summary.avg_points_in_losses == Fraction(17, 2)
    assert summary.scored_wins == 3
    assert summary.scored_losses == 2
    assert summary.median_points == 10
    assert summary.best_points == 15
    assert summary.avg_target_share == Fraction(27, 25)
    assert summary.avg_settlements == Fraction(18, 5)
    assert summary.avg_cities == Fraction(6, 5)
    assert summary.avg_vp_cards == 2
    assert summary.avg_metropolises == 1
    assert summary.source_averages["settlements"] == Fraction(18, 5)
    assert summary.source_samples == {
        "cities": 5,
        "constitution": 1,
        "defender_of_catan": 1,
        "largest_army": 4,
        "longest_road": 4,
        "longest_trade_route": 1,
        "merchant": 1,
        "metropolis_bonus": 1,
        "printer": 1,
        "scenario_points": 1,
        "settlements": 5,
        "vp_cards": 4,
    }
    assert summary.target_share_samples == 5
    assert summary.avg_win_margin == Fraction(5, 3)  # (10-8), (15-14), (12-max(10,10))
    assert summary.avg_loss_deficit == 2
    assert summary.close_losses == 2
    assert summary.win_margin_samples == 3
    assert summary.loss_deficit_samples == 2
    assert summary.current_streak == -1
    assert summary.longest_win_streak == 2
    assert summary.recent_form == RecordSplit(5, 3, Fraction(3, 5))
    assert summary.by_player_count == {
        2: RecordSplit(4, 3, Fraction(3, 4)),
        3: RecordSplit(1, 0, 0),
    }
    assert summary.by_game_type["cities_knights"] == RecordSplit(1, 1, 1)
    assert summary.awards["longest_road"].opportunities == 4
    assert summary.awards["longest_road"].held == 4
    assert "longest_trade_route" in summary.awards
    assert "largest_army" in summary.awards
    assert "merchant" in summary.awards


def test_zero_games_missing_scores_and_loss_streak_sign() -> None:
    records = sample()
    empty = player_summary(records, 999)
    assert empty.games == empty.wins == empty.scored_games == 0
    assert empty.scored_wins == empty.scored_losses == 0
    assert empty.win_rate is None
    assert empty.avg_points is empty.median_points is empty.best_points is None
    assert empty.avg_target_share is None
    assert empty.avg_win_margin is empty.avg_loss_deficit is None
    assert empty.close_losses == empty.win_margin_samples == empty.loss_deficit_samples == 0
    assert empty.current_streak == empty.longest_win_streak == 0
    assert empty.recent_form == RecordSplit(0, 0, None)

    # Two losses in chronological order form a negative current streak.
    loss_streak = player_summary(records, 2)
    assert loss_streak.current_streak == 1  # final game is a win for player 2
    # Player 3 has one unscored loss and one scored loss; an unavailable own
    # score cannot contribute a deficit, while its win rate still counts.
    third = player_summary(records, 3)
    assert third.games == 2 and third.scored_games == 1 and third.wins == 0
    assert third.scored_wins == 0 and third.scored_losses == 1
    assert third.avg_points == 10 and third.avg_loss_deficit == 2
    assert third.win_margin_samples == 0 and third.loss_deficit_samples == 1


def test_even_median_and_negative_streak() -> None:
    records = [
        row(1, date(2025, 1, 1), 8, points=4),
        row(2, date(2025, 1, 2), 8, points=7),
        row(3, date(2025, 1, 3), 8, points=6),
        row(4, date(2025, 1, 4), 8, points=8),
    ]
    summary = player_summary(records, 8)
    assert summary.median_points == Fraction(13, 2)
    assert summary.current_streak == -4
    assert summary.longest_win_streak == 0


def test_missing_other_scores_skip_margins_and_missing_targets_skip_share() -> None:
    records = [
        row(1, date(2025, 1, 1), 7, winner=True, points=10, target=None),
        row(1, date(2025, 1, 1), 8, points=None, target=None),
    ]
    summary = player_summary(records, 7)
    assert summary.avg_target_share is None
    assert summary.avg_win_margin is None
    assert summary.win_margin_samples == summary.loss_deficit_samples == 0


def test_win_margin_requires_every_participant_score() -> None:
    records = [
        # The unscored third player makes the runner-up unknown.
        row(1, date(2025, 1, 1), 1, winner=True, points=10, players=3),
        row(1, date(2025, 1, 1), 2, points=3, players=3),
        row(1, date(2025, 1, 1), 3, points=None, players=3),
        # Fully scored control: margin is 10 - 7 = 3.
        row(2, date(2025, 1, 2), 1, winner=True, points=10, players=3),
        row(2, date(2025, 1, 2), 2, points=7, players=3),
        row(2, date(2025, 1, 2), 3, points=5, players=3),
    ]

    winner = player_summary(records, 1)
    assert winner.avg_win_margin == 3
    assert winner.win_margin_samples == 1
    # Loss deficits still need only the winner's and player's scores.
    loser = player_summary(records, 2)
    assert loser.avg_loss_deficit == 5  # partial game: 10-3; control game: 10-7
    assert loser.loss_deficit_samples == 2

    meta = meta_summary(records)
    assert meta.avg_margin == 3
    assert meta.margin_distribution == {3: 1}
    assert meta.margin_samples == 1


def test_summaries_order_award_rates_and_tied_best_other_score() -> None:
    records = sample()
    summaries = player_summaries(records)
    assert [(s.user_id, s.games) for s in summaries] == [(1, 5), (2, 5), (3, 2)]
    a = player_summary(records, 1)
    assert a.avg_win_margin == Fraction(
        5, 3
    )  # game 4 uses max(10, 10), not either tied row arbitrarily
    road = a.awards["longest_road"]
    assert road == AwardStat("longest_road", 4, 4, 1, 3, Fraction(3, 4), 0, 0, None)
    army = a.awards["largest_army"]
    assert army.opportunities == 4 and army.held == 1
    assert army.games_without == 3 and army.wins_without == 1
    assert army.win_rate_without == Fraction(1, 3)
    assert a.awards["longest_trade_route"].opportunities == 1


def test_head_to_head_pairs_and_symmetry() -> None:
    pairs = head_to_head(sample())
    assert pairs == [
        HeadToHead(1, 2, 5, 3, 2),
        HeadToHead(1, 3, 2, 1, 0),
        HeadToHead(2, 3, 2, 1, 0),
    ]
    # Reversing input pair order is represented canonically as a < b.
    reverse = [row(8, date(2025, 4, 1), 9, winner=True), row(8, date(2025, 4, 1), 4)]
    assert head_to_head(reverse) == [HeadToHead(4, 9, 1, 0, 1)]


def test_meta_summary_hand_computed_aggregates() -> None:
    summary = meta_summary(sample())
    assert summary.games == 5 and summary.scored_games == 5
    assert summary.avg_winning_score == Fraction(58, 5)
    assert summary.winning_score_distribution == {10: 1, 9: 1, 15: 1, 12: 2}
    assert summary.avg_margin == Fraction(7, 4)
    assert summary.margin_distribution == {2: 3, 1: 1}
    assert summary.winner_composition["settlements"] == 4
    assert summary.loser_composition["settlements"] == Fraction(23, 6)
    assert summary.by_game_type == {"normal": 3, "seafarers": 1, "cities_knights": 1}
    assert summary.by_player_count == {2: 4, 3: 1}
    assert summary.games_by_weekday == {0: 2, 1: 2, 6: 1}
    assert summary.avg_winning_score_by_month == {
        "2025-01": Fraction(19, 2),
        "2025-02": Fraction(27, 2),
        "2025-03": 12,
    }
    assert summary.winning_score_samples_by_month == {
        "2025-01": 2,
        "2025-02": 2,
        "2025-03": 1,
    }
    assert summary.play_styles == {
        "city_heavy": RecordSplit(1, 1, 1),
        "settlement_heavy": RecordSplit(9, 4, Fraction(4, 9)),
        "balanced": RecordSplit(1, 0, 0),
    }
    assert summary.winners_with_vp_cards == RecordSplit(4, 2, Fraction(1, 2))
    assert summary.avg_vp_card_share_of_winning_score == Fraction(1, 6)
    assert summary.vp_share_samples == 4
    assert summary.margin_samples == 4
    assert summary.winner_composition_samples["settlements"] == 5
    assert summary.loser_composition_samples["settlements"] == 6
    assert summary.win_award_combos == {
        "road_and_army": 1,
        "road_only": 2,
        "army_only": 1,
        "neither": 0,
    }
    assert summary.awards["longest_road"].opportunities == 9


def test_meta_handles_unscored_winner_and_no_games() -> None:
    no_games = meta_summary([])
    assert no_games.games == no_games.scored_games == 0
    assert no_games.avg_winning_score is no_games.avg_margin is None
    assert no_games.winning_score_distribution == no_games.margin_distribution == {}
    assert no_games.win_award_combos == {
        "road_and_army": 0,
        "road_only": 0,
        "army_only": 0,
        "neither": 0,
    }
    records = [
        row(1, date(2025, 5, 5), 1, winner=True, points=None),
        row(1, date(2025, 5, 5), 2, points=8),
    ]
    summary = meta_summary(records)
    assert summary.games == 1 and summary.scored_games == 0
    assert summary.loser_composition["settlements"] == 8
    assert summary.winner_composition == {}


def test_meta_balanced_style_and_neither_award_combo() -> None:
    records = [
        row(
            1,
            date(2025, 6, 1),
            1,
            winner=True,
            points=8,
            breakdown={
                "settlements": 2,
                "cities": 2,
                "longest_road": 0,
                "largest_army": 0,
            },
        ),
        row(
            1,
            date(2025, 6, 1),
            2,
            points=7,
            breakdown={
                "settlements": 3,
                "cities": 2,
                "longest_road": 0,
                "largest_army": 0,
            },
        ),
    ]
    summary = meta_summary(records)
    assert summary.play_styles["balanced"] == RecordSplit(1, 1, 1)
    assert summary.win_award_combos["neither"] == 1


def test_play_styles_include_winner_and_loser_participants() -> None:
    records = [
        row(
            1,
            date(2025, 6, 2),
            1,
            winner=True,
            points=10,
            breakdown={"settlements": 2, "cities": 4},
        ),
        row(
            1,
            date(2025, 6, 2),
            2,
            points=8,
            breakdown={"settlements": 4, "cities": 2},
        ),
    ]
    styles = meta_summary(records).play_styles
    assert styles["city_heavy"] == RecordSplit(1, 1, 1)
    assert styles["settlement_heavy"] == RecordSplit(1, 0, 0)
    assert styles["balanced"] == RecordSplit(0, 0, None)


def test_zero_point_vp_winner_is_excluded_from_share_average() -> None:
    records = [
        row(
            1,
            date(2025, 6, 3),
            1,
            winner=True,
            points=0,
            breakdown={"settlements": 0, "cities": 0, "vp_cards": 0},
        ),
        row(
            1,
            date(2025, 6, 3),
            2,
            points=0,
            breakdown={"settlements": 0, "cities": 0, "vp_cards": 0},
        ),
        row(
            2,
            date(2025, 6, 4),
            1,
            winner=True,
            points=10,
            breakdown={"settlements": 4, "cities": 2, "vp_cards": 2},
        ),
        row(2, date(2025, 6, 4), 2, points=8),
    ]
    summary = meta_summary(records)
    assert summary.avg_vp_card_share_of_winning_score == Fraction(1, 5)
    assert summary.vp_share_samples == 1


def test_vp_card_rates_use_scored_normal_and_seafarers_wins() -> None:
    records = [
        row(
            1,
            date(2025, 6, 5),
            1,
            winner=True,
            points=10,
            breakdown={"settlements": 10},
        ),
        row(1, date(2025, 6, 5), 2, points=8),
        row(
            2,
            date(2025, 6, 6),
            3,
            winner=True,
            points=10,
            breakdown={"settlements": 8, "vp_cards": 2},
        ),
        row(2, date(2025, 6, 6), 4, points=8),
    ]
    summary = meta_summary(records)
    assert summary.winners_with_vp_cards == RecordSplit(2, 1, Fraction(1, 2))
    assert summary.avg_vp_card_share_of_winning_score == Fraction(1, 10)


def test_analytics_scale_for_three_thousand_four_player_games() -> None:
    records = []
    for game_id in range(1, 3001):
        day = date(2020, 1, 1).toordinal() + game_id - 1
        played_on = date.fromordinal(day)
        winner = game_id % 4
        for user_id in range(4):
            points = 10 if user_id == winner else 8
            records.append(
                row(
                    game_id,
                    played_on,
                    user_id,
                    winner=user_id == winner,
                    points=points,
                    players=4,
                    breakdown={
                        "settlements": 4,
                        "cities": 2,
                        "longest_road": 0,
                        "largest_army": 0,
                        "vp_cards": 0,
                    },
                )
            )
    started = perf_counter()
    summaries = player_summaries(records)
    meta = meta_summary(records)
    pairs = head_to_head(records)
    elapsed = perf_counter() - started
    assert elapsed < 10
    assert [(summary.user_id, summary.games, summary.wins) for summary in summaries] == [
        (user_id, 3000, 750) for user_id in range(4)
    ]
    assert meta.games == meta.scored_games == 3000
    assert len(pairs) == 6 and all(pair.games_together == 3000 for pair in pairs)


def test_player_summaries_scale_for_six_thousand_distinct_players() -> None:
    records = []
    for game_id in range(1, 3001):
        played_on = date.fromordinal(date(2020, 1, 1).toordinal() + game_id - 1)
        first_id = game_id * 2
        records.extend(
            [
                row(game_id, played_on, first_id, winner=True, points=10),
                row(game_id, played_on, first_id + 1, points=8),
            ]
        )
    started = perf_counter()
    summaries = player_summaries(records)
    elapsed = perf_counter() - started
    assert elapsed < 10
    assert len(summaries) == 6000


def test_win_rate_timeline_is_cumulative_per_player() -> None:
    timeline = win_rate_timeline(sample())
    assert timeline[1] == [
        (date(2025, 1, 6), Fraction(1)),
        (date(2025, 1, 7), Fraction(1, 2)),
        (date(2025, 2, 3), Fraction(2, 3)),
        (date(2025, 2, 4), Fraction(3, 4)),
        (date(2025, 3, 2), Fraction(3, 5)),
    ]
    assert timeline[3][0] == (date(2025, 1, 7), 0)


def test_game_type_order_is_the_catalog_order() -> None:
    assert GAME_TYPE_ORDER == (
        "normal",
        "seafarers",
        "cities_knights",
        "seafarers_cities_knights",
    )


def test_games_by_type_counts_distinct_games_not_participations() -> None:
    day = date(2025, 1, 6)
    records = [
        row(1, day, 1, winner=True),
        row(1, day, 2),
        row(1, day, 3),
        row(2, day, 1, game_type="seafarers"),
        row(2, day, 2, game_type="seafarers", winner=True),
        row(3, day, 1, game_type="seafarers"),
        row(3, day, 2, game_type="seafarers", winner=True),
    ]

    assert games_by_type(records) == {"normal": 1, "seafarers": 2}


def test_games_by_type_of_no_records_is_empty() -> None:
    assert games_by_type([]) == {}


def test_most_played_game_type_picks_the_largest_count() -> None:
    counts = {"normal": 2, "cities_knights": 5, "seafarers": 3}

    assert most_played_game_type(counts) == "cities_knights"


def test_most_played_game_type_breaks_ties_by_catalog_order() -> None:
    assert most_played_game_type({"cities_knights": 4, "seafarers": 4}) == "seafarers"
    assert most_played_game_type({"seafarers_cities_knights": 2, "normal": 2}) == "normal"
    assert (
        most_played_game_type({"seafarers_cities_knights": 3, "cities_knights": 3})
        == "cities_knights"
    )


def test_most_played_game_type_puts_unknown_types_after_known_then_by_name() -> None:
    assert most_played_game_type({"zzz": 3, "seafarers": 3}) == "seafarers"
    assert most_played_game_type({"zzz": 3, "aaa": 3}) == "aaa"
    assert most_played_game_type({"zzz": 4, "seafarers": 3}) == "zzz"


def test_most_played_game_type_is_none_without_games() -> None:
    assert most_played_game_type({}) is None
    assert most_played_game_type({"normal": 0}) is None


def test_time_of_day_boundaries_use_recorded_local_timezone_on_dst_date() -> None:
    zone = ZoneInfo("America/Chicago")
    values = [(4, 59), (5, 0), (16, 59), (17, 0), (20, 59), (21, 0)]
    records = [
        row(
            index,
            date(2025, 3, 9),
            1,
            winner=index in (2, 4, 6),
            played_at=datetime(2025, 3, 9, hour, minute, tzinfo=zone),
            played_timezone="America/Chicago",
        )
        for index, (hour, minute) in enumerate(values, 1)
    ]

    summary = player_summary(records, 1)
    assert TIME_OF_DAY_BUCKETS == ("daytime", "evening", "late_night")
    assert list(summary.by_time_of_day) == list(TIME_OF_DAY_BUCKETS)
    assert summary.by_time_of_day == {
        "daytime": RecordSplit(2, 1, Fraction(1, 2)),
        "evening": RecordSplit(2, 1, Fraction(1, 2)),
        "late_night": RecordSplit(2, 1, Fraction(1, 2)),
    }
    assert summary.timed_games == 6
    assert meta_summary(records).games_by_time_of_day == {
        "daytime": 2,
        "evening": 2,
        "late_night": 2,
    }


def test_time_of_day_skips_missing_and_invalid_metadata_and_weekday_splits() -> None:
    records = [
        row(
            1,
            date(2025, 3, 10),
            1,
            winner=True,
            played_at=datetime(2025, 3, 10, 18, tzinfo=ZoneInfo("UTC")),
            played_timezone="not/a_timezone",
        ),
        row(2, date(2025, 3, 11), 1, played_at=datetime(2025, 3, 11, 18, tzinfo=ZoneInfo("UTC"))),
        row(3, date(2025, 3, 12), 1),
        row(
            4,
            date(2025, 3, 10),
            1,
            played_at=datetime(2025, 3, 10, 12, tzinfo=ZoneInfo("UTC")),
            played_timezone="UTC",
        ),
    ]

    summary = player_summary(records, 1)
    assert summary.by_time_of_day == {"daytime": RecordSplit(1, 0, 0)}
    assert summary.timed_games == 1
    assert summary.by_weekday == {
        0: RecordSplit(2, 1, Fraction(1, 2)),
        1: RecordSplit(1, 0, 0),
        2: RecordSplit(1, 0, 0),
    }


def test_time_of_day_skips_naive_timestamp_but_buckets_aware_timestamp() -> None:
    records = [
        row(
            1,
            date(2026, 1, 1),
            1,
            winner=True,
            played_at=datetime(2026, 1, 1, 17),
            played_timezone="UTC",
        ),
        row(
            2,
            date(2026, 1, 1),
            1,
            played_at=datetime(2026, 1, 1, 17, tzinfo=ZoneInfo("UTC")),
            played_timezone="UTC",
        ),
    ]

    summary = player_summary(records, 1)
    assert summary.by_time_of_day == {"evening": RecordSplit(1, 0, 0)}
    assert summary.timed_games == 1
    assert meta_summary(records).games_by_time_of_day == {"evening": 1}


def test_matchup_highlights_threshold_ties_and_empty_cases() -> None:
    pairs = [
        HeadToHead(1, 9, 2, 0, 2),  # under threshold
        HeadToHead(1, 8, 4, 1, 2),
        HeadToHead(1, 7, 6, 2, 3),  # same rates as opponent 8, more games
        HeadToHead(1, 6, 6, 3, 2),
        HeadToHead(1, 5, 6, 2, 2),
    ]
    assert matchup_highlights(pairs, 1) == MatchupHighlights(7, 6, 5, 3)
    assert matchup_highlights(pairs, 100) == MatchupHighlights(None, None, None, 3)
    assert matchup_highlights([], 1, min_games=5) == MatchupHighlights(None, None, None, 5)


def test_win_rate_by_season_splits_and_skips_missing_season() -> None:
    records = [
        row(1, date(2025, 1, 1), 1, winner=True, season_id=3),
        row(1, date(2025, 1, 1), 2, season_id=3),
        row(2, date(2025, 1, 2), 1, season_id=3),
        row(2, date(2025, 1, 2), 2, winner=True, season_id=3),
        row(3, date(2025, 1, 3), 1, winner=True, season_id=8),
        row(4, date(2025, 1, 4), 1, winner=True),
    ]
    assert win_rate_by_season(records) == {
        3: {1: RecordSplit(2, 1, Fraction(1, 2)), 2: RecordSplit(2, 1, Fraction(1, 2))},
        8: {1: RecordSplit(1, 1, 1)},
    }
