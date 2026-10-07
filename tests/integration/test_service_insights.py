"""Integration tests for `catan_bot.services.insights_service`.

Insights never mix game types: every view covers exactly one type, the
caller's choice or else the most-played one in the scope.

Seed (guild A; "-" = no season). Winner first, then losers:

    P1  2026-03-01  normal     -   1 beats 2, 3
    P2  2026-03-05  normal     -   1 beats 2
    S1  2026-04-01  normal     S   2 beats 1
    S2  2026-04-10  seafarers  S   3 beats 1, 2

`S` is the active season, created after P1/P2 were played. All-time counts
are normal 3 / seafarers 1 (normal is picked); the season's are normal 1 /
seafarers 1 (a tie, so catalog order picks normal).
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime

import asyncpg
import pytest

from catan_bot.db.repositories import analytics as analytics_repo
from catan_bot.db.repositories import games, players, seasons
from catan_bot.domain import analytics as domain_analytics
from catan_bot.services import insights_service
from catan_bot.services.results import (
    ChartInsightsView,
    HeadToHeadView,
    InsightsFilter,
    MetaInsightsView,
    OpponentRecord,
    PlayerInsightsView,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

INVALID_SCOPE_MESSAGE = "invalid insights scope"
INVALID_GAME_TYPE_MESSAGE = "invalid insights game type"

ALL_TIME_COUNTS = {"normal": 3, "seafarers": 1}
SEASON_COUNTS = {"normal": 1, "seafarers": 1}


async def _play(
    conn: asyncpg.Connection,
    guild_id: int,
    *,
    played_on: date,
    winner: int,
    losers: list[int],
    season_id: int | None = None,
    game_type: str = "normal",
) -> None:
    await players.ensure_players(conn, guild_id, [winner, *losers])
    game = await games.create_game(
        conn,
        guild_id,
        season_id,
        played_on,
        winner,
        winner,
        losers,
        game_type=game_type,
    )
    assert await games.confirm_game(conn, guild_id, game.game_id, losers[0]) == "confirmed"


async def _start_season(conn: asyncpg.Connection, guild_id: int) -> int:
    season = await seasons.create_season(
        conn,
        guild_id,
        "Spring <b>",
        date(2026, 1, 1),
        date(2026, 12, 31),
        datetime(2027, 1, 1, tzinfo=UTC),
        1,
        1,
    )
    return season.season_id


async def _seed_prior_games(conn: asyncpg.Connection, guild_id: int) -> None:
    await _play(conn, guild_id, played_on=date(2026, 3, 1), winner=1, losers=[2, 3])
    await _play(conn, guild_id, played_on=date(2026, 3, 5), winner=1, losers=[2])


async def _seed_season_games(conn: asyncpg.Connection, guild_id: int, season_id: int) -> None:
    await _play(
        conn, guild_id, played_on=date(2026, 4, 1), winner=2, losers=[1], season_id=season_id
    )
    await _play(
        conn,
        guild_id,
        played_on=date(2026, 4, 10),
        winner=3,
        losers=[1, 2],
        season_id=season_id,
        game_type="seafarers",
    )


@pytest.fixture
async def seeded(app_conn: asyncpg.Connection, guild_id: int) -> int:
    """The full seed above; returns the active season's id."""
    await _seed_prior_games(app_conn, guild_id)
    season_id = await _start_season(app_conn, guild_id)
    await _seed_season_games(app_conn, guild_id, season_id)
    return season_id


@pytest.fixture
async def games_without_season(app_conn: asyncpg.Connection, guild_id: int) -> None:
    """Confirmed (normal) games exist, but no season was ever started."""
    await _seed_prior_games(app_conn, guild_id)


# --- scope ----------------------------------------------------------------


async def test_defaults_are_all_time_and_the_most_played_game_type(
    pool: asyncpg.Pool, guild_id: int, seeded: int
) -> None:
    view = await insights_service.player_insights(pool, guild_id, 1)

    assert isinstance(view, PlayerInsightsView)
    assert view.filter == InsightsFilter(
        scope="all_time",
        season=None,
        game_type="normal",
        available_game_types=ALL_TIME_COUNTS,
        game_type_defaulted=True,
    )
    assert view.summary.user_id == 1
    # Normal games only: P1, P2 (won) and S1 (lost). The seafarers game is not mixed in.
    assert (view.summary.games, view.summary.wins) == (3, 2)


