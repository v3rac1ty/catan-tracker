"""End-to-end pipeline: seeded guild -> `list_participations` -> analytics engine.

Every expected value below is derived by hand from the seed table in `_SPECS`
(see the per-game comments); nothing is computed by calling the engine under
test. The point of the file is to prove that the repository's ordering and
record shape actually satisfy the engine's assumptions (records grouped
adjacently by game, chronological order, `player_count` = active roster, ...).

Seed (game: players, winner, points; "-" = no score recorded). Dates are
2026; G7 and G6 share a day (G7 has an unknown time so it sorts first).

    G1 03-01 normal   t10  S1  1W10 2:8  3:6
    G2 03-10 normal   t10  S1  2W10 1:8  3:5  4:-
    G3 03-20 seafarers t12 S1  3W12 1:9  5:10
    G4 04-05 c&k      t13  S2  1W13 2:10 4:8
    G5 04-14 normal+ext t10 S2 4W10 1:8  2:6  3:-  5:-          (5 players)
    G7 04-22 seafarers t12 S2  2W-  3:-  5:-                    (nothing scored)
    G6 04-22 normal   t10  S2  1W10 2:9  [3 deactivated by a roster update]
    G8 05-03 c&k      t13  S2  3W13 1:12 5:5
    G9 05-13 normal   t10  S2  2W10 3:9
    plus: one voided game (winner 16 points), one still-pending game.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from fractions import Fraction

import asyncpg
import pytest

from catan_bot.db.repositories import analytics, games, players, seasons
from catan_bot.domain.analytics import (
    AwardStat,
    HeadToHead,
    RecordSplit,
    head_to_head,
    meta_summary,
    player_summaries,
    player_summary,
    win_rate_timeline,
)
from catan_bot.domain.participation import ParticipationRecord
from catan_bot.domain.scoring import PlayerScore, ScoreEntry, score_sources

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

F = Fraction
STARTS_ON = date(2026, 1, 1)
ENDS_ON = date(2026, 12, 31)
ENDS_AT = datetime(2027, 1, 1, tzinfo=UTC)

D_G1 = date(2026, 3, 1)
D_G2 = date(2026, 3, 10)
D_G3 = date(2026, 3, 20)
D_G4 = date(2026, 4, 5)
D_G5 = date(2026, 4, 14)
D_G67 = date(2026, 4, 22)
D_G8 = date(2026, 5, 3)
D_G9 = date(2026, 5, 13)

G6_PLAYED_AT = datetime(2026, 4, 22, 18, 0, tzinfo=UTC)
G9_PLAYED_AT = datetime(2026, 5, 13, 12, 0, tzinfo=UTC)

# Chronological order the repository must return the games in.
CHRONOLOGICAL = ["G1", "G2", "G3", "G4", "G5", "G7", "G6", "G8", "G9"]
# Creation order deliberately differs, so game ids are not chronological.
CREATION_ORDER = ["G6", "G3", "G9", "G1", "G8", "G5", "G2", "G7", "G4"]


@dataclass(frozen=True)
class Spec:
    played_on: date
    game_type: str
    target: int
    season: int
    winner: int
    losers: tuple[int, ...]
    # user -> source points; a user missing here has no recorded score.
    sheets: dict[int, dict[str, int]]
    played_at: datetime | None = None
    extension: bool = False


_SPECS: dict[str, Spec] = {
    "G1": Spec(
        D_G1,
        "normal",
        10,
        1,
        1,
        (2, 3),
        {
            1: {"settlements": 4, "cities": 4, "longest_road": 2},
            2: {"settlements": 3, "cities": 2, "largest_army": 2, "vp_cards": 1},
            3: {"settlements": 2, "cities": 4},
        },
    ),
    "G2": Spec(
        D_G2,
        "normal",
        10,
        1,
        2,
        (1, 3, 4),
        {
            2: {"settlements": 2, "cities": 4, "longest_road": 2, "vp_cards": 2},
            1: {"settlements": 4, "cities": 2, "largest_army": 2},
            3: {"settlements": 3, "cities": 2},
        },
    ),
    "G3": Spec(
        D_G3,
        "seafarers",
        12,
        1,
        3,
        (1, 5),
        {
            3: {"settlements": 4, "cities": 4, "longest_trade_route": 2, "largest_army": 2},
            1: {"settlements": 4, "cities": 2, "scenario_points": 3},
            5: {"settlements": 3, "cities": 4, "vp_cards": 1, "scenario_points": 2},
        },
    ),
    "G4": Spec(
        D_G4,
        "cities_knights",
        13,
        2,
        1,
        (2, 4),
        {
            1: {
                "settlements": 2,
                "cities": 6,
                "longest_road": 2,
                "metropolis_bonus": 2,
                "merchant": 1,
            },
            2: {"settlements": 3, "cities": 4, "defender_of_catan": 2, "constitution": 1},
            4: {"settlements": 2, "cities": 4, "defender_of_catan": 1, "printer": 1},
        },
    ),
    "G5": Spec(
        D_G5,
        "normal",
        10,
        2,
        4,
        (1, 2, 3, 5),
        {
            4: {"settlements": 4, "cities": 4, "longest_road": 2},
            1: {"settlements": 3, "cities": 2, "largest_army": 2, "vp_cards": 1},
            2: {"settlements": 4, "cities": 2},
        },
        extension=True,
    ),
    "G7": Spec(D_G67, "seafarers", 12, 2, 2, (3, 5), {}),
    "G6": Spec(
        D_G67,
        "normal",
        10,
        2,
        1,
        (2, 3),
        {
            1: {"settlements": 5, "cities": 2, "largest_army": 2, "vp_cards": 1},
            2: {"settlements": 3, "cities": 4, "longest_road": 2},
            3: {"settlements": 3, "cities": 2},  # deactivated by the roster update below
        },
        played_at=G6_PLAYED_AT,
    ),
    "G8": Spec(
        D_G8,
        "cities_knights",
        13,
        2,
        3,
        (1, 5),
        {
            3: {
                "settlements": 3,
                "cities": 4,
                "metropolis_bonus": 2,
                "defender_of_catan": 2,
                "merchant": 1,
                "printer": 1,
            },
            1: {
                "settlements": 4,
                "cities": 4,
                "longest_road": 2,
                "defender_of_catan": 1,
                "constitution": 1,
            },
            5: {"settlements": 3, "cities": 2},
        },
    ),
    "G9": Spec(
        D_G9,
        "normal",
        10,
        2,
        2,
        (3,),
        {
            2: {"settlements": 4, "cities": 4, "longest_road": 2},
            3: {"settlements": 5, "cities": 2, "largest_army": 2},
        },
        played_at=G9_PLAYED_AT,
    ),
}


def _sheet(game_type: str, user_id: int, points: dict[str, int]) -> PlayerScore:
    """A complete score row: every catalog source present (0 when unclaimed)."""
    keys = [source.key for source in score_sources(game_type)]  # type: ignore[arg-type]
    assert set(points) <= set(keys), (game_type, points)
    entries = tuple(ScoreEntry(key, points.get(key, 0)) for key in keys)
    return PlayerScore(user_id, sum(e.points for e in entries), entries)


@dataclass
class Seed:
    ids: dict[str, int]
    season_one: int
    season_two: int
    voided_id: int
    pending_id: int
    records: list[ParticipationRecord] = field(default_factory=list)

    def label_of(self, game_id: int) -> str:
        return next(label for label, gid in self.ids.items() if gid == game_id)


async def _play_game(
    conn: asyncpg.Connection, guild_id: int, spec: Spec, season_id: int
) -> games.Game:
    game = await games.create_game(
        conn,
        guild_id,
        season_id,
        spec.played_on,
        spec.winner,
        spec.winner,
        list(spec.losers),
        game_type=spec.game_type,
        extension_5_6=spec.extension,
        target_points=spec.target,
        played_at=spec.played_at,
        played_timezone="UTC" if spec.played_at is not None else None,
    )
    assert await games.confirm_game(conn, guild_id, game.game_id, spec.losers[0]) == "confirmed"
    for user_id, points in spec.sheets.items():
        score = _sheet(spec.game_type, user_id, points)
        assert await games.set_player_score(conn, guild_id, game.game_id, user_id, score)
    return game


@pytest.fixture
async def seed(app_conn: asyncpg.Connection, guild_id: int) -> Seed:
    conn = app_conn
    await players.ensure_players(conn, guild_id, [1, 2, 3, 4, 5, 9])
    season_one = (
        await seasons.create_season(conn, guild_id, "One", STARTS_ON, ENDS_ON, ENDS_AT, 1, 1)
    ).season_id
    assert await seasons.cancel_active_season(conn, guild_id) is not None
    season_two = (
        await seasons.create_season(conn, guild_id, "Two", STARTS_ON, ENDS_ON, ENDS_AT, 1, 1)
    ).season_id
    season_ids = {1: season_one, 2: season_two}

    ids: dict[str, int] = {}
    for label in CREATION_ORDER:
        spec = _SPECS[label]
        game = await _play_game(conn, guild_id, spec, season_ids[spec.season])
        ids[label] = game.game_id

    # Roster update: player 3 is removed from G6 (2 active players remain).
    g6 = _SPECS["G6"]
    result = await games.update_confirmed_game(
        conn,
        guild_id,
        ids["G6"],
        expected_revision=0,
        updated_by=9,
        reason="player 3 was not actually in this game",
        played_on=g6.played_on,
        winner_id=1,
        loser_ids=[2],
        game_type="normal",
        extension_5_6=False,
        scenario=None,
        target_points=10,
        played_at=G6_PLAYED_AT,
        played_timezone="UTC",
        scores=[_sheet("normal", 1, g6.sheets[1]), _sheet("normal", 2, g6.sheets[2])],
    )
    assert not isinstance(result, str)

    # A confirmed-then-voided game with a monster winner (16 points) ...
    voided = Spec(
        date(2026, 5, 20),
        "normal",
        10,
        2,
        1,
        (2,),
        {
            1: {
                "settlements": 5,
                "cities": 4,
                "longest_road": 2,
                "largest_army": 2,
                "vp_cards": 3,
            },
            2: {"settlements": 3, "cities": 2},
        },
    )
    voided_game = await _play_game(conn, guild_id, voided, season_two)
    assert await games.void_game(conn, guild_id, voided_game.game_id, 9, "bogus") == "voided"

    # ... and a game nobody has confirmed yet.
    pending = await games.create_game(
        conn,
        guild_id,
        season_two,
        date(2026, 5, 21),
        4,
        4,
        [1],
        scores=[
            _sheet("normal", 4, {"settlements": 5, "cities": 4, "longest_road": 2, "vp_cards": 3}),
            _sheet("normal", 1, {"settlements": 3}),
        ],
    )

    records = await analytics.list_participations(conn, guild_id)
    return Seed(ids, season_one, season_two, voided_game.game_id, pending.game_id, records)


# --------------------------------------------------------------------------- helpers


def _rs(games_: int, wins: int) -> RecordSplit:
    return RecordSplit(games_, wins, F(wins, games_) if games_ else None)


def _award(
    key: str, opportunities: int, held: int, wins_held: int, without: int, wins_without: int
) -> AwardStat:
    assert held + without == opportunities
    return AwardStat(
        key,
        opportunities,
        held,
        F(held, opportunities) if opportunities else None,
        wins_held,
        F(wins_held, held) if held else None,
        without,
        wins_without,
        F(wins_without, without) if without else None,
    )


def _check(obj: object, **expected: object) -> None:
    for name, value in expected.items():
        actual = getattr(obj, name)
        assert actual == value, f"{name}: expected {value!r}, got {actual!r}"


def _labels(seed: Seed, records: list[ParticipationRecord]) -> list[str]:
    """Game labels in record order, one entry per game (blocks collapsed)."""
    result: list[str] = []
    for record in records:
        label = seed.label_of(record.game_id)
        if not result or result[-1] != label:
            result.append(label)
    return result


# --------------------------------------------------------------------------- repo shape


async def test_records_are_chronological_grouped_by_game_and_exclude_unconfirmed_and_inactive(
    seed: Seed,
) -> None:
    records = seed.records
    assert len(records) == 28  # 3+4+3+3+5+3+2+3+2 active participations

    # Chronological, and each game is ONE contiguous block (the engine's `_groups`
    # assumption): collapsing adjacent duplicates must not repeat a game.
    collapsed = _labels(seed, records)
    assert collapsed == CHRONOLOGICAL
    assert len(set(collapsed)) == len(collapsed)

    # Within a game, rows are user-id ascending.
    for label in CHRONOLOGICAL:
        users = [r.user_id for r in records if r.game_id == seed.ids[label]]
        assert users == sorted(users)

    # Exactly one winner per game, player_count is the active roster size.
    expected_roster = {
        "G1": [1, 2, 3],
        "G2": [1, 2, 3, 4],
        "G3": [1, 3, 5],
        "G4": [1, 2, 4],
        "G5": [1, 2, 3, 4, 5],
        "G7": [2, 3, 5],
        "G6": [1, 2],  # player 3 was deactivated
        "G8": [1, 3, 5],
        "G9": [2, 3],
    }
    expected_winner = {
        "G1": 1,
        "G2": 2,
        "G3": 3,
        "G4": 1,
        "G5": 4,
        "G7": 2,
        "G6": 1,
        "G8": 3,
        "G9": 2,
    }
    for label, roster in expected_roster.items():
        rows = [r for r in records if r.game_id == seed.ids[label]]
        assert [r.user_id for r in rows] == roster
        assert {r.player_count for r in rows} == {len(roster)}
        assert [r.user_id for r in rows if r.is_winner] == [expected_winner[label]]
        assert {r.game_type for r in rows} == {_SPECS[label].game_type}
        assert {r.target_points for r in rows} == {_SPECS[label].target}
        assert {r.extension_5_6 for r in rows} == {label == "G5"}

    # Voided / pending games and the deactivated participant never appear.
    seen_games = {r.game_id for r in records}
    assert seed.voided_id not in seen_games
    assert seed.pending_id not in seen_games
    assert (seed.ids["G6"], 3) not in {(r.game_id, r.user_id) for r in records}

    # Unscored rows carry (None, None); scored rows carry a full dict.
    by_key = {(seed.label_of(r.game_id), r.user_id): r for r in records}
    for key in [("G2", 4), ("G5", 3), ("G5", 5), ("G7", 2), ("G7", 3), ("G7", 5)]:
        assert (by_key[key].total_points, by_key[key].breakdown) == (None, None), key
    assert by_key[("G1", 1)].total_points == 10
    assert by_key[("G1", 1)].breakdown == {
        "settlements": 4,
        "cities": 4,
        "longest_road": 2,
        "largest_army": 0,
        "vp_cards": 0,
    }
    # The roster update kept the scores of the remaining players.
    assert by_key[("G6", 1)].total_points == 10
    assert by_key[("G6", 2)].total_points == 9
    # played_at: G7 unknown, G6 timed on the same day.
    assert by_key[("G7", 2)].played_at is None
    assert by_key[("G6", 1)].played_at == G6_PLAYED_AT


# --------------------------------------------------------------------------- players


async def test_player_one_summary(seed: Seed) -> None:
    s = player_summary(seed.records, 1)
    # G1 W10, G2 L8, G3 L9, G4 W13, G5 L8, G6 W10, G8 L12  (chronological)
    _check(
        s,
        user_id=1,
        games=7,
        wins=3,
        win_rate=F(3, 7),
        scored_games=7,
        scored_wins=3,
        scored_losses=4,
        avg_points=F(70, 7),  # 10
        avg_points_in_wins=F(33, 3),  # 10 + 13 + 10
        avg_points_in_losses=F(37, 4),  # 8 + 9 + 8 + 12
        median_points=F(10),  # 8 8 9 [10] 10 12 13
        best_points=13,
        avg_target_share=(F(1) + F(4, 5) + F(3, 4) + F(1) + F(4, 5) + F(1) + F(12, 13)) / 7,
        target_share_samples=7,
        avg_settlements=F(26, 7),  # 4 4 4 2 3 5 4
        avg_cities=F(22, 14),  # city POINTS 4 2 2 6 2 2 4 -> count = points / 2
        avg_vp_cards=F(2, 5),  # only normal/seafarers games: 0 0 0 1 1
        avg_metropolises=F(2, 4),  # C&K games only: 2 and 0 points -> count = points / 2
        avg_win_margin=F(2),  # (10-8) (13-10) (10-9)
        win_margin_samples=3,
        avg_loss_deficit=F(2),  # (10-8) (12-9) (10-8) (13-12)
        loss_deficit_samples=4,
        close_losses=3,  # deficits 2, 3, 2, 1 -> three within 1..2
        current_streak=-1,
        longest_win_streak=1,
        recent_form=_rs(7, 3),
    )
    assert s.source_averages == {
        "settlements": F(26, 7),
        "cities": F(22, 7),
        "longest_road": F(6, 6),
        "largest_army": F(6, 5),
        "vp_cards": F(2, 5),
        "longest_trade_route": F(0),
        "scenario_points": F(3),
        "metropolis_bonus": F(1),
        "defender_of_catan": F(1, 2),
        "merchant": F(1, 2),
        "constitution": F(1, 2),
        "printer": F(0),
    }
    assert s.source_samples == {
        "settlements": 7,
        "cities": 7,
        "longest_road": 6,  # normal + C&K games (G1 G2 G4 G5 G6 G8)
        "largest_army": 5,  # normal + seafarers (G1 G2 G3 G5 G6)
        "vp_cards": 5,
        "longest_trade_route": 1,  # seafarers G3 only
        "scenario_points": 1,
        "metropolis_bonus": 2,
        "defender_of_catan": 2,
        "merchant": 2,
        "constitution": 2,
        "printer": 2,
    }
    assert set(s.source_samples) == set(s.source_averages)
    assert s.awards == {
        "longest_road": _award("longest_road", 6, 3, 2, 3, 1),
        "longest_trade_route": _award("longest_trade_route", 1, 0, 0, 1, 0),
        "largest_army": _award("largest_army", 5, 3, 1, 2, 1),
        "merchant": _award("merchant", 2, 1, 1, 1, 0),
        "defender_of_catan": _award("defender_of_catan", 2, 1, 0, 1, 1),
        "constitution": _award("constitution", 2, 1, 0, 1, 1),
        "printer": _award("printer", 2, 0, 0, 2, 1),
    }
    # Spot-check the headline award numbers as literal fractions.
    road = s.awards["longest_road"]
    assert (road.held_rate, road.win_rate_when_held, road.win_rate_without) == (
        F(1, 2),
        F(2, 3),
        F(1, 3),
    )
    army = s.awards["largest_army"]
    assert (army.held_rate, army.win_rate_when_held, army.win_rate_without) == (
        F(3, 5),
        F(1, 3),
        F(1, 2),
    )
    assert s.by_player_count == {
        2: _rs(1, 1),  # G6 after the roster update
        3: _rs(4, 2),  # G1 G3 G4 G8
        4: _rs(1, 0),
        5: _rs(1, 0),
    }
    assert s.by_game_type == {
        "cities_knights": _rs(2, 1),
        "normal": _rs(4, 2),
        "seafarers": _rs(1, 0),
    }


async def test_player_two_summary_orders_same_day_games_by_known_time(seed: Seed) -> None:
    s = player_summary(seed.records, 2)
    # G1 L8, G2 W10, G4 L10, G5 L6, G7 W(unscored), G6 L9, G9 W10.
    # G7 (unknown time) must precede G6 on 04-22, so the streak at the end is +1:
    # if the repo ordered G6 first, the last two games would both be wins.
    _check(
        s,
        games=7,
        wins=3,
        win_rate=F(3, 7),
        scored_games=6,
        scored_wins=2,  # the G7 win has no score
        scored_losses=4,
        avg_points=F(53, 6),  # 8 10 10 6 9 10
        avg_points_in_wins=F(10),  # G2 and G9; the unscored G7 win is not a sample
        avg_points_in_losses=F(33, 4),  # 8 10 6 9
        median_points=F(19, 2),  # 6 8 9 10 10 10 -> (9 + 10) / 2
        best_points=10,
        avg_target_share=(F(8, 10) + F(1) + F(10, 13) + F(6, 10) + F(9, 10) + F(1)) / 6,
        target_share_samples=6,
        avg_settlements=F(19, 6),
        avg_cities=F(20, 12),
        avg_vp_cards=F(3, 5),
        avg_metropolises=F(0),  # one C&K game with 0 metropolis points: 0, not None
        avg_win_margin=F(3, 2),  # G2: 10-8, G9: 10-9; G7 has no score so no margin
        win_margin_samples=2,
        avg_loss_deficit=F(5, 2),  # (10-8) (13-10) (10-6) (10-9)
        loss_deficit_samples=4,
        close_losses=2,  # deficits 2 and 1
        current_streak=1,
        longest_win_streak=1,
        recent_form=_rs(7, 3),
    )
    assert s.source_samples == {
        "settlements": 6,
        "cities": 6,
        "longest_road": 6,
        "largest_army": 5,
        "vp_cards": 5,
        "metropolis_bonus": 1,
        "defender_of_catan": 1,
        "merchant": 1,
        "constitution": 1,
        "printer": 1,
    }
    assert s.source_averages == {
        "settlements": F(19, 6),
        "cities": F(20, 6),
        "longest_road": F(1),
        "largest_army": F(2, 5),
        "vp_cards": F(3, 5),
        "metropolis_bonus": F(0),
        "defender_of_catan": F(2),
        "merchant": F(0),
        "constitution": F(1),
        "printer": F(0),
    }
    # The only seafarers game of player 2 (G7) is unscored: no trade-route stat at all.
    assert "longest_trade_route" not in s.awards
    assert s.awards == {
        "longest_road": _award("longest_road", 6, 3, 2, 3, 0),
        "largest_army": _award("largest_army", 5, 1, 0, 4, 2),
        "merchant": _award("merchant", 1, 0, 0, 1, 0),
        "defender_of_catan": _award("defender_of_catan", 1, 1, 0, 0, 0),
        "constitution": _award("constitution", 1, 1, 0, 0, 0),
        "printer": _award("printer", 1, 0, 0, 1, 0),
    }
    assert s.by_player_count == {
        2: _rs(2, 1),  # G6 L, G9 W
        3: _rs(3, 1),  # G1 L, G4 L, G7 W
        4: _rs(1, 1),
        5: _rs(1, 0),
    }
    assert s.by_game_type == {
        "cities_knights": _rs(1, 0),
        "normal": _rs(5, 2),
        "seafarers": _rs(1, 1),
    }


async def test_player_three_summary_ignores_the_deactivated_game(seed: Seed) -> None:
    s = player_summary(seed.records, 3)
    # G1 L6, G2 L5, G3 W12, G5 L(unscored), G7 L(unscored), G8 W13, G9 L9.
    # G6 (deactivated roster row, 5 points) must not exist.
    _check(
        s,
        games=7,
        wins=2,
        win_rate=F(2, 7),
        scored_games=5,
        scored_wins=2,
        scored_losses=3,
        avg_points=F(9),  # 6 5 12 13 9 = 45
        avg_points_in_wins=F(25, 2),
        avg_points_in_losses=F(20, 3),
        median_points=F(9),
        best_points=13,
        avg_target_share=(F(6, 10) + F(5, 10) + F(1) + F(1) + F(9, 10)) / 5,
        target_share_samples=5,
        avg_settlements=F(17, 5),
        avg_cities=F(16, 10),
        avg_vp_cards=F(0),
        avg_metropolises=F(1),  # G8: 2 points -> 1 metropolis
        avg_win_margin=F(3, 2),  # G3: 12-10, G8: 13-12
        win_margin_samples=2,
        avg_loss_deficit=F(10, 3),  # G1: 4, G2: 5, G9: 1 (unscored losses excluded)
        loss_deficit_samples=3,
        close_losses=1,
        current_streak=-1,
        longest_win_streak=1,
        recent_form=_rs(7, 2),
    )
    assert s.source_samples == {
        "settlements": 5,
        "cities": 5,
        "longest_road": 4,
        "largest_army": 4,
        "vp_cards": 4,
        "longest_trade_route": 1,
        "scenario_points": 1,
        "metropolis_bonus": 1,
        "defender_of_catan": 1,
        "merchant": 1,
        "constitution": 1,
        "printer": 1,
    }
    assert s.awards == {
        "longest_road": _award("longest_road", 4, 0, 0, 4, 1),
        "longest_trade_route": _award("longest_trade_route", 1, 1, 1, 0, 0),
        "largest_army": _award("largest_army", 4, 2, 1, 2, 0),
        "merchant": _award("merchant", 1, 1, 1, 0, 0),
        "defender_of_catan": _award("defender_of_catan", 1, 1, 1, 0, 0),
        "constitution": _award("constitution", 1, 0, 0, 1, 1),
        "printer": _award("printer", 1, 1, 1, 0, 0),
    }
    assert s.by_player_count == {
        2: _rs(1, 0),  # G9
        3: _rs(4, 2),  # G1 G3 G7 G8
        4: _rs(1, 0),
        5: _rs(1, 0),
    }
    assert s.by_game_type == {
        "cities_knights": _rs(1, 1),
        "normal": _rs(4, 0),
        "seafarers": _rs(2, 1),
    }


async def test_players_with_unscored_games_four_and_five(seed: Seed) -> None:
    four = player_summary(seed.records, 4)
    # G2 L(unscored), G4 L8, G5 W10 (5-player extension game)
    _check(
        four,
        games=3,
        wins=1,
        win_rate=F(1, 3),
        scored_games=2,
        scored_wins=1,
        scored_losses=1,
        avg_points=F(9),
        avg_points_in_wins=F(10),
        avg_points_in_losses=F(8),
        median_points=F(9),
        best_points=10,
        avg_target_share=(F(8, 13) + F(1)) / 2,
        target_share_samples=2,
        avg_settlements=F(3),
        avg_cities=F(8, 4),
        avg_vp_cards=F(0),  # G5 is a normal game with vp_cards recorded as 0
        avg_metropolises=F(0),
        avg_win_margin=F(2),  # G5: 10 - 8
        win_margin_samples=1,
        avg_loss_deficit=F(5),  # G4 only: 13 - 8; G2 own score missing
        loss_deficit_samples=1,
        close_losses=0,
        current_streak=1,
        longest_win_streak=1,
    )
    assert four.by_player_count == {3: _rs(1, 0), 4: _rs(1, 0), 5: _rs(1, 1)}

    five = player_summary(seed.records, 5)
    # G3 L10, G5 L(unscored), G7 L(unscored), G8 L5 -- never wins.
    _check(
        five,
        games=4,
        wins=0,
        win_rate=F(0),
        scored_games=2,
        scored_wins=0,
        scored_losses=2,
        avg_points=F(15, 2),
        avg_points_in_wins=None,
        avg_points_in_losses=F(15, 2),
        median_points=F(15, 2),
        best_points=10,
        avg_win_margin=None,
        win_margin_samples=0,
        avg_loss_deficit=F(5),  # (12-10) (13-5) = 2, 8
        loss_deficit_samples=2,
        close_losses=1,
        current_streak=-4,
        longest_win_streak=0,
        recent_form=_rs(4, 0),
    )
    assert five.source_samples["scenario_points"] == 1
    assert five.source_samples["settlements"] == 2


async def test_player_summaries_sort_and_match_individual_summaries(seed: Seed) -> None:
    summaries = player_summaries(seed.records)
    # games desc, then user id: 1,2,3 have 7; 5 has 4; 4 has 3. Player 9 never played.
    assert [(s.user_id, s.games) for s in summaries] == [(1, 7), (2, 7), (3, 7), (5, 4), (4, 3)]
    for summary in summaries:
        assert summary == player_summary(seed.records, summary.user_id)
    assert sum(s.games for s in summaries) == len(seed.records)
    # Unknown player: an empty summary rather than leakage from anyone else.
    ghost = player_summary(seed.records, 9)
    _check(ghost, games=0, wins=0, win_rate=None, avg_points=None, scored_games=0)


# --------------------------------------------------------------------------- meta


async def test_meta_summary_hand_computed(seed: Seed) -> None:
    m = meta_summary(seed.records)
    _check(
        m,
        games=9,
        scored_games=8,  # G7's winner has no score
        avg_winning_score=F(88, 8),  # 10 10 12 13 10 10 13 10
        winning_score_distribution={10: 5, 12: 1, 13: 2},
        avg_margin=F(14, 8),  # 2 2 2 3 2 1 1 1
        margin_distribution={1: 3, 2: 4, 3: 1},
        margin_samples=8,
        vp_share_samples=6,
        avg_vp_card_share_of_winning_score=F(3, 60),  # 0 .2 0 0 .1 0 over 6 wins
        winners_with_vp_cards=_rs(6, 2),
        winning_score_samples_by_month={"2026-03": 3, "2026-04": 3, "2026-05": 2},
        by_game_type={"normal": 5, "seafarers": 2, "cities_knights": 2},
        by_player_count={2: 2, 3: 5, 4: 1, 5: 1},
        # Mon=0: G1/G4/G8 are Sundays, G2/G5 Tuesdays, G3 Friday, G7/G6/G9 Wednesdays.
        games_by_weekday={6: 3, 1: 2, 4: 1, 2: 3},
        avg_winning_score_by_month={
            "2026-03": F(32, 3),  # 10 10 12
            "2026-04": F(11),  # 13 10 10 (G7 unscored)
            "2026-05": F(23, 2),  # 13 10
        },
    )
    assert m.win_award_combos == {"road_and_army": 1, "road_only": 4, "army_only": 1, "neither": 0}
    assert m.play_styles == {
        "city_heavy": _rs(8, 3),
        "settlement_heavy": _rs(9, 1),
        "balanced": _rs(5, 4),
    }
    assert sum(split.games for split in m.play_styles.values()) == 22  # every scored row

    # 8 winners with a breakdown; each source's mean over the games that have it.
    assert m.winner_composition == {
        "settlements": F(28, 8),
        "cities": F(32, 8),
        "longest_road": F(10, 7),
        "largest_army": F(4, 6),
        "vp_cards": F(3, 6),
        "longest_trade_route": F(2),
        "scenario_points": F(0),
        "metropolis_bonus": F(2),
        "defender_of_catan": F(1),
        "merchant": F(1),
        "constitution": F(0),
        "printer": F(1, 2),
    }
    assert m.winner_composition_samples == {
        "settlements": 8,
        "cities": 8,
        "longest_road": 7,
        "largest_army": 6,
        "vp_cards": 6,
        "longest_trade_route": 1,
        "scenario_points": 1,
        "metropolis_bonus": 2,
        "defender_of_catan": 2,
        "merchant": 2,
        "constitution": 2,
        "printer": 2,
    }
    # 14 losing rows with a breakdown (unscored losers and the deactivated player excluded).
    assert m.loser_composition == {
        "settlements": F(46, 14),
        "cities": F(40, 14),
        "longest_road": F(4, 12),
        "largest_army": F(8, 10),
        "vp_cards": F(3, 10),
        "longest_trade_route": F(0),
        "scenario_points": F(5, 2),
        "metropolis_bonus": F(0),
        "defender_of_catan": F(1),
        "merchant": F(0),
        "constitution": F(1, 2),
        "printer": F(1, 4),
    }
    assert m.loser_composition_samples == {
        "settlements": 14,
        "cities": 14,
        "longest_road": 12,
        "largest_army": 10,
        "vp_cards": 10,
        "longest_trade_route": 2,
        "scenario_points": 2,
        "metropolis_bonus": 4,
        "defender_of_catan": 4,
        "merchant": 4,
        "constitution": 4,
        "printer": 4,
    }
    # Aggregated awards over every scored participation.
    assert m.awards == {
        "constitution": _award("constitution", 6, 2, 0, 4, 2),
        "defender_of_catan": _award("defender_of_catan", 6, 4, 1, 2, 1),
        "largest_army": _award("largest_army", 16, 6, 2, 10, 4),
        "longest_road": _award("longest_road", 19, 7, 5, 12, 2),
        "longest_trade_route": _award("longest_trade_route", 3, 1, 1, 2, 0),
        "merchant": _award("merchant", 6, 2, 2, 4, 0),
        "printer": _award("printer", 6, 2, 1, 4, 1),
    }
    road = m.awards["longest_road"]
    assert (road.held_rate, road.win_rate_when_held, road.win_rate_without) == (
        F(7, 19),
        F(5, 7),
        F(1, 6),
    )
    army = m.awards["largest_army"]
    assert (army.held_rate, army.win_rate_when_held, army.win_rate_without) == (
        F(3, 8),
        F(1, 3),
        F(2, 5),
    )
    trade = m.awards["longest_trade_route"]
    assert (trade.held_rate, trade.win_rate_when_held, trade.win_rate_without) == (
        F(1, 3),
        F(1),
        F(0),
    )


# --------------------------------------------------------------------------- pairs/timeline


async def test_head_to_head_hand_computed(seed: Seed) -> None:
    expected = [
        HeadToHead(1, 2, 5, 3, 1),  # G1 G2 G4 G5 G6
        HeadToHead(1, 3, 5, 1, 2),  # G1 G2 G3 G5 G8 (NOT G6: 3 was deactivated)
        HeadToHead(1, 4, 3, 1, 1),  # G2 G4 G5
        HeadToHead(1, 5, 3, 0, 0),  # G3 G5 G8, won by others every time
        HeadToHead(2, 3, 5, 3, 0),  # G1 G2 G5 G7 G9
        HeadToHead(2, 4, 3, 1, 1),  # G2 G4 G5
        HeadToHead(2, 5, 2, 1, 0),  # G5 G7
        HeadToHead(3, 4, 2, 0, 1),  # G2 G5
        HeadToHead(3, 5, 4, 2, 0),  # G3 G5 G7 G8
        HeadToHead(4, 5, 1, 1, 0),  # G5
    ]
    assert head_to_head(seed.records) == expected
    # Every game contributes C(n, 2) pairs: 3+6+3+3+10+3+1+3+1.
    assert sum(h.games_together for h in expected) == 33


async def test_win_rate_timeline_follows_chronological_order(seed: Seed) -> None:
    timeline = win_rate_timeline(seed.records)
    assert sorted(timeline) == [1, 2, 3, 4, 5]
    assert timeline[1] == [
        (D_G1, F(1, 1)),
        (D_G2, F(1, 2)),
        (D_G3, F(1, 3)),
        (D_G4, F(2, 4)),
        (D_G5, F(2, 5)),
        (D_G67, F(3, 6)),  # G6
        (D_G8, F(3, 7)),
    ]
    # G7 (win, unknown time) before G6 (loss, timed) on the same day.
    assert timeline[2] == [
        (D_G1, F(0, 1)),
        (D_G2, F(1, 2)),
        (D_G4, F(1, 3)),
        (D_G5, F(1, 4)),
        (D_G67, F(2, 5)),  # G7 win
        (D_G67, F(2, 6)),  # G6 loss
        (D_G9, F(3, 7)),
    ]
    assert timeline[3] == [
        (D_G1, F(0, 1)),
        (D_G2, F(0, 2)),
        (D_G3, F(1, 3)),
        (D_G5, F(1, 4)),
        (D_G67, F(1, 5)),  # G7
        (D_G8, F(2, 6)),
        (D_G9, F(2, 7)),
    ]
    assert timeline[4] == [(D_G2, F(0, 1)), (D_G4, F(0, 2)), (D_G5, F(1, 3))]
    assert timeline[5] == [
        (D_G3, F(0, 1)),
        (D_G5, F(0, 2)),
        (D_G67, F(0, 3)),
        (D_G8, F(0, 4)),
    ]
    for points in timeline.values():
        dates = [d for d, _ in points]
        assert dates == sorted(dates)


# --------------------------------------------------------------------------- filters


async def test_season_one_filter_flows_through_every_stat(
    app_conn: asyncpg.Connection, guild_id: int, seed: Seed
) -> None:
    records = await analytics.list_participations(app_conn, guild_id, season_id=seed.season_one)
    assert _labels(seed, records) == ["G1", "G2", "G3"]
    assert len(records) == 10
    assert [r.player_count for r in records if r.game_id == seed.ids["G2"]] == [4, 4, 4, 4]

    m = meta_summary(records)
    _check(
        m,
        games=3,
        scored_games=3,
        avg_winning_score=F(32, 3),
        winning_score_distribution={10: 2, 12: 1},
        avg_margin=F(2),
        margin_distribution={2: 3},
        margin_samples=3,
        by_game_type={"normal": 2, "seafarers": 1},
        by_player_count={3: 2, 4: 1},
        avg_winning_score_by_month={"2026-03": F(32, 3)},
        winners_with_vp_cards=_rs(3, 1),  # only G2's winner holds vp cards
        vp_share_samples=3,
        avg_vp_card_share_of_winning_score=F(2, 30),
    )
    assert m.win_award_combos == {"road_and_army": 1, "road_only": 2, "army_only": 0, "neither": 0}

    summaries = player_summaries(records)
    assert [(s.user_id, s.games) for s in summaries] == [(1, 3), (3, 3), (2, 2), (4, 1), (5, 1)]
    one = player_summary(records, 1)
    _check(
        one,
        games=3,
        wins=1,
        scored_games=3,
        avg_points=F(9),
        avg_win_margin=F(2),
        win_margin_samples=1,
        avg_loss_deficit=F(5, 2),  # G2: 10-8, G3: 12-9
        loss_deficit_samples=2,
        current_streak=-2,
        longest_win_streak=1,
    )
    assert one.by_player_count == {3: _rs(2, 1), 4: _rs(1, 0)}
    four = player_summary(records, 4)
    _check(four, games=1, wins=0, scored_games=0, avg_points=None, avg_loss_deficit=None)
    assert head_to_head(records)[0] == HeadToHead(1, 2, 2, 1, 1)
    assert sum(h.games_together for h in head_to_head(records)) == 3 + 6 + 3
    assert sorted(win_rate_timeline(records)) == [1, 2, 3, 4, 5]


async def test_season_two_filter_excludes_season_one_voided_and_inactive(
    app_conn: asyncpg.Connection, guild_id: int, seed: Seed
) -> None:
    records = await analytics.list_participations(app_conn, guild_id, season_id=seed.season_two)
    assert _labels(seed, records) == ["G4", "G5", "G7", "G6", "G8", "G9"]
    m = meta_summary(records)
    _check(
        m,
        games=6,
        scored_games=5,
        avg_winning_score=F(56, 5),  # 13 10 10 13 10
        winning_score_distribution={10: 3, 13: 2},
        avg_margin=F(8, 5),  # 3 2 1 1 1
        margin_distribution={1: 3, 2: 1, 3: 1},
        by_game_type={"cities_knights": 2, "normal": 3, "seafarers": 1},
        by_player_count={2: 2, 3: 3, 5: 1},
    )
    one = player_summary(records, 1)
    _check(
        one,
        games=4,
        wins=2,
        avg_points=F(43, 4),  # 13 8 10 12
        best_points=13,
        current_streak=-1,
    )
    assert one.by_player_count == {2: _rs(1, 1), 3: _rs(2, 1), 5: _rs(1, 0)}
    assert player_summary(records, 3).games == 4  # G5 G7 G8 G9, never the voided/inactive rows


async def test_game_type_filters_flow_through(
    app_conn: asyncpg.Connection, guild_id: int, seed: Seed
) -> None:
    normal = await analytics.list_participations(app_conn, guild_id, game_type="normal")
    assert _labels(seed, normal) == ["G1", "G2", "G5", "G6", "G9"]
    one = player_summary(normal, 1)
    _check(one, games=4, wins=2, scored_games=4, avg_points=F(9))  # 10 8 8 10
    assert one.by_game_type == {"normal": _rs(4, 2)}
    assert one.by_player_count == {2: _rs(1, 1), 3: _rs(1, 1), 4: _rs(1, 0), 5: _rs(1, 0)}
    three = player_summary(normal, 3)
    _check(three, games=4, wins=0, scored_games=3, avg_points=F(20, 3))  # 6 5 9
    assert meta_summary(normal).by_game_type == {"normal": 5}

    ck = await analytics.list_participations(app_conn, guild_id, game_type="cities_knights")
    assert _labels(seed, ck) == ["G4", "G8"]
    assert head_to_head(ck) == [
        HeadToHead(1, 2, 1, 1, 0),
        HeadToHead(1, 3, 1, 0, 1),
        HeadToHead(1, 4, 1, 1, 0),
        HeadToHead(1, 5, 1, 0, 0),
        HeadToHead(2, 4, 1, 0, 0),
        HeadToHead(3, 5, 1, 1, 0),
    ]
    mck = meta_summary(ck)
    _check(
        mck,
        games=2,
        scored_games=2,
        avg_winning_score=F(13),
        avg_margin=F(2),  # 3 and 1
        winners_with_vp_cards=_rs(0, 0),
        avg_vp_card_share_of_winning_score=None,
        vp_share_samples=0,
    )
    assert mck.win_award_combos == {
        "road_and_army": 0,
        "road_only": 0,
        "army_only": 0,
        "neither": 0,
    }
    assert set(mck.awards) == {
        "longest_road",
        "merchant",
        "defender_of_catan",
        "constitution",
        "printer",
    }

    seafarers = await analytics.list_participations(app_conn, guild_id, game_type="seafarers")
    assert _labels(seed, seafarers) == ["G3", "G7"]
    ms = meta_summary(seafarers)
    _check(ms, games=2, scored_games=1, avg_winning_score=F(12), avg_margin=F(2), margin_samples=1)
    assert ms.win_award_combos["road_and_army"] == 1
    assert ms.awards["longest_trade_route"] == _award("longest_trade_route", 3, 1, 1, 2, 0)
    five = player_summary(seafarers, 5)
    _check(five, games=2, wins=0, scored_games=1, avg_points=F(10))

    empty = await analytics.list_participations(
        app_conn, guild_id, game_type="seafarers_cities_knights"
    )
    assert empty == []
    assert player_summaries(empty) == []
    assert head_to_head(empty) == []
    assert win_rate_timeline(empty) == {}
    me = meta_summary(empty)
    _check(me, games=0, scored_games=0, avg_winning_score=None, avg_margin=None)


async def test_season_and_game_type_filters_combine(
    app_conn: asyncpg.Connection, guild_id: int, seed: Seed
) -> None:
    both = await analytics.list_participations(
        app_conn, guild_id, season_id=seed.season_two, game_type="normal"
    )
    assert _labels(seed, both) == ["G5", "G6", "G9"]
    m = meta_summary(both)
    _check(m, games=3, scored_games=3, avg_winning_score=F(10), avg_margin=F(4, 3))  # 2 1 1
    # G5 (1 2 3 4 5), G6 (1 2), G9 (2 3).
    assert [(s.user_id, s.games) for s in player_summaries(both)] == [
        (2, 3),
        (1, 2),
        (3, 2),
        (4, 1),
        (5, 1),
    ]

    # Season two seafarers is only G7, which has no scores anywhere.
    unscored = await analytics.list_participations(
        app_conn, guild_id, season_id=seed.season_two, game_type="seafarers"
    )
    assert _labels(seed, unscored) == ["G7"]
    mu = meta_summary(unscored)
    _check(
        mu,
        games=1,
        scored_games=0,
        avg_winning_score=None,
        avg_margin=None,
        margin_samples=0,
        winning_score_distribution={},
        winner_composition={},
        awards={},
    )
    two = player_summary(unscored, 2)
    _check(
        two,
        games=1,
        wins=1,
        scored_games=0,
        avg_points=None,
        avg_win_margin=None,
        win_margin_samples=0,
        source_averages={},
        source_samples={},
        awards={},
    )


# --------------------------------------------------------------------------- exclusions


async def test_voided_pending_and_inactive_data_never_reaches_any_stat(seed: Seed) -> None:
    records = seed.records
    m = meta_summary(records)

    # The voided game's 16-point winner (and its game id) leaves no trace.
    assert 16 not in m.winning_score_distribution
    assert max(s.best_points or 0 for s in player_summaries(records)) == 13
    assert m.games == 9
    assert sum(m.by_game_type.values()) == 9
    assert "2026-05" in m.avg_winning_score_by_month
    assert m.avg_winning_score_by_month["2026-05"] == F(23, 2)  # not diluted by the void

    # Player 1 has exactly 7 games: neither the voided nor the pending game counts.
    assert player_summary(records, 1).games == 7
    # Player 4 would have 4/5 games if the pending (4,1) game leaked in.
    assert player_summary(records, 4).games == 3
    # Player 3: no 8th game from the deactivated G6 row; the 5-point row is absent.
    assert player_summary(records, 3).games == 7
    assert player_summary(records, 3).by_game_type["normal"] == _rs(4, 0)
    # Player 2's voided-game loss (5 points) would change this average: it stays 53/6.
    assert player_summary(records, 2).avg_points == F(53, 6)
    # No pair counts a game from the voided/pending rosters.
    pair = {(h.player_a, h.player_b): h for h in head_to_head(records)}
    assert pair[(1, 2)].games_together == 5
    assert pair[(1, 4)].games_together == 3
    assert pair[(1, 3)].games_together == 5
    # Winners with vp cards: the voided winner had 3 vp cards; still 2 of 6.
    assert m.winners_with_vp_cards == _rs(6, 2)
    # Award counts: the voided winner's road+army would add a held count each.
    assert m.awards["longest_road"].held == 7
    assert m.awards["largest_army"].held == 6
