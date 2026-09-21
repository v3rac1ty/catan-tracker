"""Static guard: every table a migration creates must be tracked by the
integration SQL-injection suite's expected schema.

CI postmortem this exists for: `0005_score_collection.sql` added a new
table, `game_score_requests`, but `tests/integration/test_sql_injection.py`'s
hardcoded `_ALL_TABLES` (and `_TABLE_COUNT_QUERIES`) were never updated to
match. `_assert_schema_intact` in that file asserts the live database's
table set equals `_ALL_TABLES` exactly -- deliberately, since it's a
security test proving no SQL-injection payload ever created/dropped a
table, and a test that instead derived its expected set from the live
database could never notice an attacker (or an unreviewed migration)
adding one. That correctness property means the mismatch could only ever
be caught by actually running the integration suite against Postgres --
which happens in CI, but never locally on a machine with no Docker/no
Postgres, where every `pytest.mark.integration` test unconditionally
skips. The result: 171 failures (one per parametrized payload/assertion
reaching the shared schema check) that no local run had any way to catch.

This module is the Postgres-free, always-runs counterpart: it regexes
`CREATE TABLE` statements straight out of every
`src/catan_bot/db/migrations/*.sql` file and compares the result against
`_ALL_TABLES`, imported directly (by file path, not as a hand-copied
constant) from `tests/integration/test_sql_injection.py`. There is
deliberately only one hardcoded table list in the whole suite -- this
module reads it rather than duplicating it, since a second hardcoded copy
here would just relocate the exact bug it exists to catch. A future
migration that creates a table without also updating `_ALL_TABLES` (and
`_TABLE_COUNT_QUERIES`, which `test_migration_table_guard.py` checks
separately) now fails immediately, in the fast static/unit suite, on
every machine, with no database required.

Deliberate scope limits, matching this file's narrow, mechanical purpose
(not a SQL parser):

  - Only a flat, non-recursive glob of `*.sql` directly under the
    migrations directory is scanned, matching how
    `catan_bot.db.migrate._discover_migrations` itself only ever looks at
    that one directory.
  - `schema_migrations` is the one legitimate exception: it is never
    created by a numbered migration file at all, but by `migrate.py`
    itself (`_CREATE_SCHEMA_MIGRATIONS_SQL`), before any migration file
    runs -- see `TABLE_NOT_FROM_A_MIGRATION_FILE` below.
  - The regex tolerates `IF NOT EXISTS` and an optional `public.`/quoted
    prefix even though no real migration file (as of this writing) uses
    either, so it stays correct if that style is ever used later, rather
    than silently under-matching.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_ROOT = REPO_ROOT / "src" / "catan_bot" / "db" / "migrations"
SQL_INJECTION_TEST_PATH = REPO_ROOT / "tests" / "integration" / "test_sql_injection.py"

# See the module docstring: created by migrate.py itself, not by any file
# this module scans.
TABLE_NOT_FROM_A_MIGRATION_FILE = "schema_migrations"

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?\"?(\w+)\"?",
    re.IGNORECASE,
)


def tables_created_by_migrations(migrations_root: Path = MIGRATIONS_ROOT) -> set[str]:
    """Every table name in a `CREATE TABLE ...` statement across every
    `*.sql` file directly under `migrations_root` (lowercased; Postgres
    identifiers are case-folded to lowercase unless quoted, and every real
    migration file here uses plain, unquoted, lowercase names anyway)."""
    tables: set[str] = set()
    for path in sorted(migrations_root.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        tables.update(match.group(1).lower() for match in _CREATE_TABLE_RE.finditer(text))
    return tables


def load_expected_tables(path: Path = SQL_INJECTION_TEST_PATH) -> frozenset[str]:
    """Import `_ALL_TABLES` directly out of `test_sql_injection.py`, by
    file path rather than a package import.

    File-path loading is deliberate: it works regardless of pytest's
    collection order or which directories happen to already be on
    `sys.path` at the time this runs (unlike a plain `import
    tests.integration.test_sql_injection`, which would depend on both),
    and it never registers the loaded module in `sys.modules`, so it can't
    collide with pytest's own, separate import of the same file when that
    module is collected as a real test file.
    """
    spec = importlib.util.spec_from_file_location("_catan_sql_injection_schema_source", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load a module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._ALL_TABLES


def load_expected_count_queries(path: Path = SQL_INJECTION_TEST_PATH) -> dict[str, str]:
    """Import `_TABLE_COUNT_QUERIES` the same way as `load_expected_tables`."""
    spec = importlib.util.spec_from_file_location("_catan_sql_injection_schema_source", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load a module spec for {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._TABLE_COUNT_QUERIES


def untracked_tables(
    migrations_root: Path = MIGRATIONS_ROOT,
    sql_injection_test_path: Path = SQL_INJECTION_TEST_PATH,
) -> set[str]:
    """Tables a migration creates that `_ALL_TABLES` doesn't list."""
    created = tables_created_by_migrations(migrations_root)
    expected = {t.lower() for t in load_expected_tables(sql_injection_test_path)}
    return created - expected


def stale_expected_tables(
    migrations_root: Path = MIGRATIONS_ROOT,
    sql_injection_test_path: Path = SQL_INJECTION_TEST_PATH,
) -> set[str]:
    """The converse check: entries in `_ALL_TABLES` that no migration (and
    isn't the one documented `TABLE_NOT_FROM_A_MIGRATION_FILE` exception)
    actually creates -- catches a typo'd or stale entry left behind by a
    table rename or removal."""
    created = tables_created_by_migrations(migrations_root)
    expected = {t.lower() for t in load_expected_tables(sql_injection_test_path)}
    return expected - created - {TABLE_NOT_FROM_A_MIGRATION_FILE}
