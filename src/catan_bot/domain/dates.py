"""Pure date/time parsing and timezone handling.

Nothing here reads the system clock: every function that needs "now" or
"today" takes it as a parameter. Timezone names are whitelisted against
`zoneinfo.available_timezones()` rather than trusted as free text, since
they eventually become part of a `ZoneInfo(...)` lookup.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from zoneinfo import ZoneInfo, available_timezones

from catan_bot.domain.errors import DomainValidationError

MIN_GAME_DATE = date(1995, 1, 1)
MAX_DATE = date(2100, 12, 31)
MAX_DATE_INPUT_LEN = 32
MAX_TIME_INPUT_LEN = 16
MAX_TIMEZONE_INPUT_LEN = 64

_UTC = ZoneInfo("UTC")

_DATE_FORMAT_HINT = "Use YYYY-MM-DD or MM/DD/YYYY (or 'today' / 'yesterday')."
_TIME_FORMAT_HINT = "Use 24-hour HH:MM (e.g. 19:30) or 12-hour h:MMam/pm (e.g. 7:30pm)."
_TIMEZONE_HINT = "That isn't a recognized timezone name. Pick one from the list."
_OUT_OF_RANGE_HINT = "That date or time is too far in the past or future to schedule."

# ASCII digits only ('[0-9]', never '\d'), so lookalike Arabic-Indic or
# fullwidth digits never sneak a date/time past the shape check.
_ISO_DATE_RE = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_US_DATE_RE = re.compile(r"\A[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}\Z")

_TIME_24H_RE = re.compile(r"\A(2[0-3]|[01]?[0-9]):([0-5][0-9])\Z")
_TIME_12H_RE = re.compile(r"\A(1[0-2]|[1-9])(?::([0-5][0-9]))?[ ]?([AaPp][Mm])\Z")

_BISECTION_TOLERANCE = timedelta.resolution


def ensure_aware(value: datetime, *, name: str) -> None:
    """Raise plain `ValueError` (not `DomainValidationError`) for a naive datetime.

    A naive "now"/"starts_at" is a caller bug, not a user input problem, so
    it doesn't get a friendly `user_message`.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime.")


def to_utc(value: datetime) -> datetime:
    """Normalize an *already-aware* datetime to UTC for a safe comparison.

    Two aware datetimes that happen to share the same `tzinfo` object
    compare using their naive wall-clock fields only (a documented
    `datetime` rule) -- silently ignoring `fold`. That can misorder an
    ambiguous local hour (a DST fall-back), so every comparison in this
    package normalizes both sides to UTC first. Callers must call
    `ensure_aware` first; this never reads the system clock or timezone.
    """
    return value.astimezone(UTC)


@cache
def _available_timezones() -> frozenset[str]:
    return frozenset(available_timezones())


def validate_timezone(name: str) -> str:
    """Return `name` (surrounding whitespace stripped) if it is a real IANA zone.

    The error message never echoes `name` back: an unbounded, unescaped
    value reflected into user-facing text is a markdown/masked-link
    injection vector once it reaches Discord.
    """
    if len(name) > MAX_TIMEZONE_INPUT_LEN:
        raise DomainValidationError(_TIMEZONE_HINT)
    stripped = name.strip()
    if stripped not in _available_timezones():
        raise DomainValidationError(_TIMEZONE_HINT)
    return stripped


def today_in_timezone(tz_name: str, *, now: datetime) -> date:
    """The calendar date `now` falls on, local to `tz_name`."""
    ensure_aware(now, name="now")
    zone = ZoneInfo(validate_timezone(tz_name))
    return now.astimezone(zone).date()


def local_time_in_timezone(tz_name: str, *, now: datetime) -> time:
    """The wall-clock time-of-day `now` falls on, local to `tz_name`.

    Companion to `today_in_timezone`: the recurring daily leaderboard digest
    (`services.leaderboard_service.due_daily_leaderboards`) needs both --
    the date to check "has today already been posted" and the time to check
    "has the configured post time passed yet" -- computed from the exact
    same `now`/timezone pair so the two never disagree about what "now"
    locally means.
    """
    ensure_aware(now, name="now")
    zone = ZoneInfo(validate_timezone(tz_name))
    return now.astimezone(zone).time()


def parse_date(text: str | None, *, today: date) -> date:
    """Parse a user-supplied date, defaulting to `today` when omitted."""
    stripped = "" if text is None else text.strip()
    if not stripped:
        return today
    if len(stripped) > MAX_DATE_INPUT_LEN:
        raise DomainValidationError(_DATE_FORMAT_HINT)
    lowered = stripped.lower()
    if lowered == "today":
        return today
    if lowered == "yesterday":
        return today - timedelta(days=1)
    if _ISO_DATE_RE.fullmatch(stripped):
        fmt = "%Y-%m-%d"
    elif _US_DATE_RE.fullmatch(stripped):
        fmt = "%m/%d/%Y"
    else:
        raise DomainValidationError(_DATE_FORMAT_HINT)
    try:
        parsed = datetime.strptime(stripped, fmt).date()
    except ValueError as exc:
        raise DomainValidationError(_DATE_FORMAT_HINT) from exc
    if parsed < MIN_GAME_DATE or parsed > MAX_DATE:
        raise DomainValidationError(_DATE_FORMAT_HINT)
    return parsed


