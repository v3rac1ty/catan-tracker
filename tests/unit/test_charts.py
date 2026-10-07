"""Unit tests for the `/insights chart` PNG renderer (`catan_bot.charts`)."""

from __future__ import annotations

import ast
import random
import struct
import subprocess  # noqa: S404 - runs this interpreter on a fixed snippet, no user input
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta
from fractions import Fraction
from io import BytesIO
from pathlib import Path

import pytest
from matplotlib.colors import to_rgba
from PIL import Image

from catan_bot import charts
from catan_bot.charts import (
    CATEGORICAL,
    CHART_KINDS,
    CHART_TITLES,
    MAX_PLAYERS,
    RenderedChart,
    render_chart,
    source_label,
)
from catan_bot.db.models import Season
from catan_bot.domain import analytics
from catan_bot.domain.participation import ParticipationRecord
from catan_bot.domain.scoring import score_sources
from catan_bot.services.results import ChartInsightsView, InsightsFilter

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PLAYER_KINDS = ("points-by-source", "win-rate-trend", "head-to-head", "season-trend")
PLAYER_FREE_KINDS = ("winning-formula", "award-impact", "winning-scores", "winning-lead")
ALL_FILTER = InsightsFilter(scope="all_time", season=None, game_type=None)


# --- data builders ---------------------------------------------------------
def _breakdown(rng: random.Random, game_type: str, *, winner: bool) -> dict[str, int]:
    lo, hi = (3, 6) if winner else (1, 4)
    values = {
        "settlements": rng.randint(lo, hi),
        "cities": 2 * rng.randint(1 if winner else 0, 3),
    }
    if game_type == "normal":
        values["longest_road"] = 0
        values["largest_army"] = 0
        values["vp_cards"] = rng.choice([0, 0, 1, 2])
    else:
        values["longest_road"] = 0
        values["metropolis_bonus"] = 2 if values["cities"] >= 2 and rng.random() < 0.4 else 0
        values["defender_of_catan"] = rng.choice([0, 0, 1])
        values["merchant"] = 0
        values["constitution"] = 0
        values["printer"] = 0
    return values


def make_records(
    *,
    games: int = 30,
    players: int = 6,
    seed: int = 7,
    unscored_every: int = 6,
    start: date = date(2026, 8, 1),
    spread_days: int = 1,
    seasons: int = 4,
) -> list[ParticipationRecord]:
    """Chronological participation rows mixing normal / C&K games, some unscored."""
    rng = random.Random(seed)  # noqa: S311 - deterministic test data
    ids = [1000 + i for i in range(players)]
    records: list[ParticipationRecord] = []
    for game_id in range(1, games + 1):
        game_type = "cities_knights" if game_id % 3 == 0 else "normal"
        size = rng.choice([3, 4, 4, 5]) if players >= 5 else min(players, 4)
        size = min(size, players)
        seated = sorted(rng.sample(ids, size))
        target = 13 if game_type == "cities_knights" else 10
        played_on = start + timedelta(days=(game_id - 1) * spread_days)
        scored = unscored_every <= 0 or game_id % unscored_every != 0
        winner_id = rng.choice(seated)
        raw = {uid: _breakdown(rng, game_type, winner=uid == winner_id) for uid in seated}
        # One exclusive award per game, handed to a random seated player.
        for key in ("longest_road", "largest_army", "merchant", "constitution", "printer"):
            if key in raw[seated[0]] and rng.random() < 0.8:
                holder = rng.choice(seated)
                raw[holder][key] = 1 if key in ("merchant", "constitution", "printer") else 2
        # The winner must reach the target.
        shortfall = target - sum(raw[winner_id].values())
        if shortfall > 0:
            raw[winner_id]["settlements"] += shortfall
        for uid in seated:
            total = sum(raw[uid].values())
            records.append(
                ParticipationRecord(
                    game_id=game_id,
                    played_on=played_on,
                    played_at=None,
                    game_type=game_type,
                    extension_5_6=False,
                    target_points=target,
                    player_count=size,
                    user_id=uid,
                    is_winner=uid == winner_id,
                    total_points=total if scored else None,
                    breakdown=raw[uid] if scored else None,
                    season_id=1 + (game_id - 1) * seasons // games,
                )
            )
    return records


def make_season(season_id: int) -> Season:
    start = date(2026, 1, 1) + timedelta(days=30 * season_id)
    return Season(
        season_id=season_id,
        guild_id=1,
        name=f"Secret Season Name {season_id}",
        starts_on=start,
        ends_on=start + timedelta(days=29),
        ends_at=datetime(2026, 1, 1, 12, 0),
        min_games=3,
        status="completed",
        resolved_at=None,
        announced_at=None,
        created_by=1,
        created_at=datetime(2026, 1, 1, 12, 0),
    )


def _season_records(
    records: list[ParticipationRecord],
) -> dict[int, dict[int, analytics.RecordSplit]]:
    """season_id -> user_id -> RecordSplit (independent of the domain helper)."""
    tally: dict[int, dict[int, list[int]]] = {}
    for r in records:
        if r.season_id is None:
            continue
        wins_games = tally.setdefault(r.season_id, {}).setdefault(r.user_id, [0, 0])
        wins_games[0] += r.is_winner
        wins_games[1] += 1
    return {
        sid: {uid: analytics.RecordSplit(g, w, Fraction(w, g)) for uid, (w, g) in by_user.items()}
        for sid, by_user in tally.items()
    }


