"""`events` / `event_rsvps` / `event_reminders` repository.

Every SQL statement here is a module-level string constant, bound exactly
once, with values passed only as `$n` arguments (see CLAUDE.md and
`tests/static/sql_guard.py`).

`claim_due_reminders` and `complete_past_events` are the two documented
exceptions to "every query is guild-scoped": they are system-wide scheduler
queries that must see every guild's due reminders/events in one pass.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import asyncpg

from catan_bot.db.models import ClaimedReminder, Event, RsvpCounts, TransitionResult
from catan_bot.db.repositories._params import (
    require_aware,
    require_id,
    require_limit,
    require_optional_id,
    require_str_sequence,
)

_INSERT_EVENT_SQL = """
INSERT INTO events (guild_id, title, description, location, starts_at, created_by, channel_id)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING event_id, guild_id, title, description, location, starts_at, status,
          created_by, channel_id, message_id, created_at
"""

_INSERT_EVENT_REMINDERS_SQL = """
INSERT INTO event_reminders (event_id, offset_minutes, remind_at)
SELECT $1, o, r
FROM unnest($2::int[], $3::timestamptz[]) AS t(o, r)
"""

_UPDATE_EVENT_MESSAGE_SQL = """
UPDATE events
SET channel_id = $3, message_id = $4
WHERE guild_id = $1 AND event_id = $2
RETURNING event_id, guild_id, title, description, location, starts_at, status,
          created_by, channel_id, message_id, created_at
"""

_SELECT_EVENT_SQL = """
SELECT event_id, guild_id, title, description, location, starts_at, status,
       created_by, channel_id, message_id, created_at
FROM events
WHERE guild_id = $1 AND event_id = $2
"""

_LIST_UPCOMING_EVENTS_SQL = """
SELECT event_id, guild_id, title, description, location, starts_at, status,
       created_by, channel_id, message_id, created_at
FROM events
WHERE guild_id = $1 AND status = 'scheduled' AND starts_at >= $2
ORDER BY starts_at
LIMIT $3
"""

_CANCEL_EVENT_SQL = """
UPDATE events
SET status = 'cancelled'
WHERE event_id = $1 AND guild_id = $2 AND status = 'scheduled'
      AND (created_by = $3 OR $4)
RETURNING event_id
"""

_SELECT_EVENT_FOR_CANCEL_CLASSIFY_SQL = """
SELECT status, created_by
FROM events
WHERE guild_id = $1 AND event_id = $2
"""

_UPSERT_RSVP_SQL = """
INSERT INTO event_rsvps (event_id, user_id, response)
SELECT $2, $3, $4
WHERE EXISTS (
    SELECT 1 FROM events WHERE event_id = $2 AND guild_id = $1 AND status = 'scheduled'
)
ON CONFLICT (event_id, user_id) DO UPDATE SET response = EXCLUDED.response, updated_at = now()
RETURNING event_id
"""

_SELECT_RSVP_COUNTS_SQL = """
SELECT
    COUNT(*) FILTER (WHERE r.response = 'going') AS going,
    COUNT(*) FILTER (WHERE r.response = 'maybe') AS maybe,
    COUNT(*) FILTER (WHERE r.response = 'not_going') AS not_going
FROM event_rsvps r
JOIN events e ON e.event_id = r.event_id
WHERE r.event_id = $1 AND e.guild_id = $2
"""

_SELECT_RSVP_USER_IDS_SQL = """
SELECT r.user_id
FROM event_rsvps r
JOIN events e ON e.event_id = r.event_id
WHERE r.event_id = $1 AND e.guild_id = $2 AND r.response = ANY($3::text[])
"""

# System-wide (no guild_id): the scheduler must claim every guild's due
# reminders in one pass. The UPDATE ... RETURNING makes each reminder
# claimed at-most-once, even across two concurrent scheduler ticks.
_CLAIM_DUE_REMINDERS_SQL = """
UPDATE event_reminders r
SET sent_at = $1
FROM events e
WHERE r.event_id = e.event_id
      AND e.status = 'scheduled'
      AND r.sent_at IS NULL
      AND r.remind_at <= $1
