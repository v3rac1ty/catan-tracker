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


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def test_asyncpg_sinks_cover_every_query_bearing_method() -> None:
    """Every Connection/Pool/PoolConnectionProxy method -- public *or*
    private, but not a dunder -- with a query/command/where/sql/copy_stmt
    parameter must be one of the guard's known sink names.

    Private asyncpg methods are real sinks reachable straight off a
    `conn`/`pool` object (e.g. `conn._execute(...)`); skipping every
    underscore-prefixed name (as an earlier version of this test did) let
    `_execute` and friends bypass the guard entirely. `copy_stmt` is
    `_copy_in`/`_copy_out`'s raw-SQL parameter -- differently named from the
    other four, but the same shape of sink, so it's included here too.
    """
    interesting = {"query", "command", "where", "sql", "copy_stmt"}
    missing: set[str] = set()
    for cls in (asyncpg.Connection, asyncpg.Pool, asyncpg.pool.PoolConnectionProxy):
        for name, member in inspect.getmembers(cls):
            if _is_dunder(name) or not callable(member):
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
    # ------------------------------------------------------------------
    # M3a audit: bypass 1 -- `**`/`*` unpacking into a sink hides the
    # query-carrying argument from the query=/command=/positional checks.
    # ------------------------------------------------------------------
    pytest.param(
        REPO_PATH,
        'async def f(conn, q):\n    return await conn.fetch(**{"query": q})\n',
        id="m3a1-01-double-star-dict-into-fetch",
    ),
    pytest.param(
        REPO_PATH,
        "async def f(conn, kwargs):\n    return await conn.execute(**kwargs)\n",
        id="m3a1-02-double-star-kwargs-into-execute",
    ),
    pytest.param(
        REPO_PATH,
        "async def f(conn, args):\n    return await conn.fetchrow(*args)\n",
        id="m3a1-03-star-args-into-fetchrow",
    ),
    pytest.param(
        REPO_PATH,
        "async def f(conn, kw):\n    return await conn.copy_records_to_table(**kw)\n",
        id="m3a1-04-double-star-into-copy-records-to-table",
    ),
    # ------------------------------------------------------------------
    # M3a audit: bypass 2 -- private asyncpg sinks (`_execute` etc.) were
    # invisible to the location rule, the constant-argument rule, and the
    # alias rule alike, since `_execute` wasn't in SQL_METHOD_NAMES.
    # ------------------------------------------------------------------
    pytest.param(
        REPO_PATH,
        "async def f(conn, q):\n    return await conn._execute(q, 1, 1, False)\n",
        id="m3a2-01-private-execute-dynamic-positional",
    ),
    pytest.param(
        REPO_PATH,
        "async def f(conn, untrusted):\n    run = conn._execute\n    return await run(untrusted)\n",
        id="m3a2-02-alias-conn-private-execute",
    ),
    pytest.param(
        REPO_PATH,
        'async def f(conn, q):\n    return await getattr(conn, "_execute")(q)\n',
        id="m3a2-03-getattr-private-execute",
    ),
    pytest.param(
        COG_PATH,
        'async def f(conn):\n    return await conn._execute("SELECT 1", (), None, None)\n',
        id="m3a2-04-private-execute-outside-repositories-const",
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
# Pre-M4 hardening (DESIGN.md "Guard backlog"): a large English negative
# corpus, so the retuned build-time SQL-shape patterns (layer 3) don't
# false-positive on ordinary Discord bot copy. Every sample is a full,
# self-contained module (like `_BAD_SINGLE_FILE` above) rather than a
# shared wrapper, since the strings need different free variables and
# construction forms (f-string / .format / '+' / '%', "where natural" --
# most of these have a natural interpolation slot; a few don't, and are
# built with `.format()` or `+` purely to exercise the checker's code
# paths on plain English text).
# ---------------------------------------------------------------------------

_SQL_SHAPE_NEGATIVE = [
    # --- The 9 audit false-positive examples (DESIGN.md Guard backlog). ---
    pytest.param(
        'def build(user):\n    return f"{user} is returning to the table"\n',
        id="audit-01-returning-to-the-table",
    ),
    pytest.param(
        'def build():\n    return "Ranked by win rate, then order by games played"\n',
        id="audit-02-ranked-by-win-rate-order-by",
    ),
    pytest.param(
        'def build(title):\n    return f"Delete from your calendar: {title}"\n',
        id="audit-03-delete-from-your-calendar",
    ),
    pytest.param(
        'def build(a, b):\n    return f"Values ({a}, {b})"\n', id="audit-04-values-paren-titlecase"
    ),
    pytest.param(
        'def build(x):\n    return f"Copy the link to {x} from the event"\n',
        id="audit-05-copy-the-link-to-from-the-event",
    ),
    pytest.param(
        'def build():\n    return "Group by table, " + "then order the snacks"\n',
        id="audit-06-group-by-table-order-the-snacks",
    ),
    pytest.param(
        'def build():\n    return "Drop table tennis night? React below".format()\n',
        id="audit-07-drop-table-tennis-night",
    ),
    pytest.param(
        'def build():\n    return "Truncate the description if it is too long".format()\n',
        id="audit-08-truncate-the-description",
    ),
    pytest.param(
        'def build(user):\n    return f"Grant {user} admin on the server? (yes/no)"\n',
        id="audit-09-grant-admin-on-the-server",
    ),
    # --- Help text. ---
    pytest.param(
        'def build():\n    return "Use /season start to begin a season from today"\n',
        id="help-01-season-start-from-today",
    ),
    pytest.param(
        'def build():\n    return "Select a winner from the list"\n',
        id="help-02-select-a-winner-from-the-list",
    ),
    pytest.param(
        'def build(date):\n    return f"Update: the season ends on {date}"\n',
        id="help-03-update-season-ends-on",
    ),
    pytest.param(
        'def build():\n    return "Set the timezone with /config timezone"\n',
        id="help-04-set-the-timezone-with-config",
    ),
    pytest.param(
        'def build(location):\n    return f"Where is game night? {location}"\n',
        id="help-05-where-is-game-night",
    ),
    # --- Error messages. ---
    pytest.param(
        "def build():\n"
        '    return "That game has already been confirmed, rejected, or voided.".format()\n',
        id="error-01-already-confirmed-rejected-voided",
    ),
    pytest.param(
        'def build():\n    return "Only the event\'s creator " + "or an admin can cancel it."\n',
        id="error-02-creator-or-admin-can-cancel",
    ),
    # --- Embed titles / leaderboard lines. ---
    pytest.param(
        "def build(rank, name, wins, losses, rate):\n"
        '    return f"#{rank} {name} — {wins}W/{losses}L ({rate}%)"\n',
        id="embed-01-rank-name-wins-losses-rate",
    ),
    pytest.param('def build():\n    return "Insert coin to continue"\n', id="embed-02-insert-coin"),
    pytest.param(
        'def build(date, place):\n    return f"Join us on {date} at {place}"\n',
        id="embed-03-join-us-on-at",
    ),
    pytest.param(
        'def build():\n    return "Create a season with /season start"\n',
        id="embed-04-create-a-season-with",
    ),
    pytest.param(
        'def build(start, end):\n    return f"From {start} to {end}"\n', id="embed-05-from-to"
    ),
    pytest.param(
        'def build(names):\n    return f"Order of play: {names}"\n', id="embed-06-order-of-play"
    ),
    pytest.param(
        'def build(n):\n    return f"Limit reached: {n} games"\n', id="embed-07-limit-reached"
    ),
    # --- Event descriptions. ---
    pytest.param(
        'def build():\n    return "Bring snacks; we start at 7"\n', id="event-01-bring-snacks"
    ),
    # --- Catan flavour. ---
    pytest.param(
        'def build(a, b):\n    return "Robber moved from %s to %s" % (a, b)\n',
        id="catan-01-robber-moved-from-to-percent-form",
    ),
    pytest.param(
        'def build():\n    return "Trade 2 wood for 1 ore"\n', id="catan-02-trade-wood-for-ore"
    ),
    # --- Extra realistic Discord bot strings, to comfortably clear the "at
    # least ~25" bar with headroom and exercise more constructions/forms. ---
    pytest.param(
        "def build(name):\n    return f\"Season '{name}' has ended. Congrats to the winners!\"\n",
        id="extra-01-season-has-ended",
    ),
    pytest.param(
        'def build():\n    return "You must be an admin to run this command."\n',
        id="extra-02-must-be-an-admin",
    ),
    pytest.param(
        'def build():\n    return "React with your choice below."\n',
        id="extra-03-react-with-your-choice",
    ),
    pytest.param(
        'def build(admin):\n    return f"The game has been voided by {admin}."\n',
        id="extra-04-voided-by-admin",
    ),
    pytest.param(
        'def build(name):\n    return f"{name} confirmed the game."\n',
        id="extra-05-confirmed-the-game",
    ),
    pytest.param(
        'def build():\n    return "No active season right now. Start one with /season start."\n',
        id="extra-06-no-active-season-right-now",
    ),
    pytest.param(
        'def build(title):\n    return "Reminder: %s starts in 1 hour!" % title\n',
        id="extra-07-reminder-starts-in-1-hour-percent-form",
    ),
    pytest.param(
        'def build(user, status):\n    return f"RSVP updated: {user} is now {status}."\n',
        id="extra-08-rsvp-updated",
    ),
    pytest.param(
        'def build(season):\n    return f"Leaderboard for {season}"\n',
        id="extra-09-leaderboard-for-season",
    ),
    pytest.param(
        "def build(member, wins, losses):\n"
        '    return f"Stats for {member}: {wins} wins, {losses} losses"\n',
        id="extra-10-stats-for-member",
    ),
    pytest.param(
        'def build(title):\n    return f"Cancelled: {title}"\n', id="extra-11-cancelled-title"
    ),
    pytest.param(
        "def build():\n"
        '    return "This command can only be used by the event creator or an admin."\n',
        id="extra-12-command-only-creator-or-admin",
    ),
    pytest.param(
        'def build():\n    return "Please select a valid member."\n',
        id="extra-13-please-select-a-valid-member",
    ),
    pytest.param(
        "def build(minutes):\n"
        "    return (\n"
        '        "The cooldown period has not elapsed yet. "\n'
        '        f"Try again in {minutes} minutes."\n'
        "    )\n",
        id="extra-14-cooldown-not-elapsed",
    ),
    pytest.param(
        'def build(channel):\n    return f"Config updated: channel set to {channel}"\n',
        id="extra-15-config-updated-channel",
    ),
    pytest.param(
        "def build(date, min_games):\n"
        '    return f"Season ends on {date}, min games required: {min_games}"\n',
        id="extra-16-season-ends-on-min-games",
    ),
    pytest.param(
        'def build(member, n):\n    return f"History for {member} (last {n} games)"\n',
        id="extra-17-history-for-member",
    ),
    pytest.param(
        'def build(tz):\n    return "Set the timezone to {}".format(tz)\n',
        id="extra-18-set-the-timezone-to-format",
    ),
    pytest.param(
        'def build(names):\n    return "Order of play: " + names\n',
        id="extra-19-order-of-play-plus-form",
    ),
]


@pytest.mark.parametrize("source", _SQL_SHAPE_NEGATIVE)
def test_sql_shape_negative_corpus_is_not_flagged(source: str) -> None:
    assert find_violations_in_source(source, COG_PATH) == []


# ---------------------------------------------------------------------------
# Pre-M4 hardening: a positive corpus of real SQL construction forms that
# must stay flagged after the layer-3 patterns were retuned for precision
# on English. Each targets a different clause/keyword/construction shape
# from DESIGN.md's Guard backlog.
# ---------------------------------------------------------------------------

_SQL_SHAPE_POSITIVE = [
    pytest.param(
        'def build(pid):\n    return f"SELECT * FROM players WHERE id = {pid}"\n',
        id="pos-01-fstring-select-from-where",
    ),
    pytest.param(
        "def build(pid, name):\n"
        '    return f"INSERT INTO players (id, name) VALUES ({pid}, {name})"\n',
        id="pos-02-fstring-insert-into-values",
    ),
    pytest.param(
        "def build(name, pid):\n"
        "    return f\"UPDATE players SET name = '{name}' WHERE id = {pid}\"\n",
        id="pos-03-fstring-update-set-where",
    ),
    pytest.param(
        'def build(pid):\n    return f"DELETE FROM players WHERE id = {pid}"\n',
        id="pos-04-fstring-delete-from-where",
    ),
    pytest.param(
        'def build(col):\n    return f"ORDER BY {col}"\n', id="pos-05-order-by-placeholder"
    ),
    pytest.param(
        'def build(col):\n    return f"GROUP BY {col}"\n', id="pos-06-group-by-placeholder"
    ),
    pytest.param('def build(n):\n    return f"LIMIT {n}"\n', id="pos-07-limit-placeholder"),
    pytest.param('def build(n):\n    return f"OFFSET {n}"\n', id="pos-08-offset-placeholder"),
    pytest.param(
        'def build(col):\n    return f"RETURNING {col}"\n', id="pos-09-returning-placeholder"
    ),
    pytest.param('def build(t):\n    return f"TRUNCATE {t}"\n', id="pos-10-truncate-placeholder"),
    pytest.param(
        'def build(a, b):\n    return f"VALUES ({a}, {b})"\n', id="pos-11-values-placeholder"
    ),
    pytest.param(
        "def build(i):\n"
        "    return (\n"
        '        f"SELECT * FROM t WHERE id={i} "\n'
        '        "UNION SELECT username, password FROM users"\n'
        "    )\n",
        id="pos-12-union-select-injection-shape",
    ),
    pytest.param(
        'def build():\n    return f"SELECT a FROM x UNION ALL SELECT b FROM y"\n',
        id="pos-13-union-all-select",
    ),
    pytest.param(
        'def build(gid, t):\n    return f"SELECT * FROM games WHERE id = {gid}; DROP TABLE {t}"\n',
        id="pos-14-stacked-drop-table",
    ),
    pytest.param(
        'def build(cols):\n    return "SELECT " + cols + " FROM t"\n',
        id="pos-15-dynamic-column-list-plus",
    ),
    pytest.param(
        'def build(c, t):\n    return " ".join(["SELECT", c, "FROM", t])\n',
        id="pos-16-dynamic-column-list-join",
    ),
    pytest.param(
        "def build(name):\n    return \"SELECT * FROM players WHERE name = '%s'\" % name\n",
        id="pos-17-percent-form",
    ),
    pytest.param(
        "import string\n\n\n"
        "def build(n):\n"
        "    tmpl = string.Template(\"SELECT * FROM players WHERE name = '$n'\")\n"
        "    return tmpl.substitute(n=n)\n",
        id="pos-18-string-template-form",
    ),
    pytest.param(
        'def build(t, i):\n    return f"select * from {t} where id = {i}"\n',
        id="pos-19-lowercase-full-sql-still-flagged",
    ),
    pytest.param(
        'def build():\n    return f"select a from x union select b from y"\n',
        id="pos-20-lowercase-union-select",
    ),
    pytest.param(
        'def build(t):\n    return f"DROP TABLE IF EXISTS {t}"\n',
        id="pos-21-drop-table-if-exists-placeholder",
    ),
    pytest.param(
        'def build(t):\n    return f"CREATE TABLE {t} (id serial)"\n',
        id="pos-22-create-table-placeholder-paren",
    ),
    pytest.param(
        'def build(t):\n    return "ALTER TABLE " + t\n',
        id="pos-23-alter-table-plus-dynamic-tail",
    ),
    pytest.param(
        'def build(role):\n    return f"DROP ROLE {role}"\n', id="pos-24-drop-role-placeholder"
    ),
    pytest.param(
        "def build(pid):\n"
        '    return f"INSERT INTO archive SELECT * FROM players WHERE id = {pid}"\n',
        id="pos-25-insert-into-select-form",
    ),
    pytest.param(
        'def build(pid):\n    return "SELECT * FROM players WHERE id = {}".format(pid)\n',
        id="pos-26-format-select-from-where",
    ),
    pytest.param(
        'def build(col):\n    return "ORDER BY {}".format(col)\n',
        id="pos-27-format-order-by-placeholder",
    ),
    pytest.param(
        'def build(t):\n    return str.__add__("DROP TABLE ", t)\n',
        id="pos-28-dunder-add-drop-table",
    ),
    pytest.param(
        'def build(col):\n    return "RETURNING " + col\n',
        id="pos-29-plus-returning-dynamic-tail",
    ),
    pytest.param(
        'def build(extra):\n    return f"SELECT id, {extra} FROM t"\n',
        id="pos-30-partially-dynamic-column-list",
    ),
]


@pytest.mark.parametrize("source", _SQL_SHAPE_POSITIVE)
def test_sql_shape_positive_corpus_is_flagged(source: str) -> None:
    assert find_violations_in_source(source, COG_PATH) != []


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


# ---------------------------------------------------------------------------
# M3a audit: private-sink lookalikes must stay clean -- adding `_execute`
# and friends to the sink sets must not turn into "flag every underscore
# attribute". Domain/repository helper names and plain private-attribute
# access on non-connection objects are unaffected.
# ---------------------------------------------------------------------------

_GOOD_SINGLE_FILE = [
    pytest.param(
        REPO_PATH,
        '_SELECT_SQL = "SELECT * FROM guilds WHERE guild_id = $1"\n\n\n'
        "async def f(conn, guild_id):\n    return await conn.fetchrow(_SELECT_SQL, guild_id)\n",
        id="m3a-good-01-sound-fetchrow-in-repository",
    ),
    pytest.param(
        REPO_PATH,
        "def build(row):\n    return _row_to_game(row)\n",
        id="m3a-good-02-row-mapper-helper-call",
    ),
    pytest.param(
        REPO_PATH,
        "class R:\n    def pool(self):\n        return self._pool\n",
        id="m3a-good-03-private-pool-attribute-access",
    ),
    pytest.param(
        REPO_PATH,
        "def build(obj):\n    return obj._private_helper()\n",
        id="m3a-good-04-private-helper-call",
    ),
]


@pytest.mark.parametrize(("rel_path", "source"), _GOOD_SINGLE_FILE)
def test_m3a_good_sample_stays_clean(rel_path: str, source: str) -> None:
    assert find_violations_in_source(source, rel_path) == []


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


# ---------------------------------------------------------------------------
# Task 2 (DESIGN.md Guard backlog, Low items): close the remaining
# sink-reachability bypasses. Each is flagged anywhere in src/, not just in
# repositories/migrate.py -- these are all ways to reach an attribute or a
# module by something other than a static, literal name, so the location
# rule (rule 5) doesn't apply to them the way it does to sink calls.
# ---------------------------------------------------------------------------

_TASK2_BYPASS_SAMPLES = [
    pytest.param(
        'import sys\n\n\ndef f(x):\n    setattr(sys.modules[__name__], "Q", x)\n',
        id="t2-01-setattr-onto-sys-modules-subscript",
    ),
    pytest.param(
        'import os\n\n\ndef f(x):\n    setattr(os, "environ", x)\n',
        id="t2-02-setattr-onto-imported-module",
    ),
    pytest.param(
        "import sys\n\n\ndef f(x):\n    sys.modules[__name__].Q = x\n",
        id="t2-03-attribute-store-on-sys-modules-subscript",
    ),
    pytest.param(
        'def f(conn):\n    return conn.__dict__["execute"]\n',
        id="t2-04-dunder-dict-subscript-sink-name",
    ),
    pytest.param(
        "def f(conn, key):\n    return conn.__dict__[key]\n",
        id="t2-05-dunder-dict-subscript-non-literal-key",
    ),
    pytest.param(
        'import inspect\n\n\ndef f(conn):\n    return inspect.getattr_static(conn, "execute")\n',
        id="t2-06a-inspect-getattr-static",
    ),
    pytest.param(
        "from inspect import getattr_static\n\n\ndef f(conn):\n"
        '    return getattr_static(conn, "execute")\n',
        id="t2-06b-getattr-static-imported-bare",
    ),
    pytest.param(
        "from inspect import getattr_static as gs\n\n\ndef f(conn, q):\n"
        "    fn = gs(conn, 'fetch')\n    return fn(q)\n",
        id="t2-06c-aliased-inspect-getattr-static",
    ),
    pytest.param(
        "from inspect import getattr_static as gs\n\n\ndef f(conn, q):\n"
        "    fn = gs\n    return fn(conn, 'fetch')(q)\n",
        id="t2-06d-aliased-inspect-getattr-static-as-value",
    ),
    pytest.param(
        'def f(conn):\n    return conn.__getattribute__("execute")\n',
        id="t2-07-dunder-getattribute-call-on-sink-name",
    ),
    pytest.param(
        "def f(conn):\n    return conn._protocol\n", id="t2-08-underscore-protocol-attribute"
    ),
    pytest.param(
        'import sys as s\n\n\ndef f(fn):\n    setattr(s.modules["x"], "execute", fn)\n',
        id="t2-08a-aliased-sys-modules-setattr",
    ),
    pytest.param(
        'import sys as s\n\n\ndef f(fn):\n    s.modules["x"].fetch = fn\n',
        id="t2-08b-aliased-sys-modules-attribute-store",
    ),
    pytest.param(
        "from sys import modules as registry\n\n\ndef f(fn):\n"
        '    setattr(registry["x"], "execute", fn)\n',
        id="t2-08c-direct-sys-modules-setattr",
    ),
    pytest.param(
        'from sys import modules as registry\n\n\ndef f(fn):\n    registry["x"].fetch = fn\n',
        id="t2-08d-direct-sys-modules-attribute-store",
    ),
    pytest.param("from os import *\n", id="t2-09-star-import"),
    pytest.param(
        'from importlib import import_module as im\n\n\ndef f():\n    return im("os")\n',
        id="t2-10-import-module-aliased-plus-call",
    ),
    pytest.param(
        'import builtins\n\n\ndef f():\n    return builtins.__import__("os")\n',
        id="t2-11-builtins-import-and-dunder-import-attr",
    ),
    pytest.param(
        'from builtins import __import__ as imp\n\n\ndef f():\n    return imp("asyncpg")\n',
        id="t2-11a-aliased-builtins-dunder-import",
    ),
    pytest.param(
        "import importlib as il\n\n\ndef f():\n"
        '    fn = il.import_module\n    return fn("asyncpg")\n',
        id="t2-11b-aliased-importlib-import-module-as-value",
    ),
    pytest.param(
        'def f():\n    return __builtins__["open"]\n', id="t2-12-dunder-builtins-subscript"
    ),
    pytest.param("def f():\n    return __builtins__.open\n", id="t2-13-dunder-builtins-attribute"),
    pytest.param(
        "def f(conn):\n    run = conn.__getattribute__\n    return run\n",
        id="t2-14-dunder-getattribute-referenced-as-value",
    ),
    # `conn.__getattribute__` (the task's own bullet) is the call/reference
    # forms above with `conn` as the receiver -- both are already covered.
]


@pytest.mark.parametrize("source", _TASK2_BYPASS_SAMPLES)
def test_task2_bypass_sample_is_flagged(source: str) -> None:
    violations = find_violations_in_source(source, COG_PATH)
    assert violations, f"expected a violation for:\n{source}"


# ---------------------------------------------------------------------------
# Task 2: make sure the new checks don't over-flag ordinary code shapes that
# merely resemble the bypasses above.
# ---------------------------------------------------------------------------

_TASK2_GOOD_SAMPLES = [
    pytest.param(
        'def f(self, val):\n    setattr(self, "x", val)\n',
        id="t2-good-01-setattr-onto-self-is-not-a-module",
    ),
    pytest.param(
        'def f(obj):\n    return obj.__dict__["some_field"]\n',
        id="t2-good-02-dict-subscript-literal-non-sink-name",
    ),
    pytest.param(
        "from typing import Optional\n\n\ndef f(x):\n    return x if x else None\n",
        id="t2-good-03-plain-star-free-import",
    ),
    pytest.param(
        'def f(obj):\n    return getattr(obj, "name", None)\n',
        id="t2-good-04-plain-getattr-with-literal-non-sink-name",
    ),
]


@pytest.mark.parametrize("source", _TASK2_GOOD_SAMPLES)
def test_task2_good_sample_stays_clean(source: str) -> None:
    assert find_violations_in_source(source, COG_PATH) == []
