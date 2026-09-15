"""Concurrency tests: separate pool connections racing via `asyncio.gather`.

Each test proves a guarded atomic UPDATE (or the active-season unique index)
gives exactly the winner/loser split DESIGN.md promises, with no double
application and no lost update.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import asyncpg
import pytest

from catan_bot.db.errors import ActiveSeasonExistsError
from catan_bot.db.models import SeasonResultRow
from catan_bot.db.repositories import events, games, players, seasons

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

STARTS_ON = date(2026, 1, 1)
ENDS_ON = date(2026, 12, 31)
PLAYED_ON = date(2026, 2, 1)
NOW = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture
async def two_conns() -> AsyncIterator[tuple[asyncpg.Connection, asyncpg.Connection]]:
    """Two independent connections, so concurrent calls truly overlap
    server-side instead of serializing on one connection's protocol."""
    dsn = os.environ["TEST_DATABASE_URL"]
    a = await asyncpg.connect(dsn)
    b = await asyncpg.connect(dsn)
    try:
        yield a, b
    finally:
        await a.close()
        await b.close()


async def test_two_different_participants_confirming_concurrently_only_one_wins(
    app_conn: asyncpg.Connection,
    guild_id: int,
    two_conns: tuple[asyncpg.Connection, asyncpg.Connection],
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2, 3])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2, 3])
    conn_a, conn_b = two_conns

    result_a, result_b = await asyncio.gather(
        games.confirm_game(conn_a, guild_id, game.game_id, 2),
        games.confirm_game(conn_b, guild_id, game.game_id, 3),
    )

    results = {result_a, result_b}
    assert results == {"confirmed", "not_pending"}

    final = await games.get_game(app_conn, guild_id, game.game_id)
    assert final is not None
    assert final.game.status == "confirmed"
    assert final.game.confirmed_by in (2, 3)


async def test_same_user_double_confirming_concurrently_only_one_succeeds(
    app_conn: asyncpg.Connection,
    guild_id: int,
    two_conns: tuple[asyncpg.Connection, asyncpg.Connection],
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])
    conn_a, conn_b = two_conns

    result_a, result_b = await asyncio.gather(
        games.confirm_game(conn_a, guild_id, game.game_id, 2),
        games.confirm_game(conn_b, guild_id, game.game_id, 2),
    )

    results = sorted([result_a, result_b])
    assert results == ["confirmed", "not_pending"]


async def test_two_concurrent_create_season_one_succeeds_one_raises(
    app_conn: asyncpg.Connection,
    guild_id: int,
    two_conns: tuple[asyncpg.Connection, asyncpg.Connection],
) -> None:
    conn_a, conn_b = two_conns
    ends_at = datetime(2027, 1, 1, tzinfo=UTC)

    results = await asyncio.gather(
        seasons.create_season(conn_a, guild_id, "A", STARTS_ON, ENDS_ON, ends_at, 2, 1),
        seasons.create_season(conn_b, guild_id, "B", STARTS_ON, ENDS_ON, ends_at, 2, 1),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ActiveSeasonExistsError)

    all_seasons = await seasons.list_seasons(app_conn, guild_id, 10)
    assert len(all_seasons) == 1


async def test_two_concurrent_claim_due_reminders_claim_every_reminder_exactly_once(
    app_conn: asyncpg.Connection,
    guild_id: int,
    two_conns: tuple[asyncpg.Connection, asyncpg.Connection],
) -> None:
    starts_at = NOW + timedelta(hours=2)
    reminder_ids = []
    for _ in range(4):
        event = await events.create_event(
            app_conn,
            guild_id,
            "Reminder race",
            None,
            None,
            starts_at,
            1,
            None,
            [(60, NOW - timedelta(minutes=1))],
        )
        reminder_ids.append(event.event_id)
    conn_a, conn_b = two_conns

    claimed_a, claimed_b = await asyncio.gather(
        events.claim_due_reminders(conn_a, NOW),
        events.claim_due_reminders(conn_b, NOW),
    )

    all_claimed_event_ids = [c.event_id for c in claimed_a] + [c.event_id for c in claimed_b]
    assert sorted(all_claimed_event_ids) == sorted(reminder_ids)
    # No reminder claimed by both.
    assert len(set(all_claimed_event_ids)) == len(all_claimed_event_ids)