def make_view(records: list[ParticipationRecord]) -> ChartInsightsView:
    season_ids = sorted({r.season_id for r in records if r.season_id is not None})
    return ChartInsightsView(
        seasons=[make_season(sid) for sid in season_ids],
        season_records=_season_records(records),
        filter=ALL_FILTER,
        meta=analytics.meta_summary(records),
        players=analytics.player_summaries(records),
        head_to_head=analytics.head_to_head(records),
        timeline=analytics.win_rate_timeline(records),
    )


@pytest.fixture(scope="module")
def view() -> ChartInsightsView:
    return make_view(make_records())


@pytest.fixture(scope="module")
def empty_view() -> ChartInsightsView:
    return make_view([])


def _png_size(png: bytes) -> tuple[int, int]:
    width, height = struct.unpack(">II", png[16:24])
    return width, height


# --- public API ------------------------------------------------------------
def test_chart_kinds_and_titles_match_contract() -> None:
    assert CHART_KINDS == (
        "winning-formula",
        "points-by-source",
        "award-impact",
        "win-rate-trend",
        "winning-scores",
        "head-to-head",
        "season-trend",
        "winning-lead",
    )
    assert set(CHART_TITLES) == set(CHART_KINDS)
    assert CHART_TITLES["winning-scores"] == "Winning scores & margins"
    assert CHART_TITLES["winning-lead"] == "Where the winning lead came from"
    assert MAX_PLAYERS == 8


@pytest.mark.parametrize("kind", CHART_KINDS)
def test_each_kind_renders_a_valid_png(kind: str, view: ChartInsightsView) -> None:
    chart = render_chart(kind, view)
    assert isinstance(chart, RenderedChart)
    assert chart.png.startswith(PNG_MAGIC)
    assert chart.title == CHART_TITLES[kind]
    width, height = _png_size(chart.png)
    assert (width, height) == (1200, 720)
    # A dark-surface image: the corner pixel is the surface color, never white.
    assert Image.open(BytesIO(chart.png)).convert("RGB").getpixel((0, 0)) == (26, 26, 25)
    assert len(chart.png) > 5_000


@pytest.mark.parametrize("kind", CHART_KINDS)
def test_empty_data_returns_none(kind: str, empty_view: ChartInsightsView) -> None:
    assert render_chart(kind, empty_view) is None


@pytest.mark.parametrize(
    "kind", ["winning-formula", "award-impact", "winning-scores", "winning-lead"]
)
def test_scored_charts_are_none_when_nothing_is_scored(kind: str) -> None:
    unscored = make_view(make_records(games=6, unscored_every=1))
    assert unscored.players  # players exist, but no recorded points
    assert render_chart(kind, unscored) is None


def test_points_by_source_needs_scores_but_trend_and_h2h_do_not() -> None:
    unscored = make_view(make_records(games=6, unscored_every=1))
    assert render_chart("points-by-source", unscored) is None
    assert render_chart("win-rate-trend", unscored) is not None
    assert render_chart("head-to-head", unscored) is not None


def test_head_to_head_needs_two_players() -> None:
    solo = make_view(make_records(games=3, players=1, unscored_every=0))
    assert len(solo.players) == 1
    assert render_chart("head-to-head", solo) is None


def test_head_to_head_needs_shared_games() -> None:
    base = make_view(make_records(games=4))
    assert render_chart("head-to-head", replace(base, head_to_head=[])) is None


def test_unknown_kind_raises_value_error(view: ChartInsightsView) -> None:
    with pytest.raises(ValueError, match="unknown chart kind"):
        render_chart("pie-chart", view)


def test_unknown_kind_raises_even_for_empty_data(empty_view: ChartInsightsView) -> None:
    with pytest.raises(ValueError, match="unknown chart kind"):
        render_chart("", empty_view)


# --- legend / note ---------------------------------------------------------
@pytest.mark.parametrize("kind", PLAYER_KINDS)
def test_player_charts_carry_a_legend_in_label_order(kind: str, view: ChartInsightsView) -> None:
    chart = render_chart(kind, view)
    assert chart is not None
    assert chart.legend == tuple((f"P{i}", p.user_id) for i, p in enumerate(view.players, 1))
    assert chart.note is None


@pytest.mark.parametrize("kind", PLAYER_FREE_KINDS)
def test_charts_without_player_labels_have_no_legend(kind: str, view: ChartInsightsView) -> None:
    chart = render_chart(kind, view)
    assert chart is not None
    assert chart.legend == ()
    assert chart.note is None


@pytest.fixture(scope="module")
def crowded_view() -> ChartInsightsView:
    view = make_view(make_records(games=60, players=11, seed=3))
    assert len(view.players) == 11
    return view


@pytest.mark.parametrize("kind", PLAYER_KINDS)
def test_more_than_max_players_is_capped_with_a_note(
    kind: str, crowded_view: ChartInsightsView
) -> None:
    chart = render_chart(kind, crowded_view)
    assert chart is not None
    assert [label for label, _ in chart.legend] == [f"P{i}" for i in range(1, MAX_PLAYERS + 1)]
    assert [uid for _, uid in chart.legend] == [p.user_id for p in crowded_view.players[:8]]
    noun = "players with season games" if kind == "season-trend" else "players"
    assert chart.note == f"Showing the 8 most active of 11 {noun}."
    assert chart.png.startswith(PNG_MAGIC)


