"""Static SQL-injection guard: self-tests + the real-tree scan.

Scans the real `src/` tree for violations of the project's SQL rules (see
CLAUDE.md), and self-tests the checker itself (`sql_guard.py`) against
known-bad samples (drawn from a security audit's bypass catalogue -- see
`tests/static/sql_guard.py`'s module docstring for the three rule layers),
known-good samples, and an asyncpg-introspection test that fails if a future
asyncpg upgrade adds a query-bearing sink the guard doesn't know about.
"""

from __future__ import annotations

import inspect
import shutil
from pathlib import Path

import asyncpg
import asyncpg.pool
import pytest
from sql_guard import SQL_METHOD_NAMES, find_violations_in_source, scan_tree

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

REPO_PATH = "catan_bot/db/repositories/players.py"
BASE_REPO_PATH = "catan_bot/db/repositories/_base.py"
COG_PATH = "catan_bot/cogs/game_cog.py"
MIGRATE_PATH = "catan_bot/db/migrate.py"
SQL_PARTS_PATH = "catan_bot/sql_parts.py"


# ---------------------------------------------------------------------------
# The real tree: must be scanned (non-vacuously) and clean.
# ---------------------------------------------------------------------------


def test_real_source_tree_is_scanned_and_clean() -> None:
    assert SRC_ROOT.is_dir()
    result = scan_tree(SRC_ROOT)
    assert len(result.scanned_files) >= 10, result.scanned_files
    assert "catan_bot/db/migrate.py" in result.scanned_files
    assert result.violations == [], "\n" + "\n".join(str(v) for v in result.violations)


def test_scan_tree_of_a_missing_directory_scans_nothing() -> None:
    result = scan_tree(Path("/nonexistent/src"))
    assert result.scanned_files == []
    assert result.violations == []


def test_scan_tree_catches_a_planted_violation(tmp_path: Path) -> None:
    copy = tmp_path / "src"
    shutil.copytree(SRC_ROOT, copy, ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "catan_bot" / "cogs" / "planted.py").write_text(
        "async def f(conn, n):\n"
        "    return await conn.fetch(f\"SELECT * FROM players WHERE name = '{n}'\")\n"
    )
    result = scan_tree(copy)
    assert result.violations != []
    assert any("planted.py" in v.path for v in result.violations)


# ---------------------------------------------------------------------------
# Introspection: an asyncpg upgrade can't silently add an uncovered sink.
# ---------------------------------------------------------------------------


def test_asyncpg_sinks_cover_every_query_bearing_method() -> None:
    """Every public Connection/Pool method with a query/command/where/sql
    parameter must be one of the guard's known sink names."""
    interesting = {"query", "command", "where", "sql"}
    missing: set[str] = set()
    for cls in (asyncpg.Connection, asyncpg.pool.Pool):
        for name, member in inspect.getmembers(cls):
            if name.startswith("_") or not callable(member):
                continue
            try:
                sig = inspect.signature(member)
            except (TypeError, ValueError):
                continue
            if interesting & set(sig.parameters) and name not in SQL_METHOD_NAMES:
                missing.add(f"{cls.__name__}.{name}")
    assert not missing, f"asyncpg methods not covered by SQL_METHOD_NAMES: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Audit bypass samples that must be flagged, single file per sample.
# id prefixes match the security audit's bypass catalogue for traceability.
# ---------------------------------------------------------------------------

