"""Static migration/table-tracking guard: self-tests + the real-tree scan.

See `tests/static/migration_table_guard.py`'s module docstring for the CI
postmortem this guards against and the rule it enforces. Same self-testing
shape as `tests/static/test_no_dynamic_sql.py` and
`tests/static/test_param_type_guard.py`: a real-tree scan that must be
non-vacuous and clean, plus planted-bad and known-good samples run
straight through the checker functions in isolation.
"""

from __future__ import annotations

from pathlib import Path

from migration_table_guard import (
    MIGRATIONS_ROOT,
    SQL_INJECTION_TEST_PATH,
    TABLE_NOT_FROM_A_MIGRATION_FILE,
    load_expected_count_queries,
    load_expected_tables,
    stale_expected_tables,
    tables_created_by_migrations,
    untracked_tables,
)

# ---------------------------------------------------------------------------
# The real tree: must be scanned (non-vacuously) and clean.
# ---------------------------------------------------------------------------


def test_real_migrations_tree_is_scanned_and_non_vacuous() -> None:
    assert MIGRATIONS_ROOT.is_dir()
    created = tables_created_by_migrations()
    # Non-vacuous: a regression that silently made the regex match nothing
    # would otherwise "pass" every assertion below for the wrong reason.
    assert len(created) >= 10, created
    assert "guild_config" in created
    assert "game_score_requests" in created  # 0005 -- the table this guard exists for
    assert "game_updates" in created  # 0004


def test_real_sql_injection_test_all_tables_is_loadable_and_non_vacuous() -> None:
    assert SQL_INJECTION_TEST_PATH.is_file()
    expected = load_expected_tables()
    assert len(expected) >= 10, expected
    assert "game_score_requests" in expected


def test_every_migration_table_is_tracked_in_the_integration_suite() -> None:
    """The primary guard: every `CREATE TABLE` across
    `src/catan_bot/db/migrations/*.sql` must appear in `_ALL_TABLES`
    (`tests/integration/test_sql_injection.py`). This is exactly the check
    that would have failed the moment `0005_score_collection.sql` was
    written, with no Postgres required."""
    missing = untracked_tables()
    assert missing == set(), (
        f"migration(s) create table(s) {sorted(missing)} not present in "
        "_ALL_TABLES (tests/integration/test_sql_injection.py) -- add them "
        "there, and to _TABLE_COUNT_QUERIES, alongside the migration."
    )


def test_no_stale_entries_in_all_tables() -> None:
    """The converse: `_ALL_TABLES` must never list a table nothing creates
    -- catches a typo, or a leftover entry from a table that was renamed
    or dropped in a later migration."""
    stale = stale_expected_tables()
    assert stale == set(), (
        f"_ALL_TABLES lists table(s) {sorted(stale)} that no migration creates "
        f"(and which aren't {TABLE_NOT_FROM_A_MIGRATION_FILE!r}) -- remove or fix them."
    )


def test_table_count_queries_covers_exactly_the_non_schema_migrations_tables() -> None:
    """`_TABLE_COUNT_QUERIES` (used by `_table_counts` in the same file) must
    have exactly one entry per table in `_ALL_TABLES` other than
    `schema_migrations` -- catan_app has no grants on that table, so a
    `SELECT COUNT(*)` against it would itself raise
    `InsufficientPrivilegeError`. Guards the second half of the same class
    of bug: it's possible to update `_ALL_TABLES` for a new table and
    forget `_TABLE_COUNT_QUERIES`, or vice versa."""
    all_tables = load_expected_tables()
    count_queries = load_expected_count_queries()
    expected_keys = all_tables - {TABLE_NOT_FROM_A_MIGRATION_FILE}
    assert set(count_queries) == expected_keys


# ---------------------------------------------------------------------------
# Self-tests: prove the checker itself catches (and doesn't over-catch),
# entirely on planted tmp_path fixtures -- never touching the real tree.
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


def test_tables_created_by_migrations_finds_plain_and_qualified_names(tmp_path: Path) -> None:
    _write(
        tmp_path / "0001_init.sql",
        "CREATE TABLE widgets (\n    id BIGINT PRIMARY KEY\n);\n"
        'CREATE TABLE IF NOT EXISTS "Gadgets" (id BIGINT);\n',
    )
    _write(tmp_path / "0002_more.sql", "CREATE TABLE public.gizmos (id BIGINT);\n")

    found = tables_created_by_migrations(tmp_path)

    assert found == {"widgets", "gadgets", "gizmos"}