@pytest.mark.parametrize("kind", PLAYER_FREE_KINDS)
def test_player_free_charts_have_no_note_even_when_crowded(
    kind: str, crowded_view: ChartInsightsView
) -> None:
    chart = render_chart(kind, crowded_view)
    assert chart is not None
    assert chart.legend == ()
    assert chart.note is None


# --- design details --------------------------------------------------------
def _fig(builder, view: ChartInsightsView):  # noqa: ANN001, ANN202
    built = builder(view)
    return None if built is None else built.figure


def _legend_texts(fig) -> list[str]:  # noqa: ANN001
    return [text.get_text() for legend in fig.legends for text in legend.get_texts()]


def test_points_by_source_collapses_extra_sources_into_other(view: ChartInsightsView) -> None:
    fig = _fig(charts._build_points_by_source, view)
    assert fig is not None
    names = _legend_texts(fig)
    assert len(names) == 8  # 7 named sources + Other
    assert names[-1] == "Other"
    assert len(set(names)) == len(names)


def test_points_by_source_stack_height_equals_avg_points(view: ChartInsightsView) -> None:
    mixed = [
        p
        for p in view.players
        if {"normal", "cities_knights"} <= set(p.by_game_type) and p.scored_games
    ]
    assert mixed  # players with both game types and scored games
    fig = _fig(charts._build_points_by_source, view)
    assert fig is not None
    ax = fig.axes[0]
    heights = [0.0] * len(ax.get_xticks())
    for patch in ax.patches:
        heights[round(patch.get_x() + patch.get_width() / 2)] += patch.get_height()
    labels = [p for p in view.players[:MAX_PLAYERS] if p.scored_games and p.source_averages]
    assert len(heights) == len(labels)
    for height, player in zip(heights, labels, strict=True):
        assert height == pytest.approx(float(player.avg_points))
    assert {text.get_text() for text in ax.texts} == {f"{h:.1f}" for h in heights}


def test_win_rate_trend_hides_the_date_offset_text(view: ChartInsightsView) -> None:
    fig = _fig(charts._build_win_rate_trend, view)
    assert fig is not None
    fig.canvas.draw()
    assert fig.axes[0].xaxis.get_offset_text().get_text() == ""


def test_points_by_source_without_overflow_has_no_other() -> None:
    normal_only = [r for r in make_records(games=24) if r.game_type == "normal"]
    fig = _fig(charts._build_points_by_source, make_view(normal_only))
    assert fig is not None
    assert "Other" not in _legend_texts(fig)


def test_winning_formula_has_two_series_legend(view: ChartInsightsView) -> None:
    fig = _fig(charts._build_winning_formula, view)
    assert fig is not None
    assert _legend_texts(fig) == ["Winners", "Everyone else"]


def test_award_impact_has_with_and_without_legend(view: ChartInsightsView) -> None:
    fig = _fig(charts._build_award_impact, view)
    assert fig is not None
    assert _legend_texts(fig) == ["With award", "Without award"]


def test_win_rate_trend_uses_fixed_player_color_slots(view: ChartInsightsView) -> None:
    fig = _fig(charts._build_win_rate_trend, view)
    assert fig is not None
    ax = fig.axes[0]
    colors = [line.get_color() for line in ax.get_lines()]
    assert colors == list(CATEGORICAL[: len(colors)])
    assert _legend_texts(fig) == [f"P{i}" for i in range(1, len(colors) + 1)]
    assert ax.get_ylim() == (0, 100)


def test_win_rate_trend_on_a_single_day_falls_back_to_game_number() -> None:
    one_day = make_view(make_records(games=10, spread_days=0, unscored_every=0))
    chart = render_chart("win-rate-trend", one_day)
    assert chart is not None
    assert chart.png.startswith(PNG_MAGIC)
    fig = _fig(charts._build_win_rate_trend, one_day)
    assert fig is not None
    assert fig.axes[0].get_xlabel() == "Games played"


def test_winning_scores_is_two_subplots_side_by_side(view: ChartInsightsView) -> None:
    fig = _fig(charts._build_winning_scores, view)
    assert fig is not None
    first, second = fig.axes
    assert first.get_position().x1 <= second.get_position().x0 + 1e-6
    assert fig.legends == []


def test_winning_scores_without_margin_samples_still_renders(view: ChartInsightsView) -> None:
    meta = replace(view.meta, margin_distribution={}, margin_samples=0, avg_margin=None)
    chart = render_chart("winning-scores", replace(view, meta=meta))
    assert chart is not None
    assert chart.png.startswith(PNG_MAGIC)


def test_award_impact_handles_missing_rates() -> None:
    # Nobody ever holds Largest Army, so its "with award" win rate is undefined.
    records = [
        replace(r, breakdown={**r.breakdown, "largest_army": 0})
        for r in make_records(games=18)
        if r.game_type == "normal" and r.breakdown is not None
    ]
    assert analytics.meta_summary(records).awards["largest_army"].win_rate_when_held is None
    chart = render_chart("award-impact", make_view(records))
    assert chart is not None


def test_heatmap_share_matches_head_to_head_records() -> None:
    records = make_records(games=30, players=3, unscored_every=0)
    view = make_view(records)
    fig = _fig(charts._build_head_to_head, view)
    assert fig is not None
    ax = fig.axes[0]
    cells = {text.get_text() for text in ax.texts}
    by_pair = {(h.player_a, h.player_b): h for h in view.head_to_head}
    for h in by_pair.values():
        assert f"{h.a_wins}/{h.games_together}" in cells
        assert f"{h.b_wins}/{h.games_together}" in cells


