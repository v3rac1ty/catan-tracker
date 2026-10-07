"""Pure Discord embed formatting and user-text escaping.

Every embed a cog sends is built here, from typed service results, and
never talks to Discord's network or the database: no `discord.Client`
calls, no `catan_bot.db.repositories`, no `asyncpg`. Cogs render a response
by calling one of the `build_*_embed` functions below and passing the
result to `interaction.response.send_message`/`followup.send` with
`allowed_mentions=discord.AllowedMentions.none()`.

Escaping contract (see CLAUDE.md and DESIGN.md's M4 display notes):
  - Every piece of *free text a user typed or that came back out of the
    database* (a season name, a void reason) is passed through
    `escape_user_text` before it goes anywhere near an embed.
  - User *mentions* are built with `mention`/`channel_mention`/
    `role_mention` from a plain `int` id only -- never from stored text --
    and are never passed through `escape_user_text`: `escape_mentions`
    would corrupt the very `<@id>`/`<#id>`/`<@&id>` syntax those helpers
    produce, since that syntax is indistinguishable from a real mention.
  - Every field is truncated to its Discord limit right before it's
    attached to the embed, via `_add_field`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, time
from fractions import Fraction
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import discord

from catan_bot.db.models import (
    Event,
    Game,
    GameWithParticipants,
    GuildConfig,
    RsvpCounts,
    RsvpRoster,
    Season,
)

if TYPE_CHECKING:
    from catan_bot.charts import RenderedChart

from catan_bot.domain.analytics import (
    BOARD_LEADER_MIN,
    CLOSE_FINISH_MIN,
    FAIR_SHARE_MIN,
    INGREDIENTS_MIN,
    LEAD_SOURCES_MIN,
    OVERSHOOT_MIN,
    AwardStat,
    MetaSummary,
    PlayerSummary,
    RecordSplit,
)
from catan_bot.domain.ranking import PlayerMovement, RankedPlayer
from catan_bot.domain.scoring import GameRules, ScoreSource, score_sources
from catan_bot.services.results import (
    Announcement,
    ChartInsightsView,
    HeadToHeadView,
    InsightsFilter,
    Leaderboard,
    LeaderboardPost,
    MetaInsightsView,
    OpponentRecord,
    PlayerInsightsView,
    PlayerStatsView,
    ScoreCollectionStatus,
    SeasonInfo,
    SeasonResolution,
)

# ---------------------------------------------------------------------------
# Discord embed limits (see DESIGN.md's M4 display notes).
# ---------------------------------------------------------------------------

EMBED_TITLE_MAX = 256
EMBED_FIELD_NAME_MAX = 256
EMBED_FIELD_VALUE_MAX = 1024
EMBED_DESCRIPTION_MAX = 4096
EMBED_MAX_FIELDS = 25
EMBED_TOTAL_MAX = 6000

# ---------------------------------------------------------------------------
# escape_user_text: strip display-spoofing -> collapse zalgo -> escape.
# ---------------------------------------------------------------------------

_ZERO_WIDTH_NON_JOINER = "\u200c"
_ZERO_WIDTH_JOINER = "\u200d"
_TEXT_VARIATION_SELECTOR = "\ufe0e"
_EMOJI_VARIATION_SELECTOR = "\ufe0f"

# Default_Ignorable_Code_Point ranges that are not all covered by the Cf
# category. Emoji VS15/VS16 and join controls are handled contextually below.
_DEFAULT_IGNORABLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)

_SHAPING_SCRIPT_RANGES = (
    (0x0600, 0x08FF),
    (0x0900, 0x0D7F),
    (0x0F00, 0x109F),
    (0x1780, 0x18AF),
    (0xA840, 0xA87F),
    (0x10A00, 0x10A7F),
    (0x11000, 0x11FFF),
)

_CHANNEL_MENTION_RE = re.compile(r"<#(?=[0-9]{17,20}>)")

_MAX_CONSECUTIVE_COMBINING_MARKS = 4
_MARK_CATEGORIES = frozenset({"Mn", "Me"})


def _in_ranges(code: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start <= code <= end for start, end in ranges)


def _is_noncharacter(code: int) -> bool:
    return (0xFDD0 <= code <= 0xFDEF) or (code & 0xFFFE) == 0xFFFE


def _is_emoji_base(ch: str) -> bool:
    code = ord(ch)
    return 0x2300 <= code <= 0x23FF or 0x2600 <= code <= 0x27BF or 0x1F000 <= code <= 0x1FAFF


def _is_shaping_character(ch: str) -> bool:
    return _in_ranges(ord(ch), _SHAPING_SCRIPT_RANGES) and unicodedata.category(ch)[0] in "LM"


def _neighbor(text: str, index: int, direction: int) -> str | None:
    index += direction
    while 0 <= index < len(text):
        ch = text[index]
        code = ord(ch)
        if ch in {_TEXT_VARIATION_SELECTOR, _EMOJI_VARIATION_SELECTOR} or (
            0x1F3FB <= code <= 0x1F3FF
        ):
            index += direction
            continue
        return ch
    return None


def _keep_join_control(text: str, index: int) -> bool:
    previous = _neighbor(text, index, -1)
    following = _neighbor(text, index, 1)
    if previous is None or following is None:
        return False
    ch = text[index]
    if ch == _ZERO_WIDTH_JOINER and _is_emoji_base(previous) and _is_emoji_base(following):
        return True
    return _is_shaping_character(previous) and _is_shaping_character(following)


def _is_display_spoofing(text: str, index: int) -> bool:
    ch = text[index]
    code = ord(ch)
    if ch in {_ZERO_WIDTH_NON_JOINER, _ZERO_WIDTH_JOINER}:
        return not _keep_join_control(text, index)
    if ch in {_TEXT_VARIATION_SELECTOR, _EMOJI_VARIATION_SELECTOR}:
        previous = _neighbor(text, index, -1)
        return previous is None or not _is_emoji_base(previous)
    if _in_ranges(code, _DEFAULT_IGNORABLE_RANGES):
        return True
    if code == 0x2800 or _is_noncharacter(code):
        return True
    category = unicodedata.category(ch)
    if category in {"Cc", "Cf", "Co", "Cs", "Zl", "Zp"}:
        return ch != "\n"
    return category == "Zs" and ch != " "


def _strip_display_spoofing(text: str) -> str:
    return "".join(ch for index, ch in enumerate(text) if not _is_display_spoofing(text, index))


def _collapse_combining_marks(text: str) -> str:
    out: list[str] = []
    run = 0
    for ch in text:
        if unicodedata.category(ch) in _MARK_CATEGORIES:
            run += 1
            if run > _MAX_CONSECUTIVE_COMBINING_MARKS:
                continue
        else:
            run = 0
        out.append(ch)
    return "".join(out)


def escape_user_text(text: str) -> str:
    """Escape one piece of free text a user typed, or that came back out of storage.

    Pipeline: strip invisible display-spoofing characters, collapse a
    "zalgo" flood of stacked combining marks, then escape Discord markdown
    and mentions. Stripping happens first so it cannot remove the zero-width
    separator deliberately inserted by Discord's mention escaper.
    Truncation to a specific Discord limit is a separate step
    (`_add_field`/`truncate`) applied at the embed-building call site, since
    the limit depends on where the text lands (title vs. field vs. description).
    """
    stripped = _strip_display_spoofing(text)
    collapsed = _collapse_combining_marks(stripped)
    escaped = discord.utils.escape_markdown(collapsed)
    escaped = discord.utils.escape_mentions(escaped)
    return _CHANNEL_MENTION_RE.sub("<#\u200b", escaped)


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


# ---------------------------------------------------------------------------
# Mentions: built from int ids only, never from text, never escaped.
# ---------------------------------------------------------------------------


def mention(user_id: int) -> str:
    """A user mention, built from a plain `int` id only.

    Never pass the result through `escape_user_text`: `escape_mentions`
    exists specifically to neutralize this exact syntax.
    """
    if type(user_id) is not int:
        raise TypeError(f"mention() requires an int user id, got {user_id!r}")
    return f"<@{user_id}>"


def channel_mention(channel_id: int) -> str:
    if type(channel_id) is not int:
        raise TypeError(f"channel_mention() requires an int channel id, got {channel_id!r}")
    return f"<#{channel_id}>"


def role_mention(role_id: int) -> str:
    if type(role_id) is not int:
        raise TypeError(f"role_mention() requires an int role id, got {role_id!r}")
    return f"<@&{role_id}>"


def format_win_rate(win_rate: Fraction) -> str:
    return f"{float(win_rate) * 100:.1f}%"


def _discord_timestamp(value: datetime, style: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Discord timestamps require an aware datetime")
    return f"<t:{int(value.timestamp())}:{style}>"


def _add_field(
    embed: discord.Embed,
    name: str,
    value: str,
    *,
    inline: bool,
    budget: int | None = None,
) -> None:
    """Add a field without exceeding any Discord field or aggregate limit."""
    if len(embed.fields) >= EMBED_MAX_FIELDS:
        return
    remaining = EMBED_TOTAL_MAX - len(embed)
    if budget is not None:
        remaining = min(remaining, budget)
    if remaining < 2:
        return
    value_reserve = min(len(value), EMBED_FIELD_VALUE_MAX, max(1, remaining // 2))
    safe_name = truncate(name, min(EMBED_FIELD_NAME_MAX, remaining - value_reserve))
    safe_value = truncate(value, min(EMBED_FIELD_VALUE_MAX, remaining - len(safe_name)))
    if not safe_name or not safe_value:
        return
    embed.add_field(
        name=safe_name,
        value=safe_value,
        inline=inline,
    )


def _add_field_rows(embed: discord.Embed, rows: Sequence[tuple[str, str]]) -> None:
    """Share the aggregate budget across rows so a 25-row result stays complete."""
    for index, (name, value) in enumerate(rows):
        rows_left = len(rows) - index
        budget = (EMBED_TOTAL_MAX - len(embed)) // rows_left
        _add_field(embed, name, value, inline=False, budget=budget)


def _set_description(embed: discord.Embed, description: str) -> None:
    available = max(EMBED_TOTAL_MAX - len(embed), 0)
    embed.description = truncate(description, min(EMBED_DESCRIPTION_MAX, available))


_STATUS_LABELS: dict[str, str] = {
    "pending": "Pending confirmation",
    "confirmed": "Confirmed",
    "rejected": "Rejected",
    "voided": "Voided",
}


def _status_label(status: str) -> str:
    return _STATUS_LABELS.get(status, status.capitalize())


_GAME_TYPE_LABELS = {
    "normal": "Normal",
    "seafarers": "Seafarers",
    "cities_knights": "Cities & Knights",
    "seafarers_cities_knights": "Seafarers + Cities & Knights",
}


def _game_type_label(game_type: str) -> str:
    """Return a safe, human-friendly label for a stored game type."""

    return _GAME_TYPE_LABELS.get(game_type, escape_user_text(game_type))


def _game_rules(game: Game) -> GameRules | None:
    """Build display rules from a game, tolerating legacy/corrupt metadata."""

    try:
        return GameRules(
            game_type=game.game_type,
            extension_5_6=game.extension_5_6,
            scenario=game.scenario,
            target_points=game.target_points,
        )
    except (TypeError, ValueError):
        return None


def format_time_12h(value: time) -> str:
    """Render a wall-clock time as 12-hour with AM/PM, e.g. `7:30 PM` (no leading zero).

    `%-I` (no leading zero) is a glibc/macOS-only strftime extension, not
    portable to Windows -- so this computes the 12-hour hour directly
    instead of formatting with `%I` and stripping a zero, which also
    sidesteps any locale dependence in `%p`'s "AM"/"PM" spelling.
    """
    hour_12 = value.hour % 12 or 12
    period = "AM" if value.hour < 12 else "PM"
    return f"{hour_12}:{value.minute:02d} {period}"


def _played_label(game: Game) -> str:
    """Render the stable game date and, when available, its local time."""

    date_label = game.played_on.isoformat()
    if game.played_at is None or not game.played_timezone:
        return f"{date_label}\nTime not recorded"
    try:
        zone = ZoneInfo(game.played_timezone)
        if game.played_at.tzinfo is None or game.played_at.utcoffset() is None:
            return f"{date_label}\nTime not recorded"
        local_time = game.played_at.astimezone(zone)
    except (KeyError, TypeError, ValueError):
        return f"{date_label}\nTime not recorded"
    timezone = escape_user_text(game.played_timezone)
    return f"{date_label} at {format_time_12h(local_time.time())} ({timezone})"


def _history_played_label(game: Game) -> str:
    """Keep history field names single-line while retaining missing-time detail."""

    return _played_label(game).replace("\n", " — ")


def _add_game_details(embed: discord.Embed, game: Game) -> None:
    """Add rule metadata shared by pending and lifecycle game embeds."""

    _add_field(embed, "Game type", _game_type_label(game.game_type), inline=True)
    if game.extension_5_6:
        _add_field(embed, "Extension", "5–6 Player Extension", inline=True)
    if game.scenario:
        _add_field(embed, "Scenario", escape_user_text(game.scenario), inline=True)
    if game.target_points is not None:
        _add_field(embed, "Target", f"{game.target_points} points", inline=True)
    # Keep the historical field name while including optional local time and
    # timezone in the value for newer reports.
    _add_field(embed, "Date", _played_label(game), inline=True)


_SCORE_LABELS = {
    "settlements": "Houses",
    "cities": "Cities",
    "longest_road": "Longest road",
    "longest_trade_route": "Trade route",
    "largest_army": "Largest army",
    "vp_cards": "VP cards",
    "metropolis_bonus": "Metropolis",
    "defender_of_catan": "Defender",
    "merchant": "Merchant",
    "constitution": "Constitution",
    "printer": "Printer",
    "scenario_points": "Scenario",
}


def _score_source_label(source: ScoreSource) -> str:
    return _SCORE_LABELS.get(source.key, source.label)


def format_game_score_table(created: GameWithParticipants) -> str:
    """Render a compact P1..P6 score table, preserving NULL as an em dash.

    Discord member mentions are kept in the legend rather than the table so
    long display names never widen or truncate the score columns.  The
    returned text is safe to put in an embed delivered with AllowedMentions.none().
    """

    if not created.scores:
        return "Points not recorded"

    player_ids = (created.winner_id, *created.loser_ids)
    labels = [f"P{index}" for index in range(1, len(player_ids) + 1)]
    legend = "Players: " + " • ".join(
        f"{label} {mention(user_id)}" for label, user_id in zip(labels, player_ids, strict=True)
    )
    rules = _game_rules(created.game)
    sources = score_sources(rules) if rules is not None else ()
    score_by_player = {score.user_id: score for score in created.scores}
    entries_by_player = {
        user_id: {entry.key: entry.points for entry in score.breakdown}
        for user_id, score in score_by_player.items()
    }

    row_labels = [_score_source_label(source)[:12] for source in sources]
    row_labels.append("Total")
    row_label_width = max((len(label) for label in row_labels), default=5)
    separator = " | "
    header_cells = [f"{label:>3}" for label in labels]
    table_rows = [separator.join((f"{'':<{row_label_width}}", *header_cells))]
    for source in sources:
        cells = [
            str(entries_by_player.get(user_id, {}).get(source.key, "—")) for user_id in player_ids
        ]
        value_cells = [f"{cell:>3}" for cell in cells]
        table_rows.append(
            separator.join((f"{_score_source_label(source)[:12]:<{row_label_width}}", *value_cells))
        )
    total_cells = [
        str(score_by_player[user_id].total_points) if user_id in score_by_player else "—"
        for user_id in player_ids
    ]
    table_rows.append(
        separator.join((f"{'Total':<{row_label_width}}", *[f"{cell:>3}" for cell in total_cells]))
    )
    return legend + "\n```text\n" + "\n".join(table_rows) + "\n```"


def _add_game_scores(embed: discord.Embed, created: GameWithParticipants) -> None:
    _add_field(embed, "Point breakdown", format_game_score_table(created), inline=False)


def _score_collection_icon_line(status: ScoreCollectionStatus) -> str:
    """One ✅/⏳/🚫 icon per participant, in `game_score_requests` order."""

    icons: list[str] = []
    for request in status.requests:
        if request.submitted:
            icons.append(f"✅ {mention(request.user_id)}")
        elif request.delivery_status == "blocked":
            icons.append(f"🚫 {mention(request.user_id)} (DMs closed, run /game scores)")
        else:
            icons.append(f"⏳ {mention(request.user_id)}")
    return "   ".join(icons)


def _add_score_collection_field(embed: discord.Embed, status: ScoreCollectionStatus | None) -> None:
    """The public message's live per-player score-entry progress (Phase 2).

    `status` is `None` for a legacy game reported before per-player DM
    collection existed (no `game_score_requests` rows at all) and for a
    caller that hasn't fetched collection status -- either way, the field is
    simply omitted rather than shown empty or wrong.
    """

    if status is None or not status.requests:
        return
    total = len(status.requests)
    submitted = len(status.submitted_ids)
    if status.complete:
        value = f"All scores received ({submitted}/{total})."
    else:
        value = f"{submitted} of {total} received\n" + _score_collection_icon_line(status)
    _add_field(embed, "Score entry", value, inline=False)


def _updated_at_label(value: datetime | None) -> str:
    """Render an audit timestamp without trusting or exposing user text."""

    if value is None:
        return "Time not recorded"
    if value.tzinfo is not None and value.utcoffset() is not None:
        try:
            return _discord_timestamp(value, "f")
        except (OverflowError, OSError, ValueError):
            pass
    # A naive value is legacy/corrupt data, but ISO formatting is still a
    # bounded, non-user-controlled fallback that is useful to administrators.
    return value.isoformat()


def _add_game_audit(embed: discord.Embed, game: Game) -> None:
    """Show the current correction metadata for revised confirmed games."""

    revision = getattr(game, "revision", 0)
    if type(revision) is not int or revision <= 0:
        return
    updated_by = getattr(game, "updated_by", None)
    updated_by_text = mention(updated_by) if type(updated_by) is int else "Unknown"
    _add_field(embed, "Revision", str(revision), inline=True)
    _add_field(embed, "Updated by", updated_by_text, inline=True)
    _add_field(
        embed,
        "Updated at",
        _updated_at_label(getattr(game, "updated_at", None)),
        inline=True,
    )
    reason = getattr(game, "update_reason", None)
    if reason:
        _add_field(embed, "Update reason", escape_user_text(reason), inline=False)


# Escape sequences (not raw glyphs) so `tests/static/test_source_hygiene.py`
# never has to reason about which emoji codepoints are "default emoji
# presentation" and which need an invisible VS16 appended -- these three are
# all default-emoji already (matching the precedent `_score_collection_icon_line`
# sets with checkmark/hourglass/no-entry), but spelling them as escapes makes
# that a non-issue rather than something to get right by eye.
_MOVEMENT_ARROWS: dict[str, str] = {
    "up": "\U0001f53c",
    "down": "\U0001f53d",
    "new": "\U0001f195",
    "unchanged": "-",
}


def _movement_suffix(movement: PlayerMovement | None) -> str:
    """A trailing ` <arrow>[N]` marker, or `""` when there's nothing to show."""
    if movement is None:
        return ""
    arrow = _MOVEMENT_ARROWS[movement.direction]
    return f" {arrow}{movement.change}" if movement.change else f" {arrow}"