_BAD_SINGLE_FILE = [
    pytest.param(
        REPO_PATH,
        "async def f(conn, untrusted):\n    run = conn.execute\n    return await run(untrusted)\n",
        id="01-alias-run-eq-conn-execute",
    ),
    pytest.param(
        REPO_PATH,
        'async def f(conn, untrusted):\n    return await getattr(conn, "execute")(untrusted)\n',
        id="02-getattr-execute",
    ),
    pytest.param(
        REPO_PATH,
        "async def f(conn, untrusted):\n    return await conn.fetch(query=untrusted)\n",
        id="03a-query-kwarg-dynamic",
    ),
    pytest.param(
        REPO_PATH,
        'async def f(conn, untrusted):\n    return await conn.fetch(untrusted, query="SELECT 1")\n',
        id="03b-positional-dynamic-plus-query-kwarg-const",
    ),
    pytest.param(
        REPO_PATH,
        "async def f(conn, untrusted):\n"
        "    return await conn.executemany(command=untrusted, args=[(1,)])\n",
        id="03c-executemany-command-kwarg",
    ),
    pytest.param(
        REPO_PATH,
        "import textwrap\n\n\nasync def f(conn, untrusted):\n"
        "    return await conn.fetch(textwrap.dedent(\n"
        "        f\"SELECT * FROM players WHERE name = '{untrusted}'\"\n    ))\n",
        id="05-dedent-fstring",
    ),
    pytest.param(
        REPO_PATH,
        "async def f(conn, untrusted):\n"
        '    q = "".join(["SELECT * FROM players WHERE name = \'", untrusted, "\'"])\n'
        "    return await conn.fetch(q)\n",
        id="06a-join-passed-to-fetch",
    ),
    pytest.param(
        REPO_PATH,
        "def build(untrusted):\n"
        '    return "".join(["SELECT * FROM players WHERE name = \'", untrusted, "\'"])\n',
        id="06b-join-construction-only-no-sink",
    ),
    pytest.param(
        REPO_PATH,
        "async def f(conn, untrusted):\n"
        "    return await conn.fetch(\n"
        '        str.__add__("SELECT * FROM players WHERE name = ", untrusted)\n    )\n',
        id="08a-str-dunder-add-passed-to-fetch",
    ),
    pytest.param(
        REPO_PATH,
        "def build(untrusted):\n"
        '    return str.__add__("SELECT * FROM players WHERE name = ", untrusted)\n',
        id="08b-str-dunder-add-construction-only",
    ),
    pytest.param(
        REPO_PATH,
        "async def f(conn, untrusted):\n"
        "    tmpl = \"SELECT * FROM players WHERE name = '%s'\"\n"
        "    q = tmpl % untrusted\n    return await conn.fetch(q)\n",
        id="09a-percent-via-local-var-passed-to-fetch",
    ),
    pytest.param(
        REPO_PATH,
        "def build(untrusted):\n"
        "    tmpl = \"SELECT * FROM players WHERE name = '%s'\"\n"
        "    return tmpl % untrusted\n",
        id="09b-percent-via-local-var-construction-only",
    ),
    pytest.param(
        REPO_PATH,
        'import os\nQ = "SELECT 1"\nQ = os.environ["UNTRUSTED"]\n\n\n'
        "async def f(conn):\n    return await conn.fetch(Q)\n",
        id="10a-module-constant-reassigned",
    ),
    pytest.param(
        REPO_PATH,
        'Q = "SELECT 1"\n\n\ndef set_q(untrusted):\n    global Q\n    Q = untrusted\n\n\n'
        "async def f(conn):\n    return await conn.fetch(Q)\n",
        id="10b-module-constant-reassigned-via-global",
    ),
    pytest.param(
        REPO_PATH,
        'import os\nQ = "SELECT * FROM players WHERE name = "\nQ += os.environ["UNTRUSTED"]\n\n\n'
        "async def f(conn):\n    return await conn.fetch(Q)\n",
        id="10c-module-constant-augmented",
    ),
    pytest.param(
        REPO_PATH,
        'Q = "SELECT 1"\n\n\nasync def f(conn, untrusted):\n    globals()["Q"] = untrusted\n'
        "    return await conn.fetch(Q)\n",
        id="10d-globals-rebind",
    ),
    pytest.param(
        REPO_PATH,
        'Q = "SELECT 1"\n\n\nasync def f(conn, untrusted):\n'
        '    Q = "".join(["SELECT * FROM players WHERE name = \'", untrusted, "\'"])\n'
        "    return await conn.fetch(Q)\n",
        id="11a-constant-shadowed-by-local",
    ),
    pytest.param(
        REPO_PATH,
        'Q = "SELECT 1"\n\n\nasync def f(conn, Q):\n    return await conn.fetch(Q)\n',
        id="11b-constant-shadowed-by-parameter",
    ),
    pytest.param(
        REPO_PATH,
        'Q = "SELECT 1"\n\n\nasync def f(conn, untrusted_list):\n'
        "    for Q in untrusted_list:\n        await conn.execute(Q)\n"
        "    if Q := untrusted_list[0]:\n        await conn.fetch(Q)\n",
        id="11c-constant-shadowed-by-for-target-and-walrus",
    ),
    pytest.param(
        REPO_PATH,
        "async def f(pool, untrusted):\n    async with pool.acquire() as c:\n"
        "        return await c.fetch(f\"SELECT * FROM players WHERE name = '{untrusted}'\")\n",
        id="12-with-acquire-fstring",
    ),
    pytest.param(
        REPO_PATH,
        "import asyncpg\n\n\nasync def f(conn, untrusted):\n"
        "    return await asyncpg.Connection.execute(conn, untrusted)\n",
        id="14-unbound-connection-execute",
    ),
    pytest.param(
        REPO_PATH,
        "class R:\n    async def f(self, untrusted):\n"
        "        return await self._pool.fetchval(\n"
        "            f\"SELECT id FROM players WHERE name = '{untrusted}'\"\n        )\n",
        id="15a-self-pool-fetchval-fstring",
    ),
    pytest.param(
        REPO_PATH,
        "class R:\n    async def f(self, untrusted):\n        q = untrusted\n"
        "        return await self._pool.fetchval(q)\n",
        id="15b-self-pool-fetchval-dynamic-local",
    ),
    pytest.param(
        REPO_PATH,
        "async def f(conn, untrusted):\n"
        '    return await __import__("asyncpg").Connection.fetch(conn, untrusted)\n',
        id="16a-dunder-import-connection-fetch",
    ),
    pytest.param(
        REPO_PATH,
        "async def f(conn, untrusted):\n"
        '    return await getattr(__import__("asyncpg").Connection, "fetch")(conn, untrusted)\n',
        id="16b-getattr-of-dunder-import-connection-fetch",
    ),
    pytest.param(
        REPO_PATH,
        "async def f(conn, untrusted):\n"
        "    run = lambda q: conn.execute(q)  # noqa: E731\n    return await run(untrusted)\n",
        id="17a-lambda-wraps-execute",
    ),
    pytest.param(
        REPO_PATH,
        'Q = "SELECT 1"\n\n\nasync def f(conn, untrusted):\n'
        "    run = lambda Q: conn.execute(Q)  # noqa: E731\n    return await run(untrusted)\n",
        id="17b-lambda-param-shadows-module-constant",
    ),
    pytest.param(
        REPO_PATH,
        "async def f(conn, untrusted):\n    return await conn.fetchmany(untrusted, [(1,)])\n",
        id="X1-fetchmany-missing-sink",
    ),
    pytest.param(
        REPO_PATH,
        "async def f(conn, untrusted):\n"
        "    return await conn.copy_records_to_table(\n"
        '        "players", records=[(1, 2)], where=untrusted\n    )\n',
        id="X2-copy-records-to-table-where",
    ),
    pytest.param(
        REPO_PATH,
        "async def f(conn, src, untrusted):\n"
        '    return await conn.copy_to_table("players", source=src, where=untrusted)\n',
        id="X3-copy-to-table-where",
    ),
    pytest.param(
        REPO_PATH,
        "import functools\n\n\nasync def f(conn, untrusted):\n"
        "    return await functools.partial(conn.execute, untrusted)()\n",
        id="X4-functools-partial-conn-execute",
    ),
    pytest.param(
        REPO_PATH,
        "import operator\n\n\nasync def f(conn, untrusted):\n"
        '    return await operator.methodcaller("execute", untrusted)(conn)\n',
        id="X5-operator-methodcaller-execute",
    ),
    pytest.param(
        REPO_PATH,
        'def build(col, t):\n    a = f"ORDER BY {col} LIMIT 5"\n    b = f"TRUNCATE {t}"\n'
        '    c = "ALTER TABLE " + t\n    return a, b, c\n',
        id="X8-order-by-truncate-alter-table",
    ),
    pytest.param(
        REPO_PATH,
        "import string\n\n\ndef build(untrusted):\n"
        "    tmpl = string.Template(\"SELECT * FROM players WHERE name = '$n'\")\n"
        "    return tmpl.substitute(n=untrusted)\n",
        id="X9-string-template-substitute",
    ),
    # Location rule: a sound constant sink call still isn't allowed outside
    # repositories/ or migrate.py.
    pytest.param(
        COG_PATH,
        "import asyncpg\n\n\nasync def f(dsn):\n    conn = await asyncpg.connect(dsn)\n"
        '    return await conn.execute("SELECT 1")\n',
        id="13b-connect-and-execute-const-outside-repositories",
    ),
]