def test_heatmap_marks_pairs_without_shared_games() -> None:
    base = make_view(make_records(games=4, players=2, unscored_every=0))
    stranger = replace(base.players[0], user_id=9999)  # never shared a game with anyone
    view = replace(base, players=[*base.players, stranger])
    fig = _fig(charts._build_head_to_head, view)
    assert fig is not None
    assert "—" in {text.get_text() for text in fig.axes[0].texts}


# --- source labels ---------------------------------------------------------
def test_every_catalog_source_has_a_short_ascii_label() -> None:
    keys = {
        source.key
        for game_type in ("normal", "seafarers", "cities_knights", "seafarers_cities_knights")
        for source in score_sources(game_type)
    }
    for key in keys:
        label = source_label(key)
        assert label
        assert label.isascii()
        assert len(label) <= 16, (key, label)


def test_unknown_source_key_gets_a_safe_label() -> None:
    assert source_label("some_new_source") == "Some New Source"
    assert source_label("café_bonus") == "Caf Bonus"


# --- thread safety / no pyplot ---------------------------------------------
def test_charts_module_never_imports_pyplot() -> None:
    source = Path(charts.__file__).read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported.extend(f"{node.module}.{alias.name}" for alias in node.names)
    assert not [name for name in imported if "pyplot" in name]


def test_rendering_does_not_load_pyplot_in_a_fresh_interpreter() -> None:
    snippet = (
        "import sys\n"
        "from tests.unit.test_charts import make_records, make_view\n"
        "from catan_bot.charts import CHART_KINDS, render_chart\n"
        "view = make_view(make_records(games=12))\n"
        "assert all(render_chart(k, view) for k in CHART_KINDS)\n"
        "assert 'matplotlib.pyplot' not in sys.modules\n"
    )
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(  # noqa: S603 - fixed argv, current interpreter
        [sys.executable, "-c", snippet],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_concurrent_rendering_is_safe_and_deterministic(view: ChartInsightsView) -> None:
    jobs = [kind for kind in CHART_KINDS for _ in range(3)]
    expected = {kind: render_chart(kind, view) for kind in CHART_KINDS}
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda kind: (kind, render_chart(kind, view)), jobs))
    assert len(results) == len(jobs)
    for kind, chart in results:
        assert chart is not None
        assert chart.png.startswith(PNG_MAGIC)
        assert chart == expected[kind]


# --- review round 2 --------------------------------------------------------
@pytest.mark.parametrize("which", ["sample", "crowded"])
def test_points_by_source_labels_every_bar_total(
    which: str, view: ChartInsightsView, crowded_view: ChartInsightsView
) -> None:
    fig = _fig(charts._build_points_by_source, view if which == "sample" else crowded_view)
    assert fig is not None
    ax = fig.axes[0]
    bars = len(ax.get_xticks())
    assert bars >= 6
    positions = sorted(round(text.get_position()[0]) for text in ax.texts)
    assert positions == list(range(bars))  # exactly one label per bar, last bar included
    fig.canvas.draw()
    for text in ax.texts:
        assert text.get_window_extent().x1 <= fig.bbox.x1  # not clipped off the figure


def _record(game_id: int, game_type: str, uid: int, winner: bool, **points: int):
    breakdown = {"settlements": 3, "cities": 4, **points}
    return ParticipationRecord(
        game_id=game_id,
        played_on=date(2026, 8, game_id),
        played_at=None,
        game_type=game_type,
        extension_5_6=False,
        target_points=10,
        player_count=2,
        user_id=uid,
        is_winner=winner,
        total_points=sum(breakdown.values()),
        breakdown=breakdown,
    )


def _formula_records() -> list[ParticipationRecord]:
    records = []
    for game_id in range(1, 10):  # nine normal games, no metropolis possible
        records += [_record(game_id, "normal", 1, True), _record(game_id, "normal", 2, False)]
    records += [  # one C&K game where the winner has 2 metropolis points
        _record(10, "cities_knights", 1, True, metropolis_bonus=2),
        _record(10, "cities_knights", 2, False, metropolis_bonus=0),
    ]
    return records


def _bar_label_for(ax, category: str, series_index: int) -> str:  # noqa: ANN001
    row = [t.get_text() for t in ax.get_yticklabels()].index(category)
    height = 0.8 / 2
    y = row + (series_index - 0.5) * height
    (text,) = [t for t in ax.texts if abs(t.get_position()[1] - y) < 1e-6]
    return text.get_text().strip()


def test_winning_formula_averages_over_all_scored_players() -> None:
    view = make_view(_formula_records())
    assert view.meta.winner_composition["metropolis_bonus"] == 2  # only over C&K winners
    fig = _fig(charts._build_winning_formula, view)
    assert fig is not None
    ax = fig.axes[0]
    assert _bar_label_for(ax, "Metropolis", 0) == "0.2"  # 2 points over 10 winners
    assert _bar_label_for(ax, "Metropolis", 1) == "0.0"
    assert _bar_label_for(ax, "Settlements", 0) == "3.0"


def test_winning_formula_subtitle_shows_both_populations() -> None:
    fig = _fig(charts._build_winning_formula, make_view(_formula_records()))
    assert fig is not None
    subtitles = [t.get_text() for t in fig.texts]
    assert "Winners: 10 scored games \u00b7 Everyone else: 10 scored appearances" in subtitles


