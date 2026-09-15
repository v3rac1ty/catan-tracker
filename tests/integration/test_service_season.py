"""Integration tests for `catan_bot.services.season_service`."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, date, datetime, timedelta

import asyncpg
import pytest

from catan_bot.db.repositories import guilds as guilds_repo
from catan_bot.db.repositories import seasons as seasons_repo
from catan_bot.domain.dates import season_end_instant
from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.validation import ParticipantRef
from catan_bot.services import config_service, game_service, season_service
from catan_bot.services.context import Actor
from catan_bot.services.errors import ConflictError, PermissionDeniedError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

NOW = datetime(2026, 9, 14, 4, 30, tzinfo=UTC)


def _admin(user_id: int = 1) -> Actor:
    return Actor(user_id=user_id, has_manage_guild=True, role_ids=frozenset())


def _actor(user_id: int) -> Actor:
    return Actor(user_id=user_id, has_manage_guild=False, role_ids=frozenset())


def _ref(user_id: int) -> ParticipantRef:
    return ParticipantRef(user_id=user_id, is_bot=False)


async def _start(pool: asyncpg.Pool, guild_id: int, **overrides):
    kwargs = {
        "name": "Season One",
        "end_date_text": "2026-12-31",
        "start_date_text": None,
        "min_games": None,
        "now": NOW,
    }
    kwargs.update(overrides)
    return await season_service.start_season(pool, guild_id, _admin(), **kwargs)


async def _confirmed_game(
    pool: asyncpg.Pool, guild_id: int, winner: int, loser: int, *, date_text: str
):
    created = await game_service.report_game(
        pool,
        guild_id,
        _actor(winner),
        winner=_ref(winner),
        losers=[_ref(loser)],
        date_text=date_text,
        now=NOW,
    )
    await game_service.confirm_game(pool, guild_id, created.game.game_id, _actor(loser))


# ---------------------------------------------------------------------------
# 3. start_season defaults, atomic set_min_games, conflicts, past end date.
# ---------------------------------------------------------------------------


async def test_start_season_defaults_start_date_and_min_games(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await config_service.set_timezone(pool, guild_id, _admin(), "America/Chicago")
    season = await _start(pool, guild_id)
    assert season.starts_on.isoformat() == "2026-09-13"  # today in America/Chicago
    assert season.min_games == 2


async def test_start_season_uses_explicit_min_games(pool: asyncpg.Pool, guild_id: int) -> None:
    season = await _start(pool, guild_id, min_games=5)
    assert season.min_games == 5


async def test_start_season_requires_admin(pool: asyncpg.Pool, guild_id: int) -> None:
    with pytest.raises(PermissionDeniedError):
        await season_service.start_season(
            pool,
            guild_id,
            _actor(2),
            name="Nope",
            end_date_text="2026-12-31",
            start_date_text=None,
            min_games=None,
            now=NOW,
        )


async def test_start_season_missing_end_date_raises_domain_error(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    with pytest.raises(DomainValidationError):
        await season_service.start_season(
            pool,
            guild_id,
            _admin(),
            name="Nope",
            end_date_text=None,
            start_date_text=None,
            min_games=None,
            now=NOW,
        )
    with pytest.raises(DomainValidationError):
        await season_service.start_season(
            pool,
            guild_id,
            _admin(),
            name="Nope",
            end_date_text="   ",
            start_date_text=None,
            min_games=None,
            now=NOW,
        )


async def test_start_season_end_date_in_the_past_is_refused(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    with pytest.raises(DomainValidationError):
        await _start(pool, guild_id, end_date_text="2020-01-01")


async def test_second_start_season_raises_conflict(pool: asyncpg.Pool, guild_id: int) -> None:
    await _start(pool, guild_id)
    with pytest.raises(ConflictError):
        await _start(pool, guild_id, name="Season Two")


async def test_set_min_games_updates_default_and_active_season(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await _start(pool, guild_id, min_games=2)
    season, config = await season_service.set_min_games(pool, guild_id, _admin(), 5)
    assert season is not None
    assert season.min_games == 5
    assert config.default_min_games == 5


async def test_set_min_games_without_active_season_still_updates_default(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    season, config = await season_service.set_min_games(pool, guild_id, _admin(), 7)
    assert season is None
    assert config.default_min_games == 7


async def test_set_min_games_requires_admin(pool: asyncpg.Pool, guild_id: int) -> None:
    with pytest.raises(PermissionDeniedError):
        await season_service.set_min_games(pool, guild_id, _actor(2), 5)


async def test_set_min_games_is_atomic_across_default_and_active_season(
    pool: asyncpg.Pool, guild_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the second write (the active season) to fail, and prove the
    guild default's write rolled back too -- both happen in one transaction."""
    await _start(pool, guild_id, min_games=2)

    async def _boom(conn, guild_id_arg, n):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(season_service.seasons, "set_active_min_games", _boom)

    with pytest.raises(RuntimeError):
        await season_service.set_min_games(pool, guild_id, _admin(), 9)

    async with pool.acquire() as conn:
        config = await guilds_repo.get_guild(conn, guild_id)
        active = await seasons_repo.get_active_season(conn, guild_id)
    assert config is not None
    assert config.default_min_games == 2
    assert active is not None
    assert active.min_games == 2


