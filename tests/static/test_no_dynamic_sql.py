"""Static SQL-injection guard.

Scans the real `src/` tree for violations of the project's SQL rules (see
CLAUDE.md), and self-tests the checker itself (`sql_guard.py`) against known
-bad and known-good sample sources, so the guard is provably not a no-op.
"""

from __future__ import annotations

from pathlib import Path

from sql_guard import find_violations_in_source, scan_tree

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def test_real_source_tree_has_no_violations() -> None:
    violations = scan_tree(SRC_ROOT)
    assert violations == [], "\n" + "\n".join(str(v) for v in violations)


# --- Self-tests: known-bad samples must be flagged --------------------------


def test_fstring_query_to_execute_is_flagged() -> None:
    source = (
        "async def report(conn, name):\n"
        "    return await conn.execute(f\"SELECT * FROM players WHERE name = '{name}'\")\n"
    )
    violations = find_violations_in_source(source, "catan_bot/db/repositories/players.py")
    assert any("execute" in v.message for v in violations)


def test_format_query_is_flagged() -> None:
    source = (
        "async def report(conn, name):\n"
        "    query = \"SELECT * FROM players WHERE name = '{}'\".format(name)\n"
        "    return await conn.execute(query)\n"
    )
    violations = find_violations_in_source(source, "catan_bot/db/repositories/players.py")
    assert any("format" in v.message for v in violations)


def test_percent_formatting_is_flagged() -> None:
    source = "def build(name):\n    return \"SELECT * FROM players WHERE name = '%s'\" % name\n"
    violations = find_violations_in_source(source, "catan_bot/db/repositories/players.py")
    assert any("%-formatting" in v.message for v in violations)


def test_concatenation_is_flagged() -> None:
    source = 'def build(table):\n    return "SELECT * FROM " + table\n'
    violations = find_violations_in_source(source, "catan_bot/db/repositories/players.py")
    assert any("concatenation" in v.message for v in violations)


def test_local_variable_bound_to_query_is_flagged() -> None:
    source = (
        'async def report(conn):\n    query = "SELECT 1"\n    return await conn.execute(query)\n'
    )
    violations = find_violations_in_source(source, "catan_bot/db/repositories/players.py")
    assert any("must use a string literal" in v.message for v in violations)


def test_sql_call_outside_repositories_is_flagged() -> None:
    source = (
        '_QUERY = "SELECT 1"\n\n\nasync def run(conn):\n    return await conn.execute(_QUERY)\n'
    )
    violations = find_violations_in_source(source, "catan_bot/bot.py")
    assert any("only allowed in" in v.message for v in violations)


def test_fstring_sql_anywhere_is_flagged() -> None:
    source = 'def build(name):\n    return f"DROP TABLE {name}"\n'
    violations = find_violations_in_source(source, "catan_bot/bot.py")
    assert any("f-string" in v.message for v in violations)


# --- Self-tests: known-good samples must pass cleanly -----------------------


def test_constant_literal_query_is_clean() -> None:
    source = (
        "async def report(conn, name):\n"
        '    return await conn.execute("SELECT * FROM players WHERE name = $1", name)\n'
    )
    assert find_violations_in_source(source, "catan_bot/db/repositories/players.py") == []


def test_module_level_constant_reference_is_clean() -> None:
    source = (
        '_SELECT_PLAYER_SQL = "SELECT * FROM players WHERE name = $1"\n\n\n'
        "async def report(conn, name):\n"
        "    return await conn.fetchrow(_SELECT_PLAYER_SQL, name)\n"
    )
    assert find_violations_in_source(source, "catan_bot/db/repositories/players.py") == []


def test_migration_file_allowlisted_exec_is_clean() -> None:
    source = (
        "async def run_migrations(conn, sql_text, version):\n"
        "    await conn.execute(sql_text)  # _APPLY_MIGRATION_FILE_SQL_ALLOWLISTED\n"
        '    await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)\n'
    )
    assert find_violations_in_source(source, "catan_bot/db/migrate.py") == []


def test_non_sql_fstring_elsewhere_is_clean() -> None:
    source = 'def log_line(name):\n    return f"loaded extension {name}"\n'
    assert find_violations_in_source(source, "catan_bot/bot.py") == []


def test_non_sql_concatenation_elsewhere_is_clean() -> None:
    source = 'def greet(name):\n    return "hello " + name\n'
    assert find_violations_in_source(source, "catan_bot/bot.py") == []