def test_points_by_source_legend_skips_unscored_players(view: ChartInsightsView) -> None:
    first = view.players[0]
    unscored_first = replace(
        view,
        players=[replace(first, scored_games=0, source_averages={}), *view.players[1:]],
    )
    chart = render_chart("points-by-source", unscored_first)
    assert chart is not None
    assert [label for label, _ in chart.legend] == ["P2", "P3", "P4", "P5", "P6"]
    assert chart.legend[0] == ("P2", view.players[1].user_id)


def test_trend_legend_skips_players_without_a_timeline(view: ChartInsightsView) -> None:
    gone = view.players[0].user_id
    trimmed = replace(view, timeline={k: v for k, v in view.timeline.items() if k != gone})
    chart = render_chart("win-rate-trend", trimmed)
    assert chart is not None
    assert [label for label, _ in chart.legend] == ["P2", "P3", "P4", "P5", "P6"]


def test_note_counts_follow_the_plotted_players(crowded_view: ChartInsightsView) -> None:
    first = crowded_view.players[0]
    skipped = replace(
        crowded_view,
        players=[replace(first, scored_games=0, source_averages={}), *crowded_view.players[1:]],
    )
    chart = render_chart("points-by-source", skipped)
    assert chart is not None
    assert [label for label, _ in chart.legend] == [f"P{i}" for i in range(2, 9)]
    assert chart.note == "Showing 7 of the 8 most active of 11 players."


def test_award_impact_keeps_a_zero_to_hundred_axis_without_clipped_labels(
    view: ChartInsightsView,
) -> None:
    fig = _fig(charts._build_award_impact, view)
    assert fig is not None
    ax = fig.axes[0]
    assert ax.get_xlim() == (0, 100)
    fig.canvas.draw()
    for text in ax.texts:
        assert text.get_window_extent().x1 <= ax.bbox.x1 + 1


def test_award_impact_puts_long_bar_labels_inside_the_axis() -> None:
    # The winner always holds Longest Road: a 100% win rate with the award.
    records = []
    for game_id in range(1, 7):
        records += [
            _record(game_id, "normal", 1, True, longest_road=2),
            _record(game_id, "normal", 2, False, longest_road=0),
        ]
    fig = _fig(charts._build_award_impact, make_view(records))
    assert fig is not None
    ax = fig.axes[0]
    assert ax.get_xlim() == (0, 100)
    fig.canvas.draw()
    assert max(t.get_window_extent().x1 for t in ax.texts) <= ax.bbox.x1 + 1


# --- M4: game type in the image title --------------------------------------
_TYPE_LABELS = {
    "normal": "Normal",
    "seafarers": "Seafarers",
    "cities_knights": "Cities & Knights",
    "seafarers_cities_knights": "Seafarers + Cities & Knights",
}


def _for_type(view: ChartInsightsView, game_type: str | None) -> ChartInsightsView:
    return replace(view, filter=replace(view.filter, game_type=game_type))


def _image_title(fig) -> str:  # noqa: ANN001
    return fig.texts[0].get_text()


@pytest.mark.parametrize("kind", CHART_KINDS)
@pytest.mark.parametrize("game_type", list(_TYPE_LABELS))
def test_image_title_names_the_game_type_and_fits(
    kind: str, game_type: str, view: ChartInsightsView
) -> None:
    built = charts._BUILDERS[kind](_for_type(view, game_type))
    assert built is not None
    title = built.figure.texts[0]
    assert title.get_text() == f"{CHART_TITLES[kind]} \u00b7 {_TYPE_LABELS[game_type]}"
    built.figure.canvas.draw()
    extent = title.get_window_extent()
    assert extent.x0 >= 0
    assert extent.x1 <= built.figure.bbox.x1 - 20  # comfortable right margin at 1200px


@pytest.mark.parametrize("kind", CHART_KINDS)
def test_image_title_is_plain_without_a_game_type(kind: str, view: ChartInsightsView) -> None:
    built = charts._BUILDERS[kind](_for_type(view, None))
    assert built is not None
    assert _image_title(built.figure) == CHART_TITLES[kind]


@pytest.mark.parametrize("kind", CHART_KINDS)
def test_rendered_chart_title_stays_the_plain_chart_title(
    kind: str, view: ChartInsightsView
) -> None:
    chart = render_chart(kind, _for_type(view, "cities_knights"))
    assert chart is not None
    assert chart.title == CHART_TITLES[kind]


def test_unknown_game_type_gets_a_safe_ascii_title(view: ChartInsightsView) -> None:
    assert charts.game_type_label("seafarers_cities_knights") == "Seafarers + Cities & Knights"
    assert charts.game_type_label("fancy_new-mode") == "Fancy New-Mode"
    assert charts.game_type_label("caf\u00e9\U0001f600_mode") == "Caf Mode"
    assert charts.game_type_label("\U0001f600") == "Other"
    built = charts._BUILDERS["winning-formula"](_for_type(view, "caf\u00e9_mode\n<@123>"))
    assert built is not None
    title = _image_title(built.figure)
    assert title.isascii() or title.count("\u00b7") == 1
    assert "\n" not in title


# --- M5: season-trend -------------------------------------------------------
def _season_view(*, games: int = 40, seasons: int = 4, skip: tuple[int, int] | None = None):
    """A view over `seasons` seasons; `skip=(player_index, season_id)` drops that cell."""
    records = make_records(games=games, players=6, seasons=seasons, unscored_every=0)
    if skip is not None:
        ids = sorted({r.user_id for r in records})
        records = [r for r in records if not (r.user_id == ids[skip[0]] and r.season_id == skip[1])]
    return make_view(records)