def test_tables_created_by_migrations_is_non_recursive(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    _write(nested / "0001_hidden.sql", "CREATE TABLE hidden_table (id BIGINT);\n")
    _write(tmp_path / "0001_visible.sql", "CREATE TABLE visible_table (id BIGINT);\n")

    found = tables_created_by_migrations(tmp_path)

    assert found == {"visible_table"}


def test_tables_created_by_migrations_ignores_non_sql_files(tmp_path: Path) -> None:
    _write(tmp_path / "0001_real.sql", "CREATE TABLE real_table (id BIGINT);\n")
    _write(tmp_path / "notes.txt", "CREATE TABLE fake_table (id BIGINT);\n")

    found = tables_created_by_migrations(tmp_path)

    assert found == {"real_table"}


def _fake_sql_injection_module(tmp_path: Path, table_names: frozenset[str]) -> Path:
    literal = ", ".join(f'"{name}"' for name in sorted(table_names))
    # This builds literal Python *source text* for a planted fixture file
    # (never executed as SQL, never reaches a conn.execute/fetch call) --
    # not a real query, so the SQL-injection heuristic below is a false
    # positive on this specific line.
    count_queries_lines = "\n".join(
        f'    "{name}": "SELECT COUNT(*) FROM {name}",'  # noqa: S608
        for name in sorted(table_names)
        if name != TABLE_NOT_FROM_A_MIGRATION_FILE
    )
    return _write(
        tmp_path / "fake_test_sql_injection.py",
        f"_ALL_TABLES = frozenset({{{literal}}})\n"
        f"_TABLE_COUNT_QUERIES = {{\n{count_queries_lines}\n}}\n",
    )


def test_load_expected_tables_reads_all_tables_from_a_planted_module(tmp_path: Path) -> None:
    fake = _fake_sql_injection_module(tmp_path, frozenset({"a_table", "b_table"}))

    assert load_expected_tables(fake) == frozenset({"a_table", "b_table"})


def test_load_expected_count_queries_reads_table_count_queries_from_a_planted_module(
    tmp_path: Path,
) -> None:
    fake = _fake_sql_injection_module(tmp_path, frozenset({"a_table", "schema_migrations"}))

    assert load_expected_count_queries(fake) == {"a_table": "SELECT COUNT(*) FROM a_table"}


def test_untracked_tables_is_empty_when_migration_and_expected_set_match(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(migrations_dir / "0001_init.sql", "CREATE TABLE widgets (id BIGINT);\n")
    fake_test = _fake_sql_injection_module(tmp_path, frozenset({"widgets", "schema_migrations"}))

    assert untracked_tables(migrations_dir, fake_test) == set()


def test_untracked_tables_catches_a_new_table_the_expected_set_never_learned_about(
    tmp_path: Path,
) -> None:
    """This is the exact shape of the real CI bug: a migration adds a new
    table and the expected-table set is never updated to match."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(migrations_dir / "0001_init.sql", "CREATE TABLE widgets (id BIGINT);\n")
    _write(
        migrations_dir / "0002_add_gadgets.sql",
        "CREATE TABLE gadgets (id BIGINT);\n",
    )
    fake_test = _fake_sql_injection_module(tmp_path, frozenset({"widgets", "schema_migrations"}))

    assert untracked_tables(migrations_dir, fake_test) == {"gadgets"}


def test_stale_expected_tables_catches_a_leftover_entry(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(migrations_dir / "0001_init.sql", "CREATE TABLE widgets (id BIGINT);\n")
    fake_test = _fake_sql_injection_module(
        tmp_path, frozenset({"widgets", "long_gone_table", "schema_migrations"})
    )

    assert stale_expected_tables(migrations_dir, fake_test) == {"long_gone_table"}


def test_stale_expected_tables_tolerates_the_schema_migrations_exception(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write(migrations_dir / "0001_init.sql", "CREATE TABLE widgets (id BIGINT);\n")
    fake_test = _fake_sql_injection_module(tmp_path, frozenset({"widgets", "schema_migrations"}))

    assert stale_expected_tables(migrations_dir, fake_test) == set()
