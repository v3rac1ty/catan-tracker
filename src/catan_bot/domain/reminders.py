"""Pure event-reminder scheduling and staleness classification.

The scheduler (outside `domain/`) is restart-safe because everything here
is a pure function of `now`: no reminder plan or classification depends on
when the process happened to be running.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from catan_bot.domain.dates import ensure_aware, to_utc

REMINDER_OFFSETS_MINUTES: tuple[int, ...] = (1440, 60)
DEFAULT_GRACE = timedelta(minutes=15)
EVENT_COMPLETE_AFTER = timedelta(hours=6)

ReminderStatus = Literal["not_due", "send", "skip_stale"]


@dataclass(frozen=True, slots=True)
class ReminderPlan:
    offset_minutes: int
    remind_at: datetime


def plan_reminders(starts_at: datetime, *, now: datetime) -> list[ReminderPlan]:
    """The reminders still worth scheduling for an event, earliest first.

    An offset whose `remind_at` has already passed `now` is skipped, e.g. an
    event created 30 minutes out never gets its 24h or 1h reminder. Every
    comparison normalizes to UTC first (see `dates.to_utc`): `remind_at`
    inherits `starts_at`'s `tzinfo`, and if that's the same `ZoneInfo`
    object `now` uses, a same-`tzinfo` comparison would silently ignore an
    ambiguous local hour's `fold`.
    """
    ensure_aware(starts_at, name="starts_at")
    ensure_aware(now, name="now")
    now_utc = to_utc(now)
    candidates = (
        ReminderPlan(offset_minutes=offset, remind_at=starts_at - timedelta(minutes=offset))
        for offset in REMINDER_OFFSETS_MINUTES
    )
    due = [plan for plan in candidates if to_utc(plan.remind_at) > now_utc]
    return sorted(due, key=lambda plan: to_utc(plan.remind_at))


def classify_reminder(
    remind_at: datetime, *, now: datetime, grace: timedelta = DEFAULT_GRACE
) -> ReminderStatus:
    """Whether a reminder is not yet due, still worth sending, or too stale.

    If the bot was down past `grace` when a reminder came due, it's marked
    sent silently (`skip_stale`) instead of pinging everyone late.
    """
    ensure_aware(remind_at, name="remind_at")
    ensure_aware(now, name="now")
    remind_at_utc = to_utc(remind_at)
    now_utc = to_utc(now)
    if remind_at_utc > now_utc:
        return "not_due"
    if now_utc - remind_at_utc <= grace:
        return "send"
    return "skip_stale"


def is_event_completed(starts_at: datetime, *, now: datetime) -> bool:
    """Whether an event is old enough to be marked completed."""
    ensure_aware(starts_at, name="starts_at")
    ensure_aware(now, name="now")
    return to_utc(now) >= to_utc(starts_at) + EVENT_COMPLETE_AFTER