def test_season_trend_axis_uses_s_labels_never_season_names() -> None:
    view = _season_view()
    fig = _fig(charts._build_season_trend, view)
    assert fig is not None
    ax = fig.axes[0]
    assert [t.get_text() for t in ax.get_xticklabels()] == ["S1", "S2", "S3", "S4"]
    everything = [t.get_text() for t in fig.findobj(lambda a: hasattr(a, "get_text"))]
    assert not [text for text in everything if "Secret" in text]
    assert ax.get_ylim() == (0, 100)


def test_season_trend_season_legend_follows_view_order() -> None:
    view = _season_view()
    chart = render_chart("season-trend", view)
    assert chart is not None
    assert chart.season_legend == tuple(
        (f"S{i}", season.season_id) for i, season in enumerate(view.seasons, 1)
    )
    assert chart.legend == tuple((f"P{i}", p.user_id) for i, p in enumerate(view.players, 1))
    assert chart.note is None


def test_other_kinds_have_no_season_legend(view: ChartInsightsView) -> None:
    for kind in CHART_KINDS:
        if kind != "season-trend":
            chart = render_chart(kind, view)
            assert chart is not None
            assert chart.season_legend == ()


def test_season_trend_draws_marked_two_pixel_lines_with_gaps() -> None:
    view = _season_view(skip=(0, 2))
    fig = _fig(charts._build_season_trend, view)
    assert fig is not None
    ax = fig.axes[0]
    lines = ax.get_lines()
    assert [line.get_color() for line in lines] == list(CATEGORICAL[: len(lines)])
    for line in lines:
        assert line.get_linewidth() == 2
        assert line.get_marker() == "o"
        assert line.get_markersize() >= 8
        assert line.get_markevery() is None  # a marker on every point
    # The player whose S2 games were dropped has a gap there, not a zero.
    dropped = min(p.user_id for p in view.players)
    index = [p.user_id for p in view.players].index(dropped)
    ydata = list(lines[index].get_ydata())
    assert ydata[1] != ydata[1]  # NaN
    assert all(v == v for i, v in enumerate(ydata) if i != 1)
    assert all(0 <= v <= 100 for v in ydata if v == v)
    assert _legend_texts(fig) == [f"P{i}" for i in range(1, len(lines) + 1)]


def test_season_trend_win_rates_match_records() -> None:
    view = _season_view()
    fig = _fig(charts._build_season_trend, view)
    assert fig is not None
    first_player = view.players[0].user_id
    expected = [
        float(view.season_records[season.season_id][first_player].win_rate) * 100
        for season in view.seasons
    ]
    assert list(fig.axes[0].get_lines()[0].get_ydata()) == pytest.approx(expected)


def test_season_trend_needs_two_seasons_with_games() -> None:
    assert render_chart("season-trend", _season_view(games=12, seasons=1)) is None
    one_played = _season_view(seasons=2)
    only_first = replace(
        one_played,
        season_records={one_played.seasons[0].season_id: one_played.season_records[1]},
    )
    assert render_chart("season-trend", only_first) is None
    assert render_chart("season-trend", replace(one_played, seasons=[])) is None


def test_season_trend_legend_skips_players_with_no_season_games() -> None:
    view = _season_view()
    gone = view.players[0].user_id
    trimmed = replace(
        view,
        season_records={
            sid: {uid: split for uid, split in by_user.items() if uid != gone}
            for sid, by_user in view.season_records.items()
        },
    )
    chart = render_chart("season-trend", trimmed)
    assert chart is not None
    assert [label for label, _ in chart.legend] == ["P2", "P3", "P4", "P5", "P6"]


def test_season_trend_caps_to_the_latest_seasons_with_a_note() -> None:
    view = _season_view(games=60, seasons=15)
    chart = render_chart("season-trend", view)
    assert chart is not None
    assert len(chart.season_legend) == charts.MAX_SEASONS
    assert chart.season_legend[-1] == ("S12", view.seasons[-1].season_id)
    assert chart.season_legend[0] == ("S1", view.seasons[-charts.MAX_SEASONS].season_id)
    assert chart.note == "Showing the latest 12 of 15 seasons."


def test_season_trend_single_season_line_is_just_a_marker() -> None:
    view = _season_view()
    only = view.players[0].user_id
    sparse = replace(
        view,
        season_records={
            sid: {uid: split for uid, split in by_user.items() if uid != only or sid == 1}
            for sid, by_user in view.season_records.items()
        },
    )
    chart = render_chart("season-trend", sparse)
    assert chart is not None and chart.png.startswith(PNG_MAGIC)


def _unseasoned_top_view(*, seasoned_top: bool = False) -> ChartInsightsView:
    """8 busy players with only unseasoned games; 2 lower-ranked ones play two seasons."""
    records = []
    game_id = 0
    for _ in range(12):  # P1..P8 play each other a lot, never in a season
        game_id += 1
        for uid in range(1, 9):
            records.append(_record(game_id, "normal", uid, winner=uid == 1 + game_id % 8))
    extra = [9, 10] + ([1, 2] if seasoned_top else [])
    for season_id in (1, 2):
        for round_ in range(2):
            game_id += 1
            for uid in extra:
                records.append(
                    replace(
                        _record(game_id, "normal", uid, winner=uid == extra[round_ % len(extra)]),
                        season_id=season_id,
                    )
                )
    return make_view(records)


