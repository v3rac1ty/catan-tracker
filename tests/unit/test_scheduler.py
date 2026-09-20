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

from catan_bot.db.models import (
    Event,
    Game,
    GameWithParticipants,
    GuildConfig,
    Season,
    SeasonResultRow,
)
from catan_bot.scheduler import (
    CatanScheduler,
    _deliverable_role_id,
    _group_score_prompts_by_game,
    _mention_batches,
)
from catan_bot.services.results import (
    Announcement,
    DueScorePrompt,
    Leaderboard,
    LeaderboardPost,
    ReminderToSend,
)

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


def _config(
    guild_id: int, channel_id: int | None, *, player_role_id: int | None = None
) -> GuildConfig:
    return GuildConfig(
        guild_id=guild_id,
        timezone="UTC",
        announce_channel_id=channel_id,
        admin_role_id=None,
        default_min_games=2,
        created_at=NOW,
        updated_at=NOW,
        player_role_id=player_role_id,
    )


def _channel(guild_id: int) -> SimpleNamespace:
    return SimpleNamespace(guild=SimpleNamespace(id=guild_id), send=AsyncMock())


def _dm_user(user_id: int, *, dm_channel_id: int = 600) -> SimpleNamespace:
    """A cached `discord.User`-shaped double that records what it was sent."""
    message = SimpleNamespace(channel=SimpleNamespace(id=dm_channel_id), id=1000 + user_id)
    return SimpleNamespace(id=user_id, send=AsyncMock(return_value=message))


def _scheduler(
    channels: dict[tuple[int, int], object] | None = None,
    users: dict[int, object] | None = None,
) -> CatanScheduler:
    channels = channels or {}
    users = users or {}

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
        get_user=Mock(side_effect=lambda user_id: users.get(user_id)),
        fetch_user=AsyncMock(side_effect=LookupError("user unavailable")),
        wait_until_ready=AsyncMock(),
        pool=pool,
    )
    return CatanScheduler(bot)  # type: ignore[arg-type]


def _score_game(
    game_id: int, guild_id: int, *, channel_id: int | None = 700, status: str = "pending"
) -> GameWithParticipants:
    game = Game(
        game_id=game_id,
        guild_id=guild_id,
        season_id=None,
        played_on=NOW.date(),
        status=status,  # type: ignore[arg-type]
        reported_by=900,
        confirmed_by=None,
        confirmed_at=None,
        voided_by=None,
        voided_at=None,
        void_reason=None,
        rejected_by=None,
        rejected_at=None,
        channel_id=channel_id,
        message_id=800,
        created_at=NOW,
    )
    return GameWithParticipants(game=game, winner_id=1, loser_ids=(2, 3))


def _due_prompt(game: GameWithParticipants, user_id: int) -> DueScorePrompt:
    return DueScorePrompt(
        game=game,
        user_id=user_id,
        dm_channel_id=None,
        dm_message_id=None,
        delivery_status="pending",
    )


def test_deleted_or_unmentionable_role_is_silenced_without_fallback_mentions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    guild = SimpleNamespace(
        id=1,
        me=object(),
        get_role=lambda _role_id: None,
    )
    channel = SimpleNamespace(
        permissions_for=lambda _member: discord.Permissions(mention_everyone=False)
    )

    with caplog.at_level(logging.WARNING, logger="catan_bot.scheduler"):
        assert _deliverable_role_id(guild, channel, 987) is None

    assert "unavailable" in caplog.text


