"""Pure summaries and comparisons over confirmed game participations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from catan_bot.domain.participation import ParticipationRecord

_ROAD_KEY = {
    "normal": "longest_road",
    "cities_knights": "longest_road",
    "seafarers": "longest_trade_route",
    "seafarers_cities_knights": "longest_trade_route",
}
_AWARDS_BY_TYPE = {
    "normal": ("longest_road", "largest_army"),
    "seafarers": ("longest_trade_route", "largest_army"),
    "cities_knights": ("longest_road", "merchant", "defender_of_catan", "constitution", "printer"),
    "seafarers_cities_knights": (
        "longest_trade_route",
        "merchant",
        "defender_of_catan",
        "constitution",
        "printer",
    ),
}


@dataclass(frozen=True, slots=True)
class AwardStat:
    key: str
    opportunities: int
    held: int
    held_rate: Fraction | None
    wins_when_held: int
    win_rate_when_held: Fraction | None
    games_without: int
    wins_without: int
    win_rate_without: Fraction | None


@dataclass(frozen=True, slots=True)
class RecordSplit:
    games: int
    wins: int
    win_rate: Fraction | None


@dataclass(frozen=True, slots=True)
class PlayerSummary:
    user_id: int
    games: int
    wins: int
    win_rate: Fraction | None
    scored_games: int
    scored_wins: int
    scored_losses: int
    avg_points: Fraction | None
    avg_points_in_wins: Fraction | None
    avg_points_in_losses: Fraction | None
    median_points: Fraction | None
    best_points: int | None
    avg_target_share: Fraction | None
    avg_settlements: Fraction | None
    avg_cities: Fraction | None
    avg_vp_cards: Fraction | None
    avg_metropolises: Fraction | None
    source_averages: dict[str, Fraction]
    source_samples: dict[str, int]
    target_share_samples: int
    awards: dict[str, AwardStat]
    avg_win_margin: Fraction | None
    avg_loss_deficit: Fraction | None
    close_losses: int
    win_margin_samples: int
    loss_deficit_samples: int
    current_streak: int
    longest_win_streak: int
    recent_form: RecordSplit
    by_player_count: dict[int, RecordSplit]
    by_game_type: dict[str, RecordSplit]


@dataclass(frozen=True, slots=True)
class HeadToHead:
    player_a: int
    player_b: int
    games_together: int
    a_wins: int
    b_wins: int


@dataclass(frozen=True, slots=True)
class MetaSummary:
    games: int
    scored_games: int
    winner_composition: dict[str, Fraction]
    loser_composition: dict[str, Fraction]
    winner_composition_samples: dict[str, int]
    loser_composition_samples: dict[str, int]
    avg_winning_score: Fraction | None
    winning_score_distribution: dict[int, int]
    avg_margin: Fraction | None
    margin_distribution: dict[int, int]
    margin_samples: int
    vp_share_samples: int
    awards: dict[str, AwardStat]
    win_award_combos: dict[str, int]
    play_styles: dict[str, RecordSplit]
    winners_with_vp_cards: RecordSplit
    avg_vp_card_share_of_winning_score: Fraction | None
    by_game_type: dict[str, int]
    by_player_count: dict[int, int]
    games_by_weekday: dict[int, int]
    avg_winning_score_by_month: dict[str, Fraction]
    winning_score_samples_by_month: dict[str, int]


def _groups(records: Sequence[ParticipationRecord]) -> list[list[ParticipationRecord]]:
    """Group adjacent chronological rows into games, preserving input order."""
    groups: list[list[ParticipationRecord]] = []
    for record in records:
        if not groups or groups[-1][0].game_id != record.game_id:
            groups.append([])
        groups[-1].append(record)
    return groups


def _rate(wins: int, games: int) -> Fraction | None:
    return Fraction(wins, games) if games else None


def _split(rows: Sequence[ParticipationRecord]) -> RecordSplit:
    wins = sum(row.is_winner for row in rows)
    return RecordSplit(len(rows), wins, _rate(wins, len(rows)))


def _award_stat(key: str, rows: Sequence[ParticipationRecord]) -> AwardStat:
    opportunities = held = wins_held = without = wins_without = 0
    for row in rows:
        if (
            row.game_type not in _AWARDS_BY_TYPE
            or key not in _AWARDS_BY_TYPE[row.game_type]
            or row.breakdown is None
        ):
            continue
        opportunities += 1
        if row.breakdown.get(key, 0) > 0:
            held += 1
            wins_held += row.is_winner
        else:
            without += 1
            wins_without += row.is_winner
    return AwardStat(
        key,
        opportunities,
        held,
        _rate(held, opportunities),
        wins_held,
        _rate(wins_held, held),
        without,
        wins_without,
        _rate(wins_without, without),
    )


def _award_stats(rows: Sequence[ParticipationRecord]) -> dict[str, AwardStat]:
    keys = sorted(
        {
            key
            for r in rows
            if r.game_type in _AWARDS_BY_TYPE
            for key in _AWARDS_BY_TYPE[r.game_type]
        }
    )
    stats = {key: _award_stat(key, rows) for key in keys}
    return {key: stat for key, stat in stats.items() if stat.opportunities}


def player_summary(records: Sequence[ParticipationRecord], user_id: int) -> PlayerSummary:
    """Summarize one player's results, omitting unavailable score samples."""
    rows = [r for r in records if r.user_id == user_id]
    own_games = [
        (game, own)
        for game in _groups(records)
        if (own := next((r for r in game if r.user_id == user_id), None)) is not None
    ]
    return _player_summary(rows, own_games, user_id)