@pytest.mark.parametrize(("rel_path", "source"), _BAD_SINGLE_FILE)
def test_bypass_sample_is_flagged(rel_path: str, source: str) -> None:
    violations = find_violations_in_source(source, rel_path)
    assert violations, f"expected a violation for:\n{source}"


# ---------------------------------------------------------------------------
# Audit bypass samples spanning multiple files.
# ---------------------------------------------------------------------------

_BAD_MULTI_FILE = [
    pytest.param(
        {
            SQL_PARTS_PATH: (
                "import os\n"
                "PLAYER_SQL = ''.join([\"SELECT * FROM players WHERE name = '\", "
                "os.environ.get('N', ''), \"'\"])\n"
            ),
            REPO_PATH: (
                "from catan_bot.sql_parts import PLAYER_SQL\n\n"
                "async def f(conn):\n    return await conn.fetch(PLAYER_SQL)\n"
            ),
        },
        id="04a-sql-built-in-other-module-imported",
    ),
    pytest.param(
        {
            SQL_PARTS_PATH: (
                "import os\nQ = ''.join(['SELECT * FROM players WHERE name = ', "
                "os.environ.get('N', '')])\n"
            ),
            REPO_PATH: (
                'Q = "SELECT 1"\nfrom catan_bot.sql_parts import Q  # noqa: E402,F811\n\n'
                "async def f(conn):\n    return await conn.fetch(Q)\n"
            ),
        },
        id="04b-import-shadows-repo-constant",
    ),
    pytest.param(
        {
            BASE_REPO_PATH: (
                "async def run_sql(conn, sql, *args):\n    return await conn.fetch(sql, *args)\n"
            ),
            COG_PATH: (
                "from catan_bot.db.repositories._base import run_sql\n\n"
                "async def cmd(conn, untrusted):\n"
                '    return await run_sql(conn, "".join(\n'
                '        ["SELECT * FROM players WHERE name = \'", untrusted, "\'"]\n    ))\n'
            ),
        },
        id="07a-wrapper-helper-plus-cog-caller",
    ),
    pytest.param(
        {
            BASE_REPO_PATH: (
                'SQL = "SELECT 1"\n\n\nasync def run_sql(conn, SQL, *args):\n'
                "    return await conn.fetch(SQL, *args)\n"
            ),
            COG_PATH: (
                "from catan_bot.db.repositories._base import run_sql\n\n"
                "async def cmd(conn, untrusted):\n"
                '    return await run_sql(conn, "".join(\n'
                '        ["SELECT * FROM players WHERE name = \'", untrusted, "\'"]\n    ))\n'
            ),
        },
        id="07b-wrapper-param-shadows-module-constant",
    ),
    pytest.param(
        {
            MIGRATE_PATH: (
                "async def f(conn, untrusted):\n"
                "    await conn.execute(untrusted)  # _APPLY_MIGRATION_FILE_SQL_ALLOWLISTED\n"
            ),
        },
        id="X6-migrate-second-dynamic-execute-copied-marker",
    ),
    pytest.param(
        {
            MIGRATE_PATH: (
                "async def f(conn, untrusted):\n"
                "    await conn.execute(untrusted); _ = '_APPLY_MIGRATION_FILE_SQL_ALLOWLISTED'\n"
            ),
        },
        id="X7-migrate-marker-inside-string-literal",
    ),
]


