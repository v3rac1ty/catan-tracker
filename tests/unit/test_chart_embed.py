from __future__ import annotations

from types import SimpleNamespace

import pytest

from catan_bot.charts import RenderedChart
from catan_bot.formatting import (
    EMBED_DESCRIPTION_MAX,
    EMBED_TITLE_MAX,
    EMBED_TOTAL_MAX,
    build_chart_embed,
    build_chart_unavailable_embed,
)
from catan_bot.services.results import InsightsFilter


def _view(scope: str = "all_time", season: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        filter=InsightsFilter(scope=scope, season=season, game_type=None),
        meta=SimpleNamespace(games=1),
        players=[],
        head_to_head=[],
        timeline={},
    )


def test_chart_embed_shows_legend_note_filter_and_attachment() -> None:
    view = _view()
    chart = RenderedChart(
        png=b"png",
        title="How winners score",
        legend=(("P1", 123), ("P2", 456)),
        note="Showing the 2 most active of 2 players.",
    )

    embed = build_chart_embed(view, chart, "insights-winning-formula.png")

    assert embed.title == "How winners score"
    assert embed.description == (
        "All-time\nP1 — <@123>\nP2 — <@456>\nShowing the 2 most active of 2 players."
    )
    assert embed.image.url == "attachment://insights-winning-formula.png"


def test_chart_embed_shows_active_season_and_game_type_filter() -> None:
    view = _view(
        "season",
        SimpleNamespace(name="Winter"),
    )
    view.filter = InsightsFilter(scope="season", season=view.filter.season, game_type="seafarers")

    embed = build_chart_embed(view, RenderedChart(b"png", "Title", (), None), "chart.png")

    assert "Season: Winter" in (embed.description or "")
    assert "Game type: Seafarers" in (embed.description or "")


def test_chart_embed_shows_defaulted_type_and_also_played_types() -> None:
    view = _view()
    view.filter = InsightsFilter(
        scope="all_time",
        season=None,
        game_type="normal",
        available_game_types={"normal": 5, "cities_knights": 1},
        game_type_defaulted=True,
    )

    embed = build_chart_embed(view, RenderedChart(b"png", "Title", (), None), "chart.png")

    assert "Game type: Normal (most played)" in (embed.description or "")
    assert "Also played: Cities & Knights (1 game). Pick game_type to see them." in (
        embed.description or ""
    )


def test_chart_embed_description_and_title_respect_discord_limits() -> None:
    chart = RenderedChart(
        png=b"png",
        title="T" * (EMBED_TITLE_MAX + 30),
        legend=tuple((f"P{index}", index) for index in range(1, 9)),
        note="N" * 10_000,
    )

    embed = build_chart_embed(_view(), chart, "chart.png")

    assert len(embed.title or "") == EMBED_TITLE_MAX
    assert len(embed.description or "") <= EMBED_DESCRIPTION_MAX
    assert len(embed) <= EMBED_TOTAL_MAX


@pytest.mark.parametrize(
    ("view", "expected"),
    [
        (_view("season", None), "There's no active season."),
        (_view(), "Not enough recorded data for this chart yet."),
    ],
)
def test_unavailable_embed_uses_m2_empty_state_wording(
    view: SimpleNamespace, expected: str
) -> None:
    embed = build_chart_unavailable_embed(view, "Head-to-head")

    assert embed.title == "Head-to-head"
    assert expected in (embed.description or "")
    assert embed.image.url is None
    assert not embed.fields


def test_unavailable_chart_includes_also_played_line() -> None:
    view = _view()
    view.filter = InsightsFilter(
        scope="all_time",
        season=None,
        game_type="normal",
        available_game_types={"normal": 5, "seafarers": 2},
    )

    embed = build_chart_unavailable_embed(view, "Head-to-head")

    assert "Not enough recorded data" in (embed.description or "")
    assert "Also played: Seafarers (2 games). Pick game_type to see them." in (
        embed.description or ""
    )


def test_unavailable_chart_with_no_games_uses_empty_state_and_also_played() -> None:
    view = _view()
    view.meta = SimpleNamespace(games=0)
    view.filter = InsightsFilter(
        scope="all_time",
        season=None,
        game_type="cities_knights",
        available_game_types={"normal": 5},
    )

    embed = build_chart_unavailable_embed(view, "Head-to-head")

    assert "No confirmed games yet." in (embed.description or "")
    assert "Also played: Normal (5 games). Pick game_type to see them." in (embed.description or "")
    assert "Not enough recorded data" not in (embed.description or "")


def test_unavailable_embed_respects_title_and_description_limits() -> None:
    embed = build_chart_unavailable_embed(_view(), "X" * (EMBED_TITLE_MAX + 10))

    assert len(embed.title or "") == EMBED_TITLE_MAX
    assert len(embed.description or "") <= EMBED_DESCRIPTION_MAX
    assert len(embed) <= EMBED_TOTAL_MAX
