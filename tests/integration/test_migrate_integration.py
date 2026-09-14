"""Integration tests for the migration runner's transaction-integrity check.

A regex over the migration text is trivially bypassable (`END;`, `ABORT;`,
`COMMIT AND CHAIN;`, a final `COMMIT` with no trailing `;`, a `COMMIT` buried
after other statements, and plpgsql procedure bodies are all real gaps), so
`run_migrations()` instead checks actual Postgres transaction state
(`pg_current_xact_id()` and `conn.is_in_transaction()`) right after running
the file. These tests prove that check catches every one of those cases and
still lets a legitimate plpgsql `DO $$ BEGIN ... END $$;` block through.

Each "bad" probe migration commits real DDL before hitting the disallowed
transaction-control statement, so Postgres has already applied it by the
time `run_migrations()` notices -- the probe table is dropped manually in a
`finally` block, exactly like an operator would have to after this happens
for real. Every DROP is a literal string constant, never built from a
variable, even though the table names only ever come from the hard-coded
dict below: this codebase has zero dynamic SQL, tests included.
"""

from __future__ import annotations

import importlib.resources
import os
from pathlib import Path

import asyncpg
import pytest

from catan_bot.db.migrate import run_migrations

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

_SELECT_SCHEMA_MIGRATIONS_VERSION_SQL = "SELECT 1 FROM schema_migrations WHERE version = $1"
_DELETE_SCHEMA_MIGRATIONS_VERSION_SQL = "DELETE FROM schema_migrations WHERE version = $1"

# (probe table name, transaction-control statement(s), literal DROP for cleanup)
_BAD_PROBE_CASES: dict[str, tuple[str, str, str]] = {
    "commit": (
        "probe_txn_commit",
        "COMMIT;",
        "DROP TABLE IF EXISTS probe_txn_commit",
    ),
    "end": (
        "probe_txn_end",
        "END;",
        "DROP TABLE IF EXISTS probe_txn_end",
    ),
    "abort": (
        "probe_txn_abort",
        "ABORT;",
        "DROP TABLE IF EXISTS probe_txn_abort",
    ),
    "commit_and_chain": (
        "probe_txn_commit_chain",
        "COMMIT AND CHAIN;",
        "DROP TABLE IF EXISTS probe_txn_commit_chain",
    ),
    "rollback_and_chain": (
        "probe_txn_rollback_chain",
        "ROLLBACK AND CHAIN;",
        "DROP TABLE IF EXISTS probe_txn_rollback_chain",
    ),
    "select_then_commit": (
        "probe_txn_select_commit",
        "SELECT 1;\nCOMMIT;",
        "DROP TABLE IF EXISTS probe_txn_select_commit",
    ),
    "commit_no_semicolon": (
        "probe_txn_commit_nosemi",
        "COMMIT",
        "DROP TABLE IF EXISTS probe_txn_commit_nosemi",
    ),
}

_PLPGSQL_PROBE_TABLE_NAME = "probe_txn_plpgsql_ok"
_DROP_PLPGSQL_PROBE_TABLE_SQL = "DROP TABLE IF EXISTS probe_txn_plpgsql_ok"

# (probe table name, transaction-control statement + a later runtime error,
# literal DROP for cleanup). These prove that when a migration errors out
# *after* already altering the transaction (COMMIT/END/etc already ran),
# the runner reports that context instead of a bare, misleading Postgres
# error that would make an operator assume a full rollback happened.
_BAD_PROBE_WITH_RUNTIME_ERROR_CASES: dict[str, tuple[str, str, str]] = {
    "end_then_runtime_error": (
        "probe_txn_end_then_error",
        "END;\nSELECT 1/0;",
        "DROP TABLE IF EXISTS probe_txn_end_then_error",
    ),
    "commit_and_chain_then_runtime_error": (
        "probe_txn_commit_chain_then_error",
        "COMMIT AND CHAIN;\nSELECT 1/0;",
        "DROP TABLE IF EXISTS probe_txn_commit_chain_then_error",
    ),
}

_SEARCH_PATH_PROBE_SCHEMA = "probe_search_path_schema"
_SEARCH_PATH_PROBE_TABLE = "probe_search_path_target"
_DROP_SEARCH_PATH_PROBE_TABLE_SQL = "DROP TABLE IF EXISTS public.probe_search_path_target"
_DROP_SEARCH_PATH_PROBE_SCHEMA_SQL = "DROP SCHEMA IF EXISTS probe_search_path_schema CASCADE"
_SELECT_SEARCH_PATH_TARGET_IN_PUBLIC_SQL = (
    "SELECT to_regclass('public.probe_search_path_target') IS NOT NULL"
)
_SELECT_SEARCH_PATH_TARGET_IN_PROBE_SCHEMA_SQL = (
    "SELECT to_regclass('probe_search_path_schema.probe_search_path_target') IS NOT NULL"
)


def _copy_0001_init_into(tmp_path: Path) -> None:
    package = importlib.resources.files("catan_bot.db.migrations")
    text = (package / "0001_init.sql").read_text(encoding="utf-8")
    (tmp_path / "0001_init.sql").write_text(text, encoding="utf-8")


async def _is_recorded(conn: asyncpg.Connection, version: str) -> bool:
    row = await conn.fetchval(_SELECT_SCHEMA_MIGRATIONS_VERSION_SQL, version)
    return row is not None


