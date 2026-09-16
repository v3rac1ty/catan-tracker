"""Database-backed scheduler delivery and restart behavior."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncpg
import discord
import pytest

from catan_bot.scheduler import CatanScheduler
from catan_bot.services import config_service, event_service, season_service
from catan_bot.services.context import Actor

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)
CHANNEL_ID = 123_456


def _actor(user_id: int, *, admin: bool = False) -> Actor:
    return Actor(user_id=user_id, has_manage_guild=admin, role_ids=frozenset())


def _scheduler(pool: asyncpg.Pool, guild_id: int, channel: object) -> CatanScheduler:
    guild = SimpleNamespace(get_channel_or_thread=Mock(return_value=channel))
    bot = SimpleNamespace(
        pool=pool,
        get_guild=Mock(side_effect=lambda candidate: guild if candidate == guild_id else None),
        fetch_channel=AsyncMock(),
        wait_until_ready=AsyncMock(),
    )
    return CatanScheduler(bot)  # type: ignore[arg-type]


def _channel(guild_id: int) -> SimpleNamespace:
    return SimpleNamespace(guild=SimpleNamespace(id=guild_id), send=AsyncMock())


async def _create_two_hour_event(pool: asyncpg.Pool, guild_id: int):
    return await event_service.create_event(
        pool,
        guild_id,
        _actor(1),
        title="Game Night",
        date_text=NOW.date().isoformat(),
        time_text=(NOW + timedelta(hours=2)).strftime("%H:%M"),
        location=None,
        description=None,
        channel_id=CHANNEL_ID,
        now=NOW,
    )


async def test_scheduler_sends_due_reminder_once_across_restart_style_ticks(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    event = await _create_two_hour_event(pool, guild_id)
    await config_service.set_player_role(pool, guild_id, _actor(1, admin=True), 765_432)
    await event_service.rsvp(pool, guild_id, event.event_id, _actor(2), "going")
    await event_service.rsvp(pool, guild_id, event.event_id, _actor(3), "maybe")
    await event_service.rsvp(pool, guild_id, event.event_id, _actor(4), "not_going")
    channel = _channel(guild_id)

    await _scheduler(pool, guild_id, channel).run_tick(NOW + timedelta(hours=1))
    await _scheduler(pool, guild_id, channel).run_tick(NOW + timedelta(hours=1))

    channel.send.assert_awaited_once()
    call = channel.send.await_args
    assert call.kwargs["content"] == "<@&765432>"
    assert call.kwargs["allowed_mentions"].to_dict() == {
        "roles": [765_432],
        "parse": [],
    }
    assert "Game Night" in (call.kwargs["embed"].title or "")


async def test_scheduler_drops_stale_claimed_reminder_without_delivery(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await _create_two_hour_event(pool, guild_id)
    channel = _channel(guild_id)
    late = NOW + timedelta(hours=1, minutes=16)

    await _scheduler(pool, guild_id, channel).run_tick(late)
    await _scheduler(pool, guild_id, channel).run_tick(late)

    channel.send.assert_not_awaited()


async def test_failed_announcement_retries_then_marks_frozen_result(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await config_service.set_announce_channel(pool, guild_id, _actor(1, admin=True), CHANNEL_ID)
    season = await season_service.start_season(
        pool,
        guild_id,
        _actor(1, admin=True),
        name="Frozen Finals",
        end_date_text="2026-12-31",
        start_date_text=None,
        min_games=2,
        now=NOW,
    )
    await season_service.end_season_now(pool, guild_id, _actor(1, admin=True), NOW)
    channel = _channel(guild_id)
    channel.send.side_effect = [discord.DiscordException("temporary"), None]
    scheduler = _scheduler(pool, guild_id, channel)

    await scheduler.run_tick(NOW)
    pending_after_failure = await season_service.pending_announcements(pool, 50)
    assert season.season_id in {item.season.season_id for item in pending_after_failure}

    await scheduler.run_tick(NOW)
    await scheduler.run_tick(NOW)

    assert channel.send.await_count == 2
    successful_embed = channel.send.await_args_list[1].kwargs["embed"]
    assert "Frozen Finals" in (successful_embed.title or "")
    assert "No confirmed games" in successful_embed.fields[0].value
    pending_after_success = await season_service.pending_announcements(pool, 50)
    assert season.season_id not in {item.season.season_id for item in pending_after_success}