def test_season_trend_uses_players_with_season_games_not_just_the_top_eight() -> None:
    view = _unseasoned_top_view()
    assert [p.user_id for p in view.players[:8]] == list(range(1, 9))  # busiest are unseasoned
    chart = render_chart("season-trend", view)
    assert chart is not None
    assert chart.legend == (("P9", 9), ("P10", 10))
    assert chart.note is None
    assert [season_id for _, season_id in chart.season_legend] == [1, 2]
    fig = _fig(charts._build_season_trend, view)
    assert fig is not None
    assert _legend_texts(fig) == ["P9", "P10"]
    # P9+ have no slot of their own, so they take the first free slots in order.
    assert [line.get_color() for line in fig.axes[0].get_lines()] == list(CATEGORICAL[:2])


def test_season_trend_keeps_stable_slots_for_top_players_alongside_p9_plus() -> None:
    view = _unseasoned_top_view(seasoned_top=True)
    chart = render_chart("season-trend", view)
    assert chart is not None
    assert chart.legend == (("P1", 1), ("P2", 2), ("P9", 9), ("P10", 10))
    fig = _fig(charts._build_season_trend, view)
    assert fig is not None
    assert [line.get_color() for line in fig.axes[0].get_lines()] == [
        CATEGORICAL[0],  # P1
        CATEGORICAL[1],  # P2
        CATEGORICAL[2],  # P9 takes the first free slot
        CATEGORICAL[3],  # P10
    ]


def test_other_player_charts_are_unaffected_by_unseasoned_top_players() -> None:
    chart = render_chart("win-rate-trend", _unseasoned_top_view())
    assert chart is not None
    assert [label for label, _ in chart.legend] == [f"P{i}" for i in range(1, 9)]
    assert chart.note == "Showing the 8 most active of 10 players."


# --- M6: winning-lead -------------------------------------------------------
def _lead_view(
    lead: dict[str, float], samples: int, game_type: str | None = None
) -> ChartInsightsView:
    """A view whose meta carries exactly the given winning-lead data."""
    base = make_view(make_records(games=12, unscored_every=0))
    meta = replace(
        base.meta,
        lead_sources={key: Fraction(value).limit_denominator(1000) for key, value in lead.items()},
        lead_source_samples=samples,
    )
    return replace(_for_type(base, game_type), meta=meta)


def _lead_fig(view: ChartInsightsView):  # noqa: ANN202
    fig = _fig(charts._build_winning_lead, view)
    assert fig is not None
    fig.canvas.draw()
    return fig


def _subtitle(fig) -> str:  # noqa: ANN001
    return fig.texts[1].get_text()


MIXED_LEAD = {
    "settlements": 0.8,
    "cities": 1.4,
    "longest_road": 0.9,
    "largest_army": -0.3,
    "vp_cards": 0.1,
    "defender_of_catan": -0.6,
}


def test_winning_lead_renders_a_valid_png_from_real_games(view: ChartInsightsView) -> None:
    assert view.meta.lead_source_samples > 0
    chart = render_chart("winning-lead", view)
    assert chart is not None
    assert chart.png.startswith(PNG_MAGIC)
    assert chart.title == "Where the winning lead came from"
    assert chart.legend == ()
    assert chart.note is None


def test_winning_lead_is_none_without_samples() -> None:
    assert render_chart("winning-lead", _lead_view({}, 0)) is None
    assert render_chart("winning-lead", _lead_view({"cities": 1.0}, 0)) is None
    assert render_chart("winning-lead", _lead_view({}, 5)) is None


def test_winning_lead_bars_are_signed_sorted_and_colored() -> None:
    fig = _lead_fig(_lead_view(MIXED_LEAD, 25))
    ax = fig.axes[0]
    ordered = sorted(MIXED_LEAD, key=lambda k: (-MIXED_LEAD[k], k))
    assert [t.get_text() for t in ax.get_yticklabels()] == [source_label(k) for k in ordered]
    assert ax.get_ylim()[0] > ax.get_ylim()[1]  # first (largest) bar on top
    widths = [patch.get_width() for patch in ax.patches]
    assert widths == pytest.approx([MIXED_LEAD[k] for k in ordered], abs=1e-3)
    assert widths == sorted(widths, reverse=True)
    colors = [patch.get_facecolor() for patch in ax.patches]
    assert colors == [to_rgba("#3987e5" if w >= 0 else "#e66767") for w in widths]
    assert fig.legends == []
    baselines = [line for line in ax.get_lines() if line.get_color() == "#383835"]
    assert [list(line.get_xdata()) for line in baselines] == [[0, 0]]


def test_winning_lead_labels_are_signed_in_ink_colors() -> None:
    fig = _lead_fig(_lead_view(MIXED_LEAD, 25))
    ax = fig.axes[0]
    assert sorted(t.get_text().strip() for t in ax.texts) == sorted(
        f"{v:+.1f}" for v in MIXED_LEAD.values()
    )
    assert {t.get_color() for t in ax.texts} <= {charts.INK, charts.INK_SECONDARY}
    for text in ax.texts:  # a label sits on the outer side of its bar
        assert (text.get_ha() == "left") == (text.get_position()[0] >= 0)


