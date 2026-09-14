"""Versioned SQL migration runner.

Run as `python -m catan_bot.db.migrate`. Connects with the `catan_migrator`
role, takes a Postgres advisory lock so concurrent runs can't race, and
applies any `*.sql` files under `catan_bot.db.migrations` that aren't yet
recorded in `schema_migrations`, each in its own transaction, in filename
order.

SQL injection note: every query in this module is a module-level string
constant with values passed as `$n` parameters, with exactly one exception —
executing the *contents* of a trusted, repo-committed migration file is the
single allowlisted non-constant SQL execution in the whole codebase (see
`_APPLY_MIGRATION_FILE_SQL_ALLOWLISTED` below). Migration filenames are
validated against a strict pattern before their contents are read.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import logging
import re
import sys
from importlib.resources.abc import Traversable

import asyncpg

from catan_bot.config import MigrateSettings

logger = logging.getLogger(__name__)

_MIGRATIONS_PACKAGE = "catan_bot.db.migrations"
_FILENAME_PATTERN = re.compile(r"^\d{4}_[a-z0-9_]+\.sql$")

# Arbitrary fixed key: any process running migrations for this app uses the
# same lock, so concurrent `migrate` runs serialize instead of racing.
_ADVISORY_LOCK_KEY = 87_651_309_001

_ADVISORY_LOCK_SQL = "SELECT pg_advisory_lock($1)"
_ADVISORY_UNLOCK_SQL = "SELECT pg_advisory_unlock($1)"

_CREATE_SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_SELECT_APPLIED_VERSIONS_SQL = "SELECT version FROM schema_migrations"

_INSERT_APPLIED_VERSION_SQL = "INSERT INTO schema_migrations (version) VALUES ($1)"


def _discover_migrations() -> list[tuple[str, Traversable]]:
    """Return (filename, resource) pairs for all migration files, sorted by name.

    Raises ValueError if any `*.sql` file's name doesn't match the required
    `NNNN_description.sql` pattern.
    """
    package = importlib.resources.files(_MIGRATIONS_PACKAGE)
    found: list[tuple[str, Traversable]] = []
    for entry in package.iterdir():
        if not entry.is_file() or not entry.name.endswith(".sql"):
            continue
        if not _FILENAME_PATTERN.match(entry.name):
            raise ValueError(
                f"Migration filename {entry.name!r} does not match {_FILENAME_PATTERN.pattern!r}"
            )
        found.append((entry.name, entry))
    found.sort(key=lambda pair: pair[0])
    return found


async def run_migrations(dsn: str) -> list[str]:
    """Apply all unapplied migrations. Returns the list of newly-applied versions."""
    conn = await asyncpg.connect(dsn)
    applied_now: list[str] = []
    try:
        await conn.execute(_ADVISORY_LOCK_SQL, _ADVISORY_LOCK_KEY)
        try:
            await conn.execute(_CREATE_SCHEMA_MIGRATIONS_SQL)
            already_applied = {
                row["version"] for row in await conn.fetch(_SELECT_APPLIED_VERSIONS_SQL)
            }

            for filename, resource in _discover_migrations():
                version = filename
                if version in already_applied:
                    continue

                sql_text = resource.read_text(encoding="utf-8")
                async with conn.transaction():
                    # Allowlisted exception: this executes the contents of a
                    # trusted, repo-committed migration file whose filename
                    # was already validated against _FILENAME_PATTERN. This
                    # is the ONLY non-constant SQL execution in the codebase.
                    await conn.execute(sql_text)  # _APPLY_MIGRATION_FILE_SQL_ALLOWLISTED
                    await conn.execute(_INSERT_APPLIED_VERSION_SQL, version)

                logger.info("Applied migration %s", version)
                applied_now.append(version)
        finally:
            await conn.execute(_ADVISORY_UNLOCK_SQL, _ADVISORY_LOCK_KEY)
    finally:
        await conn.close()

    return applied_now


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = MigrateSettings()
    dsn = settings.migrator_database_url.get_secret_value()
    applied = await run_migrations(dsn)
    if applied:
        logger.info("Applied %d migration(s): %s", len(applied), ", ".join(applied))
    else:
        logger.info("No pending migrations.")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except Exception:
        logger.exception("Migration run failed")
        sys.exit(1)
