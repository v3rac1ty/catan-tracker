"""Event scheduling, RSVPs, reminders, and completion sweeps."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import get_args

import asyncpg

from catan_bot.db.models import (
    ClaimedReminder,
    Event,
    RsvpCounts,
    RsvpResponse,
    RsvpRoster,
    TransitionResult,
)
from catan_bot.db.repositories import events, guilds
from catan_bot.domain.dates import combine_local, parse_date, parse_time, today_in_timezone
from catan_bot.domain.reminders import EVENT_COMPLETE_AFTER, classify_reminder, plan_reminders
from catan_bot.domain.validation import (
    EVENT_DESCRIPTION_MAX,
    EVENT_LOCATION_MAX,
    EVENT_TITLE_MAX,
    clean_text,
    validate_event_start,
)
from catan_bot.services.context import Actor, is_admin, require_valid_timezone
from catan_bot.services.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ServiceError,
)
from catan_bot.services.results import EventCreation, ReminderToSend

logger = logging.getLogger(__name__)

_UPCOMING_MIN_LIMIT, _UPCOMING_MAX_LIMIT = 1, 10

_VALID_RSVP_RESPONSES = frozenset(get_args(RsvpResponse))

_EVENT_NOT_FOUND = "That event doesn't exist."
_EVENT_NOT_SCHEDULED = "That event has already been cancelled or has already happened."
_EVENT_NOT_CREATOR_OR_ADMIN = "Only the event's creator or an admin can cancel it."
_RSVP_CLOSED = "That event isn't open for RSVPs anymore."


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


async def create_event(
    pool: asyncpg.Pool,
    guild_id: int,
    actor: Actor,
    *,
    title: str,
    date_text: str | None,
    time_text: str,
    location: str | None,
    description: str | None,
    channel_id: int | None,
    now: datetime,
) -> Event:
    """Create an event, retaining the legacy event-only service result."""
    created = await create_event_with_notification_role(
        pool,
        guild_id,
        actor,
        title=title,
        date_text=date_text,
        time_text=time_text,
        location=location,
        description=description,
        channel_id=channel_id,
        now=now,
    )
    return created.event


async def create_event_with_notification_role(
    pool: asyncpg.Pool,
    guild_id: int,
    actor: Actor,
    *,
    title: str,
    date_text: str | None,
    time_text: str,
    location: str | None,
    description: str | None,
    channel_id: int | None,
    now: datetime,
) -> EventCreation:
    """Create an event and atomically snapshot its announcement role."""
    clean_title = clean_text(title, field="Event title", max_len=EVENT_TITLE_MAX, min_len=1)
    clean_location = clean_text(
        location, field="Event location", max_len=EVENT_LOCATION_MAX, min_len=0
    )
    clean_description = clean_text(
        description,
        field="Event description",
        max_len=EVENT_DESCRIPTION_MAX,
        min_len=0,
        allow_newlines=True,
    )
    async with pool.acquire() as conn, conn.transaction():
        config = await guilds.ensure_guild(conn, guild_id)
        timezone = require_valid_timezone(config)
        today = today_in_timezone(timezone, now=now)
        event_date = parse_date(date_text, today=today)
        event_time = parse_time(time_text)
        starts_at = combine_local(event_date, event_time, timezone)
        validate_event_start(starts_at, now=now)

        reminders = plan_reminders(starts_at, now=now)
        event = await events.create_event(
            conn,
            guild_id,
            clean_title,
            clean_description,
            clean_location,
            starts_at,
            actor.user_id,
            channel_id,
            [(plan.offset_minutes, plan.remind_at) for plan in reminders],
        )
        return EventCreation(event=event, player_role_id=config.player_role_id)


async def record_event_message(
    pool: asyncpg.Pool,
    guild_id: int,
    event_id: int,
    channel_id: int,
    message_id: int,
) -> None:
    """Persist the Discord message used to announce an event."""
    async with pool.acquire() as conn:
        await events.set_event_message(conn, guild_id, event_id, channel_id, message_id)


async def get_event(pool: asyncpg.Pool, guild_id: int, event_id: int) -> Event | None:
    """Load one event through its guild-scoped repository query."""
    async with pool.acquire() as conn:
        return await events.get_event(conn, guild_id, event_id)


def _cancel_result_error(result: TransitionResult) -> ServiceError:
    if result == "not_found":
        return NotFoundError(_EVENT_NOT_FOUND)
    if result == "not_scheduled":
        return ConflictError(_EVENT_NOT_SCHEDULED)
    if result == "not_creator_or_admin":
        return PermissionDeniedError(_EVENT_NOT_CREATOR_OR_ADMIN)
    raise AssertionError(f"unexpected cancel_event result: {result!r}")  # pragma: no cover


async def cancel_event(pool: asyncpg.Pool, guild_id: int, event_id: int, actor: Actor) -> Event:
    """Cancel an event: the event's creator may cancel it, otherwise the actor must be an admin.

    `actor_is_admin` is computed server-side (from `Actor` plus the guild's
    `GuildConfig`) and passed to the repository -- never taken as a
    caller-supplied flag.
    """
    async with pool.acquire() as conn, conn.transaction():
        config = await guilds.ensure_guild(conn, guild_id)
        actor_is_admin = is_admin(actor, config)
        result = await events.cancel_event(conn, guild_id, event_id, actor.user_id, actor_is_admin)
        if result != "cancelled":
            raise _cancel_result_error(result)
        cancelled = await events.get_event(conn, guild_id, event_id)
        if cancelled is None:  # pragma: no cover -- the guarded UPDATE above just succeeded.
            raise RuntimeError(f"event {event_id} in guild {guild_id} vanished after cancel_event")
        return cancelled


async def rsvp(
    pool: asyncpg.Pool, guild_id: int, event_id: int, actor: Actor, response: str
) -> RsvpCounts:
    """Set `actor`'s RSVP. `response` must be a fixed literal -- buttons produce fixed ids."""
    if response not in _VALID_RSVP_RESPONSES:
        # Fixed message: never echo `repr(response)` back (I2 audit finding).
        raise ValueError("invalid RSVP response")
    async with pool.acquire() as conn, conn.transaction():
        written = await events.upsert_rsvp(conn, guild_id, event_id, actor.user_id, response)
        if not written:
            raise NotFoundError(_RSVP_CLOSED)
        return await events.rsvp_counts(conn, guild_id, event_id)