@pytest.mark.asyncio
async def test_run_tick_isolates_failed_stage_and_sanitizes_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    scheduler = _scheduler()
    resolve = AsyncMock(side_effect=RuntimeError("DETAIL password=secret"))
    announcements = AsyncMock(return_value=[])
    reminders = AsyncMock(return_value=[])
    complete = AsyncMock(return_value=0)
    score_prompts = AsyncMock(return_value=[])
    daily_leaderboards = AsyncMock(return_value=[])
    monkeypatch.setattr("catan_bot.scheduler.season_service.resolve_due_seasons", resolve)
    monkeypatch.setattr("catan_bot.scheduler.season_service.pending_announcements", announcements)
    monkeypatch.setattr("catan_bot.scheduler.event_service.due_reminders", reminders)
    monkeypatch.setattr("catan_bot.scheduler.event_service.complete_past_events", complete)
    monkeypatch.setattr("catan_bot.scheduler.game_service.due_score_prompts", score_prompts)
    monkeypatch.setattr(
        "catan_bot.scheduler.leaderboard_service.due_daily_leaderboards", daily_leaderboards
    )

    with caplog.at_level(logging.ERROR, logger="catan_bot.scheduler"):
        await scheduler.run_tick(NOW)

    announcements.assert_awaited_once()
    reminders.assert_awaited_once_with(scheduler.pool, NOW)
    complete.assert_awaited_once_with(scheduler.pool, NOW)
    score_prompts.assert_awaited_once()
    daily_leaderboards.assert_awaited_once()
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
    monkeypatch.setattr(scheduler, "_send_score_prompts", AsyncMock())
    monkeypatch.setattr(scheduler, "_send_daily_leaderboards", AsyncMock())

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
    score_prompts = AsyncMock(return_value=[])
    daily_leaderboards = AsyncMock(return_value=[])
    if failing_stage == "season_lock":
        resolve.side_effect = chained
    else:
        reminders.side_effect = chained
    monkeypatch.setattr("catan_bot.scheduler.season_service.resolve_due_seasons", resolve)
    monkeypatch.setattr("catan_bot.scheduler.season_service.pending_announcements", announcements)
    monkeypatch.setattr("catan_bot.scheduler.event_service.due_reminders", reminders)
    monkeypatch.setattr("catan_bot.scheduler.event_service.complete_past_events", complete)
    monkeypatch.setattr("catan_bot.scheduler.game_service.due_score_prompts", score_prompts)
    monkeypatch.setattr(
        "catan_bot.scheduler.leaderboard_service.due_daily_leaderboards", daily_leaderboards
    )

    with caplog.at_level(logging.ERROR, logger="catan_bot.scheduler"):
        await scheduler.run_tick(NOW)

    announcements.assert_awaited_once()
    reminders.assert_awaited_once()
    complete.assert_awaited_once_with(scheduler.pool, NOW)
    score_prompts.assert_awaited_once()
    daily_leaderboards.assert_awaited_once()
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
async def test_reminder_mentions_configured_role_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel(1)
    scheduler = _scheduler({(1, 101): channel})
    latest = _event(4)
    reminder = ReminderToSend(
        event=_event(4),
        offset_minutes=60,
        player_role_id=987,
    )
    monkeypatch.setattr(
        "catan_bot.scheduler.event_service.get_event", AsyncMock(return_value=latest)
    )
    embed = discord.Embed(title="Reminder")
    render = Mock(return_value=embed)
    monkeypatch.setattr("catan_bot.scheduler.formatting.build_event_reminder_embed", render)

    await scheduler._send_reminder(reminder)

    assert render.call_count == 1
    render.assert_called_with(latest, 60)
    assert channel.send.await_count == 1
    call = channel.send.await_args
    assert call.kwargs["content"] == "<@&987>"
    assert call.kwargs["allowed_mentions"].to_dict() == {
        "roles": [987],
        "parse": [],
    }


@pytest.mark.asyncio
async def test_reminder_without_role_has_no_content_or_allowed_mentions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel(1)
    scheduler = _scheduler({(1, 101): channel})
    reminder = ReminderToSend(event=_event(4), offset_minutes=60, player_role_id=None)
    monkeypatch.setattr(
        "catan_bot.scheduler.event_service.get_event", AsyncMock(return_value=_event(4))
    )
    monkeypatch.setattr(
        "catan_bot.scheduler.formatting.build_event_reminder_embed",
        Mock(return_value=discord.Embed(title="Reminder")),
    )

    await scheduler._send_reminder(reminder)

    call = channel.send.await_args
    assert "content" not in call.kwargs
    assert call.kwargs["allowed_mentions"].to_dict() == {"parse": []}


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


def _leaderboard_post(guild_id: int, channel_id: int) -> LeaderboardPost:
    board = Leaderboard(scope="all_time", season=None, min_games=2, ranked=[])
    return LeaderboardPost(guild_id=guild_id, channel_id=channel_id, board=board, movements=())


@pytest.mark.asyncio
async def test_send_daily_leaderboards_delivers_each_due_post_to_its_own_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _channel(1)
    scheduler = _scheduler({(1, 101): channel})
    post = _leaderboard_post(1, 101)
    monkeypatch.setattr(
        "catan_bot.scheduler.leaderboard_service.due_daily_leaderboards",
        AsyncMock(return_value=[post]),
    )
    embed = discord.Embed(title="Digest")
    render = Mock(return_value=embed)
    monkeypatch.setattr("catan_bot.scheduler.formatting.build_leaderboard_post_embed", render)

    await scheduler._send_daily_leaderboards(NOW)

    render.assert_called_once_with(post)
    channel.send.assert_awaited_once()
    assert channel.send.await_args.kwargs["embed"] is embed
    assert channel.send.await_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}


