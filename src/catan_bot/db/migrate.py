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

A migration file must not manipulate the surrounding transaction (COMMIT,
ROLLBACK, END, ABORT, `... AND CHAIN`, etc.) -- each migration already runs
inside a transaction managed by this runner. Rather than pattern-match SQL
text for this (regexes are trivially bypassable -- `END;`, `ABORT;`, a final
`COMMIT` with no trailing `;`, a `COMMIT` buried after other statements, and
plpgsql procedure bodies are all real gaps), this runner checks the actual
Postgres transaction state after executing the file: the transaction's xid
must be unchanged and the connection must still be in a transaction. If
either check fails, the migration is NOT recorded as applied, even though
any side effects it already committed cannot be undone by this runner. If
the file itself errors out (e.g. a bad statement) *after* already altering
the transaction this way, the error is re-raised with that context instead
of a bare, misleading Postgres error, so an operator doesn't assume a full
rollback happened when it didn't.

The connection is opened with a `lock_timeout` (so an orphaned lock can't
hang this process forever while it holds the migration advisory lock), and
every query here is schema-qualified against `public` so a migration that
leaves behind a `search_path` trick can't shadow this runner's own tables.
`RESET ALL` runs after each successful migration so a `SET search_path` /
`SET ROLE` inside one migration can't leak into the next.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import logging
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg

from catan_bot.config import MigrateSettings

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

logger = logging.getLogger(__name__)

_MIGRATIONS_PACKAGE = "catan_bot.db.migrations"

# Deliberately ASCII-only: Unicode digits (e.g. Arabic-indic) satisfy `\d`
# but are not valid ordinary filenames we ever want to accept.
_FILENAME_PATTERN = re.compile(r"[0-9]{4}_[a-z0-9_]+\.sql", re.ASCII)

# Non-migration entries tolerated alongside migration files, keyed by the
# entry type they're expected to be -- a file literally named `__pycache__`
# or a directory literally named `__init__.py` is NOT silently skipped.
_IGNORED_DIR_NAMES = frozenset({"__pycache__"})
_IGNORED_FILE_NAMES = frozenset({"__init__.py", ".DS_Store"})

# Arbitrary fixed key: any process running migrations for this app uses the
# same lock, so concurrent `migrate` runs serialize instead of racing.
_ADVISORY_LOCK_KEY = 87_651_309_001

_ADVISORY_LOCK_SQL = "SELECT pg_advisory_lock($1)"
_ADVISORY_UNLOCK_SQL = "SELECT pg_advisory_unlock($1)"

# Schema-qualified so a migration that changes search_path can't shadow
# this table with one of its own.
_CREATE_SCHEMA_MIGRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# The bot's runtime role must never be able to add/remove rows here: doing so
# could make a future migration silently appear "already applied" and skip.
_REVOKE_APP_ON_SCHEMA_MIGRATIONS_SQL = "REVOKE ALL ON public.schema_migrations FROM catan_app"

_SELECT_APPLIED_VERSIONS_SQL = "SELECT version FROM public.schema_migrations"

_INSERT_APPLIED_VERSION_SQL = "INSERT INTO public.schema_migrations (version) VALUES ($1)"

# Used before and after executing a migration file to detect whether it
# committed, rolled back, or "chained" the surrounding transaction. Calling
# this forces allocation of a transaction id if one isn't assigned yet, so
# two calls inside the *same* transaction always compare equal.
_CURRENT_XACT_ID_SQL = "SELECT pg_current_xact_id()::text"

# Run after each successfully-applied migration so a `SET search_path` /
# `SET ROLE` it left behind can't leak into the next one. This resets every
# GUC to its session-start value -- which includes values supplied via
# `server_settings` at connect() time (see run_migrations), not just
# role/database ALTER ... SET defaults -- so the connection's `lock_timeout`
# survives this.
_RESET_SESSION_SQL = "RESET ALL"

# Set right before executing a migration file, so that if it fails *after*
# already committing (e.g. `END;`/`COMMIT AND CHAIN;` followed by a later
# error), we can tell: once a mid-script COMMIT happens, this savepoint is
# gone even in the new ("chained") transaction, so trying to roll back to it
# fails -- that failure is the signal, since `conn.is_in_transaction()` alone
# can't distinguish "still in the original, fully-rollback-able transaction"
# from "in a brand new one after a commit" (both report True). Rolling back
# to a savepoint is one of the few commands Postgres allows even once a
# transaction has been aborted by an error, which is exactly the state we're
# in when we need to check this.
_CREATE_MIGRATION_SAVEPOINT_SQL = "SAVEPOINT catan_migrate_runner_guard"
_ROLLBACK_TO_MIGRATION_SAVEPOINT_SQL = "ROLLBACK TO SAVEPOINT catan_migrate_runner_guard"


