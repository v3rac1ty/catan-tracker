"""Periodic season announcements and event reminders.

The database services own claiming and state transitions. This module owns
the Discord boundary: locating a channel in the expected guild, rendering
safe embeds, limiting explicit mention allowlists, and isolating failures so
one guild cannot block another.

Announcements are retryable and are marked only after a successful send.
That ordering can duplicate an announcement if the process dies between the
send and the mark; exactly-once delivery is not possible without a durable
Discord-side idempotency key. Reminders use the opposite, documented
at-most-once tradeoff: the service commits each claim before this module
attempts delivery, so a failed send is not retried.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import tasks

from catan_bot import formatting
from catan_bot.services import config_service, event_service, season_service
from catan_bot.services.results import Announcement, ReminderToSend

if TYPE_CHECKING:
    from collections.abc import Iterable

    from catan_bot.bot import CatanBot

logger = logging.getLogger(__name__)

_ANNOUNCEMENT_LIMIT = 50
_MAX_MESSAGE_LENGTH = 2000
_MAX_ALLOWED_USERS = 100
_BIGINT_MAX = 2**63 - 1


def _safe_sqlstate(exc: BaseException) -> str | None:
    """Return a real five-character SQLSTATE from anywhere in an exception graph."""
    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        module = type(current).__module__
        sqlstate = getattr(current, "sqlstate", None)
        if (
            module.startswith("asyncpg")
            and isinstance(sqlstate, str)
            and len(sqlstate) == 5
            and sqlstate.isascii()
            and all(ch.isdigit() or "A" <= ch <= "Z" for ch in sqlstate)
        ):
            return sqlstate
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return None


def _log_failure(stage: str, exc: BaseException, **ids: int) -> None:
    """Log fixed metadata only; exception messages and tracebacks may contain secrets."""
    metadata = " ".join(f"{name}={value}" for name, value in sorted(ids.items()))
    sqlstate = _safe_sqlstate(exc)
    if sqlstate is None:
        logger.error("Scheduler %s failed %s: %s", stage, metadata, type(exc).__name__)
    else:
        logger.error(
            "Scheduler %s failed %s: %s (sqlstate=%s)",
            stage,
            metadata,
            type(exc).__name__,
            sqlstate,
        )


def _validated_user_ids(user_ids: Iterable[int]) -> tuple[int, ...]:
    """Keep unique PostgreSQL/Discord-shaped ids and reject surprising runtime values."""
    valid: list[int] = []
    seen: set[int] = set()
    for user_id in user_ids:
        if type(user_id) is not int or not (1 <= user_id <= _BIGINT_MAX):
            continue
        if user_id not in seen:
            seen.add(user_id)
            valid.append(user_id)
    return tuple(valid)


def _mention_batches(user_ids: Iterable[int]) -> list[tuple[str | None, tuple[int, ...]]]:
    """Build content/allowlist batches within Discord's two relevant limits."""
    batches: list[tuple[str | None, tuple[int, ...]]] = []
    current_ids: list[int] = []
    current_mentions: list[str] = []
    current_length = 0

    for user_id in _validated_user_ids(user_ids):
        mention = formatting.mention(user_id)
        addition = len(mention) + (1 if current_mentions else 0)
        if current_ids and (
            len(current_ids) >= _MAX_ALLOWED_USERS
            or current_length + addition > _MAX_MESSAGE_LENGTH
        ):
            batches.append((" ".join(current_mentions), tuple(current_ids)))
            current_ids = []
            current_mentions = []
            current_length = 0
            addition = len(mention)
        current_ids.append(user_id)
        current_mentions.append(mention)
        current_length += addition

    if current_ids:
        batches.append((" ".join(current_mentions), tuple(current_ids)))
    return batches or [(None, ())]


