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

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TEST_MIGRATOR_DATABASE_URL = os.environ.get("TEST_MIGRATOR_DATABASE_URL")

integration_env_available = bool(TEST_DATABASE_URL and TEST_MIGRATOR_DATABASE_URL)

# All tables created by 0001_init.sql that hold application data (i.e.
# everything except the migration-tracking `schema_migrations` table).
_APP_TABLES = (
    "guild_config",
    "players",
    "seasons",
    "season_results",
    "games",
    "game_participants",
    "events",
    "event_rsvps",
    "event_reminders",
)
_TRUNCATE_APP_TABLES_SQL = f"TRUNCATE TABLE {', '.join(_APP_TABLES)} RESTART IDENTITY CASCADE"  # noqa: S608


@pytest.fixture(scope="session", autouse=True)
def run_migrations() -> None:
    """Apply migrations to catan_test once per test session."""
    if not integration_env_available:
        return
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
        yield conn
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
async def _truncate_app_tables_after_test() -> AsyncIterator[None]:
    """Leave every app table empty after each test, using the migrator role."""
    if not integration_env_available:
        yield
        return
    yield
    conn = await asyncpg.connect(TEST_MIGRATOR_DATABASE_URL)
    try:
        await conn.execute(_TRUNCATE_APP_TABLES_SQL)
    finally:
        await conn.close()
