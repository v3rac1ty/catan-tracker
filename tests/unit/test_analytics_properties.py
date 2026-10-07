"""Property tests: analytics invariants over generated valid chronological games."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from fractions import Fraction

from hypothesis import given, settings
from hypothesis import strategies as st

from catan_bot.domain.analytics import (
    AwardStat,
    head_to_head,
    meta_summary,
    player_summaries,
    player_summary,
    win_rate_timeline,
)
from catan_bot.domain.participation import ParticipationRecord
from catan_bot.domain.scoring import score_sources

GAME_TYPES = ("normal", "seafarers", "cities_knights", "seafarers_cities_knights")
PLAYER_POOL = tuple(range(1, 9))
START = date(2025, 1, 1)

PROPERTY_SETTINGS = settings(max_examples=200, deadline=None)


@st.composite
def _game_rows(
    draw: st.DrawFn, game_id: int, played_on: date, user_ids: list[int]
) -> list[ParticipationRecord]:
    game_type = draw(st.sampled_from(GAME_TYPES))
    target = draw(st.one_of(st.none(), st.sampled_from((8, 10, 12, 13, 15))))
    player_count = len(user_ids)
    winner = draw(st.sampled_from(user_ids))
    scored = {uid: draw(st.booleans()) for uid in user_ids}
    sources = score_sources(game_type)

    # Each exclusive award is held by at most one scored player in the game.
    scored_ids = [uid for uid in user_ids if scored[uid]]
    holders: dict[str, int | None] = {}
    for source in sources:
        if source.exclusive:
            holders[source.key] = draw(st.sampled_from([None, *scored_ids]))

    rows: list[ParticipationRecord] = []
    for uid in user_ids:
        breakdown: dict[str, int] | None = None
        total: int | None = None
        if scored[uid]:
            breakdown = {}
            for source in sources:
                if source.exclusive:
                    assert source.fixed_points is not None
                    breakdown[source.key] = source.fixed_points if holders[source.key] == uid else 0
                elif source.requires_even:
                    breakdown[source.key] = 2 * draw(st.integers(0, 4))
                else:
                    breakdown[source.key] = draw(st.integers(0, 5))
            total = sum(breakdown.values())
        rows.append(
            ParticipationRecord(
                game_id,
                played_on,
                None,
                game_type,
                player_count >= 5,
                target,
                player_count,
                uid,
                uid == winner,
                total,
                breakdown,
            )
        )
    return rows


@st.composite
def record_sets(draw: st.DrawFn) -> list[ParticipationRecord]:
    game_total = draw(st.integers(0, 12))
    day = START
    records: list[ParticipationRecord] = []
    for game_id in range(1, game_total + 1):
        day += timedelta(days=draw(st.integers(0, 40)))
        user_ids = sorted(
            draw(st.lists(st.sampled_from(PLAYER_POOL), min_size=2, max_size=6, unique=True))
        )
        records.extend(draw(_game_rows(game_id, day, user_ids)))
    return records


def _game_count(records: list[ParticipationRecord]) -> int:
    return len({r.game_id for r in records})


def _in_unit_interval(value: Fraction | None) -> bool:
    return value is None or 0 <= value <= 1


def _check_award(stat: AwardStat) -> None:
    assert stat.held + stat.games_without == stat.opportunities
    assert stat.wins_when_held <= stat.held
    assert stat.wins_without <= stat.games_without
    assert stat.wins_when_held + stat.wins_without <= stat.opportunities
    assert stat.opportunities > 0
    for rate in (stat.held_rate, stat.win_rate_when_held, stat.win_rate_without):
        assert _in_unit_interval(rate)
    assert (stat.held_rate is None) == (stat.opportunities == 0)
    assert (stat.win_rate_when_held is None) == (stat.held == 0)
    assert (stat.win_rate_without is None) == (stat.games_without == 0)


@PROPERTY_SETTINGS
@given(record_sets())
def test_wins_and_games_add_up(records: list[ParticipationRecord]) -> None:
    summaries = player_summaries(records)
    games = _game_count(records)
    assert sum(s.wins for s in summaries) == games
    assert sum(s.games for s in summaries) == len(records)
    meta = meta_summary(records)
    assert meta.games == games
    assert sum(meta.by_game_type.values()) == games
    assert sum(meta.by_player_count.values()) == games
    assert sum(meta.games_by_weekday.values()) == games


@PROPERTY_SETTINGS
@given(record_sets())
def test_player_summary_rates_and_sample_bounds(records: list[ParticipationRecord]) -> None:
    rows_by_game: dict[int, list[ParticipationRecord]] = defaultdict(list)
    for row in records:
        rows_by_game[row.game_id].append(row)
    fully_scored_games = {
        game_id
        for game_id, game_rows in rows_by_game.items()
        if all(row.total_points is not None for row in game_rows)
    }
    for s in player_summaries(records):
        assert s.wins <= s.games
        assert s.scored_games <= s.games
        assert _in_unit_interval(s.win_rate)
        assert (s.win_rate is None) == (s.games == 0)
        assert _in_unit_interval(s.recent_form.win_rate)
        assert s.recent_form.games == min(s.games, 10)
        assert s.recent_form.wins <= s.recent_form.games
        assert set(s.source_samples) == set(s.source_averages)
        assert all(0 < n <= s.scored_games for n in s.source_samples.values())
        assert s.target_share_samples <= s.scored_games
        fully_scored_wins = sum(
            row.is_winner
            for row in records
            if row.user_id == s.user_id and row.game_id in fully_scored_games
        )
        assert s.win_margin_samples <= fully_scored_wins
        assert s.loss_deficit_samples <= s.games - s.wins
        assert s.close_losses <= s.loss_deficit_samples
        assert (s.avg_win_margin is None) == (s.win_margin_samples == 0)
        assert (s.avg_loss_deficit is None) == (s.loss_deficit_samples == 0)
        assert (s.avg_points is None) == (s.scored_games == 0)
        assert (s.median_points is None) == (s.scored_games == 0)
        assert (s.best_points is None) == (s.scored_games == 0)
        if s.avg_points is not None and s.best_points is not None:
            assert s.avg_points <= s.best_points
            assert s.median_points is not None and s.median_points <= s.best_points
        for key, avg in s.source_averages.items():
            assert avg >= 0, key
        assert sum(split.games for split in s.by_player_count.values()) == s.games
        assert sum(split.games for split in s.by_game_type.values()) == s.games
        assert sum(split.wins for split in s.by_player_count.values()) == s.wins
        assert sum(split.wins for split in s.by_game_type.values()) == s.wins
        for split in (*s.by_player_count.values(), *s.by_game_type.values()):
            assert _in_unit_interval(split.win_rate)


@PROPERTY_SETTINGS
@given(record_sets())
def test_player_summary_award_stats(records: list[ParticipationRecord]) -> None:
    for s in player_summaries(records):
        for key, stat in s.awards.items():
            assert stat.key == key
            _check_award(stat)
            assert stat.opportunities <= s.scored_games


@PROPERTY_SETTINGS
@given(record_sets())
def test_streak_bounds(records: list[ParticipationRecord]) -> None:
    for s in player_summaries(records):
        assert abs(s.current_streak) <= s.games
        assert s.longest_win_streak <= s.wins
        assert (s.current_streak == 0) == (s.games == 0)
        if s.current_streak > 0:
            assert s.longest_win_streak >= s.current_streak


@PROPERTY_SETTINGS
@given(record_sets())
def test_streak_matches_brute_force(records: list[ParticipationRecord]) -> None:
    for s in player_summaries(records):
        results = [r.is_winner for r in records if r.user_id == s.user_id]
        expected = 0
        for result in reversed(results):
            if expected == 0 or (expected > 0) == result:
                expected += 1 if result else -1
            else:
                break
        assert s.current_streak == expected


@PROPERTY_SETTINGS
@given(record_sets())
def test_optimised_summaries_match_single_player_path(
    records: list[ParticipationRecord],
) -> None:
    summaries = player_summaries(records)
    assert [s.user_id for s in summaries] == sorted(
        {r.user_id for r in records},
        key=lambda uid: (-sum(r.user_id == uid for r in records), uid),
    )
    assert summaries == [player_summary(records, s.user_id) for s in summaries]


@PROPERTY_SETTINGS
@given(record_sets())
def test_head_to_head_matches_brute_force(records: list[ParticipationRecord]) -> None:
    by_game: dict[int, dict[int, ParticipationRecord]] = defaultdict(dict)
    for r in records:
        by_game[r.game_id][r.user_id] = r

    expected: dict[tuple[int, int], list[int]] = {}
    users = sorted({r.user_id for r in records})
    for i, a in enumerate(users):
        for b in users[i + 1 :]:
            shared = [g for g in by_game.values() if a in g and b in g]
            if shared:
                expected[(a, b)] = [
                    len(shared),
                    sum(g[a].is_winner for g in shared),
                    sum(g[b].is_winner for g in shared),
                ]

    games = {s.user_id: s.games for s in player_summaries(records)}
    result = head_to_head(records)
    assert {(h.player_a, h.player_b) for h in result} == set(expected)
    assert len(result) == len(expected)
    for h in result:
        assert h.player_a < h.player_b
        assert [h.games_together, h.a_wins, h.b_wins] == expected[(h.player_a, h.player_b)]
        assert h.a_wins + h.b_wins <= h.games_together
        assert h.games_together <= min(games[h.player_a], games[h.player_b])


@PROPERTY_SETTINGS
@given(record_sets())
def test_timeline_matches_summary(records: list[ParticipationRecord]) -> None:
    timeline = win_rate_timeline(records)
    summaries = {s.user_id: s for s in player_summaries(records)}
    assert set(timeline) == set(summaries)
    for uid, points in timeline.items():
        summary = summaries[uid]
        assert len(points) == summary.games
        assert points[-1][1] == summary.win_rate
        assert all(0 <= rate <= 1 for _, rate in points)
        days = [day for day, _ in points]
        assert days == sorted(days)


@PROPERTY_SETTINGS
@given(record_sets())
def test_meta_sample_bounds_and_totals(records: list[ParticipationRecord]) -> None:
    meta = meta_summary(records)
    rows_by_game: dict[int, list[ParticipationRecord]] = defaultdict(list)
    for row in records:
        rows_by_game[row.game_id].append(row)
    with_breakdown = [r for r in records if r.breakdown is not None]
    winners_scored = [r for r in records if r.is_winner and r.total_points is not None]

    assert meta.scored_games == len(winners_scored)
    assert meta.scored_games <= meta.games
    assert sum(meta.winning_score_distribution.values()) == meta.scored_games
    assert sum(meta.win_award_combos.values()) <= meta.scored_games
    assert sum(split.games for split in meta.play_styles.values()) == len(with_breakdown)
    assert sum(split.wins for split in meta.play_styles.values()) == sum(
        r.is_winner for r in with_breakdown
    )
    for split in meta.play_styles.values():
        assert _in_unit_interval(split.win_rate)

    fully_scored_game_count = sum(
        all(row.total_points is not None for row in game_rows)
        for game_rows in rows_by_game.values()
    )
    assert meta.margin_samples <= fully_scored_game_count
    assert sum(meta.margin_distribution.values()) == meta.margin_samples
    assert (meta.avg_margin is None) == (meta.margin_samples == 0)
    assert meta.winners_with_vp_cards.games <= meta.scored_games
    assert meta.winners_with_vp_cards.wins <= meta.winners_with_vp_cards.games
    assert _in_unit_interval(meta.winners_with_vp_cards.win_rate)
    assert meta.vp_share_samples <= meta.winners_with_vp_cards.games
    assert (meta.avg_vp_card_share_of_winning_score is None) == (meta.vp_share_samples == 0)
    if meta.avg_vp_card_share_of_winning_score is not None:
        assert 0 <= meta.avg_vp_card_share_of_winning_score <= 1

    winners_with = sum(r.is_winner for r in with_breakdown)
    losers_with = len(with_breakdown) - winners_with
    assert set(meta.winner_composition_samples) == set(meta.winner_composition)
    assert set(meta.loser_composition_samples) == set(meta.loser_composition)
    assert all(0 < n <= winners_with for n in meta.winner_composition_samples.values())
    assert all(0 < n <= losers_with for n in meta.loser_composition_samples.values())

    if winners_scored:
        assert meta.avg_winning_score == Fraction(
            sum(r.total_points for r in winners_scored if r.total_points is not None),
            len(winners_scored),
        )
    else:
        assert meta.avg_winning_score is None
    assert all(len(month) == 7 for month in meta.avg_winning_score_by_month)


@PROPERTY_SETTINGS
@given(record_sets())
def test_meta_award_stats(records: list[ParticipationRecord]) -> None:
    meta = meta_summary(records)
    scored = sum(r.breakdown is not None for r in records)
    for key, stat in meta.awards.items():
        assert stat.key == key
        _check_award(stat)
        assert stat.opportunities <= scored
        # Exclusive awards are held by at most one player per game in the generator;
        # defender_of_catan is the one cumulative (non-exclusive) award column.
        if key != "defender_of_catan":
            assert stat.held <= meta.games
