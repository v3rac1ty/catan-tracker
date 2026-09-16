from __future__ import annotations

from datetime import UTC, date, datetime

from catan_bot.db.models import Event, RsvpRoster, Season, SeasonResultRow
from catan_bot.formatting import (
    EMBED_FIELD_NAME_MAX,
    EMBED_FIELD_VALUE_MAX,
    EMBED_TITLE_MAX,
    EMBED_TOTAL_MAX,
    build_event_embed,
    build_event_list_embed,
    build_event_reminder_embed,
    build_frozen_season_announcement_embed,
    escape_user_text,
)
from catan_bot.services.results import Announcement

NOW = datetime(2026, 9, 15, 1, 0, tzinfo=UTC)


def _event(
    event_id: int = 1,
    *,
    title: str = "Game Night",
    location: str | None = "My House",
    description: str | None = "Bring snacks",
    status: str = "scheduled",
) -> Event:
    return Event(
        event_id=event_id,
        guild_id=10,
        title=title,
        description=description,
        location=location,
        starts_at=NOW,
        status=status,  # type: ignore[arg-type]
        created_by=20,
        channel_id=30,
        message_id=40,
        created_at=NOW,
    )


def _season(name: str = "Fall League") -> Season:
    return Season(
        season_id=1,
        guild_id=10,
        name=name,
        starts_on=date(2026, 9, 1),
        ends_on=date(2026, 9, 30),
        ends_at=NOW,
        min_games=2,
        status="completed",
        resolved_at=NOW,
        announced_at=None,
        created_by=20,
        created_at=NOW,
    )


def test_event_embed_escapes_text_and_uses_native_timestamps() -> None:
    payload = "@everyone **Game** <#12345678901234567>"
    event = _event(title=payload, location=payload, description=payload)
    embed = build_event_embed(
        event, RsvpRoster(going=(1, 2), maybe=(3,), not_going=(4, 5, 6))
    )

    expected = escape_user_text(payload)
    assert expected in (embed.title or "")
    assert embed.description == expected
    assert payload not in str(embed.to_dict())
    when = next(field.value for field in embed.fields if field.name == "When")
    assert f"<t:{int(NOW.timestamp())}:F>" in when
    assert f"<t:{int(NOW.timestamp())}:R>" in when
    going = next(field.value for field in embed.fields if field.name == "Going (2)")
    maybe = next(field.value for field in embed.fields if field.name == "Maybe (1)")
    no = next(field.value for field in embed.fields if field.name == "Not Going (3)")
    assert going == "<@1>, <@2>"
    assert maybe == "<@3>"
    assert no == "<@4>, <@5>, <@6>"
    assert len(embed) <= EMBED_TOTAL_MAX


def test_event_embed_bounds_roster_mentions_and_reports_omitted_members() -> None:
    member_ids = tuple(range(1, 31))
    embed = build_event_embed(_event(), RsvpRoster(member_ids, (), ()))

    going = next(field for field in embed.fields if field.name == "Going (30)")
    assert "<@1>" in going.value
    assert "<@20>" in going.value
    assert "<@21>" not in going.value
    assert "and 10 more" in going.value
    assert len(going.value) <= EMBED_FIELD_VALUE_MAX


def test_event_list_limits_rows_and_embed_sizes() -> None:
    payload = "@everyone **Game** " * 100
    embed = build_event_list_embed(
        [_event(event_id=index, title=payload, location=payload) for index in range(1, 20)]
    )

    assert len(embed.fields) == 10
    assert len(embed) <= EMBED_TOTAL_MAX
    assert len(embed.title or "") <= EMBED_TITLE_MAX
    assert all(len(field.name) <= EMBED_FIELD_NAME_MAX for field in embed.fields)
    assert all(len(field.value) <= EMBED_FIELD_VALUE_MAX for field in embed.fields)
    assert "@everyone" not in str(embed.to_dict())


def test_event_reminder_escapes_stored_text() -> None:
    payload = "@everyone **Finals**"
    embed = build_event_reminder_embed(_event(title=payload, location=payload), 60)

    assert embed.title == f"In one hour: {escape_user_text(payload)}"
    assert "@everyone" not in str(embed.to_dict())
    assert len(embed) <= EMBED_TOTAL_MAX


def test_frozen_announcement_uses_stored_rows_and_outcomes() -> None:
    announcement = Announcement(
        season=_season("@everyone **Fall**"),
        results=[
            SeasonResultRow(1, 1, 3, 2, True, "payee"),
            SeasonResultRow(2, 2, 3, 1, True, "payer"),
            SeasonResultRow(3, 3, 1, 0, False, None),
        ],
    )

    embed = build_frozen_season_announcement_embed(announcement)

    assert "@everyone" not in (embed.title or "")
    assert embed.description == "<@2> buys food for <@1>!"
    assert [field.name for field in embed.fields] == ["#1 <@1>", "#2 <@2>", "#3 <@3>"]
    assert "Gets fed" in embed.fields[0].value
    assert "Pays" in embed.fields[1].value
    assert "Not eligible" in embed.fields[2].value


def test_frozen_announcement_with_no_outcomes_does_not_recompute_a_bet() -> None:
    announcement = Announcement(
        season=_season(),
        results=[SeasonResultRow(1, 1, 2, 1, True, None)],
    )

    embed = build_frozen_season_announcement_embed(announcement)

    assert embed.description == "There is no bet this season."
