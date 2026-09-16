"""Behavior tests for `catan_bot.db.repositories.events`."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from catan_bot.db.repositories import events, guilds

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


async def _reminder_rows(conn: asyncpg.Connection, event_id: int) -> list[asyncpg.Record]:
    return await conn.fetch(
        "SELECT offset_minutes, remind_at, sent_at FROM event_reminders WHERE event_id = $1",
        event_id,
    )


async def test_create_event_and_get_event_with_reminders(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    starts_at = NOW + timedelta(hours=2)
    reminders = [
        (1440, starts_at - timedelta(minutes=1440)),
        (60, starts_at - timedelta(minutes=60)),
    ]

    event = await events.create_event(
        app_conn, guild_id, "Game night", "bring snacks", "my place", starts_at, 1, 500, reminders
    )

    assert event.title == "Game night"
    assert event.status == "scheduled"

    fetched = await events.get_event(app_conn, guild_id, event.event_id)
    assert fetched is not None
    assert fetched.title == "Game night"

    stored_reminders = await _reminder_rows(app_conn, event.event_id)
    assert len(stored_reminders) == 2
    assert {row["offset_minutes"] for row in stored_reminders} == {1440, 60}


async def test_create_event_with_generator_reminders_stores_all_reminders(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """N1 regression: `reminders` must be materialized exactly once, so a
    generator (previously consumed by validation, then exhausted by the
    time the SQL call built its `unnest()` arrays) doesn't silently store
    zero reminders."""
    starts_at = NOW + timedelta(hours=2)
    pairs = [
        (1440, starts_at - timedelta(minutes=1440)),
        (60, starts_at - timedelta(minutes=60)),
    ]
    reminders = ((offset, remind_at) for offset, remind_at in pairs)

    event = await events.create_event(
        app_conn, guild_id, "Game night", None, None, starts_at, 1, None, reminders
    )

    stored_reminders = await _reminder_rows(app_conn, event.event_id)
    assert len(stored_reminders) == 2
    assert {row["offset_minutes"] for row in stored_reminders} == {1440, 60}


async def test_create_event_with_no_reminders(app_conn: asyncpg.Connection, guild_id: int) -> None:
    starts_at = NOW + timedelta(minutes=30)
    event = await events.create_event(
        app_conn, guild_id, "Quick game", None, None, starts_at, 1, None, []
    )

    stored_reminders = await _reminder_rows(app_conn, event.event_id)
    assert stored_reminders == []


async def test_set_event_message_updates_channel_and_message_id(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    event = await events.create_event(
        app_conn, guild_id, "Title", None, None, NOW + timedelta(hours=1), 1, None, []
    )

    updated = await events.set_event_message(app_conn, guild_id, event.event_id, 10, 20)

    assert updated is not None
    assert updated.channel_id == 10
    assert updated.message_id == 20


async def test_get_event_returns_none_for_unknown_event(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    assert await events.get_event(app_conn, guild_id, 999_999) is None


async def test_list_upcoming_events_filters_status_and_time(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    future = await events.create_event(
        app_conn, guild_id, "Future", None, None, NOW + timedelta(days=1), 1, None, []
    )
    past_ref = NOW - timedelta(days=1)
    old_but_scheduled = await events.create_event(
        app_conn, guild_id, "Old", None, None, past_ref + timedelta(hours=1), 1, None, []
    )
    cancelled = await events.create_event(
        app_conn, guild_id, "Cancelled", None, None, NOW + timedelta(days=2), 1, None, []
    )
    await events.cancel_event(app_conn, guild_id, cancelled.event_id, 1, False)

    upcoming = await events.list_upcoming_events(app_conn, guild_id, NOW, 10)
    upcoming_ids = {e.event_id for e in upcoming}

    assert future.event_id in upcoming_ids
    assert old_but_scheduled.event_id not in upcoming_ids  # starts_at < now
    assert cancelled.event_id not in upcoming_ids  # not scheduled


# ---------------------------------------------------------------------------
# cancel_event
# ---------------------------------------------------------------------------


async def test_cancel_event_by_creator_succeeds(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    event = await events.create_event(
        app_conn, guild_id, "Title", None, None, NOW + timedelta(hours=1), 1, None, []
    )

    result = await events.cancel_event(app_conn, guild_id, event.event_id, 1, False)

    assert result == "cancelled"
    fetched = await events.get_event(app_conn, guild_id, event.event_id)
    assert fetched is not None
    assert fetched.status == "cancelled"


async def test_cancel_event_by_admin_non_creator_succeeds(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    event = await events.create_event(
        app_conn, guild_id, "Title", None, None, NOW + timedelta(hours=1), 1, None, []
    )

    result = await events.cancel_event(app_conn, guild_id, event.event_id, 2, True)

    assert result == "cancelled"


async def test_cancel_event_by_non_creator_non_admin_is_refused(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    event = await events.create_event(
        app_conn, guild_id, "Title", None, None, NOW + timedelta(hours=1), 1, None, []
    )

    result = await events.cancel_event(app_conn, guild_id, event.event_id, 2, False)

    assert result == "not_creator_or_admin"
    fetched = await events.get_event(app_conn, guild_id, event.event_id)
    assert fetched is not None
    assert fetched.status == "scheduled"


async def test_cancel_already_cancelled_event_is_refused(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    event = await events.create_event(
        app_conn, guild_id, "Title", None, None, NOW + timedelta(hours=1), 1, None, []
    )
    await events.cancel_event(app_conn, guild_id, event.event_id, 1, False)

    result = await events.cancel_event(app_conn, guild_id, event.event_id, 1, False)

    assert result == "not_scheduled"


async def test_cancel_event_not_found(app_conn: asyncpg.Connection, guild_id: int) -> None:
    assert await events.cancel_event(app_conn, guild_id, 999_999, 1, True) == "not_found"


# ---------------------------------------------------------------------------
# upsert_rsvp / rsvp_counts / rsvp_user_ids
# ---------------------------------------------------------------------------


async def test_upsert_rsvp_inserts_then_updates(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    event = await events.create_event(
        app_conn, guild_id, "Title", None, None, NOW + timedelta(hours=1), 1, None, []
    )

    first = await events.upsert_rsvp(app_conn, guild_id, event.event_id, 2, "maybe")
    assert first is True

    second = await events.upsert_rsvp(app_conn, guild_id, event.event_id, 2, "going")
    assert second is True

    going_ids = await events.rsvp_user_ids(app_conn, guild_id, event.event_id, ["going"])
    assert going_ids == [2]


async def test_upsert_rsvp_refused_for_cancelled_event(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    event = await events.create_event(
        app_conn, guild_id, "Title", None, None, NOW + timedelta(hours=1), 1, None, []
    )
    await events.cancel_event(app_conn, guild_id, event.event_id, 1, False)

    result = await events.upsert_rsvp(app_conn, guild_id, event.event_id, 2, "going")

    assert result is False
    counts = await events.rsvp_counts(app_conn, guild_id, event.event_id)
    assert counts.going == 0


async def test_upsert_rsvp_refused_for_wrong_guild(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    event = await events.create_event(
        app_conn, guild_id, "Title", None, None, NOW + timedelta(hours=1), 1, None, []
    )

    result = await events.upsert_rsvp(app_conn, other_guild_id, event.event_id, 2, "going")

    assert result is False
    counts = await events.rsvp_counts(app_conn, guild_id, event.event_id)
    assert counts.going == 0


async def test_rsvp_counts_tallies_each_response(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    event = await events.create_event(
        app_conn, guild_id, "Title", None, None, NOW + timedelta(hours=1), 1, None, []
    )
    await events.upsert_rsvp(app_conn, guild_id, event.event_id, 1, "going")
    await events.upsert_rsvp(app_conn, guild_id, event.event_id, 2, "going")
    await events.upsert_rsvp(app_conn, guild_id, event.event_id, 3, "maybe")
    await events.upsert_rsvp(app_conn, guild_id, event.event_id, 4, "not_going")

    counts = await events.rsvp_counts(app_conn, guild_id, event.event_id)

    assert counts.going == 2
    assert counts.maybe == 1
    assert counts.not_going == 1


async def test_rsvp_user_ids_filters_by_multiple_responses(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    event = await events.create_event(
        app_conn, guild_id, "Title", None, None, NOW + timedelta(hours=1), 1, None, []
    )
    await events.upsert_rsvp(app_conn, guild_id, event.event_id, 1, "going")
    await events.upsert_rsvp(app_conn, guild_id, event.event_id, 2, "maybe")
    await events.upsert_rsvp(app_conn, guild_id, event.event_id, 3, "not_going")

    ids = await events.rsvp_user_ids(app_conn, guild_id, event.event_id, ["going", "maybe"])

    assert set(ids) == {1, 2}


async def test_rsvp_roster_groups_ids_in_stable_order_and_exposes_counts(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    event = await events.create_event(
        app_conn, guild_id, "Title", None, None, NOW + timedelta(hours=1), 1, None, []
    )
    await events.upsert_rsvp(app_conn, guild_id, event.event_id, 9, "maybe")
    await events.upsert_rsvp(app_conn, guild_id, event.event_id, 4, "going")
    await events.upsert_rsvp(app_conn, guild_id, event.event_id, 7, "not_going")
    await events.upsert_rsvp(app_conn, guild_id, event.event_id, 2, "going")

    roster = await events.rsvp_roster(app_conn, guild_id, event.event_id)

    assert roster.going == (2, 4)
    assert roster.maybe == (9,)
    assert roster.not_going == (7,)
    assert (roster.going_count, roster.maybe_count, roster.not_going_count) == (2, 1, 1)
    assert roster.counts.going == 2
    assert roster.counts.maybe == 1
    assert roster.counts.not_going == 1


# ---------------------------------------------------------------------------
# Scheduler queries.
# ---------------------------------------------------------------------------


async def test_claim_due_reminders_claims_each_reminder_once_and_skips_cancelled(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    starts_at = NOW + timedelta(hours=2)
    due_event = await events.create_event(
        app_conn,
        guild_id,
        "Due event",
        None,
        None,
        starts_at,
        1,
        555,
        [(60, NOW - timedelta(minutes=1))],
    )
    cancelled_event = await events.create_event(
        app_conn,
        guild_id,
        "Cancelled event",
        None,
        None,
        starts_at,
        1,
        555,
        [(60, NOW - timedelta(minutes=1))],
    )
    await events.cancel_event(app_conn, guild_id, cancelled_event.event_id, 1, False)
    not_due_event = await events.create_event(
        app_conn,
        guild_id,
        "Not due yet",
        None,
        None,
        starts_at,
        1,
        555,
        [(60, NOW + timedelta(minutes=30))],
    )

    claimed = await events.claim_due_reminders(app_conn, NOW)

    claimed_event_ids = {c.event_id for c in claimed}
    assert due_event.event_id in claimed_event_ids
    assert cancelled_event.event_id not in claimed_event_ids
    assert not_due_event.event_id not in claimed_event_ids

    # Claiming again at the same "now" must not re-claim it (sent_at is set).
    claimed_again = await events.claim_due_reminders(app_conn, NOW)
    assert due_event.event_id not in {c.event_id for c in claimed_again}


async def test_claim_due_reminders_propagates_configured_player_role(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await guilds.set_player_role(app_conn, guild_id, 765_432)
    event = await events.create_event(
        app_conn,
        guild_id,
        "Due event",
        None,
        None,
        NOW + timedelta(hours=2),
        1,
        555,
        [(60, NOW - timedelta(minutes=1))],
    )

    claimed = await events.claim_due_reminders(app_conn, NOW)

    reminder = next(item for item in claimed if item.event_id == event.event_id)
    assert reminder.player_role_id == 765_432


async def test_complete_past_events_marks_only_stale_scheduled_events(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    stale = await events.create_event(
        app_conn, guild_id, "Stale", None, None, NOW - timedelta(hours=7), 1, None, []
    )
    fresh = await events.create_event(
        app_conn, guild_id, "Fresh", None, None, NOW + timedelta(hours=1), 1, None, []
    )

    count = await events.complete_past_events(app_conn, NOW - timedelta(hours=6))

    assert count == 1
    stale_after = await events.get_event(app_conn, guild_id, stale.event_id)
    fresh_after = await events.get_event(app_conn, guild_id, fresh.event_id)
    assert stale_after is not None
    assert stale_after.status == "completed"
    assert fresh_after is not None
    assert fresh_after.status == "scheduled"


async def test_complete_past_events_does_not_reopen_a_cancelled_past_event(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """M3: `_COMPLETE_PAST_EVENTS_SQL` guards on `status = 'scheduled'` --
    a cancelled past event must stay cancelled, while a past *scheduled*
    event swept in the same call becomes completed."""
    past_starts_at = NOW - timedelta(hours=2)
    cancelled = await events.create_event(
        app_conn, guild_id, "Cancelled", None, None, past_starts_at, 1, None, []
    )
    await events.cancel_event(app_conn, guild_id, cancelled.event_id, 1, False)
    scheduled = await events.create_event(
        app_conn, guild_id, "Scheduled", None, None, past_starts_at, 1, None, []
    )

    count = await events.complete_past_events(app_conn, NOW)

    assert count == 1
    cancelled_after = await events.get_event(app_conn, guild_id, cancelled.event_id)
    scheduled_after = await events.get_event(app_conn, guild_id, scheduled.event_id)
    assert cancelled_after is not None
    assert cancelled_after.status == "cancelled"
    assert scheduled_after is not None
    assert scheduled_after.status == "completed"