def _to_24_hour(hour_12: int, meridiem: str) -> int:
    if meridiem == "am":
        return 0 if hour_12 == 12 else hour_12
    return 12 if hour_12 == 12 else hour_12 + 12


def parse_time(text: str) -> time:
    """Parse a required 24h (`HH:MM`) or 12h (`h:MMam`/`h am`) time string."""
    if len(text) > MAX_TIME_INPUT_LEN:
        raise DomainValidationError(_TIME_FORMAT_HINT)
    stripped = text.strip()
    if not stripped:
        raise DomainValidationError(_TIME_FORMAT_HINT)

    match_24h = _TIME_24H_RE.fullmatch(stripped)
    if match_24h:
        return time(hour=int(match_24h.group(1)), minute=int(match_24h.group(2)))

    match_12h = _TIME_12H_RE.fullmatch(stripped)
    if match_12h:
        hour_12 = int(match_12h.group(1))
        minute = int(match_12h.group(2)) if match_12h.group(2) else 0
        hour_24 = _to_24_hour(hour_12, match_12h.group(3).lower())
        return time(hour=hour_24, minute=minute)

    raise DomainValidationError(_TIME_FORMAT_HINT)


def combine_local(d: date, t: time, tz_name: str) -> datetime:
    """Combine a local date and time into an aware UTC datetime.

    Rejects a local time that never happened, i.e. one that falls in a DST
    spring-forward gap: detected by converting to UTC and back and checking
    the wall clock still reads `t`. An ambiguous (fall-back) local time
    resolves to its earlier occurrence (`fold=0`, the default), matching
    how most calendar apps interpret an ambiguous entry. A date near the
    outer edge of the supported range can push the UTC conversion past
    `datetime.min`/`datetime.max`; that becomes a friendly error rather than
    a raw `OverflowError`.
    """
    zone = ZoneInfo(validate_timezone(tz_name))
    try:
        local_dt = datetime.combine(d, t, tzinfo=zone).replace(fold=0)
        utc_dt = local_dt.astimezone(_UTC)
        round_trip = utc_dt.astimezone(zone)
    except OverflowError as exc:
        raise DomainValidationError(_OUT_OF_RANGE_HINT) from exc
    if round_trip.date() != d or round_trip.time() != t:
        raise DomainValidationError(
            "That time doesn't exist in the selected timezone because of a clock change."
        )
    return utc_dt


def preserve_local_time(value: datetime, target_date: date, tz_name: str) -> datetime:
    """Move an aware instant to ``target_date`` while retaining local wall time.

    This is used when editing a dated event or game: changing the calendar
    date should not unexpectedly change the entered local clock time.  The
    returned value is normalized to UTC and receives the same nonexistent or
    ambiguous-time validation as :func:`combine_local`.
    """
    ensure_aware(value, name="value")
    zone = ZoneInfo(validate_timezone(tz_name))
    local_time = value.astimezone(zone).timetz().replace(tzinfo=None)
    return combine_local(target_date, local_time, tz_name)


def _first_instant_of_local_date(target: date, zone: ZoneInfo, *, near: datetime) -> datetime:
    """The smallest UTC instant whose local date in `zone` is `target`.

    Only used when local midnight doesn't exist (a DST gap starts exactly
    at midnight, e.g. `America/Havana` on 2026-03-08, or a whole local day
    is skipped entirely, e.g. `Pacific/Apia` on 2011-12-30). Binary search
    is general-purpose here: a local calendar date is a monotonic
    non-decreasing function of UTC time, so there is exactly one boundary
    to find, regardless of the zone's specific transition rule.
    """
    lo, hi = near - timedelta(days=2), near + timedelta(days=2)
    while lo.astimezone(zone).date() >= target:
        lo -= timedelta(days=2)
    while hi.astimezone(zone).date() < target:
        hi += timedelta(days=2)
    while hi - lo > _BISECTION_TOLERANCE:
        mid = lo + (hi - lo) / 2
        if mid.astimezone(zone).date() >= target:
            hi = mid
        else:
            lo = mid
    return hi


def season_end_instant(ends_on: date, tz_name: str) -> datetime:
    """The first instant of the day after `ends_on`, local to `tz_name`, as UTC."""
    zone = ZoneInfo(validate_timezone(tz_name))
    try:
        next_day = ends_on + timedelta(days=1)
        naive_midnight = datetime.combine(next_day, time.min, tzinfo=zone).replace(fold=0)
        candidate_utc = naive_midnight.astimezone(_UTC)
        round_trip = candidate_utc.astimezone(zone)
        if round_trip.date() == next_day and round_trip.time() == time.min:
            return candidate_utc
        return _first_instant_of_local_date(next_day, zone, near=candidate_utc)
    except OverflowError as exc:
        raise DomainValidationError(_OUT_OF_RANGE_HINT) from exc