RETURNING e.guild_id AS guild_id, e.channel_id AS channel_id, e.title AS title,
          e.starts_at AS starts_at, r.offset_minutes AS offset_minutes,
          r.remind_at AS remind_at, r.event_id AS event_id
"""

# System-wide (no guild_id): every guild's stale scheduled events are swept
# in one pass.
_COMPLETE_PAST_EVENTS_SQL = """
UPDATE events
SET status = 'completed'
WHERE status = 'scheduled' AND starts_at <= $1
RETURNING event_id
"""


def _row_to_event(row: asyncpg.Record) -> Event:
    return Event(
        event_id=row["event_id"],
        guild_id=row["guild_id"],
        title=row["title"],
        description=row["description"],
        location=row["location"],
        starts_at=row["starts_at"],
        status=row["status"],
        created_by=row["created_by"],
        channel_id=row["channel_id"],
        message_id=row["message_id"],
        created_at=row["created_at"],
    )


async def create_event(
    conn: asyncpg.Connection,
    guild_id: int,
    title: str,
    description: str | None,
    location: str | None,
    starts_at: datetime,
    created_by: int,
    channel_id: int | None,
    reminders: Sequence[tuple[int, datetime]],
) -> Event:
    """Insert a scheduled event and its reminders in one transaction.

    `conn.transaction()` nests as a savepoint if the caller already holds
    one. `reminders` is `(offset_minutes, remind_at)` pairs; an empty
    sequence is fine (no reminders left to schedule, e.g. a same-day event
    created less than an hour out).
    """
    require_id(guild_id, name="guild_id")
    require_id(created_by, name="created_by")
    require_optional_id(channel_id, name="channel_id")
    starts_at = require_aware(starts_at, name="starts_at")
    # The comprehension both materializes `reminders` exactly once (it may be
    # a one-shot iterable, e.g. a generator -- N1 audit finding) and replaces
    # each `remind_at` with `require_aware`'s normalized-to-UTC return value
    # (N2 audit finding), so the SQL call below sees the same validated,
    # normalized instants that were checked here.
    reminders = [
        (offset_minutes, require_aware(remind_at, name=f"reminders[{i}].remind_at"))
        for i, (offset_minutes, remind_at) in enumerate(reminders)
    ]
    async with conn.transaction():
        event_row = await conn.fetchrow(
            _INSERT_EVENT_SQL,
            guild_id,
            title,
            description,
            location,
            starts_at,
            created_by,
            channel_id,
        )
        if event_row is None:  # pragma: no cover -- INSERT ... RETURNING always returns a row.
            raise RuntimeError("INSERT INTO events did not return a row")
        event = _row_to_event(event_row)

        if reminders:
            offsets = [offset for offset, _ in reminders]
            remind_ats = [remind_at for _, remind_at in reminders]
            await conn.execute(_INSERT_EVENT_REMINDERS_SQL, event.event_id, offsets, remind_ats)
    return event


async def set_event_message(
    conn: asyncpg.Connection, guild_id: int, event_id: int, channel_id: int, message_id: int
) -> Event | None:
    require_id(guild_id, name="guild_id")
    require_id(event_id, name="event_id")
    require_id(channel_id, name="channel_id")
    require_id(message_id, name="message_id")
    row = await conn.fetchrow(_UPDATE_EVENT_MESSAGE_SQL, guild_id, event_id, channel_id, message_id)
    return _row_to_event(row) if row is not None else None


async def get_event(conn: asyncpg.Connection, guild_id: int, event_id: int) -> Event | None:
    require_id(guild_id, name="guild_id")
    require_id(event_id, name="event_id")
    row = await conn.fetchrow(_SELECT_EVENT_SQL, guild_id, event_id)
    return _row_to_event(row) if row is not None else None


async def list_upcoming_events(
    conn: asyncpg.Connection, guild_id: int, now: datetime, limit: int
) -> list[Event]:
    require_id(guild_id, name="guild_id")
    now = require_aware(now, name="now")
    require_limit(limit)
    rows = await conn.fetch(_LIST_UPCOMING_EVENTS_SQL, guild_id, now, limit)
    return [_row_to_event(row) for row in rows]


async def cancel_event(
    conn: asyncpg.Connection,
    guild_id: int,
    event_id: int,
    actor_id: int,
    actor_is_admin: bool,
) -> TransitionResult:
    """`scheduled -> cancelled`, when `actor_id` created it or `actor_is_admin`.

    On success: `"cancelled"`. On failure: `"not_found"`, `"not_scheduled"`,
    or `"not_creator_or_admin"`.
    """
    require_id(guild_id, name="guild_id")
    require_id(event_id, name="event_id")
    require_id(actor_id, name="actor_id")
    updated = await conn.fetchrow(_CANCEL_EVENT_SQL, event_id, guild_id, actor_id, actor_is_admin)
    if updated is not None:
        return "cancelled"

    classify_row = await conn.fetchrow(_SELECT_EVENT_FOR_CANCEL_CLASSIFY_SQL, guild_id, event_id)
    if classify_row is None:
        return "not_found"
    if classify_row["status"] != "scheduled":
        return "not_scheduled"
    return "not_creator_or_admin"


async def upsert_rsvp(
    conn: asyncpg.Connection, guild_id: int, event_id: int, user_id: int, response: str
) -> bool:
    """Set `user_id`'s RSVP, only if the event exists in `guild_id` and is scheduled.

    Returns whether the RSVP was written. A wrong guild, missing event, or
    non-scheduled event (cancelled/completed) all leave `event_rsvps`
    untouched.
    """
    require_id(guild_id, name="guild_id")
    require_id(event_id, name="event_id")
    require_id(user_id, name="user_id")
    row = await conn.fetchrow(_UPSERT_RSVP_SQL, guild_id, event_id, user_id, response)
    return row is not None


async def rsvp_counts(conn: asyncpg.Connection, guild_id: int, event_id: int) -> RsvpCounts:
    require_id(guild_id, name="guild_id")
    require_id(event_id, name="event_id")
    row = await conn.fetchrow(_SELECT_RSVP_COUNTS_SQL, event_id, guild_id)
    if row is None:  # pragma: no cover -- a bare COUNT(*) always returns one row.
        raise RuntimeError("RSVP counts query returned no row")
    return RsvpCounts(going=row["going"], maybe=row["maybe"], not_going=row["not_going"])


async def rsvp_user_ids(
    conn: asyncpg.Connection, guild_id: int, event_id: int, responses: Sequence[str]
) -> list[int]:
    require_id(guild_id, name="guild_id")
    require_id(event_id, name="event_id")
    require_str_sequence(responses, name="responses")
    rows = await conn.fetch(_SELECT_RSVP_USER_IDS_SQL, event_id, guild_id, list(responses))
    return [row["user_id"] for row in rows]


async def claim_due_reminders(conn: asyncpg.Connection, now: datetime) -> list[ClaimedReminder]:
    """Claim (at-most-once) every reminder due across all guilds, skipping cancelled events.

    System-wide scheduler query (no `guild_id` scoping): a single atomic
    `UPDATE ... RETURNING` means two concurrent scheduler ticks can never
    both claim the same reminder.
    """
    now = require_aware(now, name="now")
    rows = await conn.fetch(_CLAIM_DUE_REMINDERS_SQL, now)
    return [
        ClaimedReminder(
            event_id=row["event_id"],
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            title=row["title"],
            starts_at=row["starts_at"],
            offset_minutes=row["offset_minutes"],
            remind_at=row["remind_at"],
        )
        for row in rows
    ]


async def complete_past_events(conn: asyncpg.Connection, cutoff: datetime) -> int:
    """Mark every scheduled event whose `starts_at <= cutoff` as completed.

    System-wide scheduler query (no `guild_id` scoping), matching
    `claim_due_reminders`. Returns the number of events completed.
    """
    cutoff = require_aware(cutoff, name="cutoff")
    rows = await conn.fetch(_COMPLETE_PAST_EVENTS_SQL, cutoff)
    return len(rows)
