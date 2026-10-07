"""PNG chart rendering for ``/insights chart``.

Every chart is drawn on a ``matplotlib.figure.Figure`` that owns its own
``FigureCanvasAgg``.  ``matplotlib.pyplot`` is never imported: pyplot keeps a
global figure registry and a global "current figure", which is exactly the
shared state that makes rendering unsafe from several worker threads at once.
With plain ``Figure`` objects, :func:`render_chart` is a pure function of its
input and can be called concurrently through ``asyncio.to_thread``.

Images never contain user-supplied text.  Players are drawn as ``P1``..``Pn``
(their position in ``view.players``); the embed that carries the image maps
those labels back to Discord mentions via :attr:`RenderedChart.legend`.

The look follows the dark categorical/sequential palette from the dataviz
reference: one dark surface, ink colors for all text, thin marks separated by
2px surface-colored gaps, one y-axis per subplot, and fixed color slots that
follow the entity (player ``Pk`` is always slot ``k``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from io import BytesIO
from typing import TYPE_CHECKING

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.cm import ScalarMappable
from matplotlib.colors import LinearSegmentedColormap, Normalize
from matplotlib.dates import AutoDateLocator, ConciseDateFormatter
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle
from matplotlib.ticker import MaxNLocator, PercentFormatter

from catan_bot.domain.analytics import LEAD_SOURCES_MIN
from catan_bot.domain.scoring import score_sources

if TYPE_CHECKING:
    from matplotlib.axes import Axes

    from catan_bot.domain.analytics import AwardStat, PlayerSummary
    from catan_bot.services.results import ChartInsightsView

CHART_KINDS: tuple[str, ...] = (
    "winning-formula",
    "points-by-source",
    "award-impact",
    "win-rate-trend",
    "winning-scores",
    "head-to-head",
    "season-trend",
    "winning-lead",
)
CHART_TITLES: dict[str, str] = {
    "winning-formula": "How winners score",
    "points-by-source": "Points by source",
    "award-impact": "Award impact",
    "win-rate-trend": "Win rate over time",
    "winning-scores": "Winning scores & margins",
    "head-to-head": "Head-to-head",
    "season-trend": "Win rate by season",
    "winning-lead": "Where the winning lead came from",
}
MAX_PLAYERS = 8
MAX_SEASONS = 12  # season-trend shows at most the most recent seasons

# --- design tokens (dark surface) -----------------------------------------
SURFACE = "#1a1a19"
INK = "#ffffff"
INK_SECONDARY = "#c3c2b7"
INK_MUTED = "#898781"
GRIDLINE = "#2c2c2a"
BASELINE = "#383835"
# Fixed categorical slot order; never cycled (at most eight series are drawn).
CATEGORICAL = (
    "#3987e5",
    "#d95926",
    "#199e70",
    "#c98500",
    "#d55181",
    "#008300",
    "#9085e9",
    "#e66767",
)
SEQUENTIAL = ("#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b")
_HEATMAP_CMAP = LinearSegmentedColormap.from_list("catan_blue", SEQUENTIAL)

LEAD_POSITIVE = CATEGORICAL[0]  # winner ahead (diverging pair, blue)
LEAD_NEGATIVE = CATEGORICAL[7]  # winner behind (diverging pair, red)

_FIGSIZE = (10, 6)
_DPI = 120
_GAP = 2  # px-ish surface-colored gap between adjacent fills
_MAX_NAMED_SOURCES = 7  # points-by-source: named sources before "Other"

# --- source labels --------------------------------------------------------
# Short, ASCII-only display labels.  The catalog in ``scoring.score_sources``
# is the source of truth for which keys exist and for their long labels; the
# overrides only shorten the ones that would crowd an axis.
_SHORT_LABELS = {
    "settlements": "Settlements",
    "longest_trade_route": "Trade Route",
    "vp_cards": "VP Cards",
    "metropolis_bonus": "Metropolis",
    "defender_of_catan": "Defender",
    "scenario_points": "Scenario",
}
_ALL_GAME_TYPES = ("normal", "seafarers", "cities_knights", "seafarers_cities_knights")
_CATALOG_LABELS = {
    source.key: source.label for game_type in _ALL_GAME_TYPES for source in score_sources(game_type)
}


# Fixed catalog labels for the game type shown in the image title.
GAME_TYPE_LABELS = {
    "normal": "Normal",
    "seafarers": "Seafarers",
    "cities_knights": "Cities & Knights",
    "seafarers_cities_knights": "Seafarers + Cities & Knights",
}


def game_type_label(game_type: str) -> str:
    """Catalog label for a game type; unknown keys become a safe ASCII title."""
    label = GAME_TYPE_LABELS.get(game_type)
    if label is not None:
        return label
    words = "".join(c if c.isascii() and (c.isalnum() or c == "-") else " " for c in game_type)
    return " ".join(words.split()).title()[:40] or "Other"


def _image_title(kind: str, view: ChartInsightsView) -> str:
    """The in-image title; names the game type whenever the view is for one."""
    game_type = view.filter.game_type
    if game_type is None:
        return CHART_TITLES[kind]
    return f"{CHART_TITLES[kind]} · {game_type_label(game_type)}"


def _ascii(text: str) -> str:
    return text.encode("ascii", "ignore").decode("ascii").strip()


def source_label(key: str) -> str:
    """Short ASCII display label for a score-source key (never user text)."""
    label = _SHORT_LABELS.get(key) or _CATALOG_LABELS.get(key) or key.replace("_", " ").title()
    return _ascii(label) or "Other"


# --- public API -----------------------------------------------------------
@dataclass(frozen=True, slots=True)
class RenderedChart:
    png: bytes
    title: str
    legend: tuple[tuple[str, int], ...]  # ("P1", user_id), ... in label order
    note: str | None
    season_legend: tuple[tuple[str, int], ...] = ()  # ("S1", season_id), ... in order


def render_chart(kind: str, view: ChartInsightsView) -> RenderedChart | None:
    """Render one chart; ``None`` means not enough recorded data for it.

    Raises ``ValueError("unknown chart kind")`` for a kind outside
    :data:`CHART_KINDS`.
    """
    builder = _BUILDERS.get(kind)
    if builder is None:
        raise ValueError("unknown chart kind")
    built = builder(view)
    if built is None:
        return None
    notes: list[str] = []
    pool = len(view.players) if built.pool_size is None else built.pool_size
    if built.players and pool > MAX_PLAYERS:
        shown = len(built.players)
        notes.append(
            f"Showing the {MAX_PLAYERS} most active of {pool} {built.pool_noun}."
            if shown == MAX_PLAYERS
            else f"Showing {shown} of the {MAX_PLAYERS} most active of {pool} {built.pool_noun}."
        )
    notes.extend(built.notes)
    return RenderedChart(
        _export_png(built.figure),
        CHART_TITLES[kind],
        built.players,
        " ".join(notes) or None,
        built.seasons,
    )


# --- shared helpers -------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Built:
    """A drawn figure plus the ``(label, user_id)`` players actually plotted.

    ``players`` stays empty for charts that never draw P-labels, so only the
    player charts carry a legend (and only for players that appear in them).
    """

    figure: Figure
    players: tuple[tuple[str, int], ...] = ()
    seasons: tuple[tuple[str, int], ...] = ()
    notes: tuple[str, ...] = ()  # chart-specific notes, e.g. a season cap
    # Pool the plotted players were picked from, when it is not all of view.players.
    pool_size: int | None = None
    pool_noun: str = "players"


def _labelled_players(view: ChartInsightsView) -> list[tuple[str, PlayerSummary]]:
    """``P1``..``Pn`` for the first MAX_PLAYERS players, in view order."""
    return [(f"P{i}", player) for i, player in enumerate(view.players[:MAX_PLAYERS], start=1)]


def _new_figure(title: str, subtitle: str | None = None, *, subtitle_lines: int = 1) -> Figure:
    """A dark figure with a left-aligned header; the plot area sits below it."""
    fig = Figure(figsize=_FIGSIZE, dpi=_DPI, facecolor=SURFACE, layout="constrained")
    FigureCanvasAgg(fig)  # attaches itself to the figure; no global state
    fig.text(0.02, 0.975, title, color=INK, fontsize=17, fontweight="bold", va="top")
    header = 0.88 - 0.04 * (subtitle_lines - 1) if subtitle else 0.92
    if subtitle:
        fig.text(0.02, 0.918, subtitle, color=INK_SECONDARY, fontsize=11, va="top")
    layout = fig.get_layout_engine()
    if layout is not None:
        layout.set(rect=(0, 0, 1, header))
    return fig


def _export_png(fig: Figure) -> bytes:
    buffer = BytesIO()
    fig.savefig(buffer, format="png", facecolor=SURFACE)
    return buffer.getvalue()


def _style_axes(ax: Axes, *, grid: str | None) -> None:
    """Dark-surface axes: hairline grid behind marks, muted ticks, open spines."""
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(BASELINE)
    ax.tick_params(colors=INK_MUTED, labelsize=11, length=0, pad=6)
    ax.xaxis.label.set_color(INK_SECONDARY)
    ax.yaxis.label.set_color(INK_SECONDARY)
    ax.xaxis.label.set_size(11)
    ax.yaxis.label.set_size(11)
    ax.set_axisbelow(True)
    if grid:
        ax.grid(axis=grid, color=GRIDLINE, linewidth=1)
    else:
        ax.grid(False)


def _panel_title(text: str) -> dict[str, object]:
    return {"label": text, "loc": "left", "color": INK_SECONDARY, "fontsize": 11, "pad": 10}


def _legend(fig: Figure, handles: Sequence[object], *, columns: int | None = None) -> None:
    """Figure-level legend under the plot; shown for two or more series."""
    if len(handles) < 2:
        return
    fig.legend(
        handles=list(handles),
        loc="outside lower center",
        ncols=columns or min(len(handles), 8),
        frameon=False,
        labelcolor=INK_SECONDARY,
        fontsize=11,
        handlelength=1.2,
        handleheight=1.0,
        columnspacing=1.6,
    )


def _swatch(color: str, name: str) -> Patch:
    return Patch(facecolor=color, edgecolor=SURFACE, linewidth=_GAP, label=name)


@dataclass(frozen=True, slots=True)
class _BarSeries:
    name: str
    values: list[float | None]  # None = no sample; drawn as "n/a"
    color: str
    labels: list[str]  # end-of-bar text, parallel to values


def _grouped_hbars(
    ax: Axes,
    categories: Sequence[str],
    series: Sequence[_BarSeries],
    *,
    inside_from: float | None = None,
) -> float:
    """Thin horizontal grouped bars with end labels; returns the longest bar.

    Bars at or beyond ``inside_from`` carry their label inside the bar end, so
    a full-scale axis (0-100%) never has to make room for text past the edge.
    """
    count = len(series)
    height = 0.8 / count
    longest = 0.0
    for index, item in enumerate(series):
        offset = (index - (count - 1) / 2) * height
        for row, (value, label) in enumerate(zip(item.values, item.labels, strict=True)):
            y = row + offset
            if value is None:
                ax.text(0, y, " n/a", va="center", ha="left", color=INK_MUTED, fontsize=9)
                continue
            ax.barh(
                y,
                value,
                height=height,
                color=item.color,
                edgecolor=SURFACE,
                linewidth=_GAP,
            )
            longest = max(longest, value)
            if inside_from is not None and value >= inside_from:
                ax.text(value, y, f"{label}  ", va="center", ha="right", color=INK, fontsize=9)
            else:
                ax.text(
                    value, y, f"  {label}", va="center", ha="left", color=INK_SECONDARY, fontsize=9
                )
    ax.set_yticks(range(len(categories)), labels=list(categories))
    ax.set_ylim(len(categories) - 0.4, -0.6)  # first category on top
    return longest


# --- 1. winning-formula ---------------------------------------------------
def _series_size(samples: dict[str, int]) -> int:
    """Scored players behind a composition (every catalog has settlements)."""
    return max(samples.values(), default=0)


def _composition_per_player(
    composition: dict[str, Fraction], samples: dict[str, int]
) -> dict[str, float]:
    """Average points per scored player from each source (absent = 0)."""
    size = _series_size(samples)
    return {
        key: float(average) * samples.get(key, size) / size
        for key, average in composition.items()
        if size
    }


def _build_winning_formula(view: ChartInsightsView) -> _Built | None:
    meta = view.meta
    winners, others = meta.winner_composition, meta.loser_composition
    if not meta.scored_games or not winners:
        return None
    winner_n = _series_size(meta.winner_composition_samples)
    other_n = _series_size(meta.loser_composition_samples)
    win_avg = _composition_per_player(winners, meta.winner_composition_samples)
    other_avg = _composition_per_player(others, meta.loser_composition_samples)
    keys = sorted(set(win_avg) | set(other_avg), key=lambda k: (-win_avg.get(k, 0.0), k))
    fig = _new_figure(
        _image_title("winning-formula", view),
        f"Winners: {winner_n} scored games · Everyone else: {other_n} scored appearances",
    )
    ax = fig.add_subplot()
    _style_axes(ax, grid="x")
    series = []
    for name, averages, color in (
        ("Winners", win_avg, CATEGORICAL[0]),
        ("Everyone else", other_avg, CATEGORICAL[1]),
    ):
        values: list[float | None] = [averages.get(k, 0.0) for k in keys]
        labels = [f"{averages.get(k, 0.0):.1f}" for k in keys]
        series.append(_BarSeries(name, values, color, labels))
    longest = _grouped_hbars(ax, [source_label(k) for k in keys], series)
    ax.set_xlim(0, max(longest * 1.15, 1))
    ax.set_xlabel("Average points per player")
    _legend(fig, [_swatch(s.color, s.name) for s in series])
    return _Built(fig)


# --- 2. points-by-source --------------------------------------------------
def _per_game_contributions(player: PlayerSummary) -> dict[str, float]:
    """Average points per scored game contributed by each source (absent = 0)."""
    return {
        key: float(average)
        * player.source_samples.get(key, player.scored_games)
        / player.scored_games
        for key, average in player.source_averages.items()
    }


def _build_points_by_source(view: ChartInsightsView) -> _Built | None:
    # Each source's contribution is its average over ALL of the player's scored
    # games (absent = 0), so a bar's height is exactly that player's avg_points.
    players = [
        (label, p, _per_game_contributions(p))
        for label, p in _labelled_players(view)
        if p.scored_games and p.source_averages
    ]
    if not players:
        return None
    totals: dict[str, float] = {}
    for _, _, contributions in players:
        for key, value in contributions.items():
            totals[key] = totals.get(key, 0.0) + value
    ranked = sorted((k for k in totals if totals[k] > 0), key=lambda k: (-totals[k], k))
    if not ranked:
        return None
    named, rest = ranked[:_MAX_NAMED_SOURCES], ranked[_MAX_NAMED_SOURCES:]
    layers: list[tuple[str, list[float], str]] = [
        (
            source_label(key),
            [contributions.get(key, 0.0) for _, _, contributions in players],
            CATEGORICAL[slot],
        )
        for slot, key in enumerate(named)
    ]
    if rest:
        other = [sum(c.get(k, 0.0) for k in rest) for _, _, c in players]
        layers.append(("Other", other, CATEGORICAL[7]))
    players_drawn = [(label, p) for label, p, _ in players]

    fig = _new_figure(
        _image_title("points-by-source", view),
        "Average points per game by source, over each player's scored games",
    )
    ax = fig.add_subplot()
    _style_axes(ax, grid="y")
    x = list(range(len(players_drawn)))
    bottoms = [0.0] * len(players_drawn)
    for _, values, color in layers:
        ax.bar(
            x,
            values,
            bottom=bottoms,
            width=0.5,
            color=color,
            edgecolor=SURFACE,
            linewidth=_GAP,
        )
        bottoms = [b + v for b, v in zip(bottoms, values, strict=True)]
    for xi, total in zip(x, bottoms, strict=True):
        ax.text(xi, total, f"{total:.1f}", ha="center", va="bottom", color=INK, fontsize=11)
    ax.set_xticks(x, labels=[label for label, _ in players_drawn])
    ax.set_xlim(-0.6, len(players_drawn) - 0.4)
    ax.set_ylim(0, max(bottoms) * 1.12)
    ax.set_ylabel("Average points per game")
    _legend(fig, [_swatch(color, name) for name, _, color in layers], columns=4)
    return _Built(fig, tuple((label, p.user_id) for label, p in players_drawn))


# --- 3. award-impact ------------------------------------------------------
def _build_award_impact(view: ChartInsightsView) -> _Built | None:
    stats: list[AwardStat] = [
        s for s in view.meta.awards.values() if s.opportunities and (s.held or s.games_without)
    ]
    if not stats:
        return None

    def sort_key(stat: AwardStat) -> tuple[float, str]:
        held = stat.win_rate_when_held
        return (-(float(held) if held is not None else -1.0), stat.key)

    stats.sort(key=sort_key)
    fig = _new_figure(
        _image_title("award-impact", view),
        "How often a player wins when holding an award, versus when not",
    )
    ax = fig.add_subplot()
    _style_axes(ax, grid="x")

    def make(name: str, color: str, rate_of: Callable[[AwardStat], tuple[object, int, int]]):
        values: list[float | None] = []
        labels: list[str] = []
        for stat in stats:
            rate, wins, games = rate_of(stat)
            if rate is None:
                values.append(None)
                labels.append("")
            else:
                values.append(float(rate) * 100)  # type: ignore[arg-type]
                labels.append(f"{float(rate) * 100:.0f}%  ({wins}/{games})")  # type: ignore[arg-type]
        return _BarSeries(name, values, color, labels)

    series = [
        make(
            "With award",
            CATEGORICAL[0],
            lambda s: (s.win_rate_when_held, s.wins_when_held, s.held),
        ),
        make(
            "Without award",
            CATEGORICAL[1],
            lambda s: (s.win_rate_without, s.wins_without, s.games_without),
        ),
    ]
    _grouped_hbars(ax, [source_label(s.key) for s in stats], series, inside_from=60)
    ax.set_xlim(0, 100)  # the data scale; long bars carry their label inside
    ax.set_xticks([0, 20, 40, 60, 80, 100])
    ax.xaxis.set_major_formatter(PercentFormatter(100))
    ax.set_xlabel("Win rate (wins / player-games)")
    _legend(fig, [_swatch(s.color, s.name) for s in series])
    return _Built(fig)


# --- 4. win-rate-trend ----------------------------------------------------
def _build_win_rate_trend(view: ChartInsightsView) -> _Built | None:
    plotted = [
        (index, label, player)
        for index, (label, player) in enumerate(_labelled_players(view))
        if view.timeline.get(player.user_id)
    ]
    lines = [(label, CATEGORICAL[index], view.timeline[p.user_id]) for index, label, p in plotted]
    if not lines:
        return None
    dates = {day for _, _, points in lines for day, _ in points}
    by_date = len(dates) >= 2  # one day of games has no time axis to speak of

    fig = _new_figure(
        _image_title("win-rate-trend", view), "Each player's win rate after every game they played"
    )
    ax = fig.add_subplot()
    _style_axes(ax, grid="y")
    for label, color, points in lines:
        xs: list[date] | list[int] = (
            [day for day, _ in points] if by_date else list(range(1, len(points) + 1))
        )
        ys = [float(rate) * 100 for _, rate in points]
        ax.plot(
            xs,
            ys,
            color=color,
            linewidth=2,
            marker="o",
            markevery=[len(ys) - 1],
            markersize=8,
            markeredgecolor=SURFACE,
            markeredgewidth=_GAP,
            clip_on=False,
            label=label,
        )
    ax.set_ylim(0, 100)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=5, steps=[1, 2, 5, 10]))
    ax.yaxis.set_major_formatter(PercentFormatter(100))
    if by_date:
        locator = AutoDateLocator(minticks=3, maxticks=7)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(ConciseDateFormatter(locator, show_offset=False))
        ax.set_xlabel("Date of game")
    else:
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
        ax.set_xlabel("Games played")
    ax.set_ylabel("Cumulative win rate")
    _legend(
        fig,
        [Line2D([], [], color=color, linewidth=2, label=label) for label, color, _ in lines],
    )
    return _Built(fig, tuple((label, p.user_id) for _, label, p in plotted))


# --- 5. winning-scores ----------------------------------------------------
def _histogram(ax: Axes, distribution: dict[int, int], *, color: str, mean: float | None) -> None:
    lo, hi = min(distribution), max(distribution)
    xs = list(range(lo, hi + 1))
    counts = [distribution.get(x, 0) for x in xs]
    ax.bar(xs, counts, width=0.7, color=color, edgecolor=SURFACE, linewidth=_GAP)
    if len(xs) <= 16:
        for x, count in zip(xs, counts, strict=True):
            if count:
                ax.text(
                    x, count, str(count), ha="center", va="bottom", color=INK_SECONDARY, fontsize=9
                )
    if mean is not None:
        ax.axvline(mean, color=INK_MUTED, linewidth=1, linestyle=(0, (4, 3)), zorder=1)
        ax.text(
            mean,
            1.0,
            f" avg {mean:.1f}",
            transform=ax.get_xaxis_transform(),
            ha="left",
            va="top",
            color=INK_SECONDARY,
            fontsize=9,
        )
    ax.set_xlim(lo - 0.8, hi + 0.8)
    ax.set_ylim(0, max(counts) * 1.2)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=8))
    ax.yaxis.set_major_locator(MaxNLocator(integer=True, nbins=5))


def _build_winning_scores(view: ChartInsightsView) -> _Built | None:
    meta = view.meta
    if not meta.scored_games or not meta.winning_score_distribution:
        return None
    fig = _new_figure(_image_title("winning-scores", view))
    score_ax, margin_ax = fig.subplots(1, 2)
    for ax in (score_ax, margin_ax):
        _style_axes(ax, grid="y")
    _histogram(
        score_ax,
        meta.winning_score_distribution,
        color=CATEGORICAL[0],
        mean=float(meta.avg_winning_score) if meta.avg_winning_score is not None else None,
    )
    score_ax.set_xlabel("Winning score (points)")
    score_ax.set_ylabel("Games")
    score_ax.set_title(**_panel_title(f"Winning score ({meta.scored_games} scored games)"))
    if meta.margin_distribution:
        _histogram(
            margin_ax,
            meta.margin_distribution,
            color=CATEGORICAL[0],
            mean=float(meta.avg_margin) if meta.avg_margin is not None else None,
        )
        margin_ax.set_xlabel("Points ahead of the runner-up")
        margin_ax.set_ylabel("Games")
    else:
        margin_ax.set_xticks([])
        margin_ax.set_yticks([])
        margin_ax.text(
            0.5,
            0.5,
            "No fully scored games yet",
            transform=margin_ax.transAxes,
            ha="center",
            va="center",
            color=INK_MUTED,
            fontsize=11,
        )
    margin_ax.set_title(
        **_panel_title(f"Winning margin ({meta.margin_samples} fully scored games)")
    )
    return _Built(fig)


# --- 6. head-to-head ------------------------------------------------------
def _ink_for(rgb: tuple[float, float, float]) -> str:
    """Pick the ink with more contrast against a cell fill."""
    r, g, b = rgb
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return SURFACE if luminance > 0.55 else INK


def _build_head_to_head(view: ChartInsightsView) -> _Built | None:
    players = _labelled_players(view)
    if len(players) < 2:
        return None
    pairs = {(h.player_a, h.player_b): h for h in view.head_to_head if h.games_together}
    shown = {p.user_id for _, p in players}
    if not any(a in shown and b in shown for a, b in pairs):
        return None

    fig = _new_figure(
        _image_title("head-to-head", view),
        "Share of shared games the row player won (wins / games together)",
    )
    ax = fig.add_subplot()
    _style_axes(ax, grid=None)
    ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_visible(False)
    count = len(players)
    for row, (_, rower) in enumerate(players):
        for col, (_, other) in enumerate(players):
            fill, text, ink = GRIDLINE, "", INK_MUTED
            if row != col:
                low, high = sorted((rower.user_id, other.user_id))
                record = pairs.get((low, high))
                if record is None:
                    text, fill = "—", SURFACE
                else:
                    wins = record.a_wins if rower.user_id == record.player_a else record.b_wins
                    rgba = _HEATMAP_CMAP(wins / record.games_together)
                    fill = rgba
                    text = f"{wins}/{record.games_together}"
                    ink = _ink_for(rgba[:3])
            ax.add_patch(
                Rectangle(
                    (col - 0.5, row - 0.5),
                    1,
                    1,
                    facecolor=fill,
                    edgecolor=SURFACE,
                    linewidth=_GAP,
                )
            )
            if text:
                ax.text(col, row, text, ha="center", va="center", color=ink, fontsize=11)
    labels = [label for label, _ in players]
    ax.set_xticks(range(count), labels=labels)
    ax.set_yticks(range(count), labels=labels)
    ax.set_xlim(-0.5, count - 0.5)
    ax.set_ylim(count - 0.5, -0.5)
    ax.set_xlabel("Opponent")
    ax.set_ylabel("Player")
    colorbar = fig.colorbar(
        ScalarMappable(norm=Normalize(0, 100), cmap=_HEATMAP_CMAP),
        ax=ax,
        fraction=0.04,
        pad=0.03,
    )
    colorbar.outline.set_visible(False)
    colorbar.ax.tick_params(colors=INK_MUTED, labelsize=10, length=0)
    colorbar.ax.yaxis.set_major_formatter(PercentFormatter(100))
    colorbar.set_ticks([0, 25, 50, 75, 100])
    return _Built(fig, tuple((label, p.user_id) for label, p in players))


# --- 7. season-trend ------------------------------------------------------
def _assign_slots(
    chosen: list[tuple[str, PlayerSummary]],
) -> list[tuple[int, str, PlayerSummary]]:
    """``(color slot, label, player)``: Pk keeps slot k; a P9+ takes a free slot."""
    taken = {int(label[1:]) - 1 for label, _ in chosen if int(label[1:]) <= len(CATEGORICAL)}
    free = iter(slot for slot in range(len(CATEGORICAL)) if slot not in taken)
    return [
        (
            int(label[1:]) - 1 if int(label[1:]) <= len(CATEGORICAL) else next(free),
            label,
            player,
        )
        for label, player in chosen
    ]


def _build_season_trend(view: ChartInsightsView) -> _Built | None:
    seasons = view.seasons[-MAX_SEASONS:]
    records = {sid: view.season_records.get(sid, {}) for sid in (s.season_id for s in seasons)}
    played = [sid for sid, splits in records.items() if any(r.games for r in splits.values())]
    if len(played) < 2:
        return None
    # Candidates are the players with games in the shown seasons (not merely the
    # most active overall), ranked by view order.  Labels stay stable: Pk is the
    # player's 1-based position in view.players, so this chart may show P2, P9...
    candidates = [
        (f"P{position}", player)
        for position, player in enumerate(view.players, start=1)
        if any(
            (split := records[sid].get(player.user_id)) is not None and split.games
            for sid in records
        )
    ]
    if not candidates:
        return None
    plotted = _assign_slots(candidates[:MAX_PLAYERS])

    fig = _new_figure(
        _image_title("season-trend", view),
        "Each player's win rate within every season they played; a gap means no games",
    )
    ax = fig.add_subplot()
    _style_axes(ax, grid="y")
    xs = list(range(len(seasons)))
    for index, label, player in plotted:
        ys: list[float] = []
        for sid in records:
            split = records[sid].get(player.user_id)
            ys.append(
                float(split.win_rate) * 100
                if split is not None and split.games and split.win_rate is not None
                else float("nan")  # no games that season: the line breaks here
            )
        ax.plot(
            xs,
            ys,
            color=CATEGORICAL[index],
            linewidth=2,
            marker="o",
            markersize=9,
            markeredgecolor=SURFACE,
            markeredgewidth=_GAP,
            clip_on=False,
            label=label,
        )
    ax.set_xticks(xs, labels=[f"S{i}" for i in range(1, len(seasons) + 1)])
    ax.set_xlim(-0.4, len(seasons) - 0.6)
    ax.set_ylim(0, 100)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=5, steps=[1, 2, 5, 10]))
    ax.yaxis.set_major_formatter(PercentFormatter(100))
    ax.set_xlabel("Season (oldest to newest)")
    ax.set_ylabel("Win rate in the season")
    _legend(
        fig,
        [Line2D([], [], color=CATEGORICAL[i], linewidth=2, label=label) for i, label, _ in plotted],
    )
    notes = (
        (f"Showing the latest {MAX_SEASONS} of {len(view.seasons)} seasons.",)
        if len(view.seasons) > MAX_SEASONS
        else ()
    )
    return _Built(
        fig,
        tuple((label, p.user_id) for _, label, p in plotted),
        tuple((f"S{i}", season.season_id) for i, season in enumerate(seasons, start=1)),
        notes,
        pool_size=len(candidates),
        pool_noun="players with season games",
    )


# --- 8. winning-lead ------------------------------------------------------
def _signed(value: Fraction) -> str:
    """One-decimal signed label; exact zero is ``0.0``, a tiny nonzero is ``+<0.1``."""
    text = f"{float(value):+.1f}"
    if text not in ("+0.0", "-0.0"):
        return text
    if value == 0:
        return "0.0"
    return "+<0.1" if value > 0 else "-<0.1"


def _plain(value: Fraction) -> str:
    """One-decimal unsigned-looking number that never prints ``-0.0``."""
    text = f"{float(value):.1f}"
    return "0.0" if text == "-0.0" else text


def _build_winning_lead(view: ChartInsightsView) -> _Built | None:
    meta = view.meta
    samples = meta.lead_source_samples
    if not samples or not meta.lead_sources:
        return None
    exact = dict(meta.lead_sources)
    values = {key: float(lead) for key, lead in exact.items()}
    keys = sorted(values, key=lambda k: (-values[k], k))
    margin = sum(exact.values(), Fraction())  # exact: bars sum to the average winning margin
    noun = "game" if samples == 1 else "games"
    subtitle = (
        f"Winner minus runner-up per source, over {samples} fully scored {noun};\n"
        f"bars sum to the average winning margin ({_plain(margin)} pts)"
    )
    if samples < LEAD_SOURCES_MIN:
        subtitle += f" — exploratory (under {LEAD_SOURCES_MIN} games)"
    fig = _new_figure(_image_title("winning-lead", view), subtitle, subtitle_lines=2)
    layout = fig.get_layout_engine()
    if layout is not None:
        layout.set(w_pad=0.3)  # keep the outermost tick labels off the image edge
    ax = fig.add_subplot()
    _style_axes(ax, grid="x")
    ax.spines["left"].set_visible(False)
    rows = list(range(len(keys)))
    ax.barh(
        rows,
        [values[k] for k in keys],
        height=0.6,
        color=[LEAD_POSITIVE if values[k] >= 0 else LEAD_NEGATIVE for k in keys],
        edgecolor=SURFACE,
        linewidth=_GAP,
        zorder=2,
    )
    ax.axvline(0, color=BASELINE, linewidth=1.5, zorder=3)
    for row, key in zip(rows, keys, strict=True):
        value = values[key]
        label = _signed(exact[key])
        if value >= 0:
            ax.text(value, row, f"  {label}", va="center", ha="left", color=INK, fontsize=11)
        else:
            ax.text(value, row, f"{label}  ", va="center", ha="right", color=INK, fontsize=11)
    reach = max(max(abs(v) for v in values.values()) * 1.3, 0.5)
    ax.set_xlim(-reach, reach)
    ax.set_yticks(rows, labels=[source_label(k) for k in keys])
    ax.set_ylim(len(keys) - 0.4, -0.6)  # biggest lead on top
    ax.xaxis.set_major_locator(MaxNLocator(nbins=7))
    ax.set_xlabel("Average points: winner minus runner-up")
    return _Built(fig)


_BUILDERS: dict[str, Callable[[ChartInsightsView], _Built | None]] = {
    "winning-formula": _build_winning_formula,
    "points-by-source": _build_points_by_source,
    "award-impact": _build_award_impact,
    "win-rate-trend": _build_win_rate_trend,
    "winning-scores": _build_winning_scores,
    "head-to-head": _build_head_to_head,
    "season-trend": _build_season_trend,
    "winning-lead": _build_winning_lead,
}