@pytest.mark.parametrize("files", _BAD_MULTI_FILE)
def test_bypass_sample_is_flagged_across_files(files: dict[str, str]) -> None:
    violations = [v for path, src in files.items() for v in find_violations_in_source(src, path)]
    assert violations, f"expected a violation somewhere in:\n{files}"


# ---------------------------------------------------------------------------
# Control case: no sink call and nothing SQL-shaped built -> nothing to flag.
# ---------------------------------------------------------------------------


def test_connect_with_no_query_call_is_not_flagged() -> None:
    source = "import asyncpg\n\n\nasync def f(dsn):\n    return await asyncpg.connect(dsn)\n"
    assert find_violations_in_source(source, COG_PATH) == []


# ---------------------------------------------------------------------------
# Negative samples: ordinary English f-strings in Discord cogs must be clean.
# ---------------------------------------------------------------------------

_ENGLISH_FSTRINGS = [
    pytest.param(
        'f"Game {game_id} confirmed by {user} from the server"', id="game-confirmed-by-from"
    ),
    pytest.param('f"Set the timezone to {tz}"', id="set-the-timezone-to"),
    pytest.param('f"Where is {name}?"', id="where-is-name"),
    pytest.param('f"Update: season {name} ends on {date}"', id="update-season-ends-on"),
    pytest.param('f"Select a winner from {n} players"', id="select-a-winner-from"),
]