def _standings_lines(
    players: Sequence[RankedPlayer],
    *,
    needs_more: bool,
    min_games: int = 0,
    movements: Mapping[int, PlayerMovement] | None = None,
) -> list[str]:
    def suffix(user_id: int) -> str:
        return _movement_suffix(movements.get(user_id)) if movements is not None else ""

    if needs_more:
        return [
            f"{mention(p.user_id)} -- needs {max(min_games - p.games, 0)} more game(s)"
            f"{suffix(p.user_id)}"
            for p in players
        ]
    return [
        f"#{p.rank} {mention(p.user_id)} -- {p.wins}-{p.games - p.wins} "
        f"({format_win_rate(p.win_rate)}){suffix(p.user_id)}"
        for p in players
    ]


# ---------------------------------------------------------------------------
# Game embeds.
# ---------------------------------------------------------------------------


def build_game_report_embed(
    created: GameWithParticipants, *, collection: ScoreCollectionStatus | None = None
) -> discord.Embed:
    """A freshly reported, still-pending game. Sent publicly with Confirm/Reject buttons.

    `collection` is this game's live score-entry progress (Phase 2): passed
    whenever the caller has it (the report cog after DM fan-out, a DM
    sheet's public-message refresh), omitted for the very first send before
    `open_score_collection` has run yet.
    """
    game = created.game
    embed = discord.Embed(
        title="Game Reported",
        description="Waiting on another player to confirm this game.",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Winner", value=mention(created.winner_id), inline=True)
    losers_text = ", ".join(mention(uid) for uid in created.loser_ids)
    embed.add_field(name="Loser(s)", value=losers_text, inline=True)
    _add_game_details(embed, game)
    _add_game_scores(embed, created)
    _add_score_collection_field(embed, collection)
    embed.set_footer(text=f"Game #{game.game_id}")
    return embed


def build_game_status_embed(
    updated: GameWithParticipants, *, collection: ScoreCollectionStatus | None = None
) -> discord.Embed:
    """A game after a confirm/reject/void transition. Replaces the report embed."""
    game = updated.game
    colors = {
        "pending": discord.Color.gold(),
        "confirmed": discord.Color.green(),
        "rejected": discord.Color.red(),
        "voided": discord.Color.dark_grey(),
    }
    embed = discord.Embed(
        title="Game Report", color=colors.get(game.status, discord.Color.light_grey())
    )
    embed.add_field(name="Winner", value=mention(updated.winner_id), inline=True)
    losers_text = ", ".join(mention(uid) for uid in updated.loser_ids) or "None"
    embed.add_field(name="Loser(s)", value=losers_text, inline=True)
    _add_game_details(embed, game)
    _add_game_scores(embed, updated)
    _add_score_collection_field(embed, collection)
    embed.add_field(name="Status", value=_status_label(game.status), inline=True)
    if game.status == "confirmed" and game.confirmed_by is not None:
        embed.add_field(name="Confirmed by", value=mention(game.confirmed_by), inline=True)
    elif game.status == "rejected" and game.rejected_by is not None:
        embed.add_field(name="Rejected by", value=mention(game.rejected_by), inline=True)
    elif game.status == "voided":
        if game.voided_by is not None:
            embed.add_field(name="Voided by", value=mention(game.voided_by), inline=True)
        if game.void_reason:
            _add_field(embed, "Reason", escape_user_text(game.void_reason), inline=False)
    _add_game_audit(embed, game)
    embed.set_footer(text=f"Game #{game.game_id}")
    return embed


def build_game_history_embed(games: Sequence[Game], *, member_id: int | None) -> discord.Embed:
    embed = discord.Embed(title="Game History", color=discord.Color.blurple())
    if member_id is not None:
        embed.description = f"Showing games for {mention(member_id)}."
    if not games:
        _add_field(embed, "Games", "No games recorded yet.", inline=False)
        return embed
    rows: list[tuple[str, str]] = []
    # `position` numbers the visible rows in the listing's own date/time
    # order (1-indexed) -- purely a display aid for scanning a long history
    # at a glance. `Game #<id>` stays right after it, unabbreviated, since
    # that id (not the position) is the stable value `/game show` and
    # `/game update` take.
    for position, game in enumerate(games[:EMBED_MAX_FIELDS], start=1):
        parts = [_status_label(game.status), f"Reported by {mention(game.reported_by)}"]
        if game.status == "confirmed" and game.confirmed_by is not None:
            parts.append(f"Confirmed by {mention(game.confirmed_by)}")
        elif game.status == "rejected" and game.rejected_by is not None:
            parts.append(f"Rejected by {mention(game.rejected_by)}")
        elif game.status == "voided":
            if game.voided_by is not None:
                parts.append(f"Voided by {mention(game.voided_by)}")
            if game.void_reason:
                parts.append(f"Reason: {escape_user_text(game.void_reason)}")
        revision = getattr(game, "revision", 0)
        if type(revision) is int and revision > 0:
            updated_by = getattr(game, "updated_by", None)
            editor = mention(updated_by) if type(updated_by) is int else "unknown editor"
            parts.append(f"Updated r{revision} by {editor}")
        field_name = (
            f"{position}. Game #{game.game_id} -- {_game_type_label(game.game_type)} -- "
            f"{_history_played_label(game)}"
        )
        rows.append((field_name, " | ".join(parts)))
    _add_field_rows(embed, rows)
    return embed


# ---------------------------------------------------------------------------
# Leaderboard / stats embeds.
# ---------------------------------------------------------------------------

_SCOPE_TITLES = {"season": "Season Leaderboard", "all_time": "All-Time Leaderboard"}


def _build_leaderboard_embed(
    board: Leaderboard,
    *,
    movements: Mapping[int, PlayerMovement] | None = None,
    lead_field: tuple[str, str] | None = None,
) -> discord.Embed:
    """Shared by `build_leaderboard_embed` and `build_leaderboard_post_embed`.

    `lead_field` (the recurring post's "Today's Results") is added -- via
    the same budget-aware `_add_field` every other field here uses -- before
    the standings fields, not after: adding it afterward could exceed
    Discord's total-embed-size limit once the standings fields had already
    spent most of the budget, since `_add_field` only ever looks at how much
    room is left *at the time it's called*.
    """
    embed = discord.Embed(title=_SCOPE_TITLES[board.scope], color=discord.Color.blurple())
    if board.scope == "season":
        if board.season is None:
            embed.description = "There's no active season."
            return embed
        _set_description(embed, f"Season: {escape_user_text(board.season.name)}")

    if lead_field is not None:
        _add_field(embed, lead_field[0], lead_field[1], inline=False)

    eligible = [p for p in board.ranked if p.eligible]
    ineligible = [p for p in board.ranked if not p.eligible]

    if not board.ranked:
        _add_field(embed, "Standings", "No confirmed games yet.", inline=False)
        return embed

    standings = _standings_lines(eligible, needs_more=False, movements=movements)
    _add_field(
        embed,
        "Standings",
        "\n".join(standings) if standings else "No eligible players yet.",
        inline=False,
    )
    if ineligible:
        needs_more = _standings_lines(
            ineligible, needs_more=True, min_games=board.min_games, movements=movements
        )
        _add_field(embed, "Needs more games", "\n".join(needs_more), inline=False)
    return embed


def build_leaderboard_embed(board: Leaderboard) -> discord.Embed:
    return _build_leaderboard_embed(board)


def _day_result_line(game: GameWithParticipants) -> str:
    losers = ", ".join(mention(uid) for uid in game.loser_ids) or "no one"
    return f"{mention(game.winner_id)} beat {losers}"


def build_leaderboard_post_embed(post: LeaderboardPost) -> discord.Embed:
    """The recurring leaderboard post: today's results first, then movement-annotated standings.

    Reuses `_build_leaderboard_embed`'s field-budget-aware construction
    (via its `movements`/`lead_field` parameters) instead of building a
    second, parallel embed and copying fields over afterward -- see that
    function's docstring for why field *order* matters here, not just
    content.
    """
    movements_by_id = {movement.user_id: movement for movement in post.movements}
    lead_field: tuple[str, str] | None = None
    if post.games:
        lines = [_day_result_line(game) for game in post.games]
        lead_field = ("Today's Results", "\n".join(lines))
    embed = _build_leaderboard_embed(post.board, movements=movements_by_id, lead_field=lead_field)
    embed.title = truncate(f"{_SCOPE_TITLES[post.board.scope]} Update", EMBED_TITLE_MAX)
    return embed


def build_stats_embed(view: PlayerStatsView) -> discord.Embed:
    embed = discord.Embed(title="Player Stats", color=discord.Color.blurple())
    embed.description = mention(view.user_id)
    if view.season is not None:
        s = view.season
        value = f"{s.wins}-{s.losses} ({format_win_rate(s.win_rate)}) over {s.games} game(s)"
    else:
        value = "No active season."
    _add_field(embed, "This Season", value, inline=False)
    a = view.all_time
    _add_field(
        embed,
        "All-Time",
        f"{a.wins}-{a.losses} ({format_win_rate(a.win_rate)}) over {a.games} game(s)",
        inline=False,
    )
    return embed


# ---------------------------------------------------------------------------
# Insights embeds (/insights player | meta | head-to-head).
#
# Every number that comes from recorded scores is shown with its sample size
# ("14 scored games"), because scores are optional on a game: a player can
# have 30 games and 3 scored ones. Nothing here ever renders `None` or a raw
# `Fraction` -- missing data becomes words ("No scores recorded yet.").
# ---------------------------------------------------------------------------

_NO_SCORES_TEXT = "No scores recorded yet."
_NO_GAMES_TEXT = "No confirmed games yet."
_NO_SEASON_TEXT = "There's no active season."
_NO_DATA_TEXT = "no data"
_SEASON_NAME_DISPLAY_MAX = 100
_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_PLAY_STYLE_LABELS = {
    "city_heavy": "City-heavy",
    "settlement_heavy": "Settlement-heavy",
    "balanced": "Balanced",
}
_TIME_OF_DAY_LABELS = {
    "daytime": "Daytime",
    "evening": "Evening",
    "late_night": "Late night",
}
_MIN_WEEKDAY_GAMES = 2
_COMBO_LABELS = {
    "road_and_army": "Road + Army",
    "road_only": "Road only",
    "army_only": "Army only",
    "neither": "Neither",
}


def _all_score_sources() -> dict[str, ScoreSource]:
    """Every catalogued score source across all rule sets, in catalog order."""
    sources: dict[str, ScoreSource] = {}
    for game_type in ("normal", "seafarers", "cities_knights", "seafarers_cities_knights"):
        for source in score_sources(game_type):  # type: ignore[arg-type]
            sources.setdefault(source.key, source)
    return sources


_ALL_SOURCES = _all_score_sources()


def _source_label_for_key(key: str) -> str:
    """A display label for a stored breakdown key (stored text, so escaped)."""
    known = _SCORE_LABELS.get(key)
    if known is not None:
        return known
    source = _ALL_SOURCES.get(key)
    if source is not None:
        return source.label
    return escape_user_text(key.replace("_", " ").capitalize())


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _scored(count: int) -> str:
    """A sample-size suffix such as `14 scored games`."""
    return _plural(count, "scored game")


def _avg(value: Fraction) -> str:
    return f"{float(value):.1f}"


_EXPLORATORY_SUFFIX = " \u2014 exploratory"
_DECIDING_TOP_SOURCES = 3


def _exploratory(line: str, sample: int, minimum: int) -> str:
    """Flag a line whose sample is below its minimum as exploratory."""
    return line + _EXPLORATORY_SUFFIX if sample < minimum else line


_LEAD_TINY = Fraction(1, 20)
_PERCENT_TINY = Fraction(1, 200)


def _signed_lead(value: Fraction) -> str:
    """A signed 1-decimal lead; magnitudes that would round to 0.0 show as `<0.1`."""
    if value == 0:
        return "0.0"
    sign = "+" if value > 0 else "−"
    if abs(value) < _LEAD_TINY:
        return f"{sign}<0.1"
    return f"{sign}{_avg(abs(value))}"


def _signed_percent(value: Fraction) -> str:
    """A signed whole percent of a fraction; never rounds a positive value away to 0."""
    percent = value * 100
    if value == 0:
        return "0%"
    sign = "+" if value > 0 else "−"
    if abs(value) < _PERCENT_TINY:
        return f"{sign}<1%"
    return f"{sign}{int(abs(percent) + Fraction(1, 2))}%"


def _top_positive_sources(values: Mapping[str, Fraction]) -> list[tuple[str, Fraction]]:
    """Positive entries, largest first; ties keep catalog order."""
    order = {key: index for index, key in enumerate(_ordered_source_keys(values))}
    positive = [(key, value) for key, value in values.items() if value > 0]
    positive.sort(key=lambda item: (-item[1], order[item[0]]))
    return positive


def _rate_text(rate: Fraction | None) -> str:
    return format_win_rate(rate) if rate is not None else _NO_DATA_TEXT


def _record_text(wins: int, games: int) -> str:
    return f"{wins}-{games - wins}"


def _split_text(split: RecordSplit) -> str:
    return f"{_record_text(split.wins, split.games)} ({_rate_text(split.win_rate)})"


def _streak_text(streak: int) -> str:
    if streak > 0:
        return f"W{streak}"
    if streak < 0:
        return f"L{-streak}"
    return "none"


def _insights_filter_line(insights_filter: InsightsFilter) -> str:
    if insights_filter.scope == "season" and insights_filter.season is not None:
        name = truncate(insights_filter.season.name, _SEASON_NAME_DISPLAY_MAX)
        line = f"Season: {escape_user_text(name)}"
    else:
        line = "All-time"
    if insights_filter.game_type is not None:
        line += f" • Game type: {_game_type_label(insights_filter.game_type)}"
    if insights_filter.game_type is not None and insights_filter.game_type_defaulted:
        line += " (most played)"
    return line


_GAME_TYPE_ORDER = ("normal", "seafarers", "cities_knights", "seafarers_cities_knights")


def _also_played_line(insights_filter: InsightsFilter) -> str | None:
    """Describe other played types in stable count/catalog order."""
    shown_type = insights_filter.game_type
    catalog_order = {game_type: index for index, game_type in enumerate(_GAME_TYPE_ORDER)}
    other_types = [
        (game_type, count)
        for game_type, count in insights_filter.available_game_types.items()
        if count > 0 and game_type != shown_type
    ]
    other_types.sort(
        key=lambda item: (-item[1], catalog_order.get(item[0], len(_GAME_TYPE_ORDER)), item[0])
    )
    if not other_types:
        return None
    labels = " · ".join(
        f"{_game_type_label(game_type)} ({_plural(count, 'game')})"
        for game_type, count in other_types
    )
    return f"Also played: {labels}. Pick game_type to see them."


def _insights_description_lines(insights_filter: InsightsFilter) -> list[str]:
    """Common description prefix for insight embeds, including alternate types."""
    lines = [_insights_filter_line(insights_filter)]
    also_played = _also_played_line(insights_filter)
    if also_played is not None:
        lines.append(also_played)
    return lines


def build_chart_embed(
    view: ChartInsightsView, chart: RenderedChart, filename: str
) -> discord.Embed:
    """Build the chart attachment embed with its filters and player legend."""
    embed = discord.Embed(
        title=truncate(chart.title, EMBED_TITLE_MAX), color=discord.Color.blurple()
    )
    if view.filter.scope == "season" and view.filter.season is None:
        _set_description(embed, _NO_SEASON_TEXT)
        return embed

    lines = _insights_description_lines(view.filter)
    lines.extend(f"{label} — {mention(user_id)}" for label, user_id in chart.legend)
    note = [escape_user_text(chart.note)] if chart.note else []
    season_lines = _season_legend_lines(view, chart)
    limit = min(EMBED_DESCRIPTION_MAX, max(EMBED_TOTAL_MAX - len(embed), 0))
    lines.extend(_fit_lines(season_lines, lines, note, limit, "season"))
    lines.extend(note)
    _set_description(embed, "\n".join(lines))
    embed.set_image(url=f"attachment://{filename}")
    return embed


def _season_legend_lines(view: ChartInsightsView, chart: RenderedChart) -> list[str]:
    """`S1 — <season name>` lines; names are stored text, so always escaped."""
    if not chart.season_legend:
        return []
    names = {season.season_id: season.name for season in view.seasons}
    lines = []
    for label, season_id in chart.season_legend:
        name = names.get(season_id)
        shown = (
            escape_user_text(truncate(name, _SEASON_NAME_DISPLAY_MAX))
            if name is not None
            else f"Season #{season_id}"
        )
        lines.append(f"{label} — {shown}")
    return lines


def _fit_lines(
    candidates: Sequence[str],
    before: Sequence[str],
    after: Sequence[str],
    limit: int,
    noun: str,
) -> list[str]:
    """Keep as many whole candidate lines as fit between `before` and `after`.

    When some are dropped, one "...and N more" line replaces them so nothing
    disappears silently and no line is cut mid-name.
    """

    def total(extra: Sequence[str]) -> int:
        return len("\n".join([*before, *extra, *after]))

    kept = list(candidates)
    while kept and total(kept) > limit:
        kept.pop()
    dropped = len(candidates) - len(kept)
    while dropped and kept and total([*kept, f"…and {dropped} more {noun}s"]) > limit:
        kept.pop()
        dropped += 1
    if dropped:
        kept.append(f"…and {dropped} more {noun}s")
    return kept


def build_chart_unavailable_embed(view: ChartInsightsView, kind_title: str) -> discord.Embed:
    """Build the M2-style empty state for a chart without enough data."""
    embed = discord.Embed(
        title=truncate(kind_title, EMBED_TITLE_MAX), color=discord.Color.blurple()
    )
    if view.filter.scope == "season" and view.filter.season is None:
        _set_description(embed, _NO_SEASON_TEXT)
    else:
        lines = _insights_description_lines(view.filter)
        if view.meta.games == 0:
            lines.append(_NO_GAMES_TEXT)
        else:
            lines.append("Not enough recorded data for this chart yet.")
        _set_description(embed, "\n".join(lines))
    return embed


def _insights_embed(
    title: str,
    insights_filter: InsightsFilter,
    *,
    subject_id: int | None = None,
    has_games: bool = True,
) -> tuple[discord.Embed, bool]:
    """A titled embed plus whether the caller should go on to add data fields.

    The description carries the filter line (and the subject's mention). When
    there is nothing to show -- no active season, or no confirmed games under
    the filters -- the description says so and the caller adds no fields.
    """
    embed = discord.Embed(title=truncate(title, EMBED_TITLE_MAX), color=discord.Color.blurple())
    if insights_filter.scope == "season" and insights_filter.season is None:
        _set_description(embed, _NO_SEASON_TEXT)
        return embed, False
    lines = []
    if subject_id is not None:
        lines.append(mention(subject_id))
    lines.extend(_insights_description_lines(insights_filter))
    if not has_games:
        lines.append(_NO_GAMES_TEXT)
    _set_description(embed, "\n".join(lines))
    return embed, has_games


def _award_held_line(key: str, stat: AwardStat) -> str:
    label = _source_label_for_key(key)
    if stat.held:
        parts = [
            f"held {_rate_text(stat.held_rate)} ({stat.held}/{_scored(stat.opportunities)})",
            f"won {_rate_text(stat.win_rate_when_held)} with it",
        ]
    else:
        parts = [f"never held in {_scored(stat.opportunities)}"]
    if stat.games_without:
        parts.append(
            f"{_rate_text(stat.win_rate_without)} without ({_plural(stat.games_without, 'game')})"
        )
    return f"{label}: " + "; ".join(parts)


def _player_record_lines(summary: PlayerSummary) -> list[str]:
    lines = [
        f"{_record_text(summary.wins, summary.games)} ({_rate_text(summary.win_rate)}) "
        f"in {_plural(summary.games, 'game')}",
        f"Streak: {_streak_text(summary.current_streak)} "
        f"(best win streak {summary.longest_win_streak})",
    ]
    if summary.recent_form.games:
        lines.append(f"Last {summary.recent_form.games}: {_split_text(summary.recent_form)}")
    if summary.expected_wins is not None and summary.wins_vs_expected is not None:
        lines.append(
            _exploratory(
                f"Wins vs fair share: {summary.wins} vs {_avg(summary.expected_wins)} expected "
                f"({float(summary.wins_vs_expected):.2f}\u00d7)",
                summary.games,
                FAIR_SHARE_MIN,
            )
        )
    return lines


def _player_points_lines(summary: PlayerSummary) -> list[str]:
    if not summary.scored_games:
        return [_NO_SCORES_TEXT]
    lines: list[str] = []
    if summary.avg_points is not None:
        lines.append(f"Average: {_avg(summary.avg_points)} pts ({_scored(summary.scored_games)})")
    if summary.avg_points_in_wins is not None:
        lines.append(
            f"In wins: {_avg(summary.avg_points_in_wins)} pts ({_scored(summary.scored_wins)})"
        )
    if summary.avg_points_in_losses is not None:
        lines.append(
            f"In losses: {_avg(summary.avg_points_in_losses)} pts "
            f"({_scored(summary.scored_losses)})"
        )
    if summary.median_points is not None:
        lines.append(f"Median: {_avg(summary.median_points)} pts ({_scored(summary.scored_games)})")
    if summary.best_points is not None:
        lines.append(f"Best: {summary.best_points} pts ({_scored(summary.scored_games)})")
    if summary.avg_target_share is not None:
        lines.append(
            f"Of target: {format_win_rate(summary.avg_target_share)} avg "
            f"({_scored(summary.target_share_samples)})"
        )
    return lines or [_NO_SCORES_TEXT]


def _player_board_lines(summary: PlayerSummary) -> list[str]:
    rows = (
        ("Settlements", "settlements", summary.avg_settlements),
        ("Cities", "cities", summary.avg_cities),
        ("VP cards", "vp_cards", summary.avg_vp_cards),
        ("Metropolises", "metropolis_bonus", summary.avg_metropolises),
    )
    return [
        f"{label}: {_avg(value)} avg ({_scored(summary.source_samples.get(key, 0))})"
        for label, key, value in rows
        if value is not None
    ]


def _player_margin_lines(summary: PlayerSummary) -> list[str]:
    lines: list[str] = []
    if summary.avg_win_margin is not None:
        lines.append(
            f"Average win margin: {_avg(summary.avg_win_margin)} pts "
            f"({_scored(summary.win_margin_samples)})"
        )
    if summary.avg_loss_deficit is not None:
        lines.append(
            f"Average loss deficit: {_avg(summary.avg_loss_deficit)} pts "
            f"({_scored(summary.loss_deficit_samples)})"
        )
    if summary.loss_deficit_samples:
        lines.append(
            f"Close losses (1-2 pts): {summary.close_losses} "
            f"of {_scored(summary.loss_deficit_samples)}"
        )
    return lines


def _player_ingredient_lines(summary: PlayerSummary) -> list[str]:
    """Sources where this player's share of the target is higher in wins than losses."""
    wins, losses = summary.ingredient_win_samples, summary.ingredient_loss_samples
    if wins < 1 or losses < 1:
        return []
    shown = _top_positive_sources(summary.winning_ingredients)[:_DECIDING_TOP_SOURCES]
    if not shown:
        return []
    lines = [
        f"{_source_label_for_key(key)}: {_signed_percent(value)} of target more in wins"
        for key, value in shown
    ]
    loss_text = f"{losses} loss" if losses == 1 else f"{losses} losses"
    lines.append(
        _exploratory(f"({_plural(wins, 'win')} / {loss_text})", min(wins, losses), INGREDIENTS_MIN)
    )
    return lines


def _weekday_extreme_lines(summary: PlayerSummary) -> list[str]:
    """Best and worst weekday by win rate, among days with enough games to compare."""
    eligible = [
        (day, split)
        for day, split in summary.by_weekday.items()
        if 0 <= day < len(_WEEKDAY_NAMES)
        and split.games >= _MIN_WEEKDAY_GAMES
        and split.win_rate is not None
    ]
    if len(eligible) < 2:
        return []

    def rate(item: tuple[int, RecordSplit]) -> Fraction:
        return item[1].win_rate or Fraction(0)

    # Ties go to the day with more games, then the earlier weekday.
    best = min(eligible, key=lambda item: (-rate(item), -item[1].games, item[0]))
    worst = min(eligible, key=lambda item: (rate(item), -item[1].games, item[0]))
    if rate(best) == rate(worst):
        return []
    return [
        f"Best day: {_WEEKDAY_NAMES[best[0]]} {_split_text(best[1])}",
        f"Worst day: {_WEEKDAY_NAMES[worst[0]]} {_split_text(worst[1])}",
        f"(weekdays with {_MIN_WEEKDAY_GAMES}+ games)",
    ]


def _player_when_you_win_lines(summary: PlayerSummary) -> list[str]:
    lines = [
        f"{_TIME_OF_DAY_LABELS.get(bucket, escape_user_text(bucket))}: {_split_text(split)}"
        for bucket, split in summary.by_time_of_day.items()
    ]
    if lines:
        lines.append(f"({_plural(summary.timed_games, 'game')} with a recorded time)")
    return lines + _weekday_extreme_lines(summary)


def build_player_insights_embed(view: PlayerInsightsView) -> discord.Embed:
    """One player's record, points, board, awards, margins and splits."""
    summary = view.summary
    embed, show = _insights_embed(
        "Player Insights",
        view.filter,
        subject_id=summary.user_id,
        has_games=summary.games > 0,
    )
    if not show:
        return embed

    _add_field(embed, "Record", "\n".join(_player_record_lines(summary)), inline=False)
    _add_field(embed, "Points", "\n".join(_player_points_lines(summary)), inline=False)
    board = _player_board_lines(summary)
    if board:
        _add_field(embed, "Board", "\n".join(board), inline=False)
    awards = [_award_held_line(key, stat) for key, stat in summary.awards.items()]
    if awards:
        _add_field(embed, "Awards", "\n".join(awards), inline=False)
    margins = _player_margin_lines(summary)
    if margins:
        _add_field(embed, "Margins", "\n".join(margins), inline=False)
    ingredients = _player_ingredient_lines(summary)
    if ingredients:
        _add_field(embed, "Winning ingredients", "\n".join(ingredients), inline=False)
    when_you_win = _player_when_you_win_lines(summary)
    if when_you_win:
        _add_field(embed, "When you win", "\n".join(when_you_win), inline=False)
    if summary.by_player_count:
        counts = [
            f"{count} players: {_split_text(split)}"
            for count, split in sorted(summary.by_player_count.items())
        ]
        _add_field(embed, "By player count", "\n".join(counts), inline=True)
    return embed


def _ordered_source_keys(*key_groups: Iterable[str]) -> list[str]:
    """Catalog order for known score sources, then unknown keys alphabetically."""
    present = {key for group in key_groups for key in group}
    known = [key for key in _ALL_SOURCES if key in present]
    return known + sorted(present - set(known))


def _meta_composition_lines(view: MetaInsightsView) -> list[str]:
    meta = view.meta
    lines = []
    for key in _ordered_source_keys(meta.winner_composition, meta.loser_composition):
        winner = meta.winner_composition.get(key)
        loser = meta.loser_composition.get(key)
        winner_text = _avg(winner) if winner is not None else _NO_DATA_TEXT
        loser_text = _avg(loser) if loser is not None else _NO_DATA_TEXT
        winner_n = meta.winner_composition_samples.get(key, 0)
        loser_n = meta.loser_composition_samples.get(key, 0)
        lines.append(
            f"{_source_label_for_key(key)}: {winner_text} vs {loser_text} "
            f"({winner_n} / {loser_n} scored appearances)"
        )
    if lines:
        lines.insert(0, "Average points per source, winners vs everyone else")
    return lines


def _meta_awards_lines(view: MetaInsightsView) -> list[str]:
    meta = view.meta
    lines: list[str] = []
    combo_total = sum(meta.win_award_combos.values())
    if combo_total:
        lines.append(f"Winners' awards ({_scored(combo_total)} in Normal/Seafarers):")
        for key, label in _COMBO_LABELS.items():
            count = meta.win_award_combos.get(key, 0)
            lines.append(f"{label}: {count} ({format_win_rate(Fraction(count, combo_total))})")
    for key, stat in meta.awards.items():
        if not stat.held:
            continue
        lines.append(
            f"{_source_label_for_key(key)}: holders won {_rate_text(stat.win_rate_when_held)} "
            f"({_plural(stat.held, 'appearance')}) vs {_rate_text(stat.win_rate_without)} "
            f"without ({_plural(stat.games_without, 'appearance')})"
        )
    return lines


def _meta_play_style_lines(view: MetaInsightsView) -> list[str]:
    lines = []
    for key, label in _PLAY_STYLE_LABELS.items():
        split = view.meta.play_styles.get(key)
        if split is None or not split.games:
            continue
        lines.append(
            f"{label}: {_rate_text(split.win_rate)} win rate "
            f"({_plural(split.games, 'scored appearance')})"
        )
    return lines


def _meta_vp_card_lines(view: MetaInsightsView) -> list[str]:
    meta = view.meta
    split = meta.winners_with_vp_cards
    if not split.games:
        return []
    lines = [
        f"Winners holding VP cards: {_rate_text(split.win_rate)} "
        f"({split.wins} of {_plural(split.games, 'scored Normal/Seafarers win')})"
    ]
    if meta.avg_vp_card_share_of_winning_score is not None:
        lines.append(
            f"VP cards were {format_win_rate(meta.avg_vp_card_share_of_winning_score)} "
            f"of the winning score on average ({_scored(meta.vp_share_samples)})"
        )
    return lines


def _most_common_score_text(distribution: Mapping[int, int], total: int) -> str | None:
    if not distribution:
        return None
    top = max(distribution.values())
    scores = sorted(score for score, count in distribution.items() if count == top)
    if len(scores) == 1:
        return f"Most common winning score: {scores[0]} pts ({_plural(top, 'game')} of {total})"
    joined = ", ".join(str(score) for score in scores)
    return f"Most common winning scores: {joined} pts ({_plural(top, 'game')} each of {total})"


def _meta_score_lines(view: MetaInsightsView) -> list[str]:
    meta = view.meta
    lines: list[str] = []
    if meta.avg_winning_score is not None:
        lines.append(
            f"Average winning score: {_avg(meta.avg_winning_score)} pts "
            f"({_scored(meta.scored_games)})"
        )
    if meta.avg_margin is not None:
        lines.append(
            f"Average margin: {_avg(meta.avg_margin)} pts ({_scored(meta.margin_samples)})"
        )
    common = _most_common_score_text(meta.winning_score_distribution, meta.scored_games)
    if common is not None:
        lines.append(common)
    return lines


def _meta_lead_source_line(meta: MetaSummary) -> str | None:
    if meta.lead_source_samples < 1:
        return None
    top = _top_positive_sources(meta.lead_sources)[:_DECIDING_TOP_SOURCES]
    if not top:
        return None
    sources = ", ".join(f"{_source_label_for_key(key)} {_signed_lead(value)}" for key, value in top)
    return _exploratory(
        f"Winning lead mostly from: {sources} "
        f"({_plural(meta.lead_source_samples, 'fully scored game')})",
        meta.lead_source_samples,
        LEAD_SOURCES_MIN,
    )


def _meta_board_leader_line(meta: MetaSummary) -> str | None:
    if meta.board_leader_games < 1:
        return None
    rate = Fraction(meta.board_leader_wins, meta.board_leader_games)
    detail = f"{meta.board_leader_wins} of {_plural(meta.board_leader_games, 'game')}"
    if meta.board_leader_ties:
        detail += f"; {_plural(meta.board_leader_ties, 'tie')} excluded"
    return _exploratory(
        f"Board leader won {format_win_rate(rate)} ({detail})",
        meta.board_leader_games,
        BOARD_LEADER_MIN,
    )


def _meta_close_finish_line(meta: MetaSummary) -> str | None:
    if meta.close_finish_games < 1 or meta.close_finish_avg is None:
        return None
    return _exploratory(
        f"Crowded finishes: avg {_avg(meta.close_finish_avg)} losers finished at "
        f"target − 2 or higher ({_plural(meta.close_finish_games, 'game')})",
        meta.close_finish_games,
        CLOSE_FINISH_MIN,
    )


def _meta_overshoot_line(meta: MetaSummary) -> str | None:
    samples = meta.overshoot_samples
    if samples < 1:
        return None
    total = sum(points * count for points, count in meta.overshoot_distribution.items())
    average = Fraction(total, samples)
    if average >= 0:
        line = f"Winners overshoot the target by {_avg(average)} on average"
    else:
        line = f"Winners finish {_avg(-average)} under the target on average"
    if meta.exact_target_rate is not None:
        line += f"; exact-target wins {format_win_rate(meta.exact_target_rate)}"
    return _exploratory(f"{line} ({_plural(samples, 'win')})", samples, OVERSHOOT_MIN)


def _meta_deciding_factor_lines(view: MetaInsightsView) -> list[str]:
    meta = view.meta
    candidates = (
        _meta_lead_source_line(meta),
        _meta_board_leader_line(meta),
        _meta_close_finish_line(meta),
        _meta_overshoot_line(meta),
    )
    return [line for line in candidates if line is not None]


def _top_holder(
    players: Sequence[PlayerSummary], key: str
) -> tuple[PlayerSummary, AwardStat] | None:
    """The player who held an award most often (lowest id wins a tie)."""
    best: tuple[PlayerSummary, AwardStat] | None = None
    for player in players:
        stat = player.awards.get(key)
        if stat is None or not stat.held:
            continue
        if best is None or (-stat.held, player.user_id) < (-best[1].held, best[0].user_id):
            best = (player, stat)
    return best


def _meta_holder_lines(view: MetaInsightsView) -> list[str]:
    lines = []
    for key in view.meta.awards:
        top = _top_holder(view.players, key)
        if top is None:
            continue
        player, stat = top
        lines.append(
            f"{_source_label_for_key(key)}: {mention(player.user_id)} "
            f"({stat.held} of {_scored(stat.opportunities)})"
        )
    return lines


def _meta_calendar_lines(view: MetaInsightsView) -> list[str]:
    weekdays = {day: n for day, n in view.meta.games_by_weekday.items() if 0 <= day < 7 and n > 0}
    if not weekdays:
        return []
    top = max(weekdays.values())
    day = min(day for day, count in weekdays.items() if count == top)
    return [f"Busiest day: {_WEEKDAY_NAMES[day]} ({_plural(top, 'game')} of {view.meta.games})"]


def _meta_time_of_day_lines(view: MetaInsightsView) -> list[str]:
    counts = {
        bucket: view.meta.games_by_time_of_day.get(bucket, 0) for bucket in _TIME_OF_DAY_LABELS
    }
    total = sum(counts.values())
    if not total:
        return []
    parts = " • ".join(f"{label} {counts[bucket]}" for bucket, label in _TIME_OF_DAY_LABELS.items())
    return [f"Time of day: {parts} ({_plural(total, 'game')} with a recorded time)"]


def _has_any_score_data(meta: MetaSummary) -> bool:
    return any(meta.winner_composition_samples.values()) or any(
        meta.loser_composition_samples.values()
    )


def build_meta_insights_embed(view: MetaInsightsView) -> discord.Embed:
    """The group-wide "how do we win" view."""
    meta = view.meta
    embed, show = _insights_embed("Meta Insights", view.filter, has_games=meta.games > 0)
    if not show:
        return embed

    # `scored_games` counts only games whose *winner* has a recorded score, so say
    # so; whether any score data exists at all is judged from the per-source
    # samples (a game where only losers scored still feeds those).
    games_line = (
        f"{_plural(meta.games, 'confirmed game')} ({meta.scored_games} with the winner's score)"
    )
    if not _has_any_score_data(meta):
        games_line += f"\n{_NO_SCORES_TEXT}"
    _add_field(embed, "Games", games_line, inline=False)
    sections = (
        ("How winners score", _meta_composition_lines(view)),
        ("Awards & winning", _meta_awards_lines(view)),
        ("Play styles", _meta_play_style_lines(view)),
        ("VP cards", _meta_vp_card_lines(view)),
        ("Winning scores & margins", _meta_score_lines(view)),
        ("Deciding factors", _meta_deciding_factor_lines(view)),
        ("Most frequent holders", _meta_holder_lines(view)),
        ("Calendar", _meta_calendar_lines(view) + _meta_time_of_day_lines(view)),
    )
    for name, lines in sections:
        if lines:
            _add_field(embed, name, "\n".join(lines), inline=False)
    return embed


_HEAD_TO_HEAD_FIELD_CHARS = 1000
_HEAD_TO_HEAD_FOOTER_RESERVE = 64


def _opponent_line(opponent: OpponentRecord) -> str:
    rate = Fraction(opponent.wins, opponent.games_together) if opponent.games_together else None
    # Not a W-L record: games a third player won are shown, never hidden.
    others = max(opponent.games_together - opponent.wins - opponent.opponent_wins, 0)
    return (
        f"{mention(opponent.opponent_id)} — {_plural(opponent.games_together, 'game')}: "
        f"won {opponent.wins}, they won {opponent.opponent_wins}, others won {others} "
        f"({_rate_text(rate)})"
    )


def _chunk_lines(lines: Sequence[str], limit: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    size = 0
    for line in lines:
        cost = len(line) + 1
        if not chunks or size + cost > limit:
            chunks.append([])
            size = 0
        chunks[-1].append(line)
        size += cost
    return chunks


def _rivalry_lines(view: HeadToHeadView) -> list[str]:
    """Nemesis / best matchup / closest rival, phrased neutrally for any subject."""
    highlights = view.highlights
    if highlights is None:
        return []
    records = {opponent.opponent_id: opponent for opponent in view.opponents}
    lines: list[str] = []

    nemesis = records.get(highlights.nemesis) if highlights.nemesis is not None else None
    if highlights.nemesis is not None:
        who = mention(highlights.nemesis)
        if nemesis is None:
            lines.append(f"Nemesis: {who}")
        else:
            lines.append(
                f"Nemesis: {who} won {nemesis.opponent_wins} of "
                f"{nemesis.games_together} shared games"
            )
    best_id = highlights.best_matchup
    best = records.get(best_id) if best_id is not None else None
    if best_id is not None:
        who = mention(best_id)
        if best is None:
            lines.append(f"Best matchup: {who}")
        else:
            lines.append(
                f"Best matchup: {who} — won {best.wins} of {best.games_together} shared games"
            )
    rival_id = highlights.closest_rival
    rival = records.get(rival_id) if rival_id is not None else None
    if rival_id is not None:
        who = mention(rival_id)
        if rival is None:
            lines.append(f"Closest rival: {who}")
        else:
            lines.append(
                f"Closest rival: {who} — won {rival.wins}, they won {rival.opponent_wins} "
                f"({rival.games_together} shared games)"
            )
    if lines:
        lines.append(f"(min {_plural(highlights.min_games, 'shared game')})")
    return lines


def build_head_to_head_embed(view: HeadToHeadView) -> discord.Embed:
    """The subject's record against each opponent, in the view's own order."""
    embed, show = _insights_embed(
        "Head-to-Head", view.filter, subject_id=view.user_id, has_games=bool(view.opponents)
    )
    if not show:
        return embed

    rivalries = _rivalry_lines(view)
    if rivalries:
        _add_field(embed, "Rivalries", "\n".join(rivalries), inline=False)

    lines = [_opponent_line(opponent) for opponent in view.opponents]
    shown = 0
    for index, chunk in enumerate(_chunk_lines(lines, _HEAD_TO_HEAD_FIELD_CHARS)):
        if len(embed.fields) >= EMBED_MAX_FIELDS:
            break
        name = "Opponents" if index == 0 else "Opponents (cont.)"
        # Keep whole rows only (never a half-cut mention), and leave room for the
        # "showing N of M" footer in case the embed fills up.
        room = EMBED_TOTAL_MAX - len(embed) - _HEAD_TO_HEAD_FOOTER_RESERVE - len(name)
        kept = list(chunk)
        while kept and len("\n".join(kept)) > room:
            kept.pop()
        if not kept:
            break
        _add_field(embed, name, "\n".join(kept), inline=False)
        shown += len(kept)
        if len(kept) < len(chunk):
            break
    if shown < len(lines):
        embed.set_footer(text=f"Showing {shown} of {len(lines)} opponents.")
    return embed


# ---------------------------------------------------------------------------
# Season embeds.
# ---------------------------------------------------------------------------


def build_season_summary_embed(season: Season, *, heading: str = "Season") -> discord.Embed:
    """A season's own facts, with no standings -- for commands that mutate a
    season but don't otherwise need a second (read) service call to render
    their response (`/season start`, `min-games`, `end-date`, `cancel`)."""
    title = truncate(f"{heading}: {escape_user_text(season.name)}", EMBED_TITLE_MAX)
    embed = discord.Embed(title=title, color=discord.Color.blurple())
    embed.add_field(name="Status", value=season.status.capitalize(), inline=True)
    embed.add_field(name="Starts", value=season.starts_on.isoformat(), inline=True)
    embed.add_field(name="Ends", value=season.ends_on.isoformat(), inline=True)
    embed.add_field(name="Minimum games", value=str(season.min_games), inline=True)
    return embed


def build_season_info_embed(info: SeasonInfo) -> discord.Embed:
    season = info.season
    embed = build_season_summary_embed(season)

    eligible = [p for p in info.ranked if p.eligible]
    ineligible = [p for p in info.ranked if not p.eligible]
    standings = _standings_lines(eligible, needs_more=False)
    _add_field(
        embed,
        "Standings",
        "\n".join(standings) if standings else "No eligible players yet.",
        inline=False,
    )
    if ineligible:
        needs_more = _standings_lines(ineligible, needs_more=True, min_games=season.min_games)
        _add_field(embed, "Needs more games", "\n".join(needs_more), inline=False)
    return embed


def build_season_history_embed(seasons: Sequence[Season]) -> discord.Embed:
    embed = discord.Embed(title="Season History", color=discord.Color.blurple())
    if not seasons:
        embed.description = "No seasons yet."
        return embed
    rows: list[tuple[str, str]] = []
    for season in seasons[:EMBED_MAX_FIELDS]:
        field_name = f"#{season.season_id} {escape_user_text(season.name)}"
        field_value = (
            f"{season.status.capitalize()} -- {season.starts_on.isoformat()} to "
            f"{season.ends_on.isoformat()} (min {season.min_games} games)"
        )
        rows.append((field_name, field_value))
    _add_field_rows(embed, rows)
    return embed


def build_season_announcement_embed(resolution: SeasonResolution) -> discord.Embed:
    season = resolution.season
    title = truncate(f"Season Complete: {escape_user_text(season.name)}", EMBED_TITLE_MAX)
    embed = discord.Embed(title=title, color=discord.Color.gold())

    outcome = resolution.outcome
    if outcome.status == "no_bet":
        if outcome.reason == "fewer_than_two_eligible":
            message = "Not enough eligible players for a bet this season."
        else:
            message = "Every eligible player tied, so there's no bet this season."
    else:
        payers = ", ".join(mention(uid) for uid in outcome.payers)
        payees = ", ".join(mention(uid) for uid in outcome.payees)
        message = f"{payers} buys food for {payees}!"
    _set_description(embed, message)

    lines = _standings_lines(resolution.ranked, needs_more=False)
    if lines:
        _add_field(embed, "Final Standings", "\n".join(lines), inline=False)
    return embed


def build_frozen_season_announcement_embed(announcement: Announcement) -> discord.Embed:
    """Render a scheduler announcement exclusively from frozen result rows."""
    season = announcement.season
    title = truncate(f"Season Complete: {escape_user_text(season.name)}", EMBED_TITLE_MAX)
    embed = discord.Embed(title=title, color=discord.Color.gold())

    payers = [mention(row.user_id) for row in announcement.results if row.outcome == "payer"]
    payees = [mention(row.user_id) for row in announcement.results if row.outcome == "payee"]
    if payers and payees:
        _set_description(embed, f"{', '.join(payers)} buys food for {', '.join(payees)}!")
    else:
        _set_description(embed, "There is no bet this season.")

    rows: list[tuple[str, str]] = []
    for result in sorted(announcement.results, key=lambda row: (row.rank, row.user_id))[:25]:
        win_rate = Fraction(result.wins, result.games) if result.games else Fraction()
        eligibility = "Eligible" if result.eligible else "Not eligible"
        outcome = {
            "payer": "Pays",
            "payee": "Gets fed",
            None: eligibility,
        }[result.outcome]
        rows.append(
            (
                f"#{result.rank} {mention(result.user_id)}",
                f"{result.wins}-{result.games - result.wins} "
                f"({format_win_rate(win_rate)}) -- {outcome}",
            )
        )
    if rows:
        _add_field_rows(embed, rows)
    else:
        _add_field(embed, "Final Standings", "No confirmed games were recorded.", inline=False)
    return embed


# ---------------------------------------------------------------------------
# Event embeds.
# ---------------------------------------------------------------------------

_EVENT_STATUS_LABELS = {
    "scheduled": "Scheduled",
    "cancelled": "Cancelled",
    "completed": "Completed",
}


_RSVP_MEMBER_DISPLAY_LIMIT = 20


def _rsvp_group_value(member_ids: Sequence[int], *, empty: str = "No responses yet.") -> str:
    """Render a bounded, mention-safe RSVP field value.

    The caller must disable user mentions when delivering the embed.  The
    field name holds the exact total; the trailing text says how many IDs did
    not fit rather than silently dropping attendees.
    """
    if not member_ids:
        return empty
    visible = list(member_ids[:_RSVP_MEMBER_DISPLAY_LIMIT])
    rendered = ", ".join(mention(member_id) for member_id in visible)
    remainder = len(member_ids) - len(visible)
    if remainder:
        rendered += f"\n…and {remainder} more"
    return truncate(rendered, EMBED_FIELD_VALUE_MAX)


def build_event_embed(event: Event, roster: RsvpRoster | RsvpCounts | None = None) -> discord.Embed:
    title = truncate(f"Event: {escape_user_text(event.title)}", EMBED_TITLE_MAX)
    colors = {
        "scheduled": discord.Color.blurple(),
        "cancelled": discord.Color.red(),
        "completed": discord.Color.dark_grey(),
    }
    embed = discord.Embed(title=title, color=colors[event.status])
    if event.description:
        _set_description(embed, escape_user_text(event.description))
    embed.add_field(name="Status", value=_EVENT_STATUS_LABELS[event.status], inline=True)
    embed.add_field(
        name="When",
        value=(
            f"{_discord_timestamp(event.starts_at, 'F')} "
            f"({_discord_timestamp(event.starts_at, 'R')})"
        ),
        inline=False,
    )
    if event.location:
        _add_field(embed, "Location", escape_user_text(event.location), inline=False)
    embed.add_field(name="Created by", value=mention(event.created_by), inline=True)
    if isinstance(roster, RsvpCounts):
        # Compatibility for callers that only have aggregate counts.  New
        # command paths always pass a roster so names can be displayed.
        counts = roster
        roster = RsvpRoster(going=(), maybe=(), not_going=())
    else:
        roster = roster or RsvpRoster(going=(), maybe=(), not_going=())
        counts = roster.counts
    _add_field(
        embed,
        f"Going ({counts.going})",
        _rsvp_group_value(roster.going),
        inline=False,
    )
    _add_field(
        embed,
        f"Maybe ({counts.maybe})",
        _rsvp_group_value(roster.maybe),
        inline=False,
    )
    _add_field(
        embed,
        f"Not Going ({counts.not_going})",
        _rsvp_group_value(roster.not_going),
        inline=False,
    )
    embed.set_footer(text=f"Event #{event.event_id}")
    return embed


def build_event_list_embed(events: Sequence[Event]) -> discord.Embed:
    embed = discord.Embed(title="Upcoming Events", color=discord.Color.blurple())
    if not events:
        _set_description(embed, "No game nights are scheduled.")
        return embed
    rows: list[tuple[str, str]] = []
    for event in events[:10]:
        details = [
            _discord_timestamp(event.starts_at, "F"),
            f"Created by {mention(event.created_by)}",
        ]
        if event.location:
            details.append(f"Location: {escape_user_text(event.location)}")
        rows.append((f"#{event.event_id} {escape_user_text(event.title)}", " | ".join(details)))
    _add_field_rows(embed, rows)
    return embed


def build_event_reminder_embed(event: Event, offset_minutes: int) -> discord.Embed:
    heading = {1440: "Tomorrow", 60: "In one hour"}.get(offset_minutes, "Coming up")
    title = truncate(f"{heading}: {escape_user_text(event.title)}", EMBED_TITLE_MAX)
    embed = discord.Embed(title=title, color=discord.Color.gold())
    embed.add_field(
        name="When",
        value=(
            f"{_discord_timestamp(event.starts_at, 'F')} "
            f"({_discord_timestamp(event.starts_at, 'R')})"
        ),
        inline=False,
    )
    if event.location:
        _add_field(embed, "Location", escape_user_text(event.location), inline=False)
    embed.set_footer(text=f"Event #{event.event_id}")
    return embed


# ---------------------------------------------------------------------------
# Config embed.
# ---------------------------------------------------------------------------


_LEADERBOARD_MODE_LABELS = {"off": "Off", "per_game": "After each game", "daily": "Daily digest"}
_LEADERBOARD_SCOPE_LABELS = {"season": "Season", "all_time": "All-time"}


def build_config_show_embed(config: GuildConfig) -> discord.Embed:
    embed = discord.Embed(title="Server Configuration", color=discord.Color.blurple())
    channel_value = (
        channel_mention(config.announce_channel_id) if config.announce_channel_id else "Not set"
    )
    embed.add_field(name="Announcement channel", value=channel_value, inline=True)
    embed.add_field(
        name="Timezone",
        value=truncate(escape_user_text(config.timezone), EMBED_FIELD_VALUE_MAX),
        inline=True,
    )
    role_value = role_mention(config.admin_role_id) if config.admin_role_id else "Not set"
    embed.add_field(name="Admin role", value=role_value, inline=True)
    player_role_value = role_mention(config.player_role_id) if config.player_role_id else "Not set"
    embed.add_field(name="Event player role", value=player_role_value, inline=True)
    embed.add_field(name="Default minimum games", value=str(config.default_min_games), inline=True)
    embed.add_field(
        name="Leaderboard post",
        value=_LEADERBOARD_MODE_LABELS.get(config.leaderboard_mode, config.leaderboard_mode),
        inline=True,
    )
    leaderboard_channel_value = (
        channel_mention(config.leaderboard_channel_id)
        if config.leaderboard_channel_id
        else "Not set"
    )
    embed.add_field(name="Leaderboard channel", value=leaderboard_channel_value, inline=True)
    embed.add_field(
        name="Leaderboard scope",
        value=_LEADERBOARD_SCOPE_LABELS.get(config.leaderboard_scope, config.leaderboard_scope),
        inline=True,
    )
    embed.add_field(
        name="Leaderboard daily time",
        value=format_time_12h(config.leaderboard_daily_time),
        inline=True,
    )
    return embed


__all__ = [
    "EMBED_DESCRIPTION_MAX",
    "EMBED_FIELD_NAME_MAX",
    "EMBED_FIELD_VALUE_MAX",
    "EMBED_MAX_FIELDS",
    "EMBED_TITLE_MAX",
    "EMBED_TOTAL_MAX",
    "build_config_show_embed",
    "build_chart_embed",
    "build_chart_unavailable_embed",
    "build_event_embed",
    "build_event_list_embed",
    "build_event_reminder_embed",
    "build_frozen_season_announcement_embed",
    "build_game_history_embed",
    "build_game_report_embed",
    "build_game_status_embed",
    "build_head_to_head_embed",
    "build_leaderboard_embed",
    "build_leaderboard_post_embed",
    "build_meta_insights_embed",
    "build_player_insights_embed",
    "build_season_announcement_embed",
    "build_season_history_embed",
    "build_season_info_embed",
    "build_season_summary_embed",
    "build_stats_embed",
    "channel_mention",
    "escape_user_text",
    "format_game_score_table",
    "format_time_12h",
    "format_win_rate",
    "mention",
    "role_mention",
    "truncate",
]
