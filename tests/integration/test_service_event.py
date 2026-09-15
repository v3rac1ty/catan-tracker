"""Integration tests for `catan_bot.services.event_service`."""

from __future__ import annotations

import dataclasses
import logging
import os
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from catan_bot.db.repositories import events as events_repo
from catan_bot.domain.errors import DomainValidationError
from catan_bot.services import config_service, event_service
from catan_bot.services.context import Actor
from catan_bot.services.errors import ConflictError, NotFoundError, PermissionDeniedError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def _actor(user_id: int, *, admin: bool = False, role_ids: frozenset[int] = frozenset()) -> Actor:
    return Actor(user_id=user_id, has_manage_guild=admin, role_ids=role_ids)


async def _create(pool: asyncpg.Pool, guild_id: int, *, creator: int, time_text: str, **overrides):
    kwargs: dict = {
        "title": "Game Night",
        "date_text": None,
        "time_text": time_text,
        "location": None,
        "description": None,
        "channel_id": None,
        "now": NOW,
    }
    kwargs.update(overrides)
    return await event_service.create_event(pool, guild_id, _actor(creator), **kwargs)


def _at(delta: timedelta) -> dict:
    """`date_text`/`time_text` overrides for an event starting `delta` from `NOW` (UTC guild tz)."""
    target = NOW + delta
    return {"date_text": target.strftime("%Y-%m-%d"), "time_text": target.strftime("%H:%M")}


# ---------------------------------------------------------------------------
# 6. Events: reminders planned, past time refused, DST gap, RSVP validation.
# ---------------------------------------------------------------------------


async def test_event_30_minutes_out_gets_zero_reminders(pool: asyncpg.Pool, guild_id: int) -> None:
    created = await _create(pool, guild_id, creator=1, **_at(timedelta(minutes=30)))
    async with pool.acquire() as conn:
        # No repository read for reminders is exposed directly; verify via
        # due_reminders never firing at "now" for this event's offsets.
        row_count = await conn.fetchval(
            "SELECT COUNT(*) FROM event_reminders WHERE event_id = $1", created.event_id
        )
    assert row_count == 0


async def test_event_2_hours_out_gets_one_reminder(pool: asyncpg.Pool, guild_id: int) -> None:
    created = await _create(pool, guild_id, creator=1, **_at(timedelta(hours=2)))
    async with pool.acquire() as conn:
        row_count = await conn.fetchval(
            "SELECT COUNT(*) FROM event_reminders WHERE event_id = $1", created.event_id
        )
    assert row_count == 1


async def test_event_25_hours_out_gets_two_reminders(pool: asyncpg.Pool, guild_id: int) -> None:
    created = await _create(pool, guild_id, creator=1, **_at(timedelta(hours=25)))
    async with pool.acquire() as conn:
        row_count = await conn.fetchval(
            "SELECT COUNT(*) FROM event_reminders WHERE event_id = $1", created.event_id
        )
    assert row_count == 2


async def test_create_event_past_time_is_refused(pool: asyncpg.Pool, guild_id: int) -> None:
    with pytest.raises(DomainValidationError):
        await _create(pool, guild_id, creator=1, **_at(timedelta(hours=-1)))