def _player_summary(
    rows: Sequence[ParticipationRecord],
    own_games: Sequence[tuple[Sequence[ParticipationRecord], ParticipationRecord]],
    user_id: int,
) -> PlayerSummary:
    games = len(rows)
    wins = sum(r.is_winner for r in rows)
    scored = [r for r in rows if r.total_points is not None]
    numeric_points = [r.total_points for r in scored if r.total_points is not None]
    scored_wins = sum(r.is_winner for r in scored)
    scored_losses = len(scored) - scored_wins

    def source_values(key: str) -> list[int]:
        return [r.breakdown[key] for r in scored if r.breakdown is not None and key in r.breakdown]

    source_averages = {
        key: Fraction(sum(values), len(values))
        for key in sorted({key for r in scored if r.breakdown is not None for key in r.breakdown})
        if (values := source_values(key))
    }
    source_samples = {key: len(source_values(key)) for key in source_averages}
    target_shares = [
        Fraction(r.total_points, r.target_points)
        for r in scored
        if r.target_points and r.total_points is not None
    ]
    settlements = source_values("settlements")
    cities = source_values("cities")
    vp_cards = source_values("vp_cards")
    metropolises = source_values("metropolis_bonus")

    win_margins: list[int] = []
    loss_deficits: list[int] = []
    for game, own in own_games:
        if own.total_points is None:
            continue
        if own.is_winner:
            others = [
                r.total_points
                for r in game
                if r.user_id != own.user_id and r.total_points is not None
            ]
            if others:
                win_margins.append(own.total_points - max(others))
        else:
            winners = [r.total_points for r in game if r.is_winner and r.total_points is not None]
            if winners:
                loss_deficits.append(max(winners) - own.total_points)

    streak = 0
    for row in reversed(rows):
        if streak == 0:
            streak = 1 if row.is_winner else -1
        elif (streak > 0) == row.is_winner:
            streak += 1 if row.is_winner else -1
        else:
            break
    longest = run = 0
    for row in rows:
        run = run + 1 if row.is_winner else 0
        longest = max(longest, run)

    counts: dict[int, list[ParticipationRecord]] = {}
    types: dict[str, list[ParticipationRecord]] = {}
    for row in rows:
        counts.setdefault(row.player_count, []).append(row)
        types.setdefault(row.game_type, []).append(row)
    sorted_points = sorted(numeric_points)
    median = (
        Fraction(
            sorted_points[(len(sorted_points) - 1) // 2] + sorted_points[len(sorted_points) // 2], 2
        )
        if sorted_points
        else None
    )

    return PlayerSummary(
        user_id,
        games,
        wins,
        _rate(wins, games),
        len(scored),
        scored_wins,
        scored_losses,
        Fraction(sum(numeric_points), len(numeric_points)) if numeric_points else None,
        Fraction(
            sum(r.total_points for r in scored if r.is_winner and r.total_points is not None),
            sum(r.is_winner for r in scored),
        )
        if any(r.is_winner for r in scored)
        else None,
        Fraction(
            sum(r.total_points for r in scored if not r.is_winner and r.total_points is not None),
            sum(not r.is_winner for r in scored),
        )
        if any(not r.is_winner for r in scored)
        else None,
        median,
        max(numeric_points) if numeric_points else None,
        Fraction(sum(target_shares), len(target_shares)) if target_shares else None,
        Fraction(sum(settlements), len(settlements)) if settlements else None,
        Fraction(sum(cities), 2 * len(cities)) if cities else None,
        Fraction(sum(vp_cards), len(vp_cards)) if vp_cards else None,
        Fraction(sum(metropolises), 2 * len(metropolises)) if metropolises else None,
        source_averages,
        source_samples,
        len(target_shares),
        _award_stats(rows),
        Fraction(sum(win_margins), len(win_margins)) if win_margins else None,
        Fraction(sum(loss_deficits), len(loss_deficits)) if loss_deficits else None,
        sum(1 for deficit in loss_deficits if 1 <= deficit <= 2),
        len(win_margins),
        len(loss_deficits),
        streak,
        longest,
        _split(rows[-10:]),
        {count: _split(group) for count, group in sorted(counts.items())},
        {game_type: _split(group) for game_type, group in sorted(types.items())},
    )


def player_summaries(records: Sequence[ParticipationRecord]) -> list[PlayerSummary]:
    """Summarize every player in stable games-descending, id-ascending order."""
    games = _groups(records)
    rows_by_user: dict[int, list[ParticipationRecord]] = {}
    games_by_user: dict[int, list[tuple[Sequence[ParticipationRecord], ParticipationRecord]]] = {}
    for row in records:
        rows_by_user.setdefault(row.user_id, []).append(row)
    for game in games:
        for row in game:
            games_by_user.setdefault(row.user_id, []).append((game, row))
    summaries = [
        _player_summary(rows, games_by_user[uid], uid) for uid, rows in rows_by_user.items()
    ]
    return sorted(summaries, key=lambda item: (-item.games, item.user_id))


def head_to_head(records: Sequence[ParticipationRecord]) -> list[HeadToHead]:
    """Return shared-game records for every pair of players."""
    pairs: dict[tuple[int, int], list[int]] = {}
    for game in _groups(records):
        by_user = {r.user_id: r for r in game}
        users = sorted(by_user)
        for i, a in enumerate(users):
            for b in users[i + 1 :]:
                bucket = pairs.setdefault((a, b), [0, 0, 0])
                bucket[0] += 1
                bucket[1] += by_user[a].is_winner
                bucket[2] += by_user[b].is_winner
    return [HeadToHead(a, b, *values) for (a, b), values in sorted(pairs.items())]


def meta_summary(records: Sequence[ParticipationRecord]) -> MetaSummary:
    """Aggregate game-level scores, awards, participation and calendar buckets."""
    games = _groups(records)
    winning_scores: list[int] = []
    margins: list[int] = []
    winner_sources: dict[str, list[int]] = {}
    loser_sources: dict[str, list[int]] = {}
    styles: dict[str, list[ParticipationRecord]] = {
        k: [] for k in ("city_heavy", "settlement_heavy", "balanced")
    }
    combos = {k: 0 for k in ("road_and_army", "road_only", "army_only", "neither")}
    vp_winners = 0
    vp_scored_wins = 0
    vp_shares: list[Fraction] = []
    by_type: dict[str, int] = {}
    by_count: dict[int, int] = {}
    weekdays: dict[int, int] = {}
    month_scores: dict[str, list[int]] = {}
    scored_games = 0

    for game in games:
        # Game metadata is shared across rows; count once per game.
        first = game[0]
        by_type[first.game_type] = by_type.get(first.game_type, 0) + 1
        by_count[first.player_count] = by_count.get(first.player_count, 0) + 1
        weekdays[first.played_on.weekday()] = weekdays.get(first.played_on.weekday(), 0) + 1
        winner = next((r for r in game if r.is_winner), None)
        for row in game:
            if row.breakdown is None:
                continue
            settlements = row.breakdown.get("settlements", 0)
            city_points = row.breakdown.get("cities", 0)
            styles[
                "city_heavy"
                if city_points > settlements
                else "settlement_heavy"
                if settlements > city_points
                else "balanced"
            ].append(row)
            destination = winner_sources if row.is_winner else loser_sources
            for key, value in row.breakdown.items():
                destination.setdefault(key, []).append(value)
        if winner is None or winner.total_points is None:
            continue
        scored_games += 1
        winning_scores.append(winner.total_points)
        month = winner.played_on.strftime("%Y-%m")
        month_scores.setdefault(month, []).append(winner.total_points)
        if winner.game_type in ("normal", "seafarers"):
            vp_scored_wins += 1
            vp_cards = (winner.breakdown or {}).get("vp_cards", 0)
            vp_winners += vp_cards > 0
            if winner.total_points > 0:
                vp_shares.append(Fraction(vp_cards, winner.total_points))
        other_scores = [
            r.total_points
            for r in game
            if r.user_id != winner.user_id and r.total_points is not None
        ]
        if other_scores:
            margins.append(winner.total_points - max(other_scores))
        road = _ROAD_KEY.get(winner.game_type)
        if (
            road
            and winner.breakdown is not None
            and "largest_army" in _AWARDS_BY_TYPE.get(winner.game_type, ())
        ):
            road_held = winner.breakdown.get(road, 0) > 0
            army_held = winner.breakdown.get("largest_army", 0) > 0
            combo = (
                "road_and_army"
                if road_held and army_held
                else "road_only"
                if road_held
                else "army_only"
                if army_held
                else "neither"
            )
            combos[combo] += 1

    total_rows = list(records)
    awards = _award_stats(total_rows)
    distribution: dict[int, int] = {}
    for score in winning_scores:
        distribution[score] = distribution.get(score, 0) + 1
    margin_distribution: dict[int, int] = {}
    for margin in margins:
        margin_distribution[margin] = margin_distribution.get(margin, 0) + 1
    return MetaSummary(
        len(games),
        scored_games,
        {key: Fraction(sum(values), len(values)) for key, values in sorted(winner_sources.items())},
        {key: Fraction(sum(values), len(values)) for key, values in sorted(loser_sources.items())},
        {key: len(values) for key, values in sorted(winner_sources.items())},
        {key: len(values) for key, values in sorted(loser_sources.items())},
        Fraction(sum(winning_scores), len(winning_scores)) if winning_scores else None,
        distribution,
        Fraction(sum(margins), len(margins)) if margins else None,
        margin_distribution,
        len(margins),
        len(vp_shares),
        awards,
        combos,
        {key: _split(value) for key, value in styles.items()},
        RecordSplit(vp_scored_wins, vp_winners, _rate(vp_winners, vp_scored_wins)),
        Fraction(sum(vp_shares), len(vp_shares)) if vp_shares else None,
        by_type,
        by_count,
        weekdays,
        {
            month: Fraction(sum(values), len(values))
            for month, values in sorted(month_scores.items())
        },
        {month: len(values) for month, values in sorted(month_scores.items())},
    )


def win_rate_timeline(
    records: Sequence[ParticipationRecord],
) -> dict[int, list[tuple[date, Fraction]]]:
    """Return each player's cumulative win rate after every game played."""
    result: dict[int, list[tuple[date, Fraction]]] = {}
    counts: dict[int, list[int]] = {}
    for row in records:
        wins, games = counts.setdefault(row.user_id, [0, 0])
        games += 1
        wins += row.is_winner
        counts[row.user_id] = [wins, games]
        result.setdefault(row.user_id, []).append((row.played_on, Fraction(wins, games)))
    return result
