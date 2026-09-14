"""Unit tests for `catan_bot.domain.reminders`. No Docker, no system clock."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from catan_bot.domain.reminders import (
    DEFAULT_GRACE,
    EVENT_COMPLETE_AFTER,
    REMINDER_OFFSETS_MINUTES,
    ReminderPlan,
    classify_reminder,
    is_event_completed,
    plan_reminders,
)

UTC = ZoneInfo("UTC")
CHICAGO = ZoneInfo("America/Chicago")

# ---------------------------------------------------------------------------
# plan_reminders
# ---------------------------------------------------------------------------


def test_plan_reminders_far_out_event_gets_both_offsets() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    starts_at = now + timedelta(days=3)
    plans = plan_reminders(starts_at, now=now)
    assert [p.offset_minutes for p in plans] == [1440, 60]
    assert plans[0].remind_at < plans[1].remind_at


def test_plan_reminders_event_30_minutes_out_has_no_reminders() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    starts_at = now + timedelta(minutes=30)
    assert plan_reminders(starts_at, now=now) == []


def test_plan_reminders_event_2_hours_out_gets_only_60_minute_reminder() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    starts_at = now + timedelta(hours=2)
    plans = plan_reminders(starts_at, now=now)
    assert len(plans) == 1
    assert plans[0].offset_minutes == 60
    assert plans[0].remind_at == starts_at - timedelta(minutes=60)


def test_plan_reminders_event_25_hours_out_gets_both() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    starts_at = now + timedelta(hours=25)
    plans = plan_reminders(starts_at, now=now)
    assert {p.offset_minutes for p in plans} == set(REMINDER_OFFSETS_MINUTES)


def test_plan_reminders_ordered_by_remind_at_earliest_first() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    starts_at = now + timedelta(days=3)
    plans = plan_reminders(starts_at, now=now)
    assert plans == sorted(plans, key=lambda p: p.remind_at)


def test_plan_reminders_exactly_at_offset_boundary_is_excluded() -> None:
    # remind_at must be strictly greater than now, not equal to it.
    now = datetime(2026, 1, 1, tzinfo=UTC)
    starts_at = now + timedelta(minutes=60)
    assert plan_reminders(starts_at, now=now) == []


def test_plan_reminders_rejects_naive_starts_at() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="starts_at"):
        plan_reminders(datetime(2026, 1, 4), now=now)


def test_plan_reminders_rejects_naive_now() -> None:
    with pytest.raises(ValueError, match="now"):
        plan_reminders(datetime(2026, 1, 4, tzinfo=UTC), now=datetime(2026, 1, 1))


def test_plan_reminders_fold_ambiguous_hour_stale_offset_excluded() -> None:
    # I2: America/Chicago falls back on 2026-11-01, so 01:00-01:59 happens
    # twice. `remind_at` (derived from `starts_at`) lands on the earlier
    # (CDT) occurrence of 01:30, while `now` is the later (CST) occurrence
    # of 01:00 -- absolutely *30 minutes after* remind_at, even though
    # "01:30" reads as later than "01:00" on the wall clock. A same-tzinfo
    # naive comparison would wrongly conclude the reminder is still ahead
    # of `now` and keep it; the correct UTC-normalized comparison excludes
    # it, since it's already 30 minutes in the past.
    starts_at = datetime(2026, 11, 1, 2, 30, tzinfo=CHICAGO)  # unambiguous
    now = datetime(2026, 11, 1, 1, 0, tzinfo=CHICAGO, fold=1)  # CST, later occurrence
    remind_at_60 = starts_at - timedelta(minutes=60)
    assert remind_at_60.astimezone(UTC) == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    assert now.astimezone(UTC) == datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
    assert remind_at_60.astimezone(UTC) < now.astimezone(UTC)  # already past, in absolute terms
    assert plan_reminders(starts_at, now=now) == []


# ---------------------------------------------------------------------------
# classify_reminder
# ---------------------------------------------------------------------------


def test_classify_reminder_not_due_when_in_the_future() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    remind_at = now + timedelta(minutes=1)
    assert classify_reminder(remind_at, now=now) == "not_due"


def test_classify_reminder_send_when_exactly_due() -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert classify_reminder(now, now=now) == "send"


def test_classify_reminder_send_within_grace() -> None:
    now = datetime(2026, 1, 1, 12, 10, tzinfo=UTC)
    remind_at = now - timedelta(minutes=10)
    assert classify_reminder(remind_at, now=now) == "send"


def test_classify_reminder_send_at_exact_grace_boundary() -> None:
    now = datetime(2026, 1, 1, 12, 15, tzinfo=UTC)
    remind_at = now - DEFAULT_GRACE
    assert classify_reminder(remind_at, now=now) == "send"


def test_classify_reminder_skip_stale_one_second_past_grace_boundary() -> None:
    now = datetime(2026, 1, 1, 12, 15, 1, tzinfo=UTC)
    remind_at = now - DEFAULT_GRACE - timedelta(seconds=1)
    assert classify_reminder(remind_at, now=now) == "skip_stale"


def test_classify_reminder_skip_stale_well_past_grace() -> None:
    now = datetime(2026, 1, 2, tzinfo=UTC)
    remind_at = now - timedelta(hours=5)
    assert classify_reminder(remind_at, now=now) == "skip_stale"


def test_classify_reminder_custom_grace_period() -> None:
    now = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)
    remind_at = now - timedelta(minutes=29)
    assert classify_reminder(remind_at, now=now, grace=timedelta(minutes=30)) == "send"
    assert classify_reminder(remind_at, now=now, grace=timedelta(minutes=20)) == "skip_stale"


def test_classify_reminder_fold_ambiguous_hour_is_correctly_stale() -> None:
    # I2: same construction as the `plan_reminders` fold test. `remind_at`
    # is actually 30 minutes before `now` in absolute terms (past the
    # default 15-minute grace), even though its wall-clock reads later.
    # A same-tzinfo naive comparison would wrongly return "not_due".
    remind_at = datetime(2026, 11, 1, 1, 30, tzinfo=CHICAGO, fold=0)  # CDT, earlier
    now = datetime(2026, 11, 1, 1, 0, tzinfo=CHICAGO, fold=1)  # CST, later occurrence
    assert now.astimezone(UTC) - remind_at.astimezone(UTC) == timedelta(minutes=30)
    assert classify_reminder(remind_at, now=now) == "skip_stale"


def test_classify_reminder_rejects_naive_remind_at() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="remind_at"):
        classify_reminder(datetime(2026, 1, 1), now=now)


def test_classify_reminder_rejects_naive_now() -> None:
    with pytest.raises(ValueError, match="now"):
        classify_reminder(datetime(2026, 1, 1, tzinfo=UTC), now=datetime(2026, 1, 1))


# ---------------------------------------------------------------------------
# is_event_completed
# ---------------------------------------------------------------------------


def test_is_event_completed_false_before_window() -> None:
    starts_at = datetime(2026, 1, 1, 18, 0, tzinfo=UTC)
    now = starts_at + EVENT_COMPLETE_AFTER - timedelta(seconds=1)
    assert is_event_completed(starts_at, now=now) is False


def test_is_event_completed_true_at_exact_boundary() -> None:
    starts_at = datetime(2026, 1, 1, 18, 0, tzinfo=UTC)
    now = starts_at + EVENT_COMPLETE_AFTER
    assert is_event_completed(starts_at, now=now) is True


def test_is_event_completed_true_after_boundary() -> None:
    starts_at = datetime(2026, 1, 1, 18, 0, tzinfo=UTC)
    now = starts_at + EVENT_COMPLETE_AFTER + timedelta(seconds=1)
    assert is_event_completed(starts_at, now=now) is True


def test_is_event_completed_false_immediately_after_start() -> None:
    starts_at = datetime(2026, 1, 1, 18, 0, tzinfo=UTC)
    now = starts_at + timedelta(minutes=1)
    assert is_event_completed(starts_at, now=now) is False


def test_is_event_completed_fold_ambiguous_hour_is_correctly_completed() -> None:
    # I2: `starts_at + EVENT_COMPLETE_AFTER` naively lands on the earlier
    # (CDT) occurrence of the ambiguous 01:30, while `now` is the later
    # (CST) occurrence of 01:00 -- absolutely *after* that instant, even
    # though "01:00" reads earlier than "01:30" on the wall clock. A
    # same-tzinfo naive comparison would wrongly say the event isn't
    # completed yet.
    starts_at = datetime(2026, 10, 31, 19, 30, tzinfo=CHICAGO)  # unambiguous
    now = datetime(2026, 11, 1, 1, 0, tzinfo=CHICAGO, fold=1)  # CST, later occurrence
    assert (starts_at + EVENT_COMPLETE_AFTER).astimezone(UTC) == datetime(
        2026, 11, 1, 6, 30, tzinfo=UTC
    )
    assert now.astimezone(UTC) == datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
    assert is_event_completed(starts_at, now=now) is True


def test_is_event_completed_rejects_naive_starts_at() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="starts_at"):
        is_event_completed(datetime(2026, 1, 1), now=now)


def test_is_event_completed_rejects_naive_now() -> None:
    with pytest.raises(ValueError, match="now"):
        is_event_completed(datetime(2026, 1, 1, tzinfo=UTC), now=datetime(2026, 1, 1))


# ---------------------------------------------------------------------------
# ReminderPlan
# ---------------------------------------------------------------------------


def test_reminder_plan_is_frozen() -> None:
    plan = ReminderPlan(offset_minutes=60, remind_at=datetime(2026, 1, 1, tzinfo=UTC))
    with pytest.raises(AttributeError):
        plan.offset_minutes = 1440  # type: ignore[misc]
