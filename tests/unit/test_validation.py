"""Unit tests for `catan_bot.domain.validation`. No Docker, no system clock."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import IntEnum
from zoneinfo import ZoneInfo

import pytest

from catan_bot.domain.dates import MIN_GAME_DATE
from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.validation import (
    EVENT_DESCRIPTION_MAX,
    EVENT_LOCATION_MAX,
    EVENT_TITLE_MAX,
    MAX_EVENT_LEAD_DAYS,
    MAX_LOSERS,
    MAX_PLAYERS,
    MAX_SEASON_SPAN_DAYS,
    MIN_LOSERS,
    MIN_PLAYERS,
    SEASON_NAME_MAX,
    VOID_REASON_MAX,
    ParticipantRef,
    clean_text,
    validate_event_start,
    validate_game_date,
    validate_game_in_season,
    validate_min_games,
    validate_participants,
    validate_season_window,
)

UTC = ZoneInfo("UTC")
CHICAGO = ZoneInfo("America/Chicago")


class _MinGamesEnum(IntEnum):
    DEFAULT = 5  # deliberately a value that would be *in range* as a plain int


def _p(user_id: int, *, is_bot: bool = False) -> ParticipantRef:
    return ParticipantRef(user_id=user_id, is_bot=is_bot)


# ---------------------------------------------------------------------------
# validate_game_date
# ---------------------------------------------------------------------------


def test_validate_game_date_accepts_today() -> None:
    today = date(2026, 9, 14)
    assert validate_game_date(today, today=today) == today


def test_validate_game_date_accepts_min_date() -> None:
    assert validate_game_date(MIN_GAME_DATE, today=date(2026, 1, 1)) == MIN_GAME_DATE


def test_validate_game_date_rejects_future() -> None:
    today = date(2026, 9, 14)
    with pytest.raises(DomainValidationError):
        validate_game_date(today + timedelta(days=1), today=today)


def test_validate_game_date_rejects_before_min() -> None:
    with pytest.raises(DomainValidationError):
        validate_game_date(MIN_GAME_DATE - timedelta(days=1), today=date(2026, 1, 1))


# ---------------------------------------------------------------------------
# validate_game_in_season
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "played_on",
    [
        pytest.param(date(2026, 6, 1), id="season-start-boundary"),
        pytest.param(date(2026, 6, 15), id="mid-season"),
        pytest.param(date(2026, 6, 30), id="season-end-boundary"),
    ],
)
def test_validate_game_in_season_accepts_inclusive_window(played_on: date) -> None:
    validate_game_in_season(played_on, starts_on=date(2026, 6, 1), ends_on=date(2026, 6, 30))


@pytest.mark.parametrize(
    "played_on",
    [
        pytest.param(date(2026, 5, 31), id="one-day-before-start"),
        pytest.param(date(2026, 7, 1), id="one-day-after-end"),
    ],
)
def test_validate_game_in_season_rejects_outside_window(played_on: date) -> None:
    with pytest.raises(DomainValidationError):
        validate_game_in_season(played_on, starts_on=date(2026, 6, 1), ends_on=date(2026, 6, 30))


def test_validate_game_in_season_message_names_the_window() -> None:
    with pytest.raises(DomainValidationError) as exc_info:
        validate_game_in_season(
            date(2026, 7, 1), starts_on=date(2026, 6, 1), ends_on=date(2026, 6, 30)
        )
    message = exc_info.value.user_message
    assert "2026-06-01" in message
    assert "2026-06-30" in message


# ---------------------------------------------------------------------------
# validate_participants
# ---------------------------------------------------------------------------


def test_validate_participants_accepts_minimum() -> None:
    winner_id, loser_ids = validate_participants(_p(1), [_p(2)])
    assert winner_id == 1
    assert loser_ids == (2,)


def test_validate_participants_accepts_maximum() -> None:
    winner_id, loser_ids = validate_participants(_p(1), [_p(2), _p(3), _p(4), _p(5), _p(6)])
    assert winner_id == 1
    assert loser_ids == (2, 3, 4, 5, 6)


def test_validate_participants_preserves_loser_order() -> None:
    _, loser_ids = validate_participants(_p(1), [_p(5), _p(3), _p(2)])
    assert loser_ids == (5, 3, 2)


def test_validate_participants_rejects_zero_losers() -> None:
    with pytest.raises(DomainValidationError):
        validate_participants(_p(1), [])


def test_validate_participants_rejects_too_many_losers() -> None:
    with pytest.raises(DomainValidationError):
        validate_participants(_p(1), [_p(2), _p(3), _p(4), _p(5), _p(6), _p(7)])


@pytest.mark.parametrize(
    ("winner", "losers"),
    [
        pytest.param(_p(1, is_bot=True), [_p(2)], id="bot-winner"),
        pytest.param(_p(1), [_p(2, is_bot=True)], id="bot-loser"),
        pytest.param(_p(1), [_p(2), _p(3, is_bot=True)], id="bot-among-multiple-losers"),
    ],
)
def test_validate_participants_rejects_bots(
    winner: ParticipantRef, losers: list[ParticipantRef]
) -> None:
    with pytest.raises(DomainValidationError):
        validate_participants(winner, losers)


def test_validate_participants_rejects_duplicate_losers() -> None:
    with pytest.raises(DomainValidationError):
        validate_participants(_p(1), [_p(2), _p(2)])


def test_validate_participants_rejects_winner_listed_as_loser() -> None:
    with pytest.raises(DomainValidationError):
        validate_participants(_p(1), [_p(1), _p(2)])


def test_validate_participants_rejects_non_int_id() -> None:
    with pytest.raises(DomainValidationError):
        validate_participants(_p(1), [_p("2")])  # type: ignore[arg-type]


def test_validate_participants_rejects_bool_id() -> None:
    # `winner=100` (not 1) so this exercises the bool check itself, not the
    # duplicate check -- `True == 1` would otherwise collide with a winner
    # id of 1 when deduplicated through a set.
    with pytest.raises(DomainValidationError):
        validate_participants(_p(100), [_p(True)])  # type: ignore[arg-type]


def test_validate_participants_unhashable_id_raises_domain_error_not_type_error() -> None:
    # Ids are type-checked *before* the duplicate-detection `set()` runs, so
    # an unhashable id gets a friendly DomainValidationError, never a raw
    # TypeError out of `set()`.
    with pytest.raises(DomainValidationError):
        validate_participants(_p(1), [_p([2, 3])])  # type: ignore[arg-type]


def test_validate_participants_rejects_int_enum_id() -> None:
    # `type(x) is int` rejects an IntEnum even at an otherwise-valid,
    # in-range positive value -- the type check, not the range check.
    with pytest.raises(DomainValidationError):
        validate_participants(_p(100), [_p(_MinGamesEnum.DEFAULT)])  # type: ignore[arg-type]


def test_validate_participants_rejects_non_positive_id() -> None:
    with pytest.raises(DomainValidationError):
        validate_participants(_p(0), [_p(2)])


def test_validate_participants_rejects_negative_id() -> None:
    with pytest.raises(DomainValidationError):
        validate_participants(_p(1), [_p(-5)])


def test_validate_participants_rejects_bigint_overflow_id() -> None:
    with pytest.raises(DomainValidationError):
        validate_participants(_p(1), [_p(2**63)])


def test_validate_participants_accepts_bigint_max_id() -> None:
    max_bigint = 2**63 - 1
    winner_id, loser_ids = validate_participants(_p(1), [_p(max_bigint)])
    assert loser_ids == (max_bigint,)


@pytest.mark.parametrize("count", list(range(MIN_LOSERS, MAX_LOSERS + 1)))
def test_validate_participants_every_valid_loser_count(count: int) -> None:
    losers = [_p(i) for i in range(2, 2 + count)]
    winner_id, loser_ids = validate_participants(_p(1), losers)
    assert len(loser_ids) == count
    assert MIN_PLAYERS <= 1 + count <= MAX_PLAYERS


# ---------------------------------------------------------------------------
# validate_event_start
# ---------------------------------------------------------------------------


def test_validate_event_start_accepts_future() -> None:
    now = datetime(2026, 9, 14, tzinfo=UTC)
    starts_at = now + timedelta(hours=1)
    assert validate_event_start(starts_at, now=now) == starts_at


def test_validate_event_start_rejects_now_exactly() -> None:
    now = datetime(2026, 9, 14, tzinfo=UTC)
    with pytest.raises(DomainValidationError):
        validate_event_start(now, now=now)


def test_validate_event_start_rejects_past() -> None:
    now = datetime(2026, 9, 14, tzinfo=UTC)
    with pytest.raises(DomainValidationError):
        validate_event_start(now - timedelta(minutes=1), now=now)


def test_validate_event_start_accepts_max_lead_time_boundary() -> None:
    now = datetime(2026, 9, 14, tzinfo=UTC)
    starts_at = now + timedelta(days=MAX_EVENT_LEAD_DAYS)
    assert validate_event_start(starts_at, now=now) == starts_at


def test_validate_event_start_rejects_one_day_past_max_lead_time() -> None:
    now = datetime(2026, 9, 14, tzinfo=UTC)
    starts_at = now + timedelta(days=MAX_EVENT_LEAD_DAYS + 1)
    with pytest.raises(DomainValidationError):
        validate_event_start(starts_at, now=now)


def test_validate_event_start_rejects_naive_starts_at() -> None:
    now = datetime(2026, 9, 14, tzinfo=UTC)
    with pytest.raises(ValueError, match="starts_at"):
        validate_event_start(datetime(2026, 9, 15), now=now)


def test_validate_event_start_rejects_naive_now() -> None:
    with pytest.raises(ValueError, match="now"):
        validate_event_start(datetime(2026, 9, 15, tzinfo=UTC), now=datetime(2026, 9, 14))


def test_validate_event_start_fold_ambiguous_hour_past_event_is_rejected() -> None:
    # America/Chicago falls back on 2026-11-01: 01:00-01:59 happens twice.
    # Both `starts_at` and `now` share the *same* ZoneInfo object, which is
    # exactly the case where a naive datetime comparison would silently
    # ignore `fold` and misorder them. The event actually started 45
    # minutes before `now` in absolute terms, so this must be rejected.
    starts_at = datetime(2026, 11, 1, 1, 15, tzinfo=CHICAGO, fold=0)  # CDT, earlier occurrence
    now = datetime(2026, 11, 1, 1, 0, tzinfo=CHICAGO, fold=1)  # CST, later occurrence
    assert starts_at.astimezone(UTC) == datetime(2026, 11, 1, 6, 15, tzinfo=UTC)
    assert now.astimezone(UTC) == datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
    with pytest.raises(DomainValidationError):
        validate_event_start(starts_at, now=now)


# ---------------------------------------------------------------------------
# validate_season_window
# ---------------------------------------------------------------------------


def test_validate_season_window_accepts_single_day_season() -> None:
    validate_season_window(date(2026, 6, 1), date(2026, 6, 1), today=date(2026, 5, 1))


def test_validate_season_window_rejects_end_before_start() -> None:
    with pytest.raises(DomainValidationError):
        validate_season_window(date(2026, 6, 2), date(2026, 6, 1), today=date(2026, 5, 1))


def test_validate_season_window_rejects_end_before_today() -> None:
    with pytest.raises(DomainValidationError):
        validate_season_window(date(2026, 1, 1), date(2026, 1, 31), today=date(2026, 2, 1))


def test_validate_season_window_accepts_end_equal_to_today() -> None:
    validate_season_window(date(2026, 1, 1), date(2026, 2, 1), today=date(2026, 2, 1))


def test_validate_season_window_rejects_start_before_min_game_date() -> None:
    # Chosen so *only* the MIN_GAME_DATE rule can reject it: the span is 2
    # days (nowhere near MAX_SEASON_SPAN_DAYS) and ends_on is right after
    # today, not in the past or absurdly far ahead. A prior version of this
    # test used a decades-long span that *also* tripped the span check,
    # so deleting the MIN_GAME_DATE check entirely wouldn't have failed it.
    starts_on = MIN_GAME_DATE - timedelta(days=1)
    ends_on = MIN_GAME_DATE + timedelta(days=1)
    today = date(1994, 12, 1)
    with pytest.raises(DomainValidationError):
        validate_season_window(starts_on, ends_on, today=today)


def test_validate_season_window_rejects_end_more_than_span_days_from_today() -> None:
    # starts_on is far in the *future*, with a short 10-day span, so the
    # start/end span check can't explain a rejection here (10 << 730) --
    # only the separate "end date too far from today" rule can, since
    # ends_on still lands more than MAX_SEASON_SPAN_DAYS after today.
    today = date(2026, 1, 1)
    starts_on = today + timedelta(days=MAX_SEASON_SPAN_DAYS - 5)
    ends_on = starts_on + timedelta(days=10)
    assert (ends_on - starts_on).days < MAX_SEASON_SPAN_DAYS
    with pytest.raises(DomainValidationError):
        validate_season_window(starts_on, ends_on, today=today)


def test_validate_season_window_accepts_end_exactly_span_days_from_today() -> None:
    today = date(2026, 1, 1)
    ends_on = today + timedelta(days=MAX_SEASON_SPAN_DAYS)
    validate_season_window(today, ends_on, today=today)


def test_validate_season_window_accepts_max_span_boundary() -> None:
    starts_on = date(2026, 1, 1)
    ends_on = starts_on + timedelta(days=MAX_SEASON_SPAN_DAYS)
    validate_season_window(starts_on, ends_on, today=starts_on)


def test_validate_season_window_rejects_one_day_past_max_span() -> None:
    starts_on = date(2026, 1, 1)
    ends_on = starts_on + timedelta(days=MAX_SEASON_SPAN_DAYS + 1)
    with pytest.raises(DomainValidationError):
        validate_season_window(starts_on, ends_on, today=starts_on)


# ---------------------------------------------------------------------------
# validate_min_games
# ---------------------------------------------------------------------------


def test_validate_min_games_accepts_lower_boundary() -> None:
    assert validate_min_games(1) == 1


def test_validate_min_games_accepts_upper_boundary() -> None:
    assert validate_min_games(100) == 100


def test_validate_min_games_rejects_zero() -> None:
    with pytest.raises(DomainValidationError):
        validate_min_games(0)


def test_validate_min_games_rejects_101() -> None:
    with pytest.raises(DomainValidationError):
        validate_min_games(101)


def test_validate_min_games_rejects_negative() -> None:
    with pytest.raises(DomainValidationError):
        validate_min_games(-1)


@pytest.mark.parametrize("value", [pytest.param(True, id="true"), pytest.param(False, id="false")])
def test_validate_min_games_rejects_bool(value: bool) -> None:
    with pytest.raises(DomainValidationError):
        validate_min_games(value)


def test_validate_min_games_rejects_non_int() -> None:
    with pytest.raises(DomainValidationError):
        validate_min_games(2.5)  # type: ignore[arg-type]


def test_validate_min_games_rejects_int_enum() -> None:
    # `type(x) is int` (not `isinstance`) rejects an IntEnum even when its
    # numeric value (5) is well within the otherwise-valid 1-100 range --
    # proving the *type* check, not a range check, is what trips here.
    with pytest.raises(DomainValidationError):
        validate_min_games(_MinGamesEnum.DEFAULT)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# clean_text
# ---------------------------------------------------------------------------


def test_clean_text_none_becomes_none_when_optional() -> None:
    assert clean_text(None, field="Description", max_len=EVENT_DESCRIPTION_MAX) is None


def test_clean_text_empty_string_becomes_none_when_optional() -> None:
    assert clean_text("   ", field="Description", max_len=EVENT_DESCRIPTION_MAX) is None


def test_clean_text_empty_raises_when_required() -> None:
    with pytest.raises(DomainValidationError):
        clean_text("   ", field="Title", max_len=EVENT_TITLE_MAX, min_len=1)


def test_clean_text_strips_surrounding_whitespace() -> None:
    assert clean_text("  hello  ", field="Title", max_len=EVENT_TITLE_MAX) == "hello"


def test_clean_text_preserves_sql_payload_verbatim() -> None:
    payload = "'; DROP TABLE x;--"
    assert clean_text(payload, field="Title", max_len=EVENT_TITLE_MAX) == payload


def test_clean_text_preserves_internal_whitespace() -> None:
    text = "hello   world"
    assert clean_text(text, field="Title", max_len=EVENT_TITLE_MAX) == text


@pytest.mark.parametrize(
    "field_max",
    [
        pytest.param(SEASON_NAME_MAX, id="season-name-max"),
        pytest.param(EVENT_TITLE_MAX, id="event-title-max"),
        pytest.param(EVENT_DESCRIPTION_MAX, id="event-description-max"),
        pytest.param(EVENT_LOCATION_MAX, id="event-location-max"),
        pytest.param(VOID_REASON_MAX, id="void-reason-max"),
    ],
)
def test_clean_text_accepts_exactly_at_max_len(field_max: int) -> None:
    text = "x" * field_max
    assert clean_text(text, field="Field", max_len=field_max) == text


@pytest.mark.parametrize(
    "field_max",
    [
        pytest.param(SEASON_NAME_MAX, id="season-name-max"),
        pytest.param(EVENT_TITLE_MAX, id="event-title-max"),
        pytest.param(EVENT_DESCRIPTION_MAX, id="event-description-max"),
        pytest.param(EVENT_LOCATION_MAX, id="event-location-max"),
        pytest.param(VOID_REASON_MAX, id="void-reason-max"),
    ],
)
def test_clean_text_rejects_one_over_max_len(field_max: int) -> None:
    text = "x" * (field_max + 1)
    with pytest.raises(DomainValidationError):
        clean_text(text, field="Field", max_len=field_max)


def test_clean_text_rejects_under_min_len() -> None:
    with pytest.raises(DomainValidationError):
        clean_text("ab", field="Name", max_len=100, min_len=3)


def test_clean_text_accepts_exactly_at_min_len() -> None:
    assert clean_text("abc", field="Name", max_len=100, min_len=3) == "abc"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("bad\x00text", id="nul-byte"),
        pytest.param("bad\rtext", id="carriage-return"),
        pytest.param("bad\ttext", id="tab"),
        pytest.param("bad\u2028text", id="line-separator-u2028"),
        pytest.param("bad\u2029text", id="paragraph-separator-u2029"),
        pytest.param("bad\x1btext", id="escape-c0-control"),
        pytest.param("bad\x7ftext", id="delete-control"),
        pytest.param("bad\x85text", id="c1-control"),
    ],
)
def test_clean_text_rejects_control_characters(raw: str) -> None:
    with pytest.raises(DomainValidationError):
        clean_text(raw, field="Title", max_len=EVENT_TITLE_MAX)


def test_clean_text_rejects_tab_even_when_newlines_allowed() -> None:
    with pytest.raises(DomainValidationError):
        clean_text(
            "bad\ttext", field="Description", max_len=EVENT_DESCRIPTION_MAX, allow_newlines=True
        )


def test_clean_text_rejects_carriage_return_even_when_newlines_allowed() -> None:
    with pytest.raises(DomainValidationError):
        clean_text(
            "bad\rtext", field="Description", max_len=EVENT_DESCRIPTION_MAX, allow_newlines=True
        )


def test_clean_text_rejects_newline_when_disallowed() -> None:
    with pytest.raises(DomainValidationError):
        clean_text("line1\nline2", field="Title", max_len=EVENT_TITLE_MAX, allow_newlines=False)


def test_clean_text_allows_newline_when_allowed() -> None:
    text = "line1\nline2"
    assert (
        clean_text(text, field="Description", max_len=EVENT_DESCRIPTION_MAX, allow_newlines=True)
        == text
    )


def test_clean_text_field_name_appears_in_error_message() -> None:
    with pytest.raises(DomainValidationError) as exc_info:
        clean_text("x" * 10, field="Void reason", max_len=5)
    assert "Void reason" in exc_info.value.user_message


# ---------------------------------------------------------------------------
# clean_text: Discord markup preserved verbatim (escaping is a display-time,
# M4 concern, not this layer's job)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "markup",
    [
        pytest.param("@everyone", id="everyone-mention"),
        pytest.param("<@&1>", id="role-mention"),
        pytest.param("[x](https://evil.example)", id="masked-link"),
        pytest.param("||spoiler||", id="spoiler-tag"),
    ],
)
def test_clean_text_preserves_discord_markup_verbatim(markup: str) -> None:
    assert clean_text(markup, field="Title", max_len=EVENT_TITLE_MAX) == markup


# ---------------------------------------------------------------------------
# clean_text: unicodedata-based character policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("bad\ud800text", id="lone-surrogate-high"),
        pytest.param("bad\udc00text", id="lone-surrogate-low"),
        pytest.param("bad\ue000text", id="private-use-area"),
        pytest.param("bad\U000f0000text", id="supplementary-private-use-plane-15"),
        pytest.param("bad\ufdd0text", id="noncharacter-fdd0"),
        pytest.param("bad\ufdeftext", id="noncharacter-fdef"),
        pytest.param("bad\ufffetext", id="noncharacter-bmp-fffe"),
        pytest.param("bad\ufffftext", id="noncharacter-bmp-ffff"),
        pytest.param("bad\U0001fffftext", id="noncharacter-plane1-ffff"),
        pytest.param("bad\u200etext", id="left-to-right-mark"),
        pytest.param("bad\u200ftext", id="right-to-left-mark"),
        pytest.param("bad\u061ctext", id="arabic-letter-mark"),
        pytest.param("bad\u200btext", id="zero-width-space"),
        pytest.param("bad\u2060text", id="word-joiner"),
        pytest.param("bad\ufefftext", id="byte-order-mark"),
        pytest.param("bad\u202atext", id="bidi-override-lro"),
        pytest.param("bad\u2066text", id="bidi-isolate-lri"),
        pytest.param("bad\U000e0001text", id="language-tag-character"),
    ],
)
def test_clean_text_rejects_dangerous_unicode_categories(raw: str) -> None:
    with pytest.raises(DomainValidationError):
        clean_text(raw, field="Title", max_len=EVENT_TITLE_MAX)


def test_clean_text_accepts_emoji_zwj_sequence() -> None:
    # Family emoji: MAN + ZWJ + WOMAN + ZWJ + GIRL. ZWJ (U+200D) is Cf but
    # explicitly allowed -- without it this wouldn't render as one emoji.
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert clean_text(family, field="Title", max_len=EVENT_TITLE_MAX) == family


def test_clean_text_accepts_persian_text_with_zwnj() -> None:
    # "mi-khwaham" ("I want"), written with a ZWNJ (U+200C) between the
    # two halves so they don't visually join -- required, correct Persian.
    persian = "می\u200cخواهم"
    assert clean_text(persian, field="Title", max_len=EVENT_TITLE_MAX) == persian


def test_clean_text_accepts_vietnamese_stacked_diacritics() -> None:
    # NFD-decomposed "e" with a combining circumflex (U+0302) *and* a
    # combining dot below (U+0323) stacked on it -- 2 consecutive combining
    # marks, well under the zalgo threshold, and completely ordinary
    # Vietnamese orthography.
    vietnamese = "Vie\u0302\u0323t Nam"
    assert clean_text(vietnamese, field="Title", max_len=EVENT_TITLE_MAX) == vietnamese


def test_clean_text_accepts_exactly_four_consecutive_combining_marks() -> None:
    text = "a" + "\u0301" * 4
    assert clean_text(text, field="Title", max_len=EVENT_TITLE_MAX) == text


def test_clean_text_rejects_five_consecutive_combining_marks_zalgo() -> None:
    text = "a" + "\u0301" * 5
    with pytest.raises(DomainValidationError):
        clean_text(text, field="Title", max_len=EVENT_TITLE_MAX)


def test_clean_text_rejects_zalgo_flood() -> None:
    text = "a" + "\u0301" * 40 + "b"
    with pytest.raises(DomainValidationError):
        clean_text(text, field="Title", max_len=EVENT_TITLE_MAX)


def test_clean_text_combining_mark_run_resets_after_a_base_character() -> None:
    # Two separate 3-mark runs (each under the threshold), broken up by
    # ordinary base characters -- never 5 in a row, so this is fine.
    text = "a" + "\u0301" * 3 + "b" + "\u0301" * 3 + "c"
    assert clean_text(text, field="Title", max_len=EVENT_TITLE_MAX) == text


def test_clean_text_accepts_exactly_four_consecutive_enclosing_marks() -> None:
    # \u20dd (COMBINING ENCLOSING CIRCLE) is category Me, not Mn -- the
    # consecutive-marks counter must treat both categories the same way.
    text = "a" + "\u20dd" * 4
    assert clean_text(text, field="Title", max_len=EVENT_TITLE_MAX) == text


def test_clean_text_rejects_five_consecutive_enclosing_marks_with_stacked_marks_message() -> None:
    text = "a" + "\u20dd" * 5
    with pytest.raises(DomainValidationError) as exc_info:
        clean_text(text, field="Title", max_len=EVENT_TITLE_MAX)
    assert "stacked" in exc_info.value.user_message


# ---------------------------------------------------------------------------
# clean_text: "meaningfully blank" after removing invisible content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filler",
    [
        pytest.param("\u3164", id="hangul-filler"),
        pytest.param("\u115f", id="hangul-choseong-filler"),
        pytest.param("\u1160", id="hangul-jungseong-filler"),
        pytest.param("\uffa0", id="halfwidth-hangul-filler"),
        pytest.param("\u2800", id="braille-pattern-blank"),
    ],
)
def test_clean_text_lone_invisible_filler_is_treated_as_blank(filler: str) -> None:
    assert clean_text(filler, field="Title", max_len=EVENT_TITLE_MAX) is None


def test_clean_text_lone_filler_required_field_raises() -> None:
    with pytest.raises(DomainValidationError):
        clean_text("\u3164", field="Title", max_len=EVENT_TITLE_MAX, min_len=1)


def test_clean_text_lone_zwnj_is_treated_as_blank() -> None:
    # ZWNJ is *allowed* as content, but on its own -- with nothing to
    # (non-)join -- it carries no meaning, so it counts as blank too.
    assert clean_text("\u200c", field="Title", max_len=EVENT_TITLE_MAX) is None


def test_clean_text_whitespace_and_fillers_combined_is_blank() -> None:
    text = "  \u3164 \u2800  "
    assert clean_text(text, field="Title", max_len=EVENT_TITLE_MAX) is None


def test_clean_text_filler_mixed_with_real_content_is_preserved_verbatim() -> None:
    # A filler *alongside* real content isn't blank -- it's legitimate,
    # if unusual, content and must come back unchanged.
    text = "Hello \u3164 World"
    assert clean_text(text, field="Title", max_len=EVENT_TITLE_MAX) == text


# ---------------------------------------------------------------------------
# clean_text: length check runs before the character policy (I4)
# ---------------------------------------------------------------------------


def test_clean_text_oversized_input_rejected_without_scanning_characters() -> None:
    # A huge string of otherwise-harmless characters must still be rejected
    # promptly on length alone.
    text = "x" * 100_000
    with pytest.raises(DomainValidationError):
        clean_text(text, field="Title", max_len=EVENT_TITLE_MAX)


def test_clean_text_over_length_null_bytes_raises_length_message_not_character_message() -> None:
    # 101 NUL (\x00 x 101) bytes is *both* over max_len=100 *and* full of
    # banned control characters -- proving the length check runs first (and
    # short-circuits before the character policy ever sees them) requires
    # checking *which* message comes back, not just that some
    # DomainValidationError does.
    with pytest.raises(DomainValidationError) as exc_info:
        clean_text("\x00" * 101, field="Name", max_len=100)
    assert "at most 100" in exc_info.value.user_message
