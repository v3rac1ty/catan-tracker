"""Unit tests for `catan_bot.domain.dates`. No Docker, no system clock."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from catan_bot.domain.dates import (
    MAX_DATE,
    MAX_DATE_INPUT_LEN,
    MAX_TIMEZONE_INPUT_LEN,
    MIN_GAME_DATE,
    _first_instant_of_local_date,
    combine_local,
    ensure_aware,
    local_time_in_timezone,
    parse_date,
    parse_time,
    season_end_instant,
    today_in_timezone,
    validate_timezone,
)
from catan_bot.domain.errors import DomainValidationError

UTC = ZoneInfo("UTC")

# ---------------------------------------------------------------------------
# validate_timezone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("UTC", id="utc-exact"),
        pytest.param("America/Chicago", id="america-chicago"),
        pytest.param("America/Havana", id="america-havana"),
        pytest.param("  UTC  ", id="utc-surrounded-by-whitespace"),
        pytest.param("\tAmerica/Chicago\n", id="tabs-and-newline-whitespace"),
    ],
)
def test_validate_timezone_accepts_real_zones(name: str) -> None:
    assert validate_timezone(name) == name.strip()


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("Mars/Base", id="fictional-zone"),
        pytest.param("", id="empty-string"),
        pytest.param("utc; DROP", id="sql-payload-suffix"),
        pytest.param("../etc/localtime", id="path-traversal-like"),
        pytest.param("utc", id="lowercase-not-a-real-key"),
    ],
)
def test_validate_timezone_rejects_bad_input(name: str) -> None:
    with pytest.raises(DomainValidationError):
        validate_timezone(name)


# L1: the error message must be a fixed, friendly string -- never the raw
# input echoed back. An unbounded, unescaped value reflected into
# user-facing text is a Discord masked-link/markdown injection vector.


def test_validate_timezone_error_message_is_friendly_short_and_fixed() -> None:
    with pytest.raises(DomainValidationError) as exc_info:
        validate_timezone("Mars/Base")
    assert "Mars/Base" not in exc_info.value.user_message
    assert len(exc_info.value.user_message) < 80


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("[click here](https://evil.example)", id="masked-link"),
        pytest.param("@everyone", id="everyone-mention"),
        pytest.param("x" * 100_000, id="hundred-thousand-chars"),
    ],
)
def test_validate_timezone_never_echoes_dangerous_input(payload: str) -> None:
    with pytest.raises(DomainValidationError) as exc_info:
        validate_timezone(payload)
    message = exc_info.value.user_message
    assert payload not in message
    assert "evil.example" not in message
    assert "@everyone" not in message
    assert len(message) < 80


def test_validate_timezone_rejects_input_over_64_chars_before_any_work() -> None:
    too_long = "America/Chicago" + "x" * (MAX_TIMEZONE_INPUT_LEN)
    assert len(too_long) > MAX_TIMEZONE_INPUT_LEN
    with pytest.raises(DomainValidationError) as exc_info:
        validate_timezone(too_long)
    assert too_long not in exc_info.value.user_message


def test_validate_timezone_at_exactly_64_chars_passes_the_length_gate() -> None:
    # No real zone name is 64 characters long, so this still rejects -- but
    # it must fail on *content*, not get short-circuited by the length cap
    # (which only rejects strictly *more* than 64 chars).
    exactly_64 = "A" * MAX_TIMEZONE_INPUT_LEN
    assert len(exactly_64) == MAX_TIMEZONE_INPUT_LEN
    with pytest.raises(DomainValidationError):
        validate_timezone(exactly_64)


def test_validate_timezone_rejects_real_zone_padded_past_length_cap() -> None:
    # The length gate checks `name` itself, before `strip()` ever runs. A
    # real, otherwise-valid zone name padded with enough leading whitespace
    # to cross MAX_TIMEZONE_INPUT_LEN must still be rejected -- if the gate
    # were removed (or moved to run after stripping), this would strip down
    # to plain "UTC" and incorrectly pass.
    padded = " " * 70 + "UTC"
    assert len(padded) > MAX_TIMEZONE_INPUT_LEN
    with pytest.raises(DomainValidationError):
        validate_timezone(padded)


# ---------------------------------------------------------------------------
# today_in_timezone
# ---------------------------------------------------------------------------


def test_today_in_timezone_differs_from_utc_late_at_night() -> None:
    now = datetime(2026, 9, 14, 4, 30, tzinfo=UTC)
    assert today_in_timezone("America/Chicago", now=now) == date(2026, 9, 13)
    assert now.date() == date(2026, 9, 14)


def test_today_in_timezone_utc_matches_utc_date() -> None:
    now = datetime(2026, 9, 14, 4, 30, tzinfo=UTC)
    assert today_in_timezone("UTC", now=now) == date(2026, 9, 14)


def test_today_in_timezone_rejects_naive_now() -> None:
    with pytest.raises(ValueError, match="aware"):
        today_in_timezone("UTC", now=datetime(2026, 9, 14, 4, 30))


def test_today_in_timezone_rejects_bad_zone() -> None:
    with pytest.raises(DomainValidationError):
        today_in_timezone("Mars/Base", now=datetime(2026, 9, 14, 4, 30, tzinfo=UTC))


# ---------------------------------------------------------------------------
# local_time_in_timezone
# ---------------------------------------------------------------------------


def test_local_time_in_timezone_differs_from_utc_late_at_night() -> None:
    now = datetime(2026, 9, 14, 4, 30, tzinfo=UTC)
    # America/Chicago is UTC-5 in September (CDT): 04:30 UTC is 23:30 the
    # previous local day.
    assert local_time_in_timezone("America/Chicago", now=now) == time(23, 30)
    assert now.time() == time(4, 30)


def test_local_time_in_timezone_utc_matches_utc_time() -> None:
    now = datetime(2026, 9, 14, 4, 30, tzinfo=UTC)
    assert local_time_in_timezone("UTC", now=now) == time(4, 30)


def test_local_time_in_timezone_rejects_naive_now() -> None:
    with pytest.raises(ValueError, match="aware"):
        local_time_in_timezone("UTC", now=datetime(2026, 9, 14, 4, 30))


def test_local_time_in_timezone_rejects_bad_zone() -> None:
    with pytest.raises(DomainValidationError):
        local_time_in_timezone("Mars/Base", now=datetime(2026, 9, 14, 4, 30, tzinfo=UTC))


# ---------------------------------------------------------------------------
# parse_date: defaulting
# ---------------------------------------------------------------------------

_TODAY = date(2026, 9, 13)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(None, id="none"),
        pytest.param("", id="empty-string"),
        pytest.param("   ", id="whitespace-only"),
    ],
)
def test_parse_date_defaults_to_today_when_omitted(text: str | None) -> None:
    assert parse_date(text, today=_TODAY) == _TODAY


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("today", id="lowercase"),
        pytest.param("Today", id="titlecase"),
        pytest.param("TODAY", id="uppercase"),
        pytest.param(" today ", id="surrounded-by-whitespace"),
    ],
)
def test_parse_date_today_keyword(text: str) -> None:
    assert parse_date(text, today=_TODAY) == _TODAY


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("yesterday", id="lowercase"),
        pytest.param("Yesterday", id="titlecase"),
        pytest.param("YESTERDAY", id="uppercase"),
    ],
)
def test_parse_date_yesterday_keyword(text: str) -> None:
    assert parse_date(text, today=_TODAY) == _TODAY - timedelta(days=1)


# ---------------------------------------------------------------------------
# parse_date: accepted formats
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("2026-09-14", date(2026, 9, 14), id="iso-standard"),
        pytest.param("2026-01-01", date(2026, 1, 1), id="iso-year-start"),
        pytest.param("09/14/2026", date(2026, 9, 14), id="us-zero-padded"),
        pytest.param("9/4/2026", date(2026, 9, 4), id="us-single-digit-month-and-day"),
        pytest.param("12/31/2026", date(2026, 12, 31), id="us-year-end"),
        pytest.param("01/01/1995", MIN_GAME_DATE, id="us-min-game-date"),
    ],
)
def test_parse_date_accepts_both_formats(text: str, expected: date) -> None:
    assert parse_date(text, today=_TODAY) == expected


# ---------------------------------------------------------------------------
# parse_date: rejections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("2026-9-14", id="iso-unpadded-month"),
        pytest.param("09-14-2026", id="dash-separated-us-order"),
        pytest.param("2026/09/14", id="slash-separated-iso-order"),
        pytest.param("14/09/2026", id="us-format-invalid-month"),
        pytest.param("2026-02-30", id="iso-shape-invalid-calendar-date"),
        pytest.param("٢٠٢٦-٠٩-١٤", id="arabic-indic-digits"),
        # `%d` in `strptime` happily accepts a non-ASCII decimal digit (e.g.
        # this Arabic-Indic 3), so if the ASCII-only US-date regex were ever
        # weakened or removed, this would silently parse as day 13 instead
        # of being rejected -- only the regex stands between this and that.
        pytest.param("12/1" + chr(0x663) + "/2026", id="arabic-indic-digit-in-us-day"),
        pytest.param("2026-09-14X" * 4, id="over-max-length"),
        pytest.param("' OR 1=1--", id="sql-payload"),
        pytest.param("not-a-date", id="garbage-text"),
        pytest.param("2026-13-01", id="iso-shape-invalid-month"),
        pytest.param("13/01/2026", id="us-shape-invalid-month-13"),
        pytest.param("12/31/9999", id="us-year-way-past-max-date"),
        pytest.param("9999-12-31", id="iso-year-way-past-max-date"),
        pytest.param("0001-01-01", id="iso-year-way-before-min-game-date"),
        pytest.param("2101-01-01", id="iso-year-one-past-max-date"),
    ],
)
def test_parse_date_rejects_bad_input(text: str) -> None:
    with pytest.raises(DomainValidationError):
        parse_date(text, today=_TODAY)


def test_parse_date_accepts_max_date_boundary() -> None:
    assert parse_date(MAX_DATE.isoformat(), today=_TODAY) == MAX_DATE


def test_parse_date_rejects_fullwidth_digits() -> None:
    # Fullwidth digit characters (U+FF10 "0" ... U+FF19 "9") look like
    # ASCII digits when rendered but aren't matched by an ASCII '[0-9]'
    # class.
    fullwidth = "２０２６-０９-１４"  # "2026-09-14"
    with pytest.raises(DomainValidationError):
        parse_date(fullwidth, today=_TODAY)


# L9 / mutant-kill: a mix of ASCII and Arabic-Indic digits in the *same*
# string. A mutant that swapped the strict ASCII '[0-9]' character class
# for the Unicode-aware '\d' would accept these (both Python's regex `\d`
# and `int()`/`strptime` happily consume Arabic-Indic digits), so an
# all-ASCII test alone wouldn't catch that regression -- a partial mix
# forces every position to be checked.
@pytest.mark.parametrize(
    "text",
    [
        pytest.param("٢٠٢٦-09-14", id="all-arabic-indic-date-part"),
        pytest.param("2026-09-1٤", id="single-arabic-indic-digit-at-end"),
        pytest.param("١٢/31/2026", id="arabic-indic-month-us-format"),
    ],
)
def test_parse_date_rejects_mixed_ascii_and_arabic_indic_digits(text: str) -> None:
    with pytest.raises(DomainValidationError):
        parse_date(text, today=_TODAY)


def test_parse_date_length_boundary_at_max_is_still_checked_for_shape() -> None:
    text = "x" * MAX_DATE_INPUT_LEN
    assert len(text) == MAX_DATE_INPUT_LEN
    with pytest.raises(DomainValidationError):
        parse_date(text, today=_TODAY)


def test_parse_date_length_boundary_over_max_is_rejected() -> None:
    text = "x" * (MAX_DATE_INPUT_LEN + 1)
    with pytest.raises(DomainValidationError):
        parse_date(text, today=_TODAY)


def test_parse_date_error_message_names_accepted_formats() -> None:
    with pytest.raises(DomainValidationError) as exc_info:
        parse_date("garbage", today=_TODAY)
    message = exc_info.value.user_message
    assert "YYYY-MM-DD" in message
    assert "MM/DD/YYYY" in message


# ---------------------------------------------------------------------------
# parse_time: accepted formats
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("7pm", time(19, 0), id="12h-hour-only-lowercase-pm"),
        pytest.param("7PM", time(19, 0), id="12h-hour-only-uppercase-pm"),
        pytest.param("7am", time(7, 0), id="12h-hour-only-am"),
        pytest.param("7:30 PM", time(19, 30), id="12h-with-minutes-and-space"),
        pytest.param("7:30pm", time(19, 30), id="12h-with-minutes-no-space"),
        pytest.param("7 pm", time(19, 0), id="12h-hour-only-with-space"),
        pytest.param("12:00am", time(0, 0), id="12h-midnight"),
        pytest.param("12:00pm", time(12, 0), id="12h-noon"),
        pytest.param("12am", time(0, 0), id="12h-midnight-hour-only"),
        pytest.param("12pm", time(12, 0), id="12h-noon-hour-only"),
        pytest.param("19:30", time(19, 30), id="24h-two-digit-hour"),
        pytest.param("0:00", time(0, 0), id="24h-single-digit-hour-midnight"),
        pytest.param("9:05", time(9, 5), id="24h-single-digit-hour-with-minutes"),
        pytest.param("23:59", time(23, 59), id="24h-last-minute-of-day"),
        pytest.param("00:00", time(0, 0), id="24h-zero-padded-midnight"),
    ],
)
def test_parse_time_accepts_both_formats(text: str, expected: time) -> None:
    assert parse_time(text) == expected


# ---------------------------------------------------------------------------
# parse_time: rejections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("24:00", id="24h-hour-out-of-range"),
        pytest.param("13pm", id="12h-hour-out-of-range"),
        pytest.param("7:60", id="24h-minute-out-of-range"),
        pytest.param("7.30pm", id="wrong-separator-period"),
        pytest.param("0pm", id="12h-hour-zero-invalid"),
        pytest.param("", id="empty-string"),
        pytest.param("   ", id="whitespace-only"),
        pytest.param("7", id="hour-only-no-meridiem"),
        pytest.param("seven pm", id="spelled-out"),
        pytest.param("' OR 1=1--", id="sql-payload"),
        pytest.param("19:30:00", id="24h-with-seconds"),
        pytest.param("١٩:٣٠", id="arabic-indic-digits"),
        # Same non-ASCII-digit gap as the date regex above, but for the 24h
        # time regex: only the ASCII-only `[0-5][0-9]` minute class stops
        # this from being read as a plausible ":30".
        pytest.param("1:3" + chr(0x660), id="arabic-indic-digit-in-24h-minute"),
        pytest.param("7:300pm", id="three-digit-minute"),
        pytest.param("x" * 32, id="way-over-length-cap"),
    ],
)
def test_parse_time_rejects_bad_input(text: str) -> None:
    with pytest.raises(DomainValidationError):
        parse_time(text)


def test_parse_time_24h_single_digit_hour_with_minutes_is_valid() -> None:
    assert parse_time("7:30") == time(7, 30)


# ---------------------------------------------------------------------------
# combine_local: DST gap and ambiguity
# ---------------------------------------------------------------------------


def test_combine_local_rejects_spring_forward_gap() -> None:
    # US DST starts 2026-03-08 at 02:00 -> 03:00 local in America/Chicago,
    # so 02:30 never happens that day.
    with pytest.raises(DomainValidationError):
        combine_local(date(2026, 3, 8), time(2, 30), "America/Chicago")


def test_combine_local_accepts_time_just_before_the_gap() -> None:
    result = combine_local(date(2026, 3, 8), time(1, 59), "America/Chicago")
    assert result == datetime(2026, 3, 8, 7, 59, tzinfo=UTC)


def test_combine_local_accepts_time_just_after_the_gap() -> None:
    result = combine_local(date(2026, 3, 8), time(3, 0), "America/Chicago")
    assert result == datetime(2026, 3, 8, 8, 0, tzinfo=UTC)


def test_combine_local_ambiguous_fall_back_uses_earlier_occurrence() -> None:
    # US DST ends 2026-11-01: clocks fall back from 02:00 CDT to 01:00 CST,
    # so 01:30 happens twice. The earlier (CDT, UTC-5) occurrence wins.
    result = combine_local(date(2026, 11, 1), time(1, 30), "America/Chicago")
    assert result == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


def test_combine_local_rejects_bad_timezone() -> None:
    with pytest.raises(DomainValidationError):
        combine_local(date(2026, 9, 14), time(19, 0), "Mars/Base")


def test_combine_local_ordinary_time_round_trips() -> None:
    result = combine_local(date(2026, 6, 15), time(12, 0), "America/Chicago")
    assert result == datetime(2026, 6, 15, 17, 0, tzinfo=UTC)


# L2: a date near datetime.min/max, combined with a fixed-offset zone that
# pushes the UTC conversion past the supported range, must raise a
# friendly DomainValidationError instead of a raw OverflowError. "Etc/GMT*"
# zones are fixed-offset with no DST and no historical LMT quirks, so the
# overflow is deterministic.


def test_combine_local_overflow_at_max_date_becomes_domain_error() -> None:
    with pytest.raises(DomainValidationError):
        combine_local(date(9999, 12, 31), time(23, 59), "Etc/GMT+12")


def test_combine_local_overflow_at_min_date_becomes_domain_error() -> None:
    with pytest.raises(DomainValidationError):
        combine_local(date(1, 1, 1), time(0, 0), "Etc/GMT-14")


# ---------------------------------------------------------------------------
# season_end_instant
# ---------------------------------------------------------------------------


def test_season_end_instant_ordinary_zone() -> None:
    result = season_end_instant(date(2026, 9, 13), "America/Chicago")
    assert result == datetime(2026, 9, 14, 5, 0, tzinfo=UTC)


def test_season_end_instant_utc() -> None:
    result = season_end_instant(date(2026, 9, 13), "UTC")
    assert result == datetime(2026, 9, 14, 0, 0, tzinfo=UTC)


def test_season_end_instant_havana_midnight_gap() -> None:
    # America/Havana's 2026 DST starts at local 00:00 -> 01:00 on 03-08, so
    # midnight on that date doesn't exist. The real first instant of that
    # local day is 01:00 local (DST), i.e. 05:00 UTC.
    result = season_end_instant(date(2026, 3, 7), "America/Havana")
    assert result == datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
    # And it really is the *first* instant: local wall clock reads 01:00:00.
    assert result.astimezone(ZoneInfo("America/Havana")) == datetime(
        2026, 3, 8, 1, 0, tzinfo=ZoneInfo("America/Havana")
    )


def test_season_end_instant_rejects_bad_timezone() -> None:
    with pytest.raises(DomainValidationError):
        season_end_instant(date(2026, 9, 13), "Mars/Base")


def test_season_end_instant_overflow_at_max_date_becomes_domain_error() -> None:
    # `ends_on + timedelta(days=1)` alone overflows past date.max here.
    with pytest.raises(DomainValidationError):
        season_end_instant(date(9999, 12, 31), "UTC")


def test_season_end_instant_apia_date_skip_requires_the_slow_path() -> None:
    # Samoa skipped the whole calendar date 2011-12-30 when it jumped the
    # international date line: local time went straight from 2011-12-29
    # 23:59:59 to 2011-12-31 00:00:00. So the "day after" 2011-12-29 that
    # actually exists locally is 2011-12-31, not 2011-12-30.
    apia = ZoneInfo("Pacific/Apia")
    ends_on = date(2011, 12, 29)
    skipped_day = date(2011, 12, 30)

    # Prove the fast path's round-trip *guard check* fails for this case --
    # converting the naive midnight candidate back to local time lands on
    # 2011-12-31 00:00, not the expected 2011-12-30 00:00, so the guard
    # correctly refuses to trust it and falls through to the slow path
    # (`_first_instant_of_local_date`). This is the property the test
    # actually exercises: the naive candidate's raw UTC value happens to
    # already match the right answer asserted below, so this isn't proof
    # that the candidate itself is numerically wrong -- only that the
    # guard can't know that without the round trip, and defers to the slow
    # path regardless.
    naive_midnight = datetime.combine(skipped_day, time.min, tzinfo=apia).replace(fold=0)
    candidate_utc = naive_midnight.astimezone(UTC)
    round_trip = candidate_utc.astimezone(apia)
    assert round_trip.date() != skipped_day

    result = season_end_instant(ends_on, "Pacific/Apia")
    assert result == datetime(2011, 12, 30, 10, 0, tzinfo=UTC)
    assert result.astimezone(apia) == datetime(2011, 12, 31, 0, 0, tzinfo=apia)


def test_first_instant_of_local_date_direct_apia_date_skip() -> None:
    apia = ZoneInfo("Pacific/Apia")
    near = datetime(2011, 12, 30, 10, 0, tzinfo=UTC)
    result = _first_instant_of_local_date(date(2011, 12, 31), apia, near=near)
    assert result == datetime(2011, 12, 30, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# ensure_aware (used across domain modules; tested directly here)
# ---------------------------------------------------------------------------


def test_ensure_aware_accepts_aware_datetime() -> None:
    ensure_aware(datetime(2026, 1, 1, tzinfo=UTC), name="now")


def test_ensure_aware_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="now"):
        ensure_aware(datetime(2026, 1, 1), name="now")
