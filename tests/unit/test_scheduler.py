"""Scheduler orchestration tests with a controllable clock and no Discord network."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import asyncpg
import discord
import pytest

from catan_bot.db.models import Event, GuildConfig, Season, SeasonResultRow
from catan_bot.scheduler import CatanScheduler, _mention_batches
from catan_bot.services.results import Announcement, ReminderToSend

NOW = datetime(2026, 1, 2, 12, tzinfo=UTC)


def _event(event_id: int, *, guild_id: int = 1, status: str = "scheduled") -> Event:
    return Event(
        event_id=event_id,
        guild_id=guild_id,
        title=f"Game night {event_id}",
        description=None,
        location=None,
        starts_at=NOW,
        status=status,  # type: ignore[arg-type]
        created_by=10,
        channel_id=100 + guild_id,
        message_id=None,
        created_at=NOW,
    )


def _season(season_id: int, guild_id: int) -> Season:
    return Season(
        season_id=season_id,
        guild_id=guild_id,
        name=f"Season {season_id}",
        starts_on=NOW.date(),
        ends_on=NOW.date(),
        ends_at=NOW,
        min_games=2,
        status="completed",
        resolved_at=NOW,
        announced_at=None,
        created_by=10,
        created_at=NOW,
    )


def _announcement(season_id: int, guild_id: int) -> Announcement:
    return Announcement(
        season=_season(season_id, guild_id),
        results=[
            SeasonResultRow(
                user_id=10,
                rank=1,
                games=2,
                wins=2,
                eligible=True,
                outcome="payee",
            )
        ],
    )


def _config(guild_id: int, channel_id: int | None) -> GuildConfig:
    return GuildConfig(
        guild_id=guild_id,
        timezone="UTC",
        announce_channel_id=channel_id,
        admin_role_id=None,
        default_min_games=2,
        created_at=NOW,
        updated_at=NOW,
    )


def _channel(guild_id: int) -> SimpleNamespace:
    return SimpleNamespace(guild=SimpleNamespace(id=guild_id), send=AsyncMock())


def _scheduler(channels: dict[tuple[int, int], object] | None = None) -> CatanScheduler:
    channels = channels or {}

    def get_guild(guild_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            get_channel_or_thread=Mock(
                side_effect=lambda channel_id: channels.get((guild_id, channel_id))
            )
        )

    pool = SimpleNamespace()
    bot = SimpleNamespace(
        get_guild=Mock(side_effect=get_guild),
        fetch_channel=AsyncMock(side_effect=LookupError("channel unavailable")),
        wait_until_ready=AsyncMock(),
        pool=pool,
    )
    return CatanScheduler(bot)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_run_tick_isolates_failed_stage_and_sanitizes_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    scheduler = _scheduler()
    resolve = AsyncMock(side_effect=RuntimeError("DETAIL password=secret"))
    announcements = AsyncMock(return_value=[])
    reminders = AsyncMock(return_value=[])
    complete = AsyncMock(return_value=0)
    monkeypatch.setattr("catan_bot.scheduler.season_service.resolve_due_seasons", resolve)
    monkeypatch.setattr("catan_bot.scheduler.season_service.pending_announcements", announcements)
    monkeypatch.setattr("catan_bot.scheduler.event_service.due_reminders", reminders)
    monkeypatch.setattr("catan_bot.scheduler.event_service.complete_past_events", complete)

    with caplog.at_level(logging.ERROR, logger="catan_bot.scheduler"):
        await scheduler.run_tick(NOW)

    announcements.assert_awaited_once()
    reminders.assert_awaited_once_with(scheduler.pool, NOW)
    complete.assert_awaited_once_with(scheduler.pool, NOW)
    assert "RuntimeError" in caplog.text
    assert "password" not in caplog.text
    assert "DETAIL" not in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_overlapping_tick_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduler = _scheduler()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def block(_now: datetime) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(scheduler, "_resolve_seasons", block)
    monkeypatch.setattr(scheduler, "_send_announcements", AsyncMock())
    monkeypatch.setattr(scheduler, "_send_reminders", AsyncMock())
    monkeypatch.setattr(scheduler, "_complete_events", AsyncMock())

    first = asyncio.create_task(scheduler.run_tick(NOW))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.wait_for(scheduler.run_tick(NOW), timeout=1)
    finally:
        release.set()
        try:
            await asyncio.wait_for(first, timeout=1)
        except BaseException:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            raise

    assert scheduler._send_announcements.await_count == 1


@pytest.mark.parametrize("failing_stage", ["season_lock", "reminder_claim"])
@pytest.mark.asyncio
async def test_chained_postgres_stage_failure_isolated_and_safely_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing_stage: str,
) -> None:
    scheduler = _scheduler()
    marker = "MARKER_private_row_and_password_MUST_NOT_LEAK"
    database_error = asyncpg.CheckViolationError("simulated")
    database_error.detail = marker
    try:
        raise database_error
    except asyncpg.PostgresError as cause:
        chained = asyncpg.InterfaceError(marker)
        chained.__cause__ = cause

    resolve = AsyncMock(return_value=[])
    announcements = AsyncMock(return_value=[])
    reminders = AsyncMock(return_value=[])
    complete = AsyncMock(return_value=0)
    if failing_stage == "season_lock":
        resolve.side_effect = chained
    else:
        reminders.side_effect = chained
    monkeypatch.setattr("catan_bot.scheduler.season_service.resolve_due_seasons", resolve)
    monkeypatch.setattr("catan_bot.scheduler.season_service.pending_announcements", announcements)
    monkeypatch.setattr("catan_bot.scheduler.event_service.due_reminders", reminders)
    monkeypatch.setattr("catan_bot.scheduler.event_service.complete_past_events", complete)

    with caplog.at_level(logging.ERROR, logger="catan_bot.scheduler"):
        await scheduler.run_tick(NOW)

    announcements.assert_awaited_once()
    reminders.assert_awaited_once()
    complete.assert_awaited_once_with(scheduler.pool, NOW)
    assert "23514" in caplog.text
    assert "InterfaceError" in caplog.text
    assert marker not in caplog.text
    assert all(record.exc_info is None and record.exc_text is None for record in caplog.records)


@pytest.mark.asyncio
async def test_announcement_uses_frozen_results_then_marks_after_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel(1)
    scheduler = _scheduler({(1, 101): channel})
    announcement = _announcement(5, 1)
    embed = discord.Embed(title="Frozen")
    monkeypatch.setattr(
        "catan_bot.scheduler.config_service.get_config",
        AsyncMock(return_value=_config(1, 101)),
    )
    mark = AsyncMock()
    monkeypatch.setattr("catan_bot.scheduler.season_service.mark_announced", mark)
    render = Mock(return_value=embed)
    monkeypatch.setattr(
        "catan_bot.scheduler.formatting.build_frozen_season_announcement_embed", render
    )

    await scheduler._send_announcement(announcement)

    render.assert_called_once_with(announcement)
    channel.send.assert_awaited_once()
    assert channel.send.await_args.kwargs["embed"] is embed
    assert channel.send.await_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    mark.assert_awaited_once_with(scheduler.pool, 1, 5)


@pytest.mark.asyncio
async def test_announcement_send_failure_is_retried_and_does_not_block_next_guild(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    failed = _channel(1)
    failed.send.side_effect = discord.HTTPException(
        SimpleNamespace(status=500, reason="secret", text="secret"), "secret"
    )
    succeeded = _channel(2)
    scheduler = _scheduler({(1, 101): failed, (2, 102): succeeded})
    announcements = [_announcement(5, 1), _announcement(6, 2)]
    monkeypatch.setattr(
        "catan_bot.scheduler.season_service.pending_announcements",
        AsyncMock(return_value=announcements),
    )
    monkeypatch.setattr(
        "catan_bot.scheduler.config_service.get_config",
        AsyncMock(side_effect=[_config(1, 101), _config(2, 102)]),
    )
    mark = AsyncMock()
    monkeypatch.setattr("catan_bot.scheduler.season_service.mark_announced", mark)
    monkeypatch.setattr(
        "catan_bot.scheduler.formatting.build_frozen_season_announcement_embed",
        Mock(return_value=discord.Embed()),
    )

    with caplog.at_level(logging.ERROR, logger="catan_bot.scheduler"):
        await scheduler._send_announcements()

    failed.send.assert_awaited_once()
    succeeded.send.assert_awaited_once()
    mark.assert_awaited_once_with(scheduler.pool, 2, 6)
    assert "HTTPException" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_missing_config_channel_is_marked_without_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _scheduler()
    announcement = _announcement(5, 1)
    monkeypatch.setattr(
        "catan_bot.scheduler.config_service.get_config",
        AsyncMock(return_value=_config(1, None)),
    )
    mark = AsyncMock()
    monkeypatch.setattr("catan_bot.scheduler.season_service.mark_announced", mark)

    await scheduler._send_announcement(announcement)

    mark.assert_awaited_once_with(scheduler.pool, 1, 5)


@pytest.mark.asyncio
async def test_cross_guild_fetched_channel_is_rejected_and_left_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _scheduler()
    scheduler.bot.fetch_channel = AsyncMock(return_value=_channel(999))
    monkeypatch.setattr(
        "catan_bot.scheduler.config_service.get_config",
        AsyncMock(return_value=_config(1, 101)),
    )
    mark = AsyncMock()
    monkeypatch.setattr("catan_bot.scheduler.season_service.mark_announced", mark)

    await scheduler._send_announcement(_announcement(5, 1))

    mark.assert_not_awaited()


@pytest.mark.asyncio
async def test_reminder_rechecks_latest_status_immediately_before_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel(1)
    scheduler = _scheduler({(1, 101): channel})
    reminder = ReminderToSend(event=_event(4), offset_minutes=60, user_ids=(10,))
    monkeypatch.setattr(
        "catan_bot.scheduler.event_service.get_event",
        AsyncMock(return_value=_event(4, status="cancelled")),
    )

    await scheduler._send_reminder(reminder)

    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_reminder_cancelled_while_channel_is_fetched_is_not_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel(1)
    scheduler = _scheduler()
    scheduler.bot.fetch_channel = AsyncMock(return_value=channel)
    reminder = ReminderToSend(event=_event(4), offset_minutes=60, user_ids=(10,))
    get_event = AsyncMock(return_value=_event(4, status="cancelled"))
    monkeypatch.setattr("catan_bot.scheduler.event_service.get_event", get_event)

    await scheduler._send_reminder(reminder)

    scheduler.bot.fetch_channel.assert_awaited_once_with(101)
    get_event.assert_awaited_once_with(scheduler.pool, 1, 4)
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_reminder_batches_content_and_user_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel(1)
    scheduler = _scheduler({(1, 101): channel})
    latest = _event(4)
    reminder = ReminderToSend(
        event=_event(4),
        offset_minutes=60,
        user_ids=tuple(range(1, 151)),
    )
    monkeypatch.setattr(
        "catan_bot.scheduler.event_service.get_event", AsyncMock(return_value=latest)
    )
    embed = discord.Embed(title="Reminder")
    render = Mock(return_value=embed)
    monkeypatch.setattr("catan_bot.scheduler.formatting.build_event_reminder_embed", render)

    await scheduler._send_reminder(reminder)

    assert render.call_count == 2
    render.assert_called_with(latest, 60)
    assert channel.send.await_count == 2
    mentioned_ids: list[int] = []
    for call in channel.send.await_args_list:
        content = call.kwargs["content"]
        allowed = call.kwargs["allowed_mentions"].to_dict()
        assert len(content) <= 2000
        assert allowed["parse"] == []
        assert len(allowed["users"]) <= 100
        mentioned_ids.extend(allowed["users"])
    assert mentioned_ids == list(range(1, 151))


def test_mention_batches_filter_invalid_duplicate_ids_and_bound_worst_case() -> None:
    largest = 2**63 - 1
    ids = [True, 0, -1, largest + 1, largest, largest, *range(1, 151)]

    batches = _mention_batches(ids)  # type: ignore[arg-type]

    flattened = [user_id for _content, batch in batches for user_id in batch]
    assert flattened == [largest, *range(1, 151)]
    assert all(content is not None and len(content) <= 2000 for content, _batch in batches)
    assert all(len(batch) <= 100 for _content, batch in batches)


@pytest.mark.asyncio
async def test_restart_style_second_tick_does_not_resend_claimed_reminder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel(1)
    scheduler = _scheduler({(1, 101): channel})
    reminder = ReminderToSend(event=_event(4), offset_minutes=60, user_ids=(10,))
    monkeypatch.setattr(
        "catan_bot.scheduler.event_service.due_reminders",
        AsyncMock(side_effect=[[reminder], []]),
    )
    monkeypatch.setattr(
        "catan_bot.scheduler.event_service.get_event", AsyncMock(return_value=_event(4))
    )
    monkeypatch.setattr(
        "catan_bot.scheduler.formatting.build_event_reminder_embed",
        Mock(return_value=discord.Embed()),
    )

    await scheduler._send_reminders(NOW)
    await scheduler._send_reminders(NOW)

    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_one_reminder_processing_failure_does_not_block_another(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    scheduler = _scheduler()
    reminders = [
        ReminderToSend(event=_event(4), offset_minutes=60, user_ids=(10,)),
        ReminderToSend(event=_event(5), offset_minutes=60, user_ids=(11,)),
    ]
    monkeypatch.setattr(
        "catan_bot.scheduler.event_service.due_reminders",
        AsyncMock(return_value=reminders),
    )
    send = AsyncMock(side_effect=[RuntimeError("password=secret"), None])
    monkeypatch.setattr(scheduler, "_send_reminder", send)

    with caplog.at_level(logging.ERROR, logger="catan_bot.scheduler"):
        await scheduler._send_reminders(NOW)

    assert send.await_count == 2
    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_close_cancels_loop_waiting_for_ready_without_hanging() -> None:
    ready = asyncio.Event()
    bot = SimpleNamespace(wait_until_ready=ready.wait, pool=SimpleNamespace())
    scheduler = CatanScheduler(bot)  # type: ignore[arg-type]

    scheduler.start()
    await asyncio.sleep(0)
    await asyncio.wait_for(scheduler.close(), timeout=1)

    assert not scheduler.scheduler_loop.is_running()
