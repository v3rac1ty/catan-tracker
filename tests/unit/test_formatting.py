"""Regression tests for Discord-safe text and embed limits."""

from __future__ import annotations

from datetime import UTC, date, datetime

from catan_bot.db.models import Game, GuildConfig, Season
from catan_bot.formatting import (
    EMBED_DESCRIPTION_MAX,
    EMBED_FIELD_NAME_MAX,
    EMBED_FIELD_VALUE_MAX,
    EMBED_MAX_FIELDS,
    EMBED_TITLE_MAX,
    EMBED_TOTAL_MAX,
    build_config_show_embed,
    build_game_history_embed,
    build_season_history_embed,
    escape_user_text,
)


def _game(game_id: int, reason: str) -> Game:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Game(
        game_id=game_id,
        guild_id=1,
        season_id=None,
        played_on=date(2026, 1, 1),
        status="voided",
        reported_by=10,
        confirmed_by=None,
        confirmed_at=None,
        voided_by=11,
        voided_at=now,
        void_reason=reason,
        rejected_by=None,
        rejected_at=None,
        channel_id=None,
        message_id=None,
        created_at=now,
    )


def _season(season_id: int, name: str) -> Season:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Season(
        season_id=season_id,
        guild_id=1,
        name=name,
        starts_on=date(2026, 1, 1),
        ends_on=date(2026, 1, 31),
        ends_at=now,
        min_games=2,
        status="completed",
        resolved_at=now,
        announced_at=None,
        created_by=10,
        created_at=now,
    )


def test_escape_user_text_escapes_markup_and_all_discord_mention_forms() -> None:
    result = escape_user_text("**winner** @everyone @12345678901234567 <#12345678901234567>")

    separator = chr(0x200B)
    assert result == (
        r"\*\*winner\*\* @"
        + separator
        + "everyone @"
        + separator
        + "12345678901234567 <#"
        + separator
        + "12345678901234567>"
    )


def test_escape_user_text_strips_default_ignorables_controls_and_fillers() -> None:
    hidden = "".join(
        chr(code)
        for code in (
            0x0000,
            0x034F,
            0x061C,
            0x115F,
            0x17B4,
            0x180B,
            0x200B,
            0x202E,
            0x2065,
            0x2800,
            0x3164,
            0xFEFF,
            0xFFA0,
            0x1BCA0,
            0x1D173,
            0xE0001,
        )
    )

    assert escape_user_text("left" + hidden + "right") == "leftright"


def test_escape_user_text_keeps_contextual_emoji_and_script_shaping() -> None:
    joiner = chr(0x200D)
    non_joiner = chr(0x200C)
    variation = chr(0xFE0F)
    woman = chr(0x1F469)
    heart = chr(0x2764)

    emoji = woman + joiner + heart + variation + joiner + woman
    persian = "می" + non_joiner + "روم"
    assert escape_user_text(emoji) == emoji
    assert escape_user_text(persian) == persian

    assert escape_user_text("a" + joiner + "b") == "ab"
    assert escape_user_text("A" + variation) == "A"


def test_escape_user_text_caps_a_combining_mark_flood() -> None:
    acute = chr(0x0301)
    assert escape_user_text("a" + acute * 20 + "b") == "a" + acute * 4 + "b"


def test_history_with_25_malicious_rows_stays_within_every_embed_limit() -> None:
    payload = ("@everyone **hidden** " + chr(0x200B)) * 10
    embed = build_game_history_embed(
        [_game(game_id, payload) for game_id in range(1, 26)], member_id=None
    )

    assert len(embed) <= EMBED_TOTAL_MAX
    assert len(embed.fields) == EMBED_MAX_FIELDS
    assert all(0 < len(field.name) <= EMBED_FIELD_NAME_MAX for field in embed.fields)
    assert all(0 < len(field.value) <= EMBED_FIELD_VALUE_MAX for field in embed.fields)
    rendered = "".join(field.value for field in embed.fields)
    assert "@everyone" not in rendered
    assert r"\*\*hidden\*\*" in rendered


def test_long_stored_season_names_are_escaped_and_bounded() -> None:
    malicious_name = "@everyone **season** " * 400
    embed = build_season_history_embed(
        [_season(season_id, malicious_name) for season_id in range(1, 26)]
    )

    assert len(embed) <= EMBED_TOTAL_MAX
    assert len(embed.title or "") <= EMBED_TITLE_MAX
    assert len(embed.description or "") <= EMBED_DESCRIPTION_MAX
    assert len(embed.fields) == EMBED_MAX_FIELDS
    assert all(len(field.name) <= EMBED_FIELD_NAME_MAX for field in embed.fields)
    assert all(len(field.value) <= EMBED_FIELD_VALUE_MAX for field in embed.fields)
    assert all("@everyone" not in field.name for field in embed.fields)


def test_config_show_escapes_stored_timezone_text() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    config = GuildConfig(
        guild_id=1,
        timezone="@everyone **UTC**" + chr(0x200B),
        announce_channel_id=None,
        admin_role_id=None,
        default_min_games=2,
        created_at=now,
        updated_at=now,
    )

    embed = build_config_show_embed(config)

    timezone_field = next(field for field in embed.fields if field.name == "Timezone")
    assert "@everyone" not in timezone_field.value
    assert r"\*\*UTC\*\*" in timezone_field.value