@pytest.mark.parametrize("case", sorted(_BAD_PROBE_CASES))
async def test_migration_manipulating_transaction_is_not_recorded(
    tmp_path: Path, migrator_conn: asyncpg.Connection, case: str
) -> None:
    table_name, control_stmt, drop_table_sql = _BAD_PROBE_CASES[case]
    version = f"9001_probe_{case}.sql"

    _copy_0001_init_into(tmp_path)
    # Writing the probe *migration file's contents* with an f-string is just
    # file text, not a query call -- the query call below (the cleanup DROP)
    # is always the literal constant from _BAD_PROBE_CASES.
    (tmp_path / version).write_text(
        f"CREATE TABLE {table_name} (id INT);\n{control_stmt}\n", encoding="utf-8"
    )

    dsn = os.environ["TEST_MIGRATOR_DATABASE_URL"]
    try:
        with pytest.raises(RuntimeError, match=version):
            await run_migrations(dsn, source=tmp_path)

        assert not await _is_recorded(migrator_conn, version)
    finally:
        # The probe's CREATE TABLE was already committed by the time the
        # runner noticed -- this is exactly the manual cleanup an operator
        # would have to do.
        await migrator_conn.execute(drop_table_sql)


async def test_migration_with_plpgsql_do_block_applies_successfully(
    tmp_path: Path, migrator_conn: asyncpg.Connection
) -> None:
    version = "9001_probe_plpgsql_ok.sql"

    _copy_0001_init_into(tmp_path)
    (tmp_path / version).write_text(
        f"CREATE TABLE {_PLPGSQL_PROBE_TABLE_NAME} (id INT);\n"
        "DO $$ BEGIN\n"
        "  RAISE NOTICE 'plpgsql BEGIN/END is not top-level transaction control';\n"
        "END $$;\n",
        encoding="utf-8",
    )

    dsn = os.environ["TEST_MIGRATOR_DATABASE_URL"]
    try:
        applied = await run_migrations(dsn, source=tmp_path)

        assert version in applied
        assert await _is_recorded(migrator_conn, version)
    finally:
        await migrator_conn.execute(_DROP_PLPGSQL_PROBE_TABLE_SQL)
        await migrator_conn.execute(_DELETE_SCHEMA_MIGRATIONS_VERSION_SQL, version)


@pytest.mark.parametrize("case", sorted(_BAD_PROBE_WITH_RUNTIME_ERROR_CASES))
async def test_migration_error_after_transaction_control_reports_context(
    tmp_path: Path, migrator_conn: asyncpg.Connection, case: str
) -> None:
    table_name, control_and_error_stmt, drop_table_sql = _BAD_PROBE_WITH_RUNTIME_ERROR_CASES[case]
    version = f"9001_probe_{case}.sql"

    _copy_0001_init_into(tmp_path)
    (tmp_path / version).write_text(
        f"CREATE TABLE {table_name} (id INT);\n{control_and_error_stmt}\n", encoding="utf-8"
    )

    dsn = os.environ["TEST_MIGRATOR_DATABASE_URL"]
    try:
        with pytest.raises(RuntimeError, match="failed after altering the surrounding transaction"):
            await run_migrations(dsn, source=tmp_path)

        assert not await _is_recorded(migrator_conn, version)
    finally:
        # The CREATE TABLE already committed (via END/COMMIT AND CHAIN)
        # before the later `SELECT 1/0` failed.
        await migrator_conn.execute(drop_table_sql)


async def test_session_state_does_not_leak_between_migrations(
    tmp_path: Path, migrator_conn: asyncpg.Connection
) -> None:
    """A migration that changes `search_path` must not affect the next one --
    `run_migrations()` issues `RESET ALL` after each successful migration."""
    version_1 = "9001_probe_search_path_set.sql"
    version_2 = "9002_probe_search_path_target.sql"

    _copy_0001_init_into(tmp_path)
    (tmp_path / version_1).write_text(
        f"CREATE SCHEMA IF NOT EXISTS {_SEARCH_PATH_PROBE_SCHEMA};\n"
        f"SET search_path = {_SEARCH_PATH_PROBE_SCHEMA};\n",
        encoding="utf-8",
    )
    (tmp_path / version_2).write_text(
        f"CREATE TABLE {_SEARCH_PATH_PROBE_TABLE} (id INT);\n",
        encoding="utf-8",
    )

    dsn = os.environ["TEST_MIGRATOR_DATABASE_URL"]
    try:
        applied = await run_migrations(dsn, source=tmp_path)

        assert applied == [version_1, version_2]
        assert await _is_recorded(migrator_conn, version_1)
        assert await _is_recorded(migrator_conn, version_2)

        # RESET ALL after migration 1 means migration 2 ran with the normal
        # search_path, so its unqualified CREATE TABLE landed in `public`,
        # not in the schema migration 1 switched to.
        assert await migrator_conn.fetchval(_SELECT_SEARCH_PATH_TARGET_IN_PUBLIC_SQL) is True
        assert await migrator_conn.fetchval(_SELECT_SEARCH_PATH_TARGET_IN_PROBE_SCHEMA_SQL) is False
    finally:
        await migrator_conn.execute(_DROP_SEARCH_PATH_PROBE_TABLE_SQL)
        await migrator_conn.execute(_DROP_SEARCH_PATH_PROBE_SCHEMA_SQL)
        await migrator_conn.execute(_DELETE_SCHEMA_MIGRATIONS_VERSION_SQL, version_1)
        await migrator_conn.execute(_DELETE_SCHEMA_MIGRATIONS_VERSION_SQL, version_2)