def _discover_migrations(
    source: Traversable | Path | None = None,
) -> list[tuple[str, Traversable | Path]]:
    """Return (filename, resource) pairs for all migration files, sorted by name.

    `source` defaults to the installed `catan_bot.db.migrations` package, but
    accepts any directory/Traversable (e.g. a `tmp_path` in tests).

    Raises ValueError if any entry isn't one of: a directory named
    `__pycache__`, a file named `__init__.py` or `.DS_Store`, a symlink (of
    any kind -- never followed), or a file matching `_FILENAME_PATTERN`.
    e.g. `0002_x.SQL`, `x.sql.bak`, a stray `notes.txt`, or a symlinked
    migration all fail loudly instead of being silently skipped or trusted.
    """
    package = source if source is not None else importlib.resources.files(_MIGRATIONS_PACKAGE)
    found: list[tuple[str, Traversable | Path]] = []
    for entry in package.iterdir():
        name = entry.name
        is_symlink = getattr(entry, "is_symlink", None)
        if callable(is_symlink) and is_symlink():
            raise ValueError(
                f"Unexpected entry {name!r} in the migrations directory: symlinks are not allowed"
            )

        if entry.is_dir():
            if name in _IGNORED_DIR_NAMES:
                continue
            raise ValueError(
                f"Unexpected entry {name!r} in the migrations directory: unexpected directory"
            )

        if entry.is_file():
            if name in _IGNORED_FILE_NAMES:
                continue
            if _FILENAME_PATTERN.fullmatch(name):
                found.append((name, entry))
                continue

        raise ValueError(
            f"Unexpected entry {name!r} in the migrations directory: only "
            f"files matching {_FILENAME_PATTERN.pattern!r} are allowed"
        )
    found.sort(key=lambda pair: pair[0])
    return found


async def run_migrations(dsn: str, source: Traversable | Path | None = None) -> list[str]:
    """Apply all unapplied migrations. Returns the list of newly-applied versions.

    `source` is forwarded to `_discover_migrations`; tests use it to point at
    a `tmp_path` instead of the installed package.
    """
    conn = await asyncpg.connect(
        dsn,
        server_settings={"lock_timeout": "30s", "application_name": "catan_migrate"},
    )
    applied_now: list[str] = []
    try:
        await conn.execute(_ADVISORY_LOCK_SQL, _ADVISORY_LOCK_KEY)
        try:
            await conn.execute(_CREATE_SCHEMA_MIGRATIONS_SQL)
            await conn.execute(_REVOKE_APP_ON_SCHEMA_MIGRATIONS_SQL)
            already_applied = {
                row["version"] for row in await conn.fetch(_SELECT_APPLIED_VERSIONS_SQL)
            }

            for filename, resource in _discover_migrations(source):
                version = filename
                if version in already_applied:
                    continue

                sql_text = resource.read_text(encoding="utf-8")

                async with conn.transaction():
                    xid_before = await conn.fetchval(_CURRENT_XACT_ID_SQL)
                    await conn.execute(_CREATE_MIGRATION_SAVEPOINT_SQL)

                    # Allowlisted exception: this executes the contents of a
                    # trusted, repo-committed migration file whose filename
                    # was already validated against _FILENAME_PATTERN. This
                    # is the ONLY non-constant SQL execution in the codebase.
                    try:
                        await conn.execute(sql_text)  # _APPLY_MIGRATION_FILE_SQL_ALLOWLISTED
                    except asyncpg.PostgresError as exc:
                        committed_before_failure = not conn.is_in_transaction()
                        if not committed_before_failure:
                            # Still "in a transaction" per the protocol, but
                            # that's true both for our original transaction
                            # (safe: a plain ROLLBACK undoes everything) and
                            # for a brand new one after a mid-script COMMIT/
                            # CHAIN (unsafe: earlier statements already
                            # committed). Telling them apart: rolling back to
                            # the savepoint we set above only succeeds in the
                            # former case -- a COMMIT anywhere in between
                            # would have already erased that savepoint.
                            try:
                                await conn.execute(_ROLLBACK_TO_MIGRATION_SAVEPOINT_SQL)
                            except asyncpg.PostgresError:
                                committed_before_failure = True
                        if committed_before_failure:
                            # A bare re-raise here would wrongly suggest a
                            # full rollback happened.
                            raise RuntimeError(
                                f"Migration {version!r} failed after altering the "
                                "surrounding transaction; statements before the "
                                "transaction-control statement may already be "
                                "committed and need manual cleanup"
                            ) from exc
                        raise

                    xid_after = await conn.fetchval(_CURRENT_XACT_ID_SQL)
                    if not conn.is_in_transaction() or xid_after != xid_before:
                        raise RuntimeError(
                            f"Migration {version!r} altered the surrounding transaction "
                            "(e.g. via COMMIT/ROLLBACK/END/ABORT, possibly with AND CHAIN); "
                            "refusing to record it as applied. Any side effects it already "
                            "committed were NOT rolled back and may need manual cleanup."
                        )

                    await conn.execute(_INSERT_APPLIED_VERSION_SQL, version)

                # Outside the transaction block (it already committed): stop
                # a SET search_path/SET ROLE from this migration leaking
                # into the next one.
                await conn.execute(_RESET_SESSION_SQL)

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