async def test_season_scope_only_counts_active_season_games(
    pool: asyncpg.Pool, guild_id: int, seeded: int
) -> None:
    view = await insights_service.player_insights(pool, guild_id, 1, scope="season")

    assert view.filter.scope == "season"
    assert view.filter.season is not None
    assert view.filter.season.season_id == seeded
    assert view.filter.season.name == "Spring <b>"
    assert view.filter.available_game_types == SEASON_COUNTS
    # 1 normal vs 1 seafarers is a tie: catalog order picks normal.
    assert view.filter.game_type == "normal"
    assert view.filter.game_type_defaulted is True
    assert (view.summary.games, view.summary.wins) == (1, 0)


async def test_all_time_scope_ignores_the_active_season(
    pool: asyncpg.Pool, guild_id: int, seeded: int
) -> None:
    view = await insights_service.meta_insights(pool, guild_id, scope="all_time")

    assert view.filter.season is None
    assert view.meta.games == 3


async def test_season_scope_without_active_season_is_an_empty_view(
    pool: asyncpg.Pool, guild_id: int, games_without_season: None
) -> None:
    expected = InsightsFilter(
        scope="season",
        season=None,
        game_type=None,
        available_game_types={},
        game_type_defaulted=False,
    )

    player = await insights_service.player_insights(pool, guild_id, 1, scope="season")
    meta = await insights_service.meta_insights(pool, guild_id, scope="season")
    h2h = await insights_service.head_to_head_insights(pool, guild_id, 1, scope="season")
    chart = await insights_service.chart_insights(pool, guild_id, scope="season")

    assert player.filter == meta.filter == h2h.filter == chart.filter == expected
    assert player.summary.games == 0
    assert meta.meta.games == 0
    assert meta.players == []
    assert h2h.opponents == []
    # The same guild's all-time data is untouched by the empty season view.
    all_time = await insights_service.meta_insights(pool, guild_id)
    assert all_time.meta.games == 2
    assert all_time.filter.game_type == "normal"


async def test_season_scope_without_active_season_keeps_an_explicit_game_type(
    pool: asyncpg.Pool, guild_id: int, games_without_season: None
) -> None:
    view = await insights_service.meta_insights(
        pool, guild_id, scope="season", game_type="seafarers"
    )

    assert view.filter == InsightsFilter("season", None, "seafarers", {}, False)
    assert view.meta.games == 0


async def test_season_scope_ignores_a_cancelled_season(
    pool: asyncpg.Pool, app_conn: asyncpg.Connection, guild_id: int
) -> None:
    season_id = await _start_season(app_conn, guild_id)
    await _seed_season_games(app_conn, guild_id, season_id)
    assert await seasons.cancel_active_season(app_conn, guild_id) is not None

    view = await insights_service.meta_insights(pool, guild_id, scope="season")

    assert view.filter.season is None
    assert view.filter.game_type is None
    assert view.meta.games == 0


# --- game type selection --------------------------------------------------


async def test_explicit_game_type_is_used_and_not_marked_as_defaulted(
    pool: asyncpg.Pool, guild_id: int, seeded: int
) -> None:
    normal = await insights_service.meta_insights(pool, guild_id, game_type="normal")
    seafarers = await insights_service.meta_insights(pool, guild_id, game_type="seafarers")

    assert normal.filter == InsightsFilter("all_time", None, "normal", ALL_TIME_COUNTS, False)
    assert normal.meta.games == 3
    # Picking a less-played type still reports every type's counts.
    assert seafarers.filter == InsightsFilter("all_time", None, "seafarers", ALL_TIME_COUNTS, False)
    assert seafarers.meta.games == 1


async def test_explicit_game_type_with_no_games_is_an_empty_view_with_counts(
    pool: asyncpg.Pool, guild_id: int, seeded: int
) -> None:
    meta = await insights_service.meta_insights(pool, guild_id, game_type="cities_knights")
    player = await insights_service.player_insights(pool, guild_id, 1, game_type="cities_knights")
    h2h = await insights_service.head_to_head_insights(
        pool, guild_id, 1, game_type="cities_knights"
    )

    expected = InsightsFilter("all_time", None, "cities_knights", ALL_TIME_COUNTS, False)
    assert meta.filter == player.filter == h2h.filter == expected
    assert meta.meta.games == 0
    assert meta.players == []
    assert player.summary.games == 0
    assert h2h.opponents == []


