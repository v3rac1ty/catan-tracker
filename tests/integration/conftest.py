"""Fixtures shared by the integration suite.

All integration tests run against the `catan_test` database and are
auto-skipped (per test module, via a `skipif` on `TEST_DATABASE_URL` /
`TEST_MIGRATOR_DATABASE_URL`) when Postgres isn't available.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import asyncpg
import pytest

from catan_bot.db.migrate import run_migrations as _run_migrations
from catan_bot.db.pool import create_pool as _create_pool
from catan_bot.db.repositories.guilds import ensure_guild as _ensure_guild

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TEST_MIGRATOR_DATABASE_URL = os.environ.get("TEST_MIGRATOR_DATABASE_URL")

integration_env_available = bool(TEST_DATABASE_URL and TEST_MIGRATOR_DATABASE_URL)

# Every application-data table across 0001-0005, i.e. everything except the
# migration-tracking `schema_migrations` table. Schema-qualified so a
# `search_path` trick can't redirect the TRUNCATE.
#
# `game_updates` (0004) and `game_score_requests` (0005) are listed
# explicitly even though neither has a sequence of its own (no
# `GENERATED ALWAYS AS IDENTITY` column -- `game_updates`'s PK is
# `(game_id, revision)`, `game_score_requests`'s is `(game_id, user_id)`,
# both plain columns, so RESTART IDENTITY has nothing to reset on either)
# and both would already be emptied via CASCADE: each has a
# `FOREIGN KEY (game_id, guild_id) REFERENCES games (game_id, guild_id)`
# with nothing referencing either of them in turn, so truncating `games`
# with CASCADE cascades onto both regardless of whether they're named here.
# Listing them anyway means this cleanup's correctness doesn't silently
# depend on that cascade continuing to exist -- a future migration that
# drops the FK, or reworks it to no longer point at `games`, would
# otherwise leave rows behind with no test failure pointing at why.
_TRUNCATE_APP_TABLES_SQL = (
    "TRUNCATE TABLE public.guild_config, public.players, public.seasons, "
    "public.season_results, public.games, public.game_participants, "
    "public.game_updates, public.game_score_requests, "
    "public.events, public.event_rsvps, public.event_reminders "
    "RESTART IDENTITY CASCADE"
)

# Schema-qualified (pg_catalog) so a same-named function earlier in
# search_path can never shadow current_database() and fool this guard.
_SELECT_CURRENT_DATABASE_SQL = "SELECT pg_catalog.current_database()"
_EXPECTED_TEST_DATABASE_NAME = "catan_test"


async def _assert_connected_to_test_database(conn: asyncpg.Connection) -> None:
    """Last-resort guard: never run destructive/session-wide operations
    against anything but `catan_test`, even if TEST_DATABASE_URL /
    TEST_MIGRATOR_DATABASE_URL were misconfigured to point elsewhere."""
    db_name = await conn.fetchval(_SELECT_CURRENT_DATABASE_SQL)
    if db_name != _EXPECTED_TEST_DATABASE_NAME:
        raise RuntimeError(
            f"Refusing to operate on database {db_name!r}: integration tests must only "
            f"ever run against {_EXPECTED_TEST_DATABASE_NAME!r}. Check TEST_DATABASE_URL "
            "and TEST_MIGRATOR_DATABASE_URL."
        )


async def _check_connected_to_test_database(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await _assert_connected_to_test_database(conn)
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
def run_migrations() -> None:
    """Apply migrations to catan_test once per test session."""
    if not integration_env_available:
        return
    asyncio.run(_check_connected_to_test_database(TEST_MIGRATOR_DATABASE_URL))
    asyncio.run(_run_migrations(TEST_MIGRATOR_DATABASE_URL))


@pytest.fixture
async def migrator_conn() -> AsyncIterator[asyncpg.Connection]:
    """A connection using the `catan_migrator` (owner) role."""
    conn = await asyncpg.connect(TEST_MIGRATOR_DATABASE_URL)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def app_conn() -> AsyncIterator[asyncpg.Connection]:
    """A connection using the least-privilege `catan_app` (runtime) role."""
    conn = await asyncpg.connect(TEST_DATABASE_URL)
    try:
        # A mispointed TEST_DATABASE_URL must never let a test write rows
        # into a real database.
        await _assert_connected_to_test_database(conn)
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    """A connection pool using the least-privilege `catan_app` role, for the
    services layer under test (services take a `Pool`, never a bare `conn`).

    Matches `app_conn`'s safety net: refuses to proceed if `TEST_DATABASE_URL`
    doesn't actually point at `catan_test`.
    """
    created = await _create_pool(TEST_DATABASE_URL)
    try:
        async with created.acquire() as conn:
            await _assert_connected_to_test_database(conn)
        yield created
    finally:
        await created.close()


async def _truncate_app_tables() -> None:
    conn = await asyncpg.connect(TEST_MIGRATOR_DATABASE_URL)
    try:
        await _assert_connected_to_test_database(conn)
        await conn.execute(_TRUNCATE_APP_TABLES_SQL)
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
async def _clean_app_tables() -> AsyncIterator[None]:
    """Leave every app table empty both before and after each test, using the
    migrator role, so tests never see leftovers from a previous run/failure.
    """
    if not integration_env_available:
        yield
        return
    await _truncate_app_tables()
    yield
    await _truncate_app_tables()


# Fixed, arbitrary guild ids used across the M3a repository/injection/race/
# isolation test suites. Safe to hardcode: `_clean_app_tables` above
# truncates every app table before and after each test, so there's never a
# leftover row from a previous test to collide with.
GUILD_ID = 900_001
OTHER_GUILD_ID = 900_002


@pytest.fixture
async def guild_id(app_conn: asyncpg.Connection) -> int:
    """`GUILD_ID`, with its `guild_config` row already ensured."""
    await _ensure_guild(app_conn, GUILD_ID)
    return GUILD_ID


@pytest.fixture
async def other_guild_id(app_conn: asyncpg.Connection) -> int:
    """A second, distinct guild (`OTHER_GUILD_ID`) with its row ensured.

    Used by the guild-isolation and second-order injection tests, which
    need two real guilds to prove one guild's calls never see or touch the
    other's data.
    """
    await _ensure_guild(app_conn, OTHER_GUILD_ID)
    return OTHER_GUILD_ID