# ---------------------------------------------------------------------------
# set_end_date
# ---------------------------------------------------------------------------


async def test_set_end_date_success(pool: asyncpg.Pool, guild_id: int) -> None:
    await _start(pool, guild_id, start_date_text="2026-09-01", end_date_text="2026-12-31")
    updated = await season_service.set_end_date(pool, guild_id, _admin(), "2026-10-31", NOW)
    assert updated.ends_on.isoformat() == "2026-10-31"


async def test_set_end_date_without_active_season_raises_conflict(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    with pytest.raises(ConflictError):
        await season_service.set_end_date(pool, guild_id, _admin(), "2026-10-31", NOW)


async def test_set_end_date_before_start_raises_domain_error(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await _start(pool, guild_id, start_date_text="2026-09-01", end_date_text="2026-12-31")
    with pytest.raises(DomainValidationError):
        await season_service.set_end_date(pool, guild_id, _admin(), "2026-08-01", NOW)


async def test_set_end_date_requires_admin(pool: asyncpg.Pool, guild_id: int) -> None:
    await _start(pool, guild_id)
    with pytest.raises(PermissionDeniedError):
        await season_service.set_end_date(pool, guild_id, _actor(2), "2026-10-31", NOW)


async def test_set_end_date_blank_text_raises_domain_error(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await _start(pool, guild_id)
    with pytest.raises(DomainValidationError):
        await season_service.set_end_date(pool, guild_id, _admin(), "   ", NOW)


async def test_set_end_date_race_after_active_check_raises_conflict_via_monkeypatch(
    pool: asyncpg.Pool, guild_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers the (very rare) race window between `get_active_season` and
    `set_active_end` in the same transaction: another actor cancels the
    season in between, so the guarded UPDATE affects zero rows."""
    await _start(pool, guild_id, start_date_text="2026-09-01", end_date_text="2026-12-31")

    async def _none(conn, guild_id_arg, ends_on, ends_at):
        return None

    monkeypatch.setattr(season_service.seasons, "set_active_end", _none)
    with pytest.raises(ConflictError):
        await season_service.set_end_date(pool, guild_id, _admin(), "2026-10-31", NOW)


# ---------------------------------------------------------------------------
# cancel_season
# ---------------------------------------------------------------------------


async def test_cancel_season_success(pool: asyncpg.Pool, guild_id: int) -> None:
    season = await _start(pool, guild_id)
    cancelled = await season_service.cancel_season(pool, guild_id, _admin())
    assert cancelled.season_id == season.season_id
    assert cancelled.status == "cancelled"


async def test_cancel_season_without_active_season_raises_conflict(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    with pytest.raises(ConflictError):
        await season_service.cancel_season(pool, guild_id, _admin())


async def test_cancel_season_requires_admin(pool: asyncpg.Pool, guild_id: int) -> None:
    await _start(pool, guild_id)
    with pytest.raises(PermissionDeniedError):
        await season_service.cancel_season(pool, guild_id, _actor(2))


# ---------------------------------------------------------------------------
# 4. end_season_now: payer/payee, frozen results, second call conflicts.
# ---------------------------------------------------------------------------


async def test_end_season_now_resolves_payer_and_payee_with_ineligible_player(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await _start(pool, guild_id, min_games=2, start_date_text="2026-09-01")
    # Player 1: 2 wins (best). Player 2: 1 win 1 loss. Player 3: 0 wins 2 losses (worst, eligible).
    # Player 4: 1 win but only 1 game total -> ineligible (min_games=2).
    await _confirmed_game(pool, guild_id, winner=1, loser=2, date_text="2026-09-10")
    await _confirmed_game(pool, guild_id, winner=1, loser=3, date_text="2026-09-10")
    await _confirmed_game(pool, guild_id, winner=2, loser=3, date_text="2026-09-11")
    await _confirmed_game(pool, guild_id, winner=4, loser=1, date_text="2026-09-11")

    resolution = await season_service.end_season_now(pool, guild_id, _admin(), NOW)
    assert resolution.completed is True
    assert resolution.outcome.status == "resolved"
    assert resolution.outcome.payees == (1,)
    assert resolution.outcome.payers == (3,)

    ranked_by_id = {p.user_id: p for p in resolution.ranked}
    assert ranked_by_id[4].eligible is False

    results = await season_service.season_results(pool, guild_id, resolution.season.season_id)
    assert {r.user_id for r in results} == {1, 2, 3, 4}
    payee_row = next(r for r in results if r.user_id == 1)
    payer_row = next(r for r in results if r.user_id == 3)
    assert payee_row.outcome == "payee"
    assert payer_row.outcome == "payer"
    ineligible_row = next(r for r in results if r.user_id == 4)
    assert ineligible_row.eligible is False
    assert ineligible_row.outcome is None


async def test_frozen_results_unchanged_after_late_confirm_of_in_season_game(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    """M3b rule 7: a pending in-season game confirmed *after* resolution
    counts toward all-time stats only -- `season_results` stays frozen."""
    await _start(pool, guild_id, min_games=2, start_date_text="2026-09-01")
    await _confirmed_game(pool, guild_id, winner=1, loser=2, date_text="2026-09-10")
    await _confirmed_game(pool, guild_id, winner=2, loser=1, date_text="2026-09-11")

    pending = await game_service.report_game(
        pool,
        guild_id,
        _actor(1),
        winner=_ref(1),
        losers=[_ref(2)],
        date_text="2026-09-12",
        now=NOW,
    )

    resolution = await season_service.end_season_now(pool, guild_id, _admin(), NOW)
    before = await season_service.season_results(pool, guild_id, resolution.season.season_id)
    before_by_id = {r.user_id: (r.games, r.wins) for r in before}

    await game_service.confirm_game(pool, guild_id, pending.game.game_id, _actor(2))

    after = await season_service.season_results(pool, guild_id, resolution.season.season_id)
    after_by_id = {r.user_id: (r.games, r.wins) for r in after}
    assert after_by_id == before_by_id


async def test_second_end_season_now_raises_conflict(pool: asyncpg.Pool, guild_id: int) -> None:
    await _start(pool, guild_id)
    await season_service.end_season_now(pool, guild_id, _admin(), NOW)
    with pytest.raises(ConflictError):
        await season_service.end_season_now(pool, guild_id, _admin(), NOW)


async def test_end_season_now_without_active_season_raises_conflict(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    with pytest.raises(ConflictError):
        await season_service.end_season_now(pool, guild_id, _admin(), NOW)


async def test_end_season_now_requires_admin(pool: asyncpg.Pool, guild_id: int) -> None:
    await _start(pool, guild_id)
    with pytest.raises(PermissionDeniedError):
        await season_service.end_season_now(pool, guild_id, _actor(2), NOW)


async def test_resolve_already_completed_season_raises_conflict_via_monkeypatch(
    pool: asyncpg.Pool, guild_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers `_resolve`'s `if not completed: raise ConflictError` branch,
    which only fires under a race that's otherwise very hard to trigger."""
    await _start(pool, guild_id)

    async def _false(conn, guild_id_arg, season_id, results):
        return False

    monkeypatch.setattr(season_service.seasons, "complete_season", _false)
    with pytest.raises(ConflictError):
        await season_service.end_season_now(pool, guild_id, _admin(), NOW)


# ---------------------------------------------------------------------------
# 5. resolve_due_seasons: per-guild failure isolation.
# ---------------------------------------------------------------------------


async def test_resolve_due_seasons_isolates_one_guilds_failure(
    pool: asyncpg.Pool, guild_id: int, other_guild_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    due_now = datetime(2026, 6, 1, tzinfo=UTC)
    healthy_season = await _start(
        pool,
        guild_id,
        start_date_text="2026-01-01",
        end_date_text="2026-05-31",
        now=due_now - timedelta(days=200),
    )
    failing_season = await _start(
        pool,
        other_guild_id,
        start_date_text="2026-01-01",
        end_date_text="2026-05-31",
        now=due_now - timedelta(days=200),
    )

    # `_resolve` is the shared per-season resolution helper; raise only for
    # the one guild's season we want to fail, so the other guild's season
    # still resolves via the real implementation.
    real_resolve = season_service._resolve

    async def _resolve_maybe_fail(conn, season):
        if season.season_id == failing_season.season_id:
            raise RuntimeError("simulated failure for this guild")
        return await real_resolve(conn, season)

    monkeypatch.setattr(season_service, "_resolve", _resolve_maybe_fail)

    resolved = await asyncio.wait_for(season_service.resolve_due_seasons(pool, due_now), timeout=5)

    assert {r.season.season_id for r in resolved} == {healthy_season.season_id}

    async with pool.acquire() as conn:
        healthy_after = await seasons_repo.get_season(conn, guild_id, healthy_season.season_id)
        failing_after = await seasons_repo.get_season(
            conn, other_guild_id, failing_season.season_id
        )
    assert healthy_after is not None
    assert healthy_after.status == "completed"
    assert failing_after is not None
    assert failing_after.status == "active"


async def test_resolve_due_seasons_second_call_is_noop_for_resolved_and_retries_failed(
    pool: asyncpg.Pool, guild_id: int, other_guild_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    due_now = datetime(2026, 6, 1, tzinfo=UTC)
    healthy_season = await _start(
        pool,
        guild_id,
        start_date_text="2026-01-01",
        end_date_text="2026-05-31",
        now=due_now - timedelta(days=200),
    )
    failing_season = await _start(
        pool,
        other_guild_id,
        start_date_text="2026-01-01",
        end_date_text="2026-05-31",
        now=due_now - timedelta(days=200),
    )

    real_resolve = season_service._resolve
    call_count = {"n": 0}

    async def _fail_once_for_other_guild(conn, season):
        if season.season_id == failing_season.season_id:
            call_count["n"] += 1
            raise RuntimeError("simulated failure")
        return await real_resolve(conn, season)

    monkeypatch.setattr(season_service, "_resolve", _fail_once_for_other_guild)
    first = await season_service.resolve_due_seasons(pool, due_now)
    assert {r.season.season_id for r in first} == {healthy_season.season_id}
    assert call_count["n"] == 1

    # Second call: the healthy season is already completed (no-op for it),
    # and the failing one is retried -- now with the real resolver.
    monkeypatch.setattr(season_service, "_resolve", real_resolve)
    second = await season_service.resolve_due_seasons(pool, due_now)
    assert {r.season.season_id for r in second} == {failing_season.season_id}


async def test_resolve_due_seasons_returns_empty_when_nothing_due(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    await _start(pool, guild_id)
    resolved = await season_service.resolve_due_seasons(pool, NOW)
    assert resolved == []


# ---------------------------------------------------------------------------
# pending_announcements / mark_announced
# ---------------------------------------------------------------------------


async def test_pending_announcements_and_mark_announced(pool: asyncpg.Pool, guild_id: int) -> None:
    await _start(pool, guild_id)
    resolution = await season_service.end_season_now(pool, guild_id, _admin(), NOW)

    pending = await season_service.pending_announcements(pool, 10)
    assert resolution.season.season_id in {a.season.season_id for a in pending}

    await season_service.mark_announced(pool, guild_id, resolution.season.season_id)

    pending_after = await season_service.pending_announcements(pool, 10)
    assert resolution.season.season_id not in {a.season.season_id for a in pending_after}


async def test_pending_announcements_clamps_limit(pool: asyncpg.Pool, guild_id: int) -> None:
    announcements = await season_service.pending_announcements(pool, 1000)
    assert len(announcements) <= 50
    clamped = await season_service.pending_announcements(pool, 0)
    assert clamped == []  # nothing pending, but call itself must not error


# ---------------------------------------------------------------------------
# season_info / season_history
# ---------------------------------------------------------------------------


async def test_season_info_with_and_without_active_season(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    assert await season_service.season_info(pool, guild_id) is None

    season = await _start(pool, guild_id, start_date_text="2026-09-01")
    await _confirmed_game(pool, guild_id, winner=1, loser=2, date_text="2026-09-13")

    info = await season_service.season_info(pool, guild_id)
    assert info is not None
    assert info.season.season_id == season.season_id
    ranked_by_id = {p.user_id: p for p in info.ranked}
    assert ranked_by_id[1].wins == 1


async def test_season_history_clamps_limit(pool: asyncpg.Pool, guild_id: int) -> None:
    await _start(pool, guild_id)
    await season_service.cancel_season(pool, guild_id, _admin())
    await _start(pool, guild_id, name="Season Two")

    history = await season_service.season_history(pool, guild_id, 1000)
    assert len(history) <= 25
    assert len(history) == 2

    clamped_low = await season_service.season_history(pool, guild_id, 0)
    assert len(clamped_low) == 1


# ---------------------------------------------------------------------------
# L5 mutant-killers: V01-V03 (start_season name bounds).
# ---------------------------------------------------------------------------


async def _no_active_season(pool: asyncpg.Pool, guild_id: int) -> bool:
    async with pool.acquire() as conn:
        return await seasons_repo.get_active_season(conn, guild_id) is None


async def test_start_season_rejects_empty_name(pool: asyncpg.Pool, guild_id: int) -> None:
    """V01: an empty `name` must raise `DomainValidationError` and create nothing."""
    with pytest.raises(DomainValidationError):
        await _start(pool, guild_id, name="")
    assert await _no_active_season(pool, guild_id)


async def test_start_season_rejects_whitespace_only_name(pool: asyncpg.Pool, guild_id: int) -> None:
    """V02: a whitespace-only `name` must raise `DomainValidationError` and create nothing."""
    with pytest.raises(DomainValidationError):
        await _start(pool, guild_id, name="    ")
    assert await _no_active_season(pool, guild_id)


async def test_start_season_rejects_name_over_100_chars(pool: asyncpg.Pool, guild_id: int) -> None:
    """V03: a 101-character `name` must raise `DomainValidationError` and create nothing."""
    with pytest.raises(DomainValidationError):
        await _start(pool, guild_id, name="x" * 101)
    assert await _no_active_season(pool, guild_id)


# ---------------------------------------------------------------------------
# L5 mutant-killers: V14/V15 (min_games bounds and type, both entry points).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_min_games", [0, 101, True], ids=["zero", "over-max", "bool-true"])
async def test_start_season_rejects_bad_min_games(
    pool: asyncpg.Pool, guild_id: int, bad_min_games: object
) -> None:
    """V14/V15: `start_season(min_games=0|101|True)` must all raise
    `DomainValidationError` -- 0 and 101 are outside [1, 100], and `True`
    (a `bool`) is rejected by `validate_min_games`'s exact-type check."""
    with pytest.raises(DomainValidationError):
        await _start(pool, guild_id, min_games=bad_min_games)
    assert await _no_active_season(pool, guild_id)


@pytest.mark.parametrize("bad_min_games", [0, 101, True], ids=["zero", "over-max", "bool-true"])
async def test_set_min_games_rejects_bad_values(
    pool: asyncpg.Pool, guild_id: int, bad_min_games: object
) -> None:
    """V14/V15: `set_min_games` applies the same bounds/type rule as
    `start_season`'s `min_games`."""
    with pytest.raises(DomainValidationError):
        await season_service.set_min_games(pool, guild_id, _admin(), bad_min_games)


# ---------------------------------------------------------------------------
# L5 mutant-killers: T03-T06 (guild-timezone date math, not UTC).
# ---------------------------------------------------------------------------


async def test_start_season_starts_on_and_ends_at_use_guild_timezone(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    """T03/T04: guild tz America/Chicago, `NOW` = 2026-09-14 04:30 UTC is
    local 2026-09-13. `starts_on` (no start date given) must default to the
    *local* today, and `ends_at` must be `season_end_instant(ends_on, tz)`
    -- the first instant of the day after `ends_on` in America/Chicago --
    which is NOT UTC midnight of `ends_on`."""
    await config_service.set_timezone(pool, guild_id, _admin(), "America/Chicago")
    season = await _start(pool, guild_id, end_date_text="2026-09-20")

    assert season.starts_on == date(2026, 9, 13)

    expected_ends_at = season_end_instant(date(2026, 9, 20), "America/Chicago")
    assert season.ends_at == expected_ends_at
    assert season.ends_at != datetime(2026, 9, 21, tzinfo=UTC)  # not naive UTC midnight


async def test_set_end_date_ends_at_uses_guild_timezone(pool: asyncpg.Pool, guild_id: int) -> None:
    """T05: `set_end_date`'s `ends_at` must also be computed in the guild's
    timezone, not UTC."""
    await config_service.set_timezone(pool, guild_id, _admin(), "America/Chicago")
    await _start(pool, guild_id, start_date_text="2026-09-01", end_date_text="2026-12-31")

    updated = await season_service.set_end_date(pool, guild_id, _admin(), "2026-10-31", NOW)

    expected_ends_at = season_end_instant(date(2026, 10, 31), "America/Chicago")
    assert updated.ends_at == expected_ends_at
    assert updated.ends_at != datetime(2026, 11, 1, tzinfo=UTC)  # not naive UTC midnight


async def test_set_end_date_yesterday_local_rejected_today_local_accepted(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    """T06: guild tz America/Chicago, `NOW` = 2026-09-14 04:30 UTC is local
    2026-09-13. 'yesterday' (local 09-12) must be rejected as in the past;
    'today' (local 09-13) must be accepted, even though `NOW`'s *UTC*
    calendar date (09-14) differs from the local one -- a mutant that
    computed "today" from UTC instead of the guild's timezone would get
    both of these wrong."""
    await config_service.set_timezone(pool, guild_id, _admin(), "America/Chicago")
    await _start(pool, guild_id, start_date_text="2026-09-01", end_date_text="2026-12-31")

    with pytest.raises(DomainValidationError):
        await season_service.set_end_date(pool, guild_id, _admin(), "yesterday", NOW)

    updated = await season_service.set_end_date(pool, guild_id, _admin(), "today", NOW)
    assert updated.ends_on == date(2026, 9, 13)


# ---------------------------------------------------------------------------
# L5 mutant-killer: L04 (pending_announcements clamps *before* the
# repository call, not just in its own return value).
# ---------------------------------------------------------------------------


async def test_pending_announcements_clamps_limit_before_repository_call(
    pool: asyncpg.Pool, guild_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L04: `pending_announcements(limit=1000)` must pass at most 50 to
    `seasons.list_unannounced_completed` -- captured directly, rather than
    inferred from the returned list's length (which could also come out
    <= 50 simply because there isn't enough data)."""
    captured: dict[str, int] = {}
    real = season_service.seasons.list_unannounced_completed

    async def _capture(conn: asyncpg.Connection, limit: int):
        captured["limit"] = limit
        return await real(conn, limit)

    monkeypatch.setattr(season_service.seasons, "list_unannounced_completed", _capture)

    await season_service.pending_announcements(pool, 1000)

    assert captured["limit"] == 50


# ---------------------------------------------------------------------------
# L5 mutant-killer: S03 (failure isolation when the failing guild's season
# has the *lower* season_id -- created first, so it would normally be
# picked before the healthy guild's season).
# ---------------------------------------------------------------------------


async def test_resolve_due_seasons_isolates_failure_when_failing_season_has_lower_id(
    pool: asyncpg.Pool, guild_id: int, other_guild_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S03: `lock_next_due_season` orders by `season_id ASC`, so if the
    *failing* guild's season has the lower id, it's the one picked first on
    every retry. The healthy guild's season (created second, higher id)
    must still resolve, and the call must still terminate."""
    due_now = datetime(2026, 6, 1, tzinfo=UTC)
    failing_season = await _start(
        pool,
        other_guild_id,
        start_date_text="2026-01-01",
        end_date_text="2026-05-31",
        now=due_now - timedelta(days=200),
    )
    healthy_season = await _start(
        pool,
        guild_id,
        start_date_text="2026-01-01",
        end_date_text="2026-05-31",
        now=due_now - timedelta(days=200),
    )
    assert failing_season.season_id < healthy_season.season_id

    real_resolve = season_service._resolve

    async def _resolve_maybe_fail(conn, season):
        if season.season_id == failing_season.season_id:
            raise RuntimeError("simulated failure for the lower-id guild")
        return await real_resolve(conn, season)

    monkeypatch.setattr(season_service, "_resolve", _resolve_maybe_fail)

    resolved = await asyncio.wait_for(season_service.resolve_due_seasons(pool, due_now), timeout=5)

    assert {r.season.season_id for r in resolved} == {healthy_season.season_id}


# ---------------------------------------------------------------------------
# L1: `_log_resolution_failure` never leaks `PostgresError` DETAIL text.
# ---------------------------------------------------------------------------


async def test_resolve_due_seasons_failure_log_never_contains_detail_text(
    pool: asyncpg.Pool,
    guild_id: int,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """L1: `PostgresMessage.__str__` (used by `asyncpg.PostgresError`)
    appends the server's DETAIL/HINT text to `str(exc)` -- which can carry
    row contents (e.g. a season name via a CHECK violation message).
    `_log_resolution_failure` must never call `str(exc)` or pass `exc_info`
    for a `PostgresError`. Force one whose `.detail` carries a unique
    marker and assert the marker is absent from every captured record's
    message, args, and formatted exception text -- while the season_id and
    exception type name are present."""
    due_now = datetime(2026, 6, 1, tzinfo=UTC)
    season = await _start(
        pool,
        guild_id,
        start_date_text="2026-01-01",
        end_date_text="2026-05-31",
        now=due_now - timedelta(days=200),
    )

    marker = "MARKER_9f3ac21f_ROW_CONTENTS_DO_NOT_LEAK"

    async def _boom(conn, guild_id_arg, season_id_arg):
        exc = asyncpg.CheckViolationError("season_results_wins_le_games")
        exc.detail = f"Key (games)=(1) violates constraint. {marker}"
        raise exc

    monkeypatch.setattr(season_service.seasons, "season_player_stats", _boom)

    with caplog.at_level(logging.ERROR):
        resolved = await season_service.resolve_due_seasons(pool, due_now)

    assert resolved == []
    assert caplog.records, "expected at least one failure log record"
    for record in caplog.records:
        assert marker not in record.getMessage()
        assert marker not in repr(record.args)
        assert marker not in (record.exc_text or "")
    assert marker not in caplog.text

    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert str(season.season_id) in combined
    assert "CheckViolationError" in combined


# ---------------------------------------------------------------------------
# M3b item 2c: `end_season_now` locks the active season row, so a
# concurrent `set_min_games` can't change `min_games` mid-resolution.
# ---------------------------------------------------------------------------


async def test_end_season_now_locks_active_season_blocking_concurrent_set_min_games(
    pool: asyncpg.Pool, guild_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`end_season_now` must use `lock_active_season` (FOR UPDATE), not
    `get_active_season`, so `set_min_games(5)` blocks until resolution
    commits. Player 4 has exactly 2 confirmed games -- eligible only under
    the *locked* min_games=2 that was true when resolution started, not the
    min_games=5 a concurrent admin tries to set mid-resolution. After both
    calls finish, `set_min_games` must see the season as no longer active."""
    await _start(pool, guild_id, min_games=2, start_date_text="2026-09-01")
    await _confirmed_game(pool, guild_id, winner=1, loser=4, date_text="2026-09-10")
    await _confirmed_game(pool, guild_id, winner=4, loser=1, date_text="2026-09-11")

    reached_lock = asyncio.Event()
    release_lock = asyncio.Event()
    real_stats = season_service.seasons.season_player_stats

    async def _paused_stats(conn, guild_id_arg, season_id_arg):
        reached_lock.set()
        await release_lock.wait()
        return await real_stats(conn, guild_id_arg, season_id_arg)

    monkeypatch.setattr(season_service.seasons, "season_player_stats", _paused_stats)

    end_task = asyncio.create_task(season_service.end_season_now(pool, guild_id, _admin(), NOW))
    await asyncio.wait_for(reached_lock.wait(), timeout=2)

    set_task = asyncio.create_task(season_service.set_min_games(pool, guild_id, _admin(), 5))
    done, _pending = await asyncio.wait({set_task}, timeout=0.3)
    assert set_task not in done  # blocked behind end_season_now's row lock

    release_lock.set()
    resolution = await asyncio.wait_for(end_task, timeout=2)
    season_after, config_after = await asyncio.wait_for(set_task, timeout=2)

    assert resolution.season.min_games == 2
    ranked_by_id = {p.user_id: p for p in resolution.ranked}
    assert ranked_by_id[4].eligible is True  # 2 games, min_games=2 (the locked value)

    assert season_after is None  # no longer active by the time this runs
    assert config_after.default_min_games == 5


# ---------------------------------------------------------------------------
# M3b item 2d: `resolve_due_seasons` locks one candidate per iteration, so
# a paused resolution in one guild never blocks a due season in another.
# ---------------------------------------------------------------------------


async def test_resolve_due_seasons_locks_only_one_candidate_leaving_other_guilds_free(
    pool: asyncpg.Pool, guild_id: int, other_guild_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before this fix, `resolve_due_seasons` used `lock_due_seasons`, which
    locks *every* currently-due season across every guild in one
    transaction -- so a slow/paused resolution of `guild_id`'s season would
    also hold `other_guild_id`'s due season locked. With `lock_next_due_
    season` locking at most one row per transaction, an admin's own
    `/season end` on the unrelated guild's due season must complete without
    blocking."""
    due_now = datetime(2026, 6, 1, tzinfo=UTC)
    paused_season = await _start(
        pool,
        guild_id,
        start_date_text="2026-01-01",
        end_date_text="2026-05-31",
        now=due_now - timedelta(days=200),
    )
    other_due_season = await _start(
        pool,
        other_guild_id,
        start_date_text="2026-01-01",
        end_date_text="2026-05-31",
        now=due_now - timedelta(days=200),
    )
    assert paused_season.season_id < other_due_season.season_id  # lowest id picked first

    reached = asyncio.Event()
    release = asyncio.Event()
    real_stats = season_service.seasons.season_player_stats

    async def _paused_stats(conn, guild_id_arg, season_id_arg):
        if guild_id_arg == guild_id:
            reached.set()
            await release.wait()
        return await real_stats(conn, guild_id_arg, season_id_arg)

    monkeypatch.setattr(season_service.seasons, "season_player_stats", _paused_stats)

    resolve_task = asyncio.create_task(season_service.resolve_due_seasons(pool, due_now))
    await asyncio.wait_for(reached.wait(), timeout=2)

    # The unrelated guild's own due-season resolution must not block.
    other_resolution = await asyncio.wait_for(
        season_service.end_season_now(pool, other_guild_id, _admin(), due_now), timeout=1
    )
    assert other_resolution.season.season_id == other_due_season.season_id

    release.set()
    resolved = await asyncio.wait_for(resolve_task, timeout=2)
    assert {r.season.season_id for r in resolved} == {paused_season.season_id}


async def test_two_concurrent_resolve_due_seasons_calls_complete_three_seasons_once_each(
    pool: asyncpg.Pool, guild_id: int, other_guild_id: int
) -> None:
    """Two concurrent `resolve_due_seasons` calls, driven by
    `lock_next_due_season`'s `FOR UPDATE SKIP LOCKED`, must together
    resolve every due season across 3 guilds exactly once -- no missed
    season, no double-resolved season."""
    third_guild_id = 900_003
    async with pool.acquire() as conn:
        await guilds_repo.ensure_guild(conn, third_guild_id)

    due_now = datetime(2026, 6, 1, tzinfo=UTC)
    started = [
        await _start(
            pool,
            gid,
            start_date_text="2026-01-01",
            end_date_text="2026-05-31",
            now=due_now - timedelta(days=200),
        )
        for gid in (guild_id, other_guild_id, third_guild_id)
    ]

    results_a, results_b = await asyncio.gather(
        season_service.resolve_due_seasons(pool, due_now),
        season_service.resolve_due_seasons(pool, due_now),
    )

    all_resolved_ids = [r.season.season_id for r in results_a] + [
        r.season.season_id for r in results_b
    ]
    assert sorted(all_resolved_ids) == sorted(s.season_id for s in started)
    assert len(set(all_resolved_ids)) == len(all_resolved_ids)