@pytest.mark.parametrize(
    "lead",
    [
        MIXED_LEAD,
        {"cities": -1.2, "settlements": -0.4, "vp_cards": -2.5},  # all negative
        {"cities": 4.37, "settlements": 0.2},  # large positive
        {"cities": -0.04, "settlements": 0.04},  # rounds to zero
    ],
    ids=["mixed", "all-negative", "positive", "tiny"],
)
def test_winning_lead_labels_stay_inside_the_axes_with_symmetric_limits(
    lead: dict[str, float],
) -> None:
    fig = _lead_fig(_lead_view(lead, 30))
    ax = fig.axes[0]
    lo, hi = ax.get_xlim()
    assert lo == pytest.approx(-hi)
    assert hi > max(abs(v) for v in lead.values())
    for text in ax.texts:
        extent = text.get_window_extent()
        assert extent.x0 >= ax.bbox.x0 - 1
        assert extent.x1 <= ax.bbox.x1 + 1
    chart = render_chart("winning-lead", _lead_view(lead, 30))
    assert chart is not None
    assert chart.png.startswith(PNG_MAGIC)


def test_winning_lead_tiny_nonzero_labels_say_less_than_a_tenth() -> None:
    fig = _lead_fig(_lead_view({"cities": -0.04, "settlements": 0.04, "vp_cards": 0}, 30))
    assert sorted(t.get_text().strip() for t in fig.axes[0].texts) == ["+<0.1", "-<0.1", "0.0"]
    assert "0.0 pts" in _subtitle(fig)
    assert "-0.0" not in _subtitle(fig)


def test_winning_lead_subtitle_sums_exact_fractions_never_negative_zero() -> None:
    # 3/10 - 1/10 - 2/10 is exactly 0, but the float sum is -2.8e-17 -> "-0.0".
    lead = {
        "cities": Fraction(3, 10),
        "settlements": Fraction(-1, 10),
        "vp_cards": Fraction(-2, 10),
    }
    assert sum(float(v) for v in lead.values()) < 0  # the float trap this guards against
    base = _lead_view({}, 0)
    meta = replace(base.meta, lead_sources=lead, lead_source_samples=30)
    fig = _lead_fig(replace(base, meta=meta))
    assert "(0.0 pts)" in _subtitle(fig)
    assert "-0.0" not in _subtitle(fig)
    assert sorted(t.get_text().strip() for t in fig.axes[0].texts) == ["+0.3", "-0.1", "-0.2"]


def test_winning_lead_subtitle_sum_matches_the_bars() -> None:
    fig = _lead_fig(_lead_view(MIXED_LEAD, 25))
    total = sum(MIXED_LEAD.values())
    assert total == pytest.approx(2.3)
    assert _subtitle(fig) == (
        "Winner minus runner-up per source, over 25 fully scored games;\n"
        f"bars sum to the average winning margin ({total:.1f} pts)"
    )
    assert "2.3 pts" in _subtitle(fig)


def test_winning_lead_subtitle_matches_real_average_margin(view: ChartInsightsView) -> None:
    fig = _lead_fig(view)
    bars = sum(patch.get_width() for patch in fig.axes[0].patches)
    assert f"({bars:.1f} pts)" in _subtitle(fig)
    assert f"over {view.meta.lead_source_samples} fully scored games" in _subtitle(fig)


EXPLORATORY = " — exploratory (under 10 games)"


@pytest.mark.parametrize(
    ("samples", "exploratory"),
    [(1, True), (9, True), (analytics.LEAD_SOURCES_MIN - 1, True), (10, False), (11, False)],
)
def test_winning_lead_exploratory_suffix_boundary(samples: int, exploratory: bool) -> None:
    assert analytics.LEAD_SOURCES_MIN == 10
    fig = _lead_fig(_lead_view({"cities": 1.0, "settlements": 0.5}, samples))
    assert _subtitle(fig).endswith(EXPLORATORY) is exploratory
    assert (EXPLORATORY in _subtitle(fig)) is exploratory


def test_winning_lead_one_game_subtitle_is_singular() -> None:
    fig = _lead_fig(_lead_view({"cities": 1.0}, 1))
    assert "over 1 fully scored game;" in _subtitle(fig)


@pytest.mark.parametrize("game_type", list(_TYPE_LABELS))
def test_winning_lead_title_names_the_game_type(game_type: str) -> None:
    fig = _lead_fig(_lead_view(MIXED_LEAD, 25, game_type))
    assert _image_title(fig) == f"Where the winning lead came from · {_TYPE_LABELS[game_type]}"
    assert _image_title(_lead_fig(_lead_view(MIXED_LEAD, 25))) == "Where the winning lead came from"


def test_winning_lead_uses_catalog_labels_and_no_legend() -> None:
    fig = _lead_fig(_lead_view({"metropolis_bonus": 0.4, "totally_new_source": -0.2}, 12))
    assert [t.get_text() for t in fig.axes[0].get_yticklabels()] == [
        "Metropolis",
        "Totally New Source",
    ]
    assert fig.legends == []


@pytest.mark.parametrize("samples", [1, 4, 27])
def test_winning_lead_subtitle_fits_inside_the_image(samples: int) -> None:
    fig = _lead_fig(_lead_view(MIXED_LEAD, samples))
    subtitle = fig.texts[1]
    extent = subtitle.get_window_extent()
    assert extent.x1 <= fig.bbox.x1 - 20
    assert extent.y0 >= fig.axes[0].get_tightbbox().y1 - 1  # clear of the plot