@pytest.mark.parametrize("expr", _ENGLISH_FSTRINGS)
def test_normal_english_fstring_is_not_flagged(expr: str) -> None:
    source = f"def build(game_id, user, tz, name, date, n):\n    return {expr}\n"
    assert find_violations_in_source(source, COG_PATH) == []


# ---------------------------------------------------------------------------
# Good samples: sound constants in the right place must stay clean.
# ---------------------------------------------------------------------------


def test_constant_literal_query_is_clean() -> None:
    source = (
        "async def report(conn, name):\n"
        '    return await conn.execute("SELECT * FROM players WHERE name = $1", name)\n'
    )
    assert find_violations_in_source(source, REPO_PATH) == []


def test_module_level_constant_with_dollar_param_in_repository_is_clean() -> None:
    source = (
        '_SELECT_PLAYER_SQL = "SELECT * FROM players WHERE name = $1"\n\n\n'
        "async def report(conn, name):\n"
        "    return await conn.fetchrow(_SELECT_PLAYER_SQL, name)\n"
    )
    assert find_violations_in_source(source, REPO_PATH) == []


def test_real_migrate_py_allowlist_pattern_is_clean() -> None:
    source = (
        "async def run_migrations(conn, sql_text, version):\n"
        "    await conn.execute(sql_text)  # _APPLY_MIGRATION_FILE_SQL_ALLOWLISTED\n"
        '    await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)\n'
    )
    assert find_violations_in_source(source, MIGRATE_PATH) == []


def test_table_sink_with_sound_constants_is_clean() -> None:
    # `columns` must be a literal tuple/list of constants at the call site --
    # a Name pointing at one isn't (rule 3 only special-cases the literal).
    source = (
        '_TABLE = "players"\n\n\n'
        "async def dump(conn, records):\n"
        "    return await conn.copy_records_to_table(\n"
        '        _TABLE, records=records, columns=("id", "name")\n'
        "    )\n"
    )
    assert find_violations_in_source(source, REPO_PATH) == []


def test_non_sql_fstring_elsewhere_is_clean() -> None:
    source = 'def log_line(name):\n    return f"loaded extension {name}"\n'
    assert find_violations_in_source(source, "catan_bot/bot.py") == []


def test_non_sql_concatenation_elsewhere_is_clean() -> None:
    source = 'def greet(name):\n    return "hello " + name\n'
    assert find_violations_in_source(source, "catan_bot/bot.py") == []


# ---------------------------------------------------------------------------
# Stacked-statement rule (rule 4): a sound constant still can't smuggle a
# second statement past the semicolon -- asyncpg's simple protocol (no args)
# would run it.
# ---------------------------------------------------------------------------


def test_stacked_statement_in_constant_is_flagged() -> None:
    source = (
        '_SQL = "SELECT 1; DROP TABLE players"\n\n\n'
        "async def f(conn):\n    return await conn.execute(_SQL)\n"
    )
    violations = find_violations_in_source(source, REPO_PATH)
    assert any("stacked statement" in v.message for v in violations)


def test_single_trailing_semicolon_is_clean() -> None:
    source = '_SQL = "SELECT 1;"\n\n\nasync def f(conn):\n    return await conn.execute(_SQL)\n'
    assert find_violations_in_source(source, REPO_PATH) == []


# ---------------------------------------------------------------------------
# Sanity checks on the older, simpler self-tests kept from before hardening.
# ---------------------------------------------------------------------------


def test_fstring_query_to_execute_is_flagged() -> None:
    source = (
        "async def report(conn, name):\n"
        "    return await conn.execute(f\"SELECT * FROM players WHERE name = '{name}'\")\n"
    )
    violations = find_violations_in_source(source, REPO_PATH)
    assert any("execute" in v.message for v in violations)


def test_format_query_is_flagged() -> None:
    source = (
        "async def report(conn, name):\n"
        "    query = \"SELECT * FROM players WHERE name = '{}'\".format(name)\n"
        "    return await conn.execute(query)\n"
    )
    violations = find_violations_in_source(source, REPO_PATH)
    assert any("format" in v.message for v in violations)


def test_sql_call_outside_repositories_is_flagged() -> None:
    source = (
        '_QUERY = "SELECT 1"\n\n\nasync def run(conn):\n    return await conn.execute(_QUERY)\n'
    )
    violations = find_violations_in_source(source, "catan_bot/bot.py")
    assert any("only allowed in" in v.message for v in violations)
