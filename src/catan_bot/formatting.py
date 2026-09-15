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
from collections.abc import Sequence
from datetime import datetime
from fractions import Fraction

import discord

from catan_bot.db.models import Event, Game, GameWithParticipants, GuildConfig, RsvpCounts, Season
from catan_bot.domain.ranking import RankedPlayer
from catan_bot.services.results import (
    Announcement,
    Leaderboard,
    PlayerStatsView,
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


def _standings_lines(
    players: Sequence[RankedPlayer], *, needs_more: bool, min_games: int = 0
) -> list[str]:
    if needs_more:
        return [
            f"{mention(p.user_id)} -- needs {max(min_games - p.games, 0)} more game(s)"
            for p in players
        ]
    return [
        f"#{p.rank} {mention(p.user_id)} -- {p.wins}-{p.games - p.wins} "
        f"({format_win_rate(p.win_rate)})"
        for p in players
    ]


# ---------------------------------------------------------------------------
# Game embeds.
# ---------------------------------------------------------------------------


def build_game_report_embed(created: GameWithParticipants) -> discord.Embed:
    """A freshly reported, still-pending game. Sent publicly with Confirm/Reject buttons."""
    game = created.game
    embed = discord.Embed(
        title="Game Reported",
        description="Waiting on another player to confirm this game.",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Winner", value=mention(created.winner_id), inline=True)
    losers_text = ", ".join(mention(uid) for uid in created.loser_ids)
    embed.add_field(name="Loser(s)", value=losers_text, inline=True)
    embed.add_field(name="Date", value=game.played_on.isoformat(), inline=True)
    embed.set_footer(text=f"Game #{game.game_id}")
    return embed


def build_game_status_embed(updated: GameWithParticipants) -> discord.Embed:
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
    embed.add_field(name="Date", value=game.played_on.isoformat(), inline=True)
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
    for game in games[:EMBED_MAX_FIELDS]:
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
        field_name = f"Game #{game.game_id} -- {game.played_on.isoformat()}"
        rows.append((field_name, " | ".join(parts)))
    _add_field_rows(embed, rows)
    return embed


# ---------------------------------------------------------------------------
# Leaderboard / stats embeds.
# ---------------------------------------------------------------------------

_SCOPE_TITLES = {"season": "Season Leaderboard", "all_time": "All-Time Leaderboard"}


def build_leaderboard_embed(board: Leaderboard) -> discord.Embed:
    embed = discord.Embed(title=_SCOPE_TITLES[board.scope], color=discord.Color.blurple())
    if board.scope == "season":
        if board.season is None:
            embed.description = "There's no active season."
            return embed
        _set_description(embed, f"Season: {escape_user_text(board.season.name)}")

    eligible = [p for p in board.ranked if p.eligible]
    ineligible = [p for p in board.ranked if not p.eligible]

    if not board.ranked:
        _add_field(embed, "Standings", "No confirmed games yet.", inline=False)
        return embed

    standings = _standings_lines(eligible, needs_more=False)
    _add_field(
        embed,
        "Standings",
        "\n".join(standings) if standings else "No eligible players yet.",
        inline=False,
    )
    if ineligible:
        needs_more = _standings_lines(ineligible, needs_more=True, min_games=board.min_games)
        _add_field(embed, "Needs more games", "\n".join(needs_more), inline=False)
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


def build_event_embed(event: Event, counts: RsvpCounts | None = None) -> discord.Embed:
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
    if counts is not None:
        embed.add_field(
            name="RSVPs",
            value=(
                f"Going: {counts.going} | Maybe: {counts.maybe} | Not going: {counts.not_going}"
            ),
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
    embed.add_field(name="Default minimum games", value=str(config.default_min_games), inline=True)
    return embed


__all__ = [
    "EMBED_DESCRIPTION_MAX",
    "EMBED_FIELD_NAME_MAX",
    "EMBED_FIELD_VALUE_MAX",
    "EMBED_MAX_FIELDS",
    "EMBED_TITLE_MAX",
    "EMBED_TOTAL_MAX",
    "build_config_show_embed",
    "build_event_embed",
    "build_event_list_embed",
    "build_event_reminder_embed",
    "build_frozen_season_announcement_embed",
    "build_game_history_embed",
    "build_game_report_embed",
    "build_game_status_embed",
    "build_leaderboard_embed",
    "build_season_announcement_embed",
    "build_season_history_embed",
    "build_season_info_embed",
    "build_season_summary_embed",
    "build_stats_embed",
    "channel_mention",
    "escape_user_text",
    "format_win_rate",
    "mention",
    "role_mention",
    "truncate",
]