async def test_the_most_played_type_wins_even_when_it_is_not_normal(
    pool: asyncpg.Pool, app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await _play(
        app_conn,
        guild_id,
        played_on=date(2026, 3, 1),
        winner=1,
        losers=[2],
        game_type="cities_knights",
    )
    await _play(
        app_conn,
        guild_id,
        played_on=date(2026, 3, 2),
        winner=2,
        losers=[1],
        game_type="cities_knights",
    )
    await _play(app_conn, guild_id, played_on=date(2026, 3, 3), winner=1, losers=[2])

    view = await insights_service.meta_insights(pool, guild_id)

    assert view.filter.game_type == "cities_knights"
    assert view.filter.game_type_defaulted is True
    assert view.filter.available_game_types == {"normal": 1, "cities_knights": 2}
    assert view.meta.games == 2
    assert view.meta.by_game_type == {"cities_knights": 2}


async def test_counts_are_distinct_games_not_participations(
    pool: asyncpg.Pool, app_conn: asyncpg.Connection, guild_id: int
) -> None:
    # One 4-player seafarers game (4 participation rows) vs two 2-player normal games.
    await _play(
        app_conn,
        guild_id,
        played_on=date(2026, 3, 1),
        winner=1,
        losers=[2, 3, 4],
        game_type="seafarers",
    )
    await _play(app_conn, guild_id, played_on=date(2026, 3, 2), winner=1, losers=[2])
    await _play(app_conn, guild_id, played_on=date(2026, 3, 3), winner=2, losers=[1])

    view = await insights_service.meta_insights(pool, guild_id)

    assert view.filter.available_game_types == {"normal": 2, "seafarers": 1}
    assert view.filter.game_type == "normal"


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("seafarers", "cities_knights", "seafarers"),
        ("cities_knights", "seafarers_cities_knights", "cities_knights"),
        ("seafarers_cities_knights", "normal", "normal"),
    ],
)
async def test_ties_break_in_catalog_order(
    pool: asyncpg.Pool,
    app_conn: asyncpg.Connection,
    guild_id: int,
    first: str,
    second: str,
    expected: str,
) -> None:
    # Insert the later-in-catalog type first so row order cannot decide the tie.
    for day, game_type in ((1, second), (2, first)):
        await _play(
            app_conn,
            guild_id,
            played_on=date(2026, 3, day),
            winner=1,
            losers=[2],
            game_type=game_type,
        )

    view = await insights_service.meta_insights(pool, guild_id)

    assert view.filter.game_type == expected
    assert view.filter.game_type_defaulted is True
    assert view.meta.games == 1


async def test_a_view_with_no_games_has_no_game_type(pool: asyncpg.Pool, guild_id: int) -> None:
    view = await insights_service.meta_insights(pool, guild_id)

    assert view.filter == InsightsFilter("all_time", None, None, {}, False)
    assert view.meta.games == 0
    assert view.players == []


async def test_game_type_and_season_combine(pool: asyncpg.Pool, guild_id: int, seeded: int) -> None:
    normal = await insights_service.meta_insights(
        pool, guild_id, scope="season", game_type="normal"
    )
    seafarers = await insights_service.meta_insights(
        pool, guild_id, scope="season", game_type="seafarers"
    )
    cities = await insights_service.meta_insights(
        pool, guild_id, scope="season", game_type="cities_knights"
    )

    assert normal.meta.games == 1  # S1 only; P1/P2 are outside the season
    assert seafarers.meta.games == 1
    assert cities.meta.games == 0
    assert normal.filter.available_game_types == SEASON_COUNTS
    assert normal.filter.game_type_defaulted is False


async def test_explicit_game_type_passes_through_every_view(
    pool: asyncpg.Pool, guild_id: int, seeded: int
) -> None:
    player = await insights_service.player_insights(pool, guild_id, 1, game_type="seafarers")
    assert player.filter.game_type == "seafarers"
    assert (player.summary.games, player.summary.wins) == (1, 0)

    h2h = await insights_service.head_to_head_insights(pool, guild_id, 1, game_type="seafarers")
    assert h2h.filter.game_type == "seafarers"
    assert [o.opponent_id for o in h2h.opponents] == [2, 3]


# --- player view ----------------------------------------------------------


