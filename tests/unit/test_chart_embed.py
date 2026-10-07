from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from catan_bot.charts import RenderedChart
from catan_bot.db.models import Season
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


# ---------------------------------------------------------------------------
# M5: season legend
# ---------------------------------------------------------------------------


def _season_row(season_id: int, name: str) -> Season:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Season(
        season_id=season_id,
        guild_id=1,
        name=name,
        starts_on=date(2026, 1, 1) + timedelta(days=season_id),
        ends_on=date(2026, 12, 31),
        ends_at=now,
        min_games=2,
        status="completed",
        resolved_at=None,
        announced_at=None,
        created_by=10,
        created_at=now,
    )


def _trend_chart(season_legend: tuple[tuple[str, int], ...], note: str | None = None):
    return RenderedChart(
        png=b"png",
        title="Win rate by season",
        legend=(("P1", 123),),
        note=note,
        season_legend=season_legend,
    )


def test_season_legend_lists_escaped_season_names_after_players() -> None:
    view = _view()
    view.seasons = [_season_row(4, "Winter"), _season_row(9, "**Spring** @everyone")]
    chart = _trend_chart((("S1", 4), ("S2", 9)), note="Showing the 1 most active of 1 players.")

    description = build_chart_embed(view, chart, "c.png").description or ""
    lines = description.splitlines()

    assert lines[:3] == ["All-time", "P1 — <@123>", "S1 — Winter"]
    assert lines[3].startswith(r"S2 — \*\*Spring\*\* @")
    assert "@everyone" not in description
    assert lines[-1] == "Showing the 1 most active of 1 players."


def test_season_legend_absent_adds_nothing() -> None:
    view = _view()  # no `seasons` attribute is even consulted
    chart = RenderedChart(b"png", "How winners score", (("P1", 1),), None)

    assert build_chart_embed(view, chart, "c.png").description == "All-time\nP1 — <@1>"


def test_season_legend_unknown_season_id_falls_back_to_a_neutral_label() -> None:
    view = _view()
    view.seasons = []
    description = build_chart_embed(view, _trend_chart((("S1", 42),)), "c.png").description or ""

    assert "S1 — Season #42" in description
    assert "None" not in description


def test_season_legend_hostile_names_never_ping_or_mention() -> None:
    hostile = "@everyone @here <@&123456789012345678> <#123456789012345678> " + "x" * 300
    view = _view()
    view.seasons = [_season_row(1, hostile)]

    description = build_chart_embed(view, _trend_chart((("S1", 1),)), "c.png").description or ""

    assert "@everyone" not in description
    assert "@here" not in description
    assert "<@&" not in description
    assert "<#1" not in description
    assert len(description.splitlines()[2]) <= len("S1 — ") + 100 + 20  # name is capped


def test_season_legend_with_many_seasons_stays_within_limits_and_says_what_was_dropped() -> None:
    names = ["N" * 100 for _ in range(100)]
    view = _view()
    view.seasons = [_season_row(index, name) for index, name in enumerate(names, start=1)]
    legend = tuple((f"S{index}", index) for index in range(1, 101))
    note = "Showing the 8 most active of 20 players."

    embed = build_chart_embed(view, _trend_chart(legend, note=note), "c.png")
    description = embed.description or ""
    lines = description.splitlines()

    assert len(description) <= EMBED_DESCRIPTION_MAX
    assert len(embed) <= EMBED_TOTAL_MAX
    assert lines[-1] == note  # the note survives
    assert lines[-2].startswith("\u2026and ") and lines[-2].endswith(" more seasons")
    shown = sum(1 for line in lines if line.startswith("S") and " — N" in line)
    dropped = int(lines[-2].split()[1])
    assert shown + dropped == 100
    assert "None" not in description


def test_season_legend_that_fits_has_no_dropped_marker() -> None:
    view = _view()
    view.seasons = [_season_row(index, f"Season {index}") for index in range(1, 6)]
    legend = tuple((f"S{index}", index) for index in range(1, 6))

    description = build_chart_embed(view, _trend_chart(legend), "c.png").description or ""

    assert "more season" not in description
    assert description.splitlines()[-1] == "S5 — Season 5"


def test_season_legend_is_skipped_when_there_is_no_active_season() -> None:
    view = _view("season", None)
    embed = build_chart_embed(view, _trend_chart((("S1", 1),)), "c.png")

    assert embed.description == "There's no active season."
    assert embed.image.url is None
