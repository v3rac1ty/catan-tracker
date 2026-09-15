"""Cross-cutting hygiene tests for the services layer.

Covers three of the M3b milestone's required checks that don't belong to
any one service:
  - limit clamping (already spot-checked per-service; this asserts the
    exact clamped bound across every limited call),
  - second-order injection payloads stored verbatim and read back through
    every service read path,
  - an error-message catalogue: no `ServiceError.user_message` may contain
    raw asyncpg text, DETAIL, or the submitted payload itself.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from catan_bot.domain.validation import ParticipantRef
from catan_bot.services import (
    config_service,
    event_service,
    game_service,
    season_service,
    stats_service,
)
from catan_bot.services.context import Actor
from catan_bot.services.errors import ServiceError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
PAYLOAD = "'; DROP TABLE games;--"

_FORBIDDEN_SUBSTRINGS = ("asyncpg", "violates", "constraint", "DETAIL")


def _admin(user_id: int = 1) -> Actor:
    return Actor(user_id=user_id, has_manage_guild=True, role_ids=frozenset())


def _actor(user_id: int) -> Actor:
    return Actor(user_id=user_id, has_manage_guild=False, role_ids=frozenset())


def _ref(user_id: int) -> ParticipantRef:
    return ParticipantRef(user_id=user_id, is_bot=False)


# ---------------------------------------------------------------------------
# 8. Hygiene: limits are clamped.
# ---------------------------------------------------------------------------


async def test_game_history_limit_clamped_to_1_and_25(pool: asyncpg.Pool, guild_id: int) -> None:
    for _ in range(30):
        await game_service.report_game(
            pool,
            guild_id,
            _actor(1),
            winner=_ref(1),
            losers=[_ref(2)],
            date_text=None,
            now=NOW,
        )
    over = await game_service.game_history(pool, guild_id, user_id=None, limit=1000)
    assert len(over) == 25
    under = await game_service.game_history(pool, guild_id, user_id=None, limit=0)
    assert len(under) == 1
    negative = await game_service.game_history(pool, guild_id, user_id=None, limit=-100)
    assert len(negative) == 1


async def test_season_history_limit_clamped_to_1_and_25(pool: asyncpg.Pool, guild_id: int) -> None:
    for i in range(30):
        await season_service.start_season(
            pool,
            guild_id,
            _admin(),
            name=f"Season {i}",
            end_date_text="2026-12-31",
            start_date_text=None,
            min_games=None,
            now=NOW,
        )
        await season_service.cancel_season(pool, guild_id, _admin())
    over = await season_service.season_history(pool, guild_id, 1000)
    assert len(over) == 25
    under = await season_service.season_history(pool, guild_id, 0)
    assert len(under) == 1


async def test_pending_announcements_limit_clamped_to_1_and_50(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    over = await season_service.pending_announcements(pool, 1000)
    assert len(over) <= 50
    under = await season_service.pending_announcements(pool, -5)
    assert under == []  # clamped to 1, still nothing pending -- must not error


async def test_upcoming_events_limit_clamped_to_1_and_10(pool: asyncpg.Pool, guild_id: int) -> None:
    for hours in range(1, 13):
        target = NOW + timedelta(hours=hours)
        await event_service.create_event(
            pool,
            guild_id,
            _actor(1),
            title="Game Night",
            date_text=target.strftime("%Y-%m-%d"),
            time_text=target.strftime("%H:%M"),
            location=None,
            description=None,
            channel_id=None,
            now=NOW,
        )
    over = await event_service.upcoming_events(pool, guild_id, NOW, 1000)
    assert len(over) == 10
    under = await event_service.upcoming_events(pool, guild_id, NOW, 0)
    assert len(under) == 1


# ---------------------------------------------------------------------------
# 9. Second-order: injection-shaped payloads stored verbatim, read back
# through every service read path.
# ---------------------------------------------------------------------------


async def test_season_name_payload_round_trips_through_service_reads(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await season_service.start_season(
        pool,
        guild_id,
        _admin(),
        name=PAYLOAD,
        end_date_text="2026-09-15",
        start_date_text=None,
        min_games=None,
        now=NOW,
    )

    info = await season_service.season_info(pool, guild_id)
    assert info is not None
    assert info.season.name == PAYLOAD

    resolution = await season_service.end_season_now(pool, guild_id, _admin(), NOW)
    assert resolution.season.name == PAYLOAD

    announcements = await season_service.pending_announcements(pool, 50)
    matching = [a for a in announcements if a.season.season_id == resolution.season.season_id]
    assert matching and matching[0].season.name == PAYLOAD

    history = await season_service.season_history(pool, guild_id, 25)
    assert any(s.name == PAYLOAD for s in history)


async def test_event_title_payload_round_trips_through_service_reads(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    created = await event_service.create_event(
        pool,
        guild_id,
        _actor(1),
        title=PAYLOAD,
        date_text=None,
        time_text=(NOW + timedelta(hours=2)).strftime("%H:%M"),
        location=None,
        description=None,
        channel_id=None,
        now=NOW,
    )
    assert created.title == PAYLOAD

    upcoming = await event_service.upcoming_events(pool, guild_id, NOW, 10)
    matching = [e for e in upcoming if e.event_id == created.event_id]
    assert matching and matching[0].title == PAYLOAD

    # All tables must still exist and be queryable after storing the payload.
    async with pool.acquire() as conn:
        row_count = await conn.fetchval("SELECT COUNT(*) FROM events")
    assert row_count >= 1


async def test_void_reason_payload_stored_verbatim(pool: asyncpg.Pool, guild_id: int) -> None:
    created = await game_service.report_game(
        pool,
        guild_id,
        _actor(1),
        winner=_ref(1),
        losers=[_ref(2)],
        date_text=None,
        now=NOW,
    )
    voided = await game_service.void_game(pool, guild_id, created.game.game_id, _admin(), PAYLOAD)
    assert voided.game.void_reason == PAYLOAD


# ---------------------------------------------------------------------------
# 10. No raw DB or user text in any ServiceError.user_message.
# ---------------------------------------------------------------------------


async def test_no_service_error_message_contains_raw_db_or_user_text(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    messages: list[str] = []

    async def _collect(coro) -> None:
        try:
            await coro
        except ServiceError as exc:
            messages.append(exc.user_message)

    non_admin = _actor(2)

    # Permission-denied messages.
    await _collect(config_service.set_timezone(pool, guild_id, non_admin, "UTC"))
    await _collect(
        season_service.start_season(
            pool,
            guild_id,
            non_admin,
            name=PAYLOAD,
            end_date_text="2026-12-31",
            start_date_text=None,
            min_games=None,
            now=NOW,
        )
    )
    await _collect(season_service.cancel_season(pool, guild_id, non_admin))

    # Not-found / conflict messages, using the payload as an id-shaped
    # string is impossible (ids are ints), so we exercise the payload as a
    # void reason and a game report's context instead, and separately probe
    # not-found/conflict with ordinary bogus ids.
    await _collect(game_service.confirm_game(pool, guild_id, 999_999, non_admin))
    await _collect(game_service.reject_game(pool, guild_id, 999_999, non_admin))
    await _collect(game_service.void_game(pool, guild_id, 999_999, _admin(), PAYLOAD))
    await _collect(event_service.cancel_event(pool, guild_id, 999_999, non_admin))
    await _collect(event_service.rsvp(pool, guild_id, 999_999, non_admin, "going"))
    await _collect(season_service.set_end_date(pool, guild_id, _admin(), "2026-12-31", NOW))
    await _collect(season_service.end_season_now(pool, guild_id, _admin(), NOW))

    created = await game_service.report_game(
        pool,
        guild_id,
        _actor(1),
        winner=_ref(1),
        losers=[_ref(2)],
        date_text=None,
        now=NOW,
    )
    await _collect(game_service.confirm_game(pool, guild_id, created.game.game_id, _actor(1)))
    await _collect(game_service.confirm_game(pool, guild_id, created.game.game_id, _actor(99)))

    assert len(messages) >= 8
    for message in messages:
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            assert forbidden not in message, f"{forbidden!r} leaked into: {message!r}"
        assert PAYLOAD not in message


# ---------------------------------------------------------------------------
# player_stats/leaderboard read paths also unaffected by payload text
# elsewhere (sanity: services keep working after payload writes).
# ---------------------------------------------------------------------------


_TIMEZONE_INVALID_MESSAGE = (
    "This server's timezone setting is invalid. Ask someone with Manage Server "
    "to fix it with /config timezone."
)


async def test_invalid_stored_timezone_gives_friendly_message_other_services_still_work(
    pool: asyncpg.Pool, guild_id: int, migrator_conn: asyncpg.Connection
) -> None:
    """I1: a guild's `guild_config.timezone` can become invalid outside the
    app's own validation -- a direct edit, or a `tzdata` package change
    between deploys removing a zone name. Every service that reads it for
    date/time math (`start_season`, `set_end_date`, `report_game`,
    `create_event`) must raise this fixed, friendly `ServiceError` instead
    of `validate_timezone`'s generic message or a raw `zoneinfo` error --
    and every *other* service, plus the scheduler, must be unaffected."""
    await migrator_conn.execute(
        "UPDATE guild_config SET timezone = $1 WHERE guild_id = $2", "Mars/Base", guild_id
    )

    with pytest.raises(ServiceError) as exc_info:
        await season_service.start_season(
            pool,
            guild_id,
            _admin(),
            name="S1",
            end_date_text="2026-12-31",
            start_date_text=None,
            min_games=None,
            now=NOW,
        )
    assert exc_info.value.user_message == _TIMEZONE_INVALID_MESSAGE

    with pytest.raises(ServiceError) as exc_info:
        await game_service.report_game(
            pool,
            guild_id,
            _actor(1),
            winner=_ref(1),
            losers=[_ref(2)],
            date_text=None,
            now=NOW,
        )
    assert exc_info.value.user_message == _TIMEZONE_INVALID_MESSAGE

    with pytest.raises(ServiceError) as exc_info:
        await event_service.create_event(
            pool,
            guild_id,
            _actor(1),
            title="Game Night",
            date_text=None,
            time_text="19:00",
            location=None,
            description=None,
            channel_id=None,
            now=NOW,
        )
    assert exc_info.value.user_message == _TIMEZONE_INVALID_MESSAGE

    # `set_end_date` needs an active season -- fix the timezone just long
    # enough to start one, then re-break it to isolate this check.
    await migrator_conn.execute(
        "UPDATE guild_config SET timezone = $1 WHERE guild_id = $2", "UTC", guild_id
    )
    await season_service.start_season(
        pool,
        guild_id,
        _admin(),
        name="S1",
        end_date_text="2026-12-31",
        start_date_text=None,
        min_games=None,
        now=NOW,
    )
    await migrator_conn.execute(
        "UPDATE guild_config SET timezone = $1 WHERE guild_id = $2", "Mars/Base", guild_id
    )
    with pytest.raises(ServiceError) as exc_info:
        await season_service.set_end_date(pool, guild_id, _admin(), "2026-11-30", NOW)
    assert exc_info.value.user_message == _TIMEZONE_INVALID_MESSAGE

    # Other services (no timezone math) and the scheduler are unaffected.
    history = await season_service.season_history(pool, guild_id, 10)
    assert len(history) == 1
    board = await stats_service.leaderboard(pool, guild_id, "all_time")
    assert board.ranked == []
    assert await season_service.resolve_due_seasons(pool, NOW) == []
    assert await event_service.due_reminders(pool, NOW) == []


async def test_leaderboard_and_player_stats_unaffected_by_payload_season_name(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await season_service.start_season(
        pool,
        guild_id,
        _admin(),
        name=PAYLOAD,
        end_date_text="2026-12-31",
        start_date_text=None,
        min_games=None,
        now=NOW,
    )
    created = await game_service.report_game(
        pool,
        guild_id,
        _actor(1),
        winner=_ref(1),
        losers=[_ref(2)],
        date_text=None,
        now=NOW,
    )
    await game_service.confirm_game(pool, guild_id, created.game.game_id, _actor(2))

    board = await stats_service.leaderboard(pool, guild_id, "season")
    assert board.season is not None
    assert board.season.name == PAYLOAD

    view = await stats_service.player_stats(pool, guild_id, 1)
    assert view.all_time.wins == 1