async def test_player_view_for_a_member_without_games(
    pool: asyncpg.Pool, guild_id: int, seeded: int
) -> None:
    view = await insights_service.player_insights(pool, guild_id, 99)

    assert view.summary.user_id == 99
    assert (view.summary.games, view.summary.wins) == (0, 0)
    assert view.summary.win_rate is None
    assert view.filter.game_type == "normal"  # the guild's type is still resolved


async def test_player_view_with_games(pool: asyncpg.Pool, guild_id: int, seeded: int) -> None:
    default = await insights_service.player_insights(pool, guild_id, 3)
    seafarers = await insights_service.player_insights(pool, guild_id, 3, game_type="seafarers")

    assert (default.summary.games, default.summary.wins) == (1, 0)  # normal: P1 only
    assert (seafarers.summary.games, seafarers.summary.wins) == (1, 1)


# --- meta view ------------------------------------------------------------


async def test_meta_view_lists_every_player_of_the_chosen_type_in_summary_order(
    pool: asyncpg.Pool, guild_id: int, seeded: int
) -> None:
    view = await insights_service.meta_insights(pool, guild_id)
    seafarers = await insights_service.meta_insights(pool, guild_id, game_type="seafarers")

    assert isinstance(view, MetaInsightsView)
    assert view.meta.games == 3
    assert view.meta.scored_games == 0
    assert view.meta.by_game_type == {"normal": 3}
    # games desc, user id asc: players 1 and 2 played 3 normal games, player 3 played 1.
    assert [(p.user_id, p.games) for p in view.players] == [(1, 3), (2, 3), (3, 1)]
    assert [(p.user_id, p.games) for p in seafarers.players] == [(1, 1), (2, 1), (3, 1)]


# --- head to head ---------------------------------------------------------


async def test_head_to_head_is_normalized_to_the_subjects_perspective(
    pool: asyncpg.Pool, guild_id: int, seeded: int
) -> None:
    one = await insights_service.head_to_head_insights(pool, guild_id, 1)
    two = await insights_service.head_to_head_insights(pool, guild_id, 2)

    assert isinstance(one, HeadToHeadView)
    assert one.user_id == 1
    assert one.opponents == [
        OpponentRecord(opponent_id=2, games_together=3, wins=2, opponent_wins=1),
        OpponentRecord(opponent_id=3, games_together=1, wins=1, opponent_wins=0),
    ]
    # Player 2 is the larger id against 1 and the smaller against 3, so both
    # orientations of the stored pair are exercised.
    assert two.opponents == [
        OpponentRecord(opponent_id=1, games_together=3, wins=1, opponent_wins=2),
        OpponentRecord(opponent_id=3, games_together=1, wins=0, opponent_wins=0),
    ]


async def test_head_to_head_normalization_in_a_second_game_type(
    pool: asyncpg.Pool, guild_id: int, seeded: int
) -> None:
    two = await insights_service.head_to_head_insights(pool, guild_id, 2, game_type="seafarers")
    three = await insights_service.head_to_head_insights(pool, guild_id, 3, game_type="seafarers")

    assert two.opponents == [
        OpponentRecord(opponent_id=1, games_together=1, wins=0, opponent_wins=0),
        OpponentRecord(opponent_id=3, games_together=1, wins=0, opponent_wins=1),
    ]
    assert three.opponents == [
        OpponentRecord(opponent_id=1, games_together=1, wins=1, opponent_wins=0),
        OpponentRecord(opponent_id=2, games_together=1, wins=1, opponent_wins=0),
    ]


async def test_head_to_head_sorts_by_games_then_opponent_id(
    pool: asyncpg.Pool, app_conn: asyncpg.Connection, guild_id: int
) -> None:
    # Subject 5 plays 4 (vs 9) , 2 (vs 3), 2 (vs 7): 9 first, then 3 before 7 on the tie.
    for day, losers in ((1, [9]), (2, [9]), (3, [9, 7]), (4, [9, 7]), (5, [3]), (6, [3])):
        await _play(app_conn, guild_id, played_on=date(2026, 3, day), winner=5, losers=losers)

    view = await insights_service.head_to_head_insights(pool, guild_id, 5)

    assert [(o.opponent_id, o.games_together) for o in view.opponents] == [
        (9, 4),
        (3, 2),
        (7, 2),
    ]