@pytest.mark.asyncio
async def test_send_daily_leaderboards_is_cheap_and_silent_when_nothing_is_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _scheduler()
    due = AsyncMock(return_value=[])
    monkeypatch.setattr("catan_bot.scheduler.leaderboard_service.due_daily_leaderboards", due)

    await scheduler._send_daily_leaderboards(NOW)

    due.assert_awaited_once_with(scheduler.pool, NOW, 50)


@pytest.mark.asyncio
async def test_send_daily_leaderboards_claim_read_failure_is_isolated_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    scheduler = _scheduler()
    due = AsyncMock(side_effect=RuntimeError("password=secret"))
    monkeypatch.setattr("catan_bot.scheduler.leaderboard_service.due_daily_leaderboards", due)

    with caplog.at_level(logging.ERROR, logger="catan_bot.scheduler"):
        await scheduler._send_daily_leaderboards(NOW)

    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_send_daily_leaderboards_one_guild_delivery_failure_does_not_block_another(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    failed = _channel(1)
    failed.send.side_effect = discord.HTTPException(
        SimpleNamespace(status=500, reason="secret", text="secret"), "secret"
    )
    succeeded = _channel(2)
    scheduler = _scheduler({(1, 101): failed, (2, 102): succeeded})
    posts = [_leaderboard_post(1, 101), _leaderboard_post(2, 102)]
    monkeypatch.setattr(
        "catan_bot.scheduler.leaderboard_service.due_daily_leaderboards",
        AsyncMock(return_value=posts),
    )
    monkeypatch.setattr(
        "catan_bot.scheduler.formatting.build_leaderboard_post_embed",
        Mock(return_value=discord.Embed()),
    )

    with caplog.at_level(logging.ERROR, logger="catan_bot.scheduler"):
        await scheduler._send_daily_leaderboards(NOW)

    failed.send.assert_awaited_once()
    succeeded.send.assert_awaited_once()
    assert "HTTPException" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_send_daily_leaderboards_skips_unavailable_channel_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _scheduler()  # no channel registered anywhere -> unavailable
    post = _leaderboard_post(1, 101)
    monkeypatch.setattr(
        "catan_bot.scheduler.leaderboard_service.due_daily_leaderboards",
        AsyncMock(return_value=[post]),
    )
    render = Mock(return_value=discord.Embed())
    monkeypatch.setattr("catan_bot.scheduler.formatting.build_leaderboard_post_embed", render)

    await scheduler._send_daily_leaderboards(NOW)

    render.assert_not_called()


# ---------------------------------------------------------------------------
# Score-request chases: `_send_score_prompts` / `_send_score_prompt_group` /
# `_send_score_prompt_dm` (Phase 6 -- claim, DM, and per-game notice).
# ---------------------------------------------------------------------------


def test_group_score_prompts_by_game_preserves_first_seen_order() -> None:
    game7 = _score_game(7, 1)
    game9 = _score_game(9, 1)
    prompts = [
        _due_prompt(game7, 2),
        _due_prompt(game9, 5),
        _due_prompt(game7, 3),
    ]

    groups = _group_score_prompts_by_game(prompts)

    assert [[p.user_id for p in group] for group in groups] == [[2, 3], [5]]


@pytest.mark.asyncio
async def test_send_score_prompts_is_cheap_and_silent_when_nothing_is_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _scheduler()
    due = AsyncMock(return_value=[])
    monkeypatch.setattr("catan_bot.scheduler.game_service.due_score_prompts", due)

    await scheduler._send_score_prompts(NOW)

    due.assert_awaited_once_with(scheduler.pool, NOW, 50)


@pytest.mark.asyncio
async def test_send_score_prompts_claim_failure_is_isolated_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    scheduler = _scheduler()
    due = AsyncMock(side_effect=RuntimeError("password=secret"))
    monkeypatch.setattr("catan_bot.scheduler.game_service.due_score_prompts", due)

    with caplog.at_level(logging.ERROR, logger="catan_bot.scheduler"):
        await scheduler._send_score_prompts(NOW)

    assert "RuntimeError" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.asyncio
async def test_score_prompt_dms_player_and_posts_one_channel_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game = _score_game(7, 1, channel_id=701)
    channel = _channel(1)
    user = _dm_user(2)
    scheduler = _scheduler({(1, 701): channel}, {2: user})
    deliver = AsyncMock()
    monkeypatch.setattr("catan_bot.scheduler.game_service.record_score_request_delivery", deliver)
    embed = discord.Embed(title="Sheet")
    view = discord.ui.View(timeout=None)
    monkeypatch.setattr(
        "catan_bot.scheduler.score_entry.build_score_entry_embed", Mock(return_value=embed)
    )
    monkeypatch.setattr(
        "catan_bot.scheduler.score_entry.build_score_entry_view", Mock(return_value=view)
    )

    await scheduler._send_score_prompt_group([_due_prompt(game, 2)])

    user.send.assert_awaited_once()
    assert user.send.await_args.kwargs["embed"] is embed
    assert user.send.await_args.kwargs["view"] is view
    deliver.assert_awaited_once_with(
        scheduler.pool, 1, 7, 2, channel_id=600, message_id=1002, delivered=True
    )
    channel.send.assert_awaited_once()
    content = channel.send.await_args.args[0]
    assert "<@2>" in content
    assert "game #7" in content
    assert channel.send.await_args.kwargs["allowed_mentions"].to_dict()["users"] == [2]


@pytest.mark.asyncio
async def test_score_prompt_multiple_outstanding_players_produce_one_notice_naming_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game = _score_game(7, 1, channel_id=701)
    channel = _channel(1)
    users = {2: _dm_user(2), 3: _dm_user(3), 4: _dm_user(4)}
    scheduler = _scheduler({(1, 701): channel}, users)
    monkeypatch.setattr(
        "catan_bot.scheduler.game_service.record_score_request_delivery", AsyncMock()
    )
    monkeypatch.setattr(
        "catan_bot.scheduler.score_entry.build_score_entry_embed",
        Mock(return_value=discord.Embed()),
    )
    monkeypatch.setattr(
        "catan_bot.scheduler.score_entry.build_score_entry_view",
        Mock(return_value=discord.ui.View(timeout=None)),
    )
    prompts = [_due_prompt(game, uid) for uid in (2, 3, 4)]

    await scheduler._send_score_prompt_group(prompts)

    for user in users.values():
        user.send.assert_awaited_once()
    channel.send.assert_awaited_once()
    content = channel.send.await_args.args[0]
    for uid in (2, 3, 4):
        assert f"<@{uid}>" in content
    allowed = channel.send.await_args.kwargs["allowed_mentions"].to_dict()
    assert allowed["users"] == [2, 3, 4]
    assert "roles" not in allowed
    assert "everyone" not in allowed["parse"]


@pytest.mark.asyncio
async def test_score_prompt_blocked_dm_is_recorded_and_does_not_stop_other_players(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game = _score_game(7, 1, channel_id=701)
    channel = _channel(1)
    blocked_user = SimpleNamespace(
        id=2,
        send=AsyncMock(
            side_effect=discord.HTTPException(
                SimpleNamespace(status=403, reason="Forbidden"), "closed DMs"
            )
        ),
    )
    ok_user = _dm_user(3)
    scheduler = _scheduler({(1, 701): channel}, {2: blocked_user, 3: ok_user})
    deliver = AsyncMock()
    monkeypatch.setattr("catan_bot.scheduler.game_service.record_score_request_delivery", deliver)
    monkeypatch.setattr(
        "catan_bot.scheduler.score_entry.build_score_entry_embed",
        Mock(return_value=discord.Embed()),
    )
    monkeypatch.setattr(
        "catan_bot.scheduler.score_entry.build_score_entry_view",
        Mock(return_value=discord.ui.View(timeout=None)),
    )

    await scheduler._send_score_prompt_group([_due_prompt(game, 2), _due_prompt(game, 3)])

    blocked_user.send.assert_awaited_once()
    ok_user.send.assert_awaited_once()
    assert deliver.await_count == 2
    deliver.assert_any_await(
        scheduler.pool, 1, 7, 2, channel_id=None, message_id=None, delivered=False
    )
    deliver.assert_any_await(
        scheduler.pool, 1, 7, 3, channel_id=600, message_id=1003, delivered=True
    )
    # The notice still names both outstanding players, blocked or not.
    channel.send.assert_awaited_once()
    content = channel.send.await_args.args[0]
    assert "<@2>" in content
    assert "<@3>" in content


@pytest.mark.asyncio
async def test_score_prompt_one_games_failure_does_not_block_another(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    scheduler = _scheduler()
    game7 = _score_game(7, 1)
    game9 = _score_game(9, 1)
    prompts = [_due_prompt(game7, 2), _due_prompt(game9, 5)]
    monkeypatch.setattr(
        "catan_bot.scheduler.game_service.due_score_prompts", AsyncMock(return_value=prompts)
    )
    send_group = AsyncMock(side_effect=[RuntimeError("password=secret"), None])
    monkeypatch.setattr(scheduler, "_send_score_prompt_group", send_group)

    with caplog.at_level(logging.ERROR, logger="catan_bot.scheduler"):
        await scheduler._send_score_prompts(NOW)

    assert send_group.await_count == 2
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