async def rsvp_roster(pool: asyncpg.Pool, guild_id: int, event_id: int) -> RsvpRoster:
    """Load the complete grouped RSVP roster for a scheduled event."""
    async with pool.acquire() as conn:
        return await events.rsvp_roster(conn, guild_id, event_id)


async def upcoming_events(
    pool: asyncpg.Pool, guild_id: int, now: datetime, limit: int
) -> list[Event]:
    n = _clamp(limit, _UPCOMING_MIN_LIMIT, _UPCOMING_MAX_LIMIT)
    async with pool.acquire() as conn:
        return await events.list_upcoming_events(conn, guild_id, now, n)


def _log_reminder_read_failure(event_id: int, guild_id: int, exc: Exception) -> None:
    """Log a claimed reminder's failed per-guild read, without leaking row contents.

    Same no-DETAIL rule as `season_service._log_resolution_failure`: never
    `str(exc)` (`asyncpg.PostgresError.__str__` appends the server's
    DETAIL/HINT text, which can carry row contents), never those fields
    directly, and no traceback -- only the ids and the exception's type
    name, plus `sqlstate` for a `PostgresError` (a fixed 5-character error
    class code, not server-supplied text).
    """
    if isinstance(exc, asyncpg.PostgresError):
        logger.error(
            "Reminder read failed for event_id=%s guild_id=%s: %s (sqlstate=%s)",
            event_id,
            guild_id,
            type(exc).__name__,
            exc.sqlstate,
        )
    else:
        logger.error(
            "Reminder read failed for event_id=%s guild_id=%s: %s",
            event_id,
            guild_id,
            type(exc).__name__,
        )


async def _read_reminder(pool: asyncpg.Pool, reminder: ClaimedReminder) -> ReminderToSend | None:
    """Load the event for one already-claimed reminder.

    Runs on its own `pool.acquire()` connection, isolated from every other
    reminder's read: a failure here (a bad connection, a guild-specific
    data problem, ...) must never take down reads for other guilds' due
    reminders in the same tick. Returns `None` both on a clean "don't send"
    (the event is gone, cancelled, or completed -- the claim query doesn't
    lock the event row, so a reminder can be claimed while a cancel is
    committing) and on a failed read (logged, then dropped).
    """
    try:
        async with pool.acquire() as conn:
            event = await events.get_event(conn, reminder.guild_id, reminder.event_id)
            if event is None or event.status != "scheduled":
                return None
    except Exception as exc:
        _log_reminder_read_failure(reminder.event_id, reminder.guild_id, exc)
        return None
    return ReminderToSend(
        event=event,
        offset_minutes=reminder.offset_minutes,
        player_role_id=reminder.player_role_id,
    )


async def due_reminders(pool: asyncpg.Pool, now: datetime) -> list[ReminderToSend]:
    """Claim every due reminder across all guilds and decide who to ping.

    Two phases, deliberately on separate connections so one guild's bad
    data can never block or lose another guild's reminders:

    1. **Claim**, in its own transaction that commits before anything else
       runs. `claim_due_reminders`'s guarded `UPDATE ... RETURNING` makes
       each reminder claimed at-most-once (even across two concurrent
       scheduler ticks), and because this commits immediately, every
       claimed reminder is already marked sent no matter what phase 2 does.
    2. **Read**, one reminder at a time, each on its own connection and its
       own `try/except` (`_read_reminder`). A stale reminder (claimed too
       long after it was due) is dropped without a read. A reminder whose
       read fails is logged (ids and exception type only -- never DETAIL,
       HINT, or `str(exc)`) and dropped too.

    At-most-once trade-off: because the claim already committed before
    phase 2 starts, a reminder is *never* retried after being claimed --
    including when its own read fails. That reminder is simply lost; every
    other claimed reminder (same guild or not) is still read and sent
    normally.
    """
    async with pool.acquire() as conn, conn.transaction():
        claimed = await events.claim_due_reminders(conn, now)

    to_send: list[ReminderToSend] = []
    for reminder in claimed:
        if classify_reminder(reminder.remind_at, now=now) == "skip_stale":
            continue
        loaded = await _read_reminder(pool, reminder)
        if loaded is not None:
            to_send.append(loaded)
    return to_send


async def complete_past_events(pool: asyncpg.Pool, now: datetime) -> int:
    cutoff = now - EVENT_COMPLETE_AFTER
    async with pool.acquire() as conn:
        return await events.complete_past_events(conn, cutoff)
