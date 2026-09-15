"""Integration tests for `catan_bot.services.game_service`."""

from __future__ import annotations

import os
from datetime import UTC, datetime

import asyncpg
import pytest

from catan_bot.db.repositories import games as games_repo
from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.validation import ParticipantRef
from catan_bot.services import config_service, game_service, season_service, stats_service
from catan_bot.services.context import Actor
from catan_bot.services.errors import ConflictError, NotFoundError, PermissionDeniedError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

NOW = datetime(2026, 9, 14, 4, 30, tzinfo=UTC)


def _actor(user_id: int, *, admin: bool = False, role_ids: frozenset[int] = frozenset()) -> Actor:
    return Actor(user_id=user_id, has_manage_guild=admin, role_ids=role_ids)


def _ref(user_id: int, *, is_bot: bool = False) -> ParticipantRef:
    return ParticipantRef(user_id=user_id, is_bot=is_bot)


async def _report(
    pool: asyncpg.Pool, guild_id: int, *, reporter: int, winner: int, losers: list[int]
):
    return await game_service.report_game(
        pool,
        guild_id,
        _actor(reporter),
        winner=_ref(winner),
        losers=[_ref(u) for u in losers],
        date_text=None,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# 1. Report -> confirm by another player -> leaderboard updates.
# ---------------------------------------------------------------------------


async def test_report_confirm_updates_leaderboard(pool: asyncpg.Pool, guild_id: int) -> None:
    created = await _report(pool, guild_id, reporter=1, winner=1, losers=[2])
    assert created.game.status == "pending"
    assert created.winner_id == 1
    assert created.loser_ids == (2,)

    confirmed = await game_service.confirm_game(pool, guild_id, created.game.game_id, _actor(2))
    assert confirmed.game.status == "confirmed"
    assert confirmed.game.confirmed_by == 2

    board = await stats_service.leaderboard(pool, guild_id, "all_time")
    ranked_ids = {p.user_id: p for p in board.ranked}
    assert ranked_ids[1].wins == 1
    assert ranked_ids[2].wins == 0


async def test_reporter_confirming_own_report_is_refused(pool: asyncpg.Pool, guild_id: int) -> None:
    created = await _report(pool, guild_id, reporter=1, winner=1, losers=[2])
    with pytest.raises(PermissionDeniedError) as exc_info:
        await game_service.confirm_game(pool, guild_id, created.game.game_id, _actor(1))
    assert "reported this game" in exc_info.value.user_message


async def test_non_participant_confirming_is_refused(pool: asyncpg.Pool, guild_id: int) -> None:
    created = await _report(pool, guild_id, reporter=1, winner=1, losers=[2])
    with pytest.raises(PermissionDeniedError) as exc_info:
        await game_service.confirm_game(pool, guild_id, created.game.game_id, _actor(99))
    assert exc_info.value.user_message


async def test_confirm_unknown_game_raises_not_found(pool: asyncpg.Pool, guild_id: int) -> None:
    with pytest.raises(NotFoundError):
        await game_service.confirm_game(pool, guild_id, 999_999, _actor(1))


async def test_confirm_already_confirmed_game_raises_conflict(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    created = await _report(pool, guild_id, reporter=1, winner=1, losers=[2])
    await game_service.confirm_game(pool, guild_id, created.game.game_id, _actor(2))
    with pytest.raises(ConflictError):
        await game_service.confirm_game(pool, guild_id, created.game.game_id, _actor(2))


# ---------------------------------------------------------------------------
# Reject: reporter retraction, participant reject, non-participant refused.
# ---------------------------------------------------------------------------


async def test_reject_by_participant_succeeds(pool: asyncpg.Pool, guild_id: int) -> None:
    created = await _report(pool, guild_id, reporter=1, winner=1, losers=[2])
    rejected = await game_service.reject_game(pool, guild_id, created.game.game_id, _actor(2))
    assert rejected.game.status == "rejected"


async def test_reporter_can_retract_own_report_even_if_not_a_participant(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    """M3a audit decision: a reporter who isn't a participant can still
    retract (reject) their own pending report."""
    created = await _report(pool, guild_id, reporter=3, winner=1, losers=[2])
    rejected = await game_service.reject_game(pool, guild_id, created.game.game_id, _actor(3))
    assert rejected.game.status == "rejected"
    assert rejected.game.rejected_by == 3


async def test_reject_by_non_participant_non_reporter_is_refused(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    created = await _report(pool, guild_id, reporter=1, winner=1, losers=[2])
    with pytest.raises(PermissionDeniedError):
        await game_service.reject_game(pool, guild_id, created.game.game_id, _actor(99))


async def test_reject_unknown_game_raises_not_found(pool: asyncpg.Pool, guild_id: int) -> None:
    with pytest.raises(NotFoundError):
        await game_service.reject_game(pool, guild_id, 999_999, _actor(1))


async def test_reject_already_rejected_raises_conflict(pool: asyncpg.Pool, guild_id: int) -> None:
    created = await _report(pool, guild_id, reporter=1, winner=1, losers=[2])
    await game_service.reject_game(pool, guild_id, created.game.game_id, _actor(2))
    with pytest.raises(ConflictError):
        await game_service.reject_game(pool, guild_id, created.game.game_id, _actor(2))


# ---------------------------------------------------------------------------
# Void: admin-only, via Manage Server or the configured admin role.
# ---------------------------------------------------------------------------


async def test_void_by_non_admin_is_refused(pool: asyncpg.Pool, guild_id: int) -> None:
    created = await _report(pool, guild_id, reporter=1, winner=1, losers=[2])
    with pytest.raises(PermissionDeniedError):
        await game_service.void_game(pool, guild_id, created.game.game_id, _actor(99), "cheating")


async def test_void_via_manage_guild(pool: asyncpg.Pool, guild_id: int) -> None:
    created = await _report(pool, guild_id, reporter=1, winner=1, losers=[2])
    voided = await game_service.void_game(
        pool, guild_id, created.game.game_id, _actor(1, admin=True), "bad report"
    )
    assert voided.game.status == "voided"
    assert voided.game.void_reason == "bad report"


async def test_void_via_configured_admin_role(pool: asyncpg.Pool, guild_id: int) -> None:
    await config_service.set_admin_role(pool, guild_id, _actor(1, admin=True), role_id=42)
    created = await _report(pool, guild_id, reporter=1, winner=1, losers=[2])
    admin_role_actor = _actor(5, admin=False, role_ids=frozenset({42}))
    voided = await game_service.void_game(
        pool, guild_id, created.game.game_id, admin_role_actor, ""
    )
    assert voided.game.status == "voided"
    assert voided.game.void_reason is None  # empty text -> None, per clean_text(min_len=0)


async def test_void_unknown_game_raises_not_found(pool: asyncpg.Pool, guild_id: int) -> None:
    with pytest.raises(NotFoundError):
        await game_service.void_game(pool, guild_id, 999_999, _actor(1, admin=True), None)


async def test_void_already_voided_raises_conflict(pool: asyncpg.Pool, guild_id: int) -> None:
    created = await _report(pool, guild_id, reporter=1, winner=1, losers=[2])
    await game_service.void_game(pool, guild_id, created.game.game_id, _actor(1, admin=True), None)
    with pytest.raises(ConflictError):
        await game_service.void_game(
            pool, guild_id, created.game.game_id, _actor(1, admin=True), None
        )


async def test_void_reason_too_long_raises_domain_error(pool: asyncpg.Pool, guild_id: int) -> None:
    created = await _report(pool, guild_id, reporter=1, winner=1, losers=[2])
    with pytest.raises(DomainValidationError):
        await game_service.void_game(
            pool, guild_id, created.game.game_id, _actor(1, admin=True), "x" * 500
        )


# ---------------------------------------------------------------------------
# Config changes by an admin-role holder (without Manage Server) refused.
# ---------------------------------------------------------------------------


async def test_config_change_by_admin_role_holder_without_manage_guild_is_refused(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await config_service.set_admin_role(pool, guild_id, _actor(1, admin=True), role_id=42)
    admin_role_actor = _actor(5, admin=False, role_ids=frozenset({42}))
    with pytest.raises(PermissionDeniedError):
        await config_service.set_timezone(pool, guild_id, admin_role_actor, "America/Chicago")


# ---------------------------------------------------------------------------
# 2. Date defaulting, future dates, season window, participants.
# ---------------------------------------------------------------------------


async def test_report_game_with_no_date_uses_today_in_guild_timezone(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    """`now` = 2026-09-14 04:30 UTC in `America/Chicago` (UTC-5 in September)
    is still 2026-09-13 locally."""
    await config_service.set_timezone(pool, guild_id, _actor(1, admin=True), "America/Chicago")
    created = await game_service.report_game(
        pool,
        guild_id,
        _actor(1),
        winner=_ref(1),
        losers=[_ref(2)],
        date_text=None,
        now=NOW,
    )
    assert created.game.played_on.isoformat() == "2026-09-13"


async def test_report_game_future_date_is_refused(pool: asyncpg.Pool, guild_id: int) -> None:
    with pytest.raises(DomainValidationError):
        await game_service.report_game(
            pool,
            guild_id,
            _actor(1),
            winner=_ref(1),
            losers=[_ref(2)],
            date_text="2099-01-01",
            now=NOW,
        )


async def test_report_game_date_outside_active_season_is_refused(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await season_service.start_season(
        pool,
        guild_id,
        _actor(1, admin=True),
        name="S1",
        end_date_text="2026-09-30",
        start_date_text="2026-09-10",
        min_games=None,
        now=NOW,
    )
    with pytest.raises(DomainValidationError):
        await game_service.report_game(
            pool,
            guild_id,
            _actor(1),
            winner=_ref(1),
            losers=[_ref(2)],
            date_text="2026-09-05",
            now=NOW,
        )


async def test_report_game_inside_active_season_assigns_season_id(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    season = await season_service.start_season(
        pool,
        guild_id,
        _actor(1, admin=True),
        name="S1",
        end_date_text="2026-09-30",
        start_date_text="2026-09-10",
        min_games=None,
        now=NOW,
    )
    created = await game_service.report_game(
        pool,
        guild_id,
        _actor(1),
        winner=_ref(1),
        losers=[_ref(2)],
        date_text="2026-09-12",
        now=NOW,
    )
    assert created.game.season_id == season.season_id


async def test_report_game_without_active_season_has_no_season_id(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    created = await _report(pool, guild_id, reporter=1, winner=1, losers=[2])
    assert created.game.season_id is None


async def test_report_game_bot_participant_is_refused_and_writes_nothing(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    with pytest.raises(DomainValidationError):
        await game_service.report_game(
            pool,
            guild_id,
            _actor(1),
            winner=_ref(1, is_bot=True),
            losers=[_ref(2)],
            date_text=None,
            now=NOW,
        )
    history = await game_service.game_history(pool, guild_id, user_id=None, limit=10)
    assert history == []


async def test_report_game_duplicate_players_is_refused_and_writes_nothing(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    with pytest.raises(DomainValidationError):
        await game_service.report_game(
            pool,
            guild_id,
            _actor(1),
            winner=_ref(1),
            losers=[_ref(1)],
            date_text=None,
            now=NOW,
        )
    history = await game_service.game_history(pool, guild_id, user_id=None, limit=10)
    assert history == []


async def test_report_game_reporter_need_not_be_a_participant(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    created = await _report(pool, guild_id, reporter=99, winner=1, losers=[2])
    assert created.game.reported_by == 99
    assert 99 not in (created.winner_id, *created.loser_ids)


# ---------------------------------------------------------------------------
# record_game_message / game_history (limits).
# ---------------------------------------------------------------------------


async def test_record_game_message_persists(pool: asyncpg.Pool, guild_id: int) -> None:
    created = await _report(pool, guild_id, reporter=1, winner=1, losers=[2])
    await game_service.record_game_message(pool, guild_id, created.game.game_id, 111, 222)

    async with pool.acquire() as conn:
        loaded = await games_repo.get_game(conn, guild_id, created.game.game_id)
    assert loaded is not None
    assert loaded.game.channel_id == 111
    assert loaded.game.message_id == 222


async def test_game_history_filters_by_user_and_clamps_limit(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    for _ in range(3):
        await _report(pool, guild_id, reporter=1, winner=1, losers=[2])
    for _ in range(2):
        await _report(pool, guild_id, reporter=3, winner=3, losers=[4])

    all_history = await game_service.game_history(pool, guild_id, user_id=None, limit=1000)
    assert len(all_history) <= 25
    assert len(all_history) == 5

    only_player_1 = await game_service.game_history(pool, guild_id, user_id=1, limit=1000)
    assert len(only_player_1) == 3

    clamped_low = await game_service.game_history(pool, guild_id, user_id=None, limit=0)
    assert len(clamped_low) == 1
    clamped_negative = await game_service.game_history(pool, guild_id, user_id=None, limit=-5)
    assert len(clamped_negative) == 1