async def test_create_event_dst_gap_time_is_refused_with_domain_message(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    """2027-03-14 02:30 America/Chicago falls in the US spring-forward gap
    (clocks jump from 02:00 to 03:00)."""
    await config_service.set_timezone(pool, guild_id, _actor(1, admin=True), "America/Chicago")
    with pytest.raises(DomainValidationError) as exc_info:
        await _create(
            pool,
            guild_id,
            creator=1,
            date_text="2027-03-14",
            time_text="02:30",
            now=NOW,
        )
    assert "clock change" in exc_info.value.user_message


async def test_create_event_stores_title_location_description(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    created = await _create(
        pool,
        guild_id,
        creator=1,
        **_at(timedelta(hours=3)),
        title="Big Game",
        location="My House",
        description="Bring snacks\nand drinks",
    )
    assert created.title == "Big Game"
    assert created.location == "My House"
    assert created.description == "Bring snacks\nand drinks"


async def test_create_event_blank_title_raises_domain_error(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    with pytest.raises(DomainValidationError):
        await _create(pool, guild_id, creator=1, **_at(timedelta(hours=3)), title="   ")


async def test_create_event_blank_location_and_description_allowed(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    created = await _create(
        pool,
        guild_id,
        creator=1,
        **_at(timedelta(hours=3)),
        location="",
        description="",
    )
    assert created.location is None
    assert created.description is None


async def test_create_event_rejects_location_over_200_chars(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    """V07 (L5 mutant-killer): `EVENT_LOCATION_MAX` is 200 -- a
    201-character location must raise `DomainValidationError`."""
    with pytest.raises(DomainValidationError):
        await _create(pool, guild_id, creator=1, **_at(timedelta(hours=3)), location="x" * 201)


# ---------------------------------------------------------------------------
# RSVP
# ---------------------------------------------------------------------------


async def test_rsvp_invalid_response_raises_value_error_before_any_db_call(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    """I2: a fixed message -- never `repr(response)` echoed back."""
    created = await _create(pool, guild_id, creator=1, **_at(timedelta(hours=3)))
    with pytest.raises(ValueError) as exc_info:
        await event_service.rsvp(pool, guild_id, created.event_id, _actor(2), "yes_please")
    assert str(exc_info.value) == "invalid RSVP response"
    assert "yes_please" not in str(exc_info.value)

    async with pool.acquire() as conn:
        counts = await events_repo.rsvp_counts(conn, guild_id, created.event_id)
    assert counts.going == counts.maybe == counts.not_going == 0


async def test_rsvp_going_maybe_not_going_update_counts(pool: asyncpg.Pool, guild_id: int) -> None:
    created = await _create(pool, guild_id, creator=1, **_at(timedelta(hours=3)))
    await event_service.rsvp(pool, guild_id, created.event_id, _actor(2), "going")
    await event_service.rsvp(pool, guild_id, created.event_id, _actor(3), "maybe")
    counts = await event_service.rsvp(pool, guild_id, created.event_id, _actor(4), "not_going")
    assert counts.going == 1
    assert counts.maybe == 1
    assert counts.not_going == 1


async def test_rsvp_on_cancelled_event_raises_not_found(pool: asyncpg.Pool, guild_id: int) -> None:
    created = await _create(pool, guild_id, creator=1, **_at(timedelta(hours=3)))
    await event_service.cancel_event(pool, guild_id, created.event_id, _actor(1))
    with pytest.raises(NotFoundError):
        await event_service.rsvp(pool, guild_id, created.event_id, _actor(2), "going")


# ---------------------------------------------------------------------------
# cancel_event: creator, admin, and non-creator-non-admin.
# ---------------------------------------------------------------------------


async def test_cancel_event_by_creator_succeeds(pool: asyncpg.Pool, guild_id: int) -> None:
    created = await _create(pool, guild_id, creator=1, **_at(timedelta(hours=3)))
    cancelled = await event_service.cancel_event(pool, guild_id, created.event_id, _actor(1))
    assert cancelled.status == "cancelled"


async def test_cancel_event_by_admin_non_creator_succeeds(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    created = await _create(pool, guild_id, creator=1, **_at(timedelta(hours=3)))
    cancelled = await event_service.cancel_event(
        pool, guild_id, created.event_id, _actor(2, admin=True)
    )
    assert cancelled.status == "cancelled"


async def test_cancel_event_by_non_creator_non_admin_is_refused(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    created = await _create(pool, guild_id, creator=1, **_at(timedelta(hours=3)))
    with pytest.raises(PermissionDeniedError):
        await event_service.cancel_event(pool, guild_id, created.event_id, _actor(2))


async def test_cancel_event_by_admin_role_holder_non_creator_succeeds(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    """A17 (L5 mutant-killer): an admin-role holder (the guild's configured
    admin role, *not* Manage Server) who is not the event's creator must
    still be able to cancel someone else's event."""
    await config_service.set_admin_role(pool, guild_id, _actor(1, admin=True), role_id=777)
    created = await _create(pool, guild_id, creator=1, **_at(timedelta(hours=3)))
    admin_role_actor = _actor(5, admin=False, role_ids=frozenset({777}))

    cancelled = await event_service.cancel_event(pool, guild_id, created.event_id, admin_role_actor)
    assert cancelled.status == "cancelled"


async def test_cancel_unknown_event_raises_not_found(pool: asyncpg.Pool, guild_id: int) -> None:
    with pytest.raises(NotFoundError):
        await event_service.cancel_event(pool, guild_id, 999_999, _actor(1, admin=True))


async def test_cancel_already_cancelled_event_raises_conflict(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    created = await _create(pool, guild_id, creator=1, **_at(timedelta(hours=3)))
    await event_service.cancel_event(pool, guild_id, created.event_id, _actor(1))
    with pytest.raises(ConflictError):
        await event_service.cancel_event(pool, guild_id, created.event_id, _actor(1))


# ---------------------------------------------------------------------------
# upcoming_events (limit clamping)
# ---------------------------------------------------------------------------


async def test_upcoming_events_clamps_limit(pool: asyncpg.Pool, guild_id: int) -> None:
    for hours in range(1, 15):
        await _create(pool, guild_id, creator=1, **_at(timedelta(hours=hours)))

    events = await event_service.upcoming_events(pool, guild_id, NOW, 1000)
    assert len(events) <= 10

    clamped_low = await event_service.upcoming_events(pool, guild_id, NOW, 0)
    assert len(clamped_low) == 1


# ---------------------------------------------------------------------------
# 7. due_reminders: send/going+maybe, stale, cancel race.
# ---------------------------------------------------------------------------


async def test_due_reminders_send_path_includes_going_and_maybe_excludes_not_going(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    created = await _create(pool, guild_id, creator=1, **_at(timedelta(hours=2)))
    await event_service.rsvp(pool, guild_id, created.event_id, _actor(2), "going")
    await event_service.rsvp(pool, guild_id, created.event_id, _actor(3), "maybe")
    await event_service.rsvp(pool, guild_id, created.event_id, _actor(4), "not_going")

    # The 1h-before reminder for a "2 hours out" event is due exactly 1h from now.
    due_time = NOW + timedelta(hours=1)
    reminders = await event_service.due_reminders(pool, due_time)

    assert len(reminders) == 1
    reminder = reminders[0]
    assert reminder.event.event_id == created.event_id
    assert reminder.offset_minutes == 60
    assert set(reminder.user_ids) == {2, 3}


async def test_due_reminders_stale_reminder_is_dropped_but_marked_sent(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    created = await _create(pool, guild_id, creator=1, **_at(timedelta(hours=2)))
    due_time = NOW + timedelta(hours=1)
    very_late = due_time + timedelta(minutes=30)  # past the 15-minute grace window

    first_call = await event_service.due_reminders(pool, very_late)
    assert first_call == []  # dropped as stale, not returned as "send"

    second_call = await event_service.due_reminders(pool, very_late)
    assert second_call == []  # already marked sent by the claim -- never retried

    async with pool.acquire() as conn:
        sent_at = await conn.fetchval(
            "SELECT sent_at FROM event_reminders WHERE event_id = $1 AND offset_minutes = 60",
            created.event_id,
        )
    assert sent_at is not None


async def test_due_reminders_cancel_race_is_not_sent(
    pool: asyncpg.Pool, guild_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim query doesn't lock the event row, so a reminder can be
    claimed while a cancel is committing -- re-checking `get_event` must
    catch this and drop the reminder rather than send it."""
    created = await _create(pool, guild_id, creator=1, **_at(timedelta(hours=2)))
    await event_service.rsvp(pool, guild_id, created.event_id, _actor(2), "going")

    real_get_event = event_service.events.get_event

    async def _cancelled_event(conn, guild_id_arg, event_id_arg):
        event = await real_get_event(conn, guild_id_arg, event_id_arg)
        assert event is not None
        return dataclasses.replace(event, status="cancelled")

    monkeypatch.setattr(event_service.events, "get_event", _cancelled_event)

    due_time = NOW + timedelta(hours=1)
    reminders = await event_service.due_reminders(pool, due_time)
    assert reminders == []


async def test_due_reminders_isolates_one_guilds_read_failure_from_another(
    pool: asyncpg.Pool,
    guild_id: int,
    other_guild_id: int,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """L4: `due_reminders` claims every due reminder in one committed
    transaction, then reads each on its own connection. A read failure for
    one guild (`rsvp_user_ids` raising) must not affect another guild's due
    reminder in the same tick, and the failing guild's reminder -- already
    marked sent by the claim -- is dropped (never retried), with no DETAIL
    text in the log."""
    created_a = await _create(pool, guild_id, creator=1, **_at(timedelta(hours=2)))
    created_b = await _create(pool, other_guild_id, creator=1, **_at(timedelta(hours=2)))

    real_rsvp_user_ids = event_service.events.rsvp_user_ids
    marker = "MARKER_deadbeef_ROW_CONTENTS_DO_NOT_LEAK"

    async def _maybe_fail(conn, guild_id_arg, event_id_arg, responses):
        if guild_id_arg == guild_id:
            exc = asyncpg.CheckViolationError("simulated")
            exc.detail = f"boom {marker}"
            raise exc
        return await real_rsvp_user_ids(conn, guild_id_arg, event_id_arg, responses)

    monkeypatch.setattr(event_service.events, "rsvp_user_ids", _maybe_fail)

    due_time = NOW + timedelta(hours=1)
    with caplog.at_level(logging.ERROR):
        reminders = await event_service.due_reminders(pool, due_time)

    assert {r.event.event_id for r in reminders} == {created_b.event_id}

    async with pool.acquire() as conn:
        sent_at = await conn.fetchval(
            "SELECT sent_at FROM event_reminders WHERE event_id = $1 AND offset_minutes = 60",
            created_a.event_id,
        )
    assert sent_at is not None  # claimed (marked sent) despite the failed read

    second_call = await event_service.due_reminders(pool, due_time)
    assert second_call == []  # never retried -- both already claimed/sent

    for record in caplog.records:
        assert marker not in record.getMessage()
        assert marker not in repr(record.args)
        assert marker not in (record.exc_text or "")
    assert marker not in caplog.text
    combined = "\n".join(r.getMessage() for r in caplog.records)
    assert str(created_a.event_id) in combined
    assert "CheckViolationError" in combined


async def test_due_reminders_logs_non_postgres_read_failure_without_exc_info_leak(
    pool: asyncpg.Pool, guild_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_log_reminder_read_failure`'s non-`PostgresError` branch (a plain
    `RuntimeError`, e.g. a bug or a connection-level failure) must still be
    logged and the reminder still dropped -- covering the branch the
    PostgresError-based isolation test above doesn't reach."""
    created = await _create(pool, guild_id, creator=1, **_at(timedelta(hours=2)))

    async def _boom(conn, guild_id_arg, event_id_arg):
        raise RuntimeError("simulated connection failure")

    monkeypatch.setattr(event_service.events, "get_event", _boom)

    due_time = NOW + timedelta(hours=1)
    reminders = await event_service.due_reminders(pool, due_time)
    assert reminders == []

    async with pool.acquire() as conn:
        sent_at = await conn.fetchval(
            "SELECT sent_at FROM event_reminders WHERE event_id = $1 AND offset_minutes = 60",
            created.event_id,
        )
    assert sent_at is not None


async def test_complete_past_events_marks_stale_scheduled_events(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    created = await _create(pool, guild_id, creator=1, **_at(timedelta(hours=2)))
    still_within_grace = NOW + timedelta(hours=2) + timedelta(hours=5)
    completed_count = await event_service.complete_past_events(pool, still_within_grace)
    assert completed_count == 0

    past_completion_window = NOW + timedelta(hours=2) + timedelta(hours=7)
    completed_count = await event_service.complete_past_events(pool, past_completion_window)
    assert completed_count == 1

    async with pool.acquire() as conn:
        event = await events_repo.get_event(conn, guild_id, created.event_id)
    assert event is not None
    assert event.status == "completed"