class CatanScheduler:
    """A restart-safe 60-second task loop around testable ``run_tick`` logic."""

    def __init__(self, bot: CatanBot) -> None:
        if bot.pool is None:
            raise RuntimeError("scheduler requires an initialized database pool")
        self.bot = bot
        self.pool = bot.pool
        self._tick_lock = asyncio.Lock()

    def start(self) -> None:
        self.scheduler_loop.start()

    async def close(self) -> None:
        task = self.scheduler_loop.get_task()
        self.scheduler_loop.cancel()
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task

    @tasks.loop(seconds=60)
    async def scheduler_loop(self) -> None:
        await self.run_tick(datetime.now(UTC))

    @scheduler_loop.before_loop
    async def before_scheduler_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def run_tick(self, now: datetime) -> None:
        """Run each scheduler stage once using the supplied, controllable clock."""
        if self._tick_lock.locked():
            logger.warning("Scheduler tick skipped because the previous tick is still running")
            return
        async with self._tick_lock:
            await self._resolve_seasons(now)
            await self._send_announcements()
            await self._send_reminders(now)
            await self._complete_events(now)

    async def _resolve_seasons(self, now: datetime) -> None:
        try:
            await season_service.resolve_due_seasons(self.pool, now)
        except Exception as exc:
            _log_failure("season resolution", exc)

    async def _send_announcements(self) -> None:
        try:
            announcements = await season_service.pending_announcements(
                self.pool, _ANNOUNCEMENT_LIMIT
            )
        except Exception as exc:
            _log_failure("announcement read", exc)
            return

        for announcement in announcements:
            await self._send_announcement(announcement)

    async def _send_announcement(self, announcement: Announcement) -> None:
        season = announcement.season
        ids = {"guild_id": season.guild_id, "season_id": season.season_id}
        try:
            config = await config_service.get_config(self.pool, season.guild_id)
            if config.announce_channel_id is None:
                logger.warning(
                    "Skipping season announcement with no configured channel "
                    "guild_id=%s season_id=%s",
                    season.guild_id,
                    season.season_id,
                )
                await season_service.mark_announced(self.pool, season.guild_id, season.season_id)
                return

            channel = await self._guild_channel(season.guild_id, config.announce_channel_id)
            if channel is None:
                logger.warning(
                    "Season announcement channel unavailable guild_id=%s "
                    "season_id=%s channel_id=%s",
                    season.guild_id,
                    season.season_id,
                    config.announce_channel_id,
                )
                return
            embed = formatting.build_frozen_season_announcement_embed(announcement)
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            await season_service.mark_announced(self.pool, season.guild_id, season.season_id)
        except Exception as exc:
            _log_failure("announcement delivery", exc, **ids)

    async def _send_reminders(self, now: datetime) -> None:
        try:
            reminders = await event_service.due_reminders(self.pool, now)
        except Exception as exc:
            _log_failure("reminder claim", exc)
            return

        for reminder in reminders:
            try:
                await self._send_reminder(reminder)
            except Exception as exc:
                _log_failure(
                    "reminder processing",
                    exc,
                    event_id=reminder.event.event_id,
                    guild_id=reminder.event.guild_id,
                )

    async def _send_reminder(self, reminder: ReminderToSend) -> None:
        event = reminder.event
        ids = {"event_id": event.event_id, "guild_id": event.guild_id}
        try:
            batches = _mention_batches(reminder.user_ids)
            if event.channel_id is None:
                logger.warning(
                    "Claimed reminder has no channel guild_id=%s event_id=%s",
                    event.guild_id,
                    event.event_id,
                )
                return
            channel = await self._guild_channel(event.guild_id, event.channel_id)
            if channel is None:
                logger.warning(
                    "Reminder channel unavailable guild_id=%s event_id=%s channel_id=%s",
                    event.guild_id,
                    event.event_id,
                    event.channel_id,
                )
                return
        except Exception as exc:
            _log_failure("reminder preparation", exc, **ids)
            return

        for batch_number, (content, user_ids) in enumerate(batches, start=1):
            try:
                # Resolving a channel awaits Discord and creates a
                # cancellation race. Re-read after that await and again for
                # every batch so a cancelled event is never knowingly sent.
                latest = await event_service.get_event(self.pool, event.guild_id, event.event_id)
                if latest is None or latest.status != "scheduled":
                    return
                embed = formatting.build_event_reminder_embed(latest, reminder.offset_minutes)
                allowed_mentions = discord.AllowedMentions(
                    everyone=False,
                    users=[discord.Object(id=user_id) for user_id in user_ids],
                    roles=False,
                    replied_user=False,
                )
                await channel.send(
                    content=content,
                    embed=embed,
                    allowed_mentions=allowed_mentions,
                )
            except Exception as exc:
                _log_failure("reminder delivery", exc, batch=batch_number, **ids)

    async def _complete_events(self, now: datetime) -> None:
        try:
            await event_service.complete_past_events(self.pool, now)
        except Exception as exc:
            _log_failure("event completion", exc)

    async def _guild_channel(self, guild_id: int, channel_id: int) -> Any | None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None

        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)

        channel_guild = getattr(channel, "guild", None)
        if channel_guild is None or getattr(channel_guild, "id", None) != guild_id:
            return None
        if not callable(getattr(channel, "send", None)):
            return None
        return channel


__all__ = ["CatanScheduler"]