async def test_head_to_head_for_a_member_without_games(
    pool: asyncpg.Pool, guild_id: int, seeded: int
) -> None:
    view = await insights_service.head_to_head_insights(pool, guild_id, 99)

    assert view.user_id == 99
    assert view.opponents == []


async def test_head_to_head_respects_season_scope(
    pool: asyncpg.Pool, guild_id: int, seeded: int
) -> None:
    default = await insights_service.head_to_head_insights(pool, guild_id, 1, scope="season")
    seafarers = await insights_service.head_to_head_insights(
        pool, guild_id, 1, scope="season", game_type="seafarers"
    )

    # Season default is normal (tie broken by catalog order): only S1, 2 beat 1.
    assert default.opponents == [
        OpponentRecord(opponent_id=2, games_together=1, wins=0, opponent_wins=1),
    ]
    assert seafarers.opponents == [
        OpponentRecord(opponent_id=2, games_together=1, wins=0, opponent_wins=0),
        OpponentRecord(opponent_id=3, games_together=1, wins=0, opponent_wins=1),
    ]


# --- chart view -----------------------------------------------------------


async def _expected_chart_parts(
    pool: asyncpg.Pool, guild_id: int, *, season_id: int | None = None, game_type: str
) -> tuple[object, ...]:
    async with pool.acquire() as conn:
        records = await analytics_repo.list_participations(
            conn, guild_id, season_id=season_id, game_type=game_type
        )
    return (
        domain_analytics.meta_summary(records),
        domain_analytics.player_summaries(records),
        domain_analytics.head_to_head(records),
        domain_analytics.win_rate_timeline(records),
    )


def _chart_parts(view: ChartInsightsView) -> tuple[object, ...]:
    return view.meta, view.players, view.head_to_head, view.timeline


async def test_chart_view_matches_the_domain_functions_over_the_same_records(
    pool: asyncpg.Pool, guild_id: int, seeded: int
) -> None:
    view = await insights_service.chart_insights(pool, guild_id)

    assert isinstance(view, ChartInsightsView)
    assert view.filter == InsightsFilter("all_time", None, "normal", ALL_TIME_COUNTS, True)
    assert _chart_parts(view) == await _expected_chart_parts(pool, guild_id, game_type="normal")
    # Spot-check the hand-derived seed too, so the comparison above can't be vacuous.
    assert [(p.user_id, p.games) for p in view.players] == [(1, 3), (2, 3), (3, 1)]
    assert [(h.player_a, h.player_b, h.games_together) for h in view.head_to_head] == [
        (1, 2, 3),
        (1, 3, 1),
        (2, 3, 1),
    ]
    assert sorted(view.timeline) == [1, 2, 3]
    assert [d for d, _ in view.timeline[1]] == [
        date(2026, 3, 1),
        date(2026, 3, 5),
        date(2026, 4, 1),
    ]
    assert view.meta.games == 3


async def test_chart_view_season_scope(pool: asyncpg.Pool, guild_id: int, seeded: int) -> None:
    view = await insights_service.chart_insights(pool, guild_id, scope="season")

    assert view.filter.scope == "season"
    assert view.filter.season is not None
    assert view.filter.season.season_id == seeded
    assert view.filter.game_type == "normal"
    assert view.filter.available_game_types == SEASON_COUNTS
    assert _chart_parts(view) == await _expected_chart_parts(
        pool, guild_id, season_id=seeded, game_type="normal"
    )
    assert view.meta.games == 1
    assert sorted(view.timeline) == [1, 2]
    assert len(view.timeline[1]) == 1


async def test_chart_view_without_active_season_is_empty(
    pool: asyncpg.Pool, guild_id: int, games_without_season: None
) -> None:
    view = await insights_service.chart_insights(pool, guild_id, scope="season")

    assert view.filter == InsightsFilter(scope="season", season=None, game_type=None)
    assert view.meta.games == 0
    assert view.players == []
    assert view.head_to_head == []
    assert view.timeline == {}