async def test_lock_due_seasons_skip_locked_is_non_blocking_for_concurrent_caller(
    app_conn: asyncpg.Connection,
    guild_id: int,
    two_conns: tuple[asyncpg.Connection, asyncpg.Connection],
) -> None:
    """L4: `_LOCK_DUE_SEASONS_SQL`'s `FOR UPDATE SKIP LOCKED` must let a
    concurrent caller return immediately with an empty list, rather than
    blocking on conn A's still-open transaction that already holds the
    row's lock.

    N6: conn B's call is wrapped in `asyncio.wait_for` so that if `SKIP
    LOCKED` is ever removed (making conn B block on conn A's lock instead),
    this test fails fast with a clear timeout instead of hanging until the
    role's `statement_timeout` fires -- or indefinitely, if that's ever
    raised or removed too."""
    due_at = NOW - timedelta(days=1)
    due = await seasons.create_season(app_conn, guild_id, "Due", STARTS_ON, ENDS_ON, due_at, 2, 1)
    conn_a, conn_b = two_conns

    tx_a = conn_a.transaction()
    await tx_a.start()
    try:
        locked_by_a = await seasons.lock_due_seasons(conn_a, NOW)
        assert [s.season_id for s in locked_by_a] == [due.season_id]

        start = time.monotonic()
        locked_by_b = await asyncio.wait_for(seasons.lock_due_seasons(conn_b, NOW), timeout=2)
        elapsed = time.monotonic() - start

        assert locked_by_b == []
        assert elapsed < 1.0
    finally:
        await tx_a.rollback()


async def test_two_concurrent_lock_and_complete_season_each_completes_once(
    app_conn: asyncpg.Connection,
    guild_id: int,
    other_guild_id: int,
    two_conns: tuple[asyncpg.Connection, asyncpg.Connection],
) -> None:
    due_at = NOW - timedelta(days=1)
    season_a = await seasons.create_season(
        app_conn, guild_id, "A", STARTS_ON, ENDS_ON, due_at, 2, 1
    )
    season_b = await seasons.create_season(
        app_conn, other_guild_id, "B", STARTS_ON, ENDS_ON, due_at, 2, 1
    )
    conn_a, conn_b = two_conns

    async def _resolve(conn: asyncpg.Connection) -> list[int]:
        completed: list[int] = []
        async with conn.transaction():
            due = await seasons.lock_due_seasons(conn, NOW)
            for season in due:
                ok = await seasons.complete_season(conn, season.guild_id, season.season_id, [])
                if ok:
                    completed.append(season.season_id)
        return completed

    completed_a, completed_b = await asyncio.gather(_resolve(conn_a), _resolve(conn_b))

    all_completed = completed_a + completed_b
    assert sorted(all_completed) == sorted([season_a.season_id, season_b.season_id])
    assert len(set(all_completed)) == len(all_completed)

    final_a = await seasons.get_season(app_conn, guild_id, season_a.season_id)
    final_b = await seasons.get_season(app_conn, other_guild_id, season_b.season_id)
    assert final_a is not None
    assert final_a.status == "completed"
    assert final_b is not None
    assert final_b.status == "completed"


async def test_complete_season_with_generator_results_stores_all_rows(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """N1 regression: `results` must be materialized exactly once, so a
    generator (previously consumed by row validation, then exhausted by the
    time the SQL call built its `unnest()` arrays) doesn't silently store
    zero `season_results` rows."""
    ends_at = datetime(2027, 1, 1, tzinfo=UTC)
    season = await seasons.create_season(app_conn, guild_id, "S", STARTS_ON, ENDS_ON, ends_at, 2, 1)
    await players.ensure_players(app_conn, guild_id, [1, 2])
    rows = [
        SeasonResultRow(user_id=1, rank=1, games=2, wins=2, eligible=True, outcome="payee"),
        SeasonResultRow(user_id=2, rank=2, games=2, wins=0, eligible=True, outcome="payer"),
    ]
    results = (row for row in rows)

    ok = await seasons.complete_season(app_conn, guild_id, season.season_id, results)

    assert ok is True
    stored = await seasons.get_season_results(app_conn, guild_id, season.season_id)
    assert {r.user_id for r in stored} == {1, 2}
