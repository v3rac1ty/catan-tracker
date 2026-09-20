"""Periodic season announcements, event reminders, and leaderboard posts.

The database services own claiming and state transitions. This module owns
the Discord boundary: locating a channel in the expected guild, rendering
safe embeds, limiting explicit mention allowlists, and isolating failures so
one guild cannot block another.

Announcements are retryable and are marked only after a successful send.
That ordering can duplicate an announcement if the process dies between the
send and the mark; exactly-once delivery is not possible without a durable
Discord-side idempotency key. Reminders and the daily leaderboard digest use
the opposite, documented at-most-once tradeoff: the service commits each
claim before this module attempts delivery, so a failed send is not
retried -- see `services.leaderboard_service`'s module docstring for why
that's the right call for a digest specifically.
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
from catan_bot.services import config_service, event_service, leaderboard_service, season_service
from catan_bot.services.results import Announcement, LeaderboardPost, ReminderToSend

if TYPE_CHECKING:
    from collections.abc import Iterable

    from catan_bot.bot import CatanBot

logger = logging.getLogger(__name__)

_ANNOUNCEMENT_LIMIT = 50
_DAILY_LEADERBOARD_LIMIT = 50
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


def _validated_role_id(role_id: object) -> int | None:
    """Return a Discord-shaped role ID, or silence the notification."""
    if type(role_id) is int and 1 <= role_id <= _BIGINT_MAX:
        return role_id
    return None


def _deliverable_role_id(guild: Any, channel: Any, role_id: object) -> int | None:
    """Return a configured role only when Discord can deliver its mention.

    Configuration can outlive a role deletion or a later permission change.
    In those cases reminders remain useful and are sent unpinged; this helper
    never falls back to ``@everyone``, users, or any other mention type.
    """
    valid_id = _validated_role_id(role_id)
    if valid_id is None:
        return None
    get_role = getattr(guild, "get_role", None)
    # Discord Guild objects always expose get_role.  The fallback keeps the
    # scheduler's small protocol test doubles compatible with the pre-role
    # boundary; real delivery still takes the strict checks below.
    if not callable(get_role):
        return valid_id
    role = get_role(valid_id) if callable(get_role) else None
    if role is None or getattr(role, "is_default", lambda: False)():
        logger.warning(
            "Configured event notification role unavailable guild_id=%s role_id=%s",
            getattr(guild, "id", 0),
            valid_id,
        )
        return None
    if getattr(role, "mentionable", False):
        return valid_id
    bot_member = getattr(guild, "me", None)
    permissions_for = getattr(channel, "permissions_for", None)
    try:
        permissions = permissions_for(bot_member) if callable(permissions_for) else None
    except Exception:
        logger.warning(
            "Unable to verify event notification role permissions guild_id=%s role_id=%s",
            getattr(guild, "id", 0),
            valid_id,
        )
        return None
    if getattr(permissions, "mention_everyone", False):
        return valid_id
    logger.warning(
        "Configured event notification role cannot be mentioned guild_id=%s role_id=%s",
        getattr(guild, "id", 0),
        valid_id,
    )
    return None


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
            await self._send_daily_leaderboards(now)

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
            if event.channel_id is None:
                logger.warning(
                    "Claimed reminder has no channel guild_id=%s event_id=%s",
                    event.guild_id,
                    event.event_id,
                )
                return
            guild = self.bot.get_guild(event.guild_id)
            channel = await self._guild_channel(event.guild_id, event.channel_id)
            if channel is None:
                logger.warning(
                    "Reminder channel unavailable guild_id=%s event_id=%s channel_id=%s",
                    event.guild_id,
                    event.event_id,
                    event.channel_id,
                )
                return
            # Role mentionability and Mention Everyone are channel-specific;
            # resolve the final decision after locating the destination.
            role_id = (
                _deliverable_role_id(guild, channel, reminder.player_role_id) if guild else None
            )
            content = formatting.role_mention(role_id) if role_id is not None else None
        except Exception as exc:
            _log_failure("reminder preparation", exc, **ids)
            return

        try:
            # Resolving a channel awaits Discord and creates a cancellation
            # race. Re-read after that await so a cancelled event is never
            # knowingly sent.
            latest = await event_service.get_event(self.pool, event.guild_id, event.event_id)
            if latest is None or latest.status != "scheduled":
                return
            embed = formatting.build_event_reminder_embed(latest, reminder.offset_minutes)
            if role_id is None:
                allowed_mentions = discord.AllowedMentions.none()
            else:
                allowed_mentions = discord.AllowedMentions(
                    everyone=False,
                    users=False,
                    roles=[discord.Object(id=role_id)],
                    replied_user=False,
                )
            send_kwargs: dict[str, object] = {
                "embed": embed,
                "allowed_mentions": allowed_mentions,
            }
            if content is not None:
                send_kwargs["content"] = content
            await channel.send(**send_kwargs)
        except Exception as exc:
            _log_failure("reminder delivery", exc, **ids)

    async def _complete_events(self, now: datetime) -> None:
        try:
            await event_service.complete_past_events(self.pool, now)
        except Exception as exc:
            _log_failure("event completion", exc)

    async def _send_daily_leaderboards(self, now: datetime) -> None:
        """Claim, then send, every daily-mode guild's digest that's due at `now`.

        Cheap on a tick where nothing is due: `due_daily_leaderboards` does
        all the "is anything actually due" filtering itself (see its
        docstring), so a normal tick costs one query here and returns an
        empty list. The claim already happened inside `due_daily_leaderboards`
        -- by the time a post reaches `_send_leaderboard_post` it is already
        durably marked as today's post for that guild, so a delivery failure
        here is simply logged, never retried (the same at-most-once tradeoff
        `leaderboard_service`'s module docstring explains for the claim
        itself).
        """
        try:
            posts = await leaderboard_service.due_daily_leaderboards(
                self.pool, now, _DAILY_LEADERBOARD_LIMIT
            )
        except Exception as exc:
            _log_failure("daily leaderboard claim", exc)
            return

        for post in posts:
            try:
                await self._send_leaderboard_post(post)
            except Exception as exc:
                _log_failure("daily leaderboard delivery", exc, guild_id=post.guild_id)

    async def _send_leaderboard_post(self, post: LeaderboardPost) -> None:
        channel = await self._guild_channel(post.guild_id, post.channel_id)
        if channel is None:
            logger.warning(
                "Daily leaderboard channel unavailable guild_id=%s channel_id=%s",
                post.guild_id,
                post.channel_id,
            )
            return
        embed = formatting.build_leaderboard_post_embed(post)
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())

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