async def test_chart_view_game_type_filter(pool: asyncpg.Pool, guild_id: int, seeded: int) -> None:
    view = await insights_service.chart_insights(pool, guild_id, game_type="seafarers")

    assert view.filter == InsightsFilter("all_time", None, "seafarers", ALL_TIME_COUNTS, False)
    assert _chart_parts(view) == await _expected_chart_parts(pool, guild_id, game_type="seafarers")
    assert view.meta.games == 1
    assert [p.user_id for p in view.players] == [1, 2, 3]
    assert all(len(points) == 1 for points in view.timeline.values())

    combined = await insights_service.chart_insights(
        pool, guild_id, scope="season", game_type="normal"
    )
    assert combined.meta.games == 1
    assert [p.user_id for p in combined.players] == [1, 2]


async def test_chart_view_is_guild_isolated(
    pool: asyncpg.Pool,
    app_conn: asyncpg.Connection,
    guild_id: int,
    other_guild_id: int,
    seeded: int,
) -> None:
    await _play(app_conn, other_guild_id, played_on=date(2026, 5, 1), winner=2, losers=[1])

    view = await insights_service.chart_insights(pool, other_guild_id)

    assert view.meta.games == 1
    assert view.filter.available_game_types == {"normal": 1}
    assert [(h.player_a, h.player_b) for h in view.head_to_head] == [(1, 2)]


# --- isolation and validation ---------------------------------------------


async def test_guilds_are_isolated(
    pool: asyncpg.Pool,
    app_conn: asyncpg.Connection,
    guild_id: int,
    other_guild_id: int,
    seeded: int,
) -> None:
    # The other guild has its own season and games between the same user ids.
    other_season = await _start_season(app_conn, other_guild_id)
    await _play(
        app_conn,
        other_guild_id,
        played_on=date(2026, 5, 1),
        winner=2,
        losers=[1],
        season_id=other_season,
    )

    mine = await insights_service.meta_insights(pool, guild_id)
    theirs = await insights_service.meta_insights(pool, other_guild_id)
    assert (mine.meta.games, theirs.meta.games) == (3, 1)
    assert mine.filter.available_game_types == ALL_TIME_COUNTS
    assert theirs.filter.available_game_types == {"normal": 1}

    my_season = await insights_service.player_insights(pool, guild_id, 1, scope="season")
    their_season = await insights_service.player_insights(pool, other_guild_id, 1, scope="season")
    assert my_season.filter.season is not None
    assert their_season.filter.season is not None
    assert my_season.filter.season.season_id == seeded
    assert their_season.filter.season.season_id == other_season
    assert my_season.summary.games == 1
    assert their_season.summary.games == 1

    h2h = await insights_service.head_to_head_insights(pool, other_guild_id, 1)
    assert h2h.opponents == [OpponentRecord(2, 1, 0, 1)]


@pytest.mark.parametrize("bad_scope", ["weekly", "", "ALL_TIME", None])
async def test_invalid_scope_raises_with_a_fixed_message(
    pool: asyncpg.Pool, guild_id: int, bad_scope: object
) -> None:
    calls = [
        insights_service.player_insights(pool, guild_id, 1, scope=bad_scope),  # type: ignore[arg-type]
        insights_service.meta_insights(pool, guild_id, scope=bad_scope),  # type: ignore[arg-type]
        insights_service.head_to_head_insights(pool, guild_id, 1, scope=bad_scope),  # type: ignore[arg-type]
        insights_service.chart_insights(pool, guild_id, scope=bad_scope),  # type: ignore[arg-type]
    ]
    for call in calls:
        with pytest.raises(ValueError) as exc_info:
            await call
        assert str(exc_info.value) == INVALID_SCOPE_MESSAGE
        assert "weekly" not in str(exc_info.value)


@pytest.mark.parametrize("bad_type", ["bogus", "", "Normal"])
async def test_invalid_game_type_raises_with_a_fixed_message(
    pool: asyncpg.Pool, guild_id: int, bad_type: str
) -> None:
    calls = [
        insights_service.player_insights(pool, guild_id, 1, game_type=bad_type),
        insights_service.meta_insights(pool, guild_id, game_type=bad_type),
        insights_service.head_to_head_insights(pool, guild_id, 1, game_type=bad_type),
        insights_service.chart_insights(pool, guild_id, game_type=bad_type),
        # Validated even when the season scope would short-circuit to an empty view.
        insights_service.meta_insights(pool, guild_id, scope="season", game_type=bad_type),
    ]
    for call in calls:
        with pytest.raises(ValueError) as exc_info:
            await call
        assert str(exc_info.value) == INVALID_GAME_TYPE_MESSAGE
        assert "bogus" not in str(exc_info.value)
