"""Static purity guard for `catan_bot.domain` (an allowlist, not a blocklist).

AST-scans every module under `src/catan_bot/domain/` (via `rglob`, so any
future subpackage is covered too) and fails on:

  - any import whose resolved target isn't one of a short, fixed allowlist
    (stdlib modules the domain layer actually needs, plus
    `catan_bot.domain` and its own submodules) -- relative imports are
    resolved against each file's own package using `node.level`, exactly
    as Python's import system would, so `from ..db import pool` inside
    `catan_bot/domain/foo.py` is correctly seen as `catan_bot.db` and
    rejected, not read as some vague "parent package" import;
  - a call or bare attribute access named `now`, `today`, `utcnow`,
    `monotonic`, `time_ns`, `perf_counter`, `localtime`, `gmtime`, or
    `process_time`, regardless of the receiver (so `dt.datetime.now()`,
    `D.now()` after `from datetime import datetime as D`, and even
    `NOW = datetime.now` used as a value are all caught) -- `.time()` is
    deliberately excluded, since `dates.py` calls it legitimately on a
    `datetime` object, and the `time` *module* itself is separately
    unreachable (it's not on the allowlist at all);
  - a call to a builtin with no legitimate use here: `open`, `__import__`,
    `exec`, `eval`, `compile`, `input`, `breakpoint`, or `getattr` with a
    missing/non-literal/clock-named attribute argument.

This is a build sanity check on code this milestone owns, not an
adversarial guard like `tests/static/sql_guard.py` -- but per a security
audit's bypass catalogue, it's still self-tested against every listed
bypass sample below.
"""

from __future__ import annotations

from ast import Attribute, Call, Constant, Import, ImportFrom, Name, parse, walk
from pathlib import Path

import pytest

DOMAIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "catan_bot" / "domain"

_EXPECTED_RELATIVE_PATHS = frozenset(
    {
        "__init__.py",
        "errors.py",
        "dates.py",
        "validation.py",
        "ranking.py",
        "bet.py",
        "reminders.py",
        "scoring.py",
    }
)

# Every top-level (dotted-exact) import target the domain layer may use.
# "catan_bot.domain" and anything under it is allowed separately, below.
_ALLOWED_EXACT_IMPORTS = frozenset(
    {
        "__future__",
        "re",
        "datetime",
        "functools",
        "zoneinfo",
        "collections.abc",
        "dataclasses",
        "typing",
        "fractions",
        "unicodedata",
        "enum",
        "math",
        "itertools",
    }
)

_DOMAIN_PACKAGE = "catan_bot.domain"

# Attribute names that indicate a direct clock/monotonic-timer read,
# regardless of what object they're accessed on. "time" is deliberately
# excluded: `some_datetime.time()` is a legitimate, non-clock-reading call
# (it extracts the time-of-day component), and the `time` *module* itself
# is already unreachable since it's not in `_ALLOWED_EXACT_IMPORTS`.
_CLOCK_ATTR_NAMES = frozenset(
    {
        "now",
        "today",
        "utcnow",
        "monotonic",
        "time_ns",
        "perf_counter",
        "localtime",
        "gmtime",
        "process_time",
    }
)

_FORBIDDEN_BUILTIN_CALLS = frozenset(
    {"open", "__import__", "exec", "eval", "compile", "input", "breakpoint"}
)


# ---------------------------------------------------------------------------
# Import resolution: turn any Import/ImportFrom node into the dotted
# module path(s) it actually reaches, honoring relative-import `level`.
# ---------------------------------------------------------------------------


def _package_for_rel_path(rel_path: str) -> str:
    """The dotted package that owns relative imports in this file.

    A regular module's package is its containing directory; a package's
    `__init__.py` is its own package (same formula either way: drop the
    filename, dot-join what's left under `catan_bot.domain`).
    """
    parts = Path(rel_path).parts[:-1]
    return ".".join(("catan_bot", "domain", *parts))


def _resolve_relative_base(package: str, level: int, module: str | None) -> str:
    """Mirrors `importlib._bootstrap._resolve_name`'s level handling."""
    if level == 0:
        return module or ""
    bits = package.rsplit(".", level - 1)
    base = bits[0]
    return f"{base}.{module}" if module else base


def _is_allowed_module_path(path: str) -> bool:
    if path == _DOMAIN_PACKAGE or path.startswith(_DOMAIN_PACKAGE + "."):
        return True
    return path in _ALLOWED_EXACT_IMPORTS


def _import_violations(node: Import | ImportFrom, rel_path: str, package: str) -> list[str]:
    violations: list[str] = []
    if isinstance(node, Import):
        for alias in node.names:
            if not _is_allowed_module_path(alias.name):
                violations.append(
                    f"{rel_path}:{node.lineno}: import of {alias.name!r} is not on the "
                    "domain layer's allowlist"
                )
        return violations

    base = _resolve_relative_base(package, node.level, node.module)
    candidates = [base] if base else []
    # Defense in depth: `from catan_bot import db` (or a relative
    # equivalent) implicitly touches `catan_bot.db` through the imported
    # *name*, not just through `node.module` -- check those too whenever
    # the base itself is inside our own project.
    if base.startswith("catan_bot"):
        candidates.extend(f"{base}.{alias.name}" for alias in node.names)

    for candidate in candidates:
        if not _is_allowed_module_path(candidate):
            violations.append(
                f"{rel_path}:{node.lineno}: import resolves to {candidate!r}, which is "
                "not on the domain layer's allowlist"
            )
            break  # one report per statement is enough
    return violations


# ---------------------------------------------------------------------------
# Clock/builtin call and attribute detection.
# ---------------------------------------------------------------------------


def _getattr_violation(call: Call) -> str | None:
    if len(call.args) < 2:
        return "getattr() with fewer than 2 arguments"
    attr_arg = call.args[1]
    if not (isinstance(attr_arg, Constant) and isinstance(attr_arg.value, str)):
        return "getattr() with a non-literal attribute name"
    if attr_arg.value in _CLOCK_ATTR_NAMES:
        return f"getattr() names a clock attribute ({attr_arg.value!r}) indirectly"
    return None


def _call_violation(call: Call) -> str | None:
    func = call.func
    if isinstance(func, Name):
        if func.id == "getattr":
            return _getattr_violation(call)
        if func.id in _FORBIDDEN_BUILTIN_CALLS:
            return f"call to builtin {func.id}() is not allowed in the domain layer"
        if func.id == "import_module":
            return "call to import_module() is a dynamic import, not allowed here"
    if isinstance(func, Attribute) and func.attr == "import_module":
        return "call to .import_module() is a dynamic import, not allowed here"
    return None


def find_purity_violations(source: str, rel_path: str) -> list[str]:
    """Every purity violation in one module's source text.

    `rel_path` is POSIX-style, relative to `catan_bot/domain/` (e.g.
    `"dates.py"` or `"helpers/__init__.py"` for a hypothetical subpackage).
    """
    tree = parse(source, filename=rel_path)
    package = _package_for_rel_path(rel_path)
    violations: list[str] = []

    for node in walk(tree):
        if isinstance(node, Import | ImportFrom):
            violations.extend(_import_violations(node, rel_path, package))
        elif isinstance(node, Call):
            msg = _call_violation(node)
            if msg:
                violations.append(f"{rel_path}:{node.lineno}: {msg}")
        elif isinstance(node, Attribute) and node.attr in _CLOCK_ATTR_NAMES:
            violations.append(
                f"{rel_path}:{node.lineno}: reference to '.{node.attr}' reads the system "
                "clock -- take it as a parameter instead"
            )

    return violations


# ---------------------------------------------------------------------------
# The real tree: must be scanned (non-vacuously) and clean.
# ---------------------------------------------------------------------------


def test_domain_tree_is_scanned_and_pure() -> None:
    assert DOMAIN_ROOT.is_dir()
    py_files = sorted(DOMAIN_ROOT.rglob("*.py"))
    scanned_rel_paths = {path.relative_to(DOMAIN_ROOT).as_posix() for path in py_files}

    # Proves the scan isn't vacuous: exactly the 6 owned modules plus
    # __init__.py, no more, no fewer.
    assert scanned_rel_paths == _EXPECTED_RELATIVE_PATHS, scanned_rel_paths

    violations: list[str] = []
    for path in py_files:
        rel_path = path.relative_to(DOMAIN_ROOT).as_posix()
        violations.extend(find_purity_violations(path.read_text(encoding="utf-8"), rel_path))
    assert violations == [], "\n" + "\n".join(violations)


# ---------------------------------------------------------------------------
# Self-tests: every bypass sample must be flagged, single file per sample.
# id prefixes match the security audit's bypass catalogue for traceability.
# ---------------------------------------------------------------------------

_BAD_SAMPLES = [
    pytest.param('importlib.import_module("asyncpg")\n', id="01-importlib-import-module-call"),
    pytest.param("from importlib import import_module\n", id="02-from-importlib-import-name"),
    pytest.param('__import__("os")\n', id="03-dunder-import-call"),
    pytest.param(
        "import datetime as dt\n\n\ndef f():\n    return dt.datetime.now()\n",
        id="04-aliased-module-attribute-chain-now",
    ),
    pytest.param(
        "from datetime import datetime as D\n\n\ndef f():\n    return D.now()\n",
        id="05-aliased-class-now",
    ),
    pytest.param(
        "import datetime\n\n\ndef f():\n    return datetime.date.today()\n",
        id="06-date-today-via-chain",
    ),
    pytest.param(
        "import datetime\n\nNOW = datetime.now\n",
        id="07-clock-attribute-bound-to-a-name",
    ),
    pytest.param(
        'import datetime\n\n\ndef f():\n    return getattr(datetime, "now")()\n',
        id="08-getattr-now",
    ),
    pytest.param(
        "import time\n\n\ndef f():\n    return time.monotonic()\n", id="09-time-monotonic"
    ),
    pytest.param("from time import time\n", id="10-from-time-import-time"),
    pytest.param("from os import environ\n", id="11-from-os-import-environ"),
    pytest.param("import os.path\n", id="12-import-os-path"),
    pytest.param("import catan_bot.db.pool as p\n", id="13-import-catan-bot-db-submodule-aliased"),
    pytest.param("from catan_bot.db import pool\n", id="14-from-catan-bot-db-import-pool"),
    pytest.param("from catan_bot import db\n", id="15-from-catan-bot-import-db-name"),
    pytest.param("from ..db import pool\n", id="16-relative-dotdot-db-import-pool"),
    pytest.param("from ..db.repositories import games\n", id="17-relative-dotdot-db-repositories"),
    pytest.param("from .. import db\n", id="18-relative-dotdot-import-db-name"),
    pytest.param("from discord.ext import commands\n", id="19-from-discord-ext-import-commands"),
    pytest.param('open("x")\n', id="20-open-call"),
    pytest.param("import asyncio\n", id="21-import-asyncio"),
    pytest.param("import urllib.request\n", id="22-import-urllib-request"),
    pytest.param("import sys\n\nsys.modules\n", id="23-import-sys-and-use-modules"),
    pytest.param("import ssl\n", id="24-import-ssl"),
]


@pytest.mark.parametrize("source", _BAD_SAMPLES)
def test_bypass_sample_is_flagged(source: str) -> None:
    assert find_purity_violations(source, "planted.py") != []


def test_bypass_sample_new_subpackage_with_asyncpg_import() -> None:
    # A hypothetical new `domain/helpers/` subpackage: proves `rglob`-style
    # per-file scanning (via a package-relative `rel_path`) still catches a
    # forbidden import even when it's not directly under the domain root.
    source = "import asyncpg\n"
    assert find_purity_violations(source, "helpers/__init__.py") != []


def test_bypass_sample_new_subpackage_relative_import_of_db() -> None:
    # Inside `domain/helpers/__init__.py`, package is "catan_bot.domain.helpers":
    # `from .. import db` should resolve to "catan_bot.domain" + name "db",
    # which passes the *domain* prefix check -- but a *deeper* relative hop
    # reaching back out to the real `catan_bot.db` must still be caught.
    source = "from ... import db\n"
    assert find_purity_violations(source, "helpers/__init__.py") != []


# ---------------------------------------------------------------------------
# Control cases: legitimate code must stay clean.
# ---------------------------------------------------------------------------


def test_clean_module_is_not_flagged() -> None:
    source = (
        "from __future__ import annotations\n\n"
        "from datetime import date, datetime\n\n\n"
        "def today_in_zone(now: datetime) -> date:\n"
        "    return now.date()\n"
    )
    assert find_purity_violations(source, "planted.py") == []


def test_dot_time_on_a_datetime_is_not_flagged() -> None:
    # `dates.py` legitimately calls `.time()` on a `datetime` to extract
    # its time-of-day component -- this must never be treated as a clock
    # read, unlike `time.time()` (which is unreachable anyway: "time" the
    # *module* isn't on the allowlist at all).
    source = (
        "from datetime import datetime\n\n\ndef f(dt: datetime) -> object:\n    return dt.time()\n"
    )
    assert find_purity_violations(source, "planted.py") == []


def test_ordinary_parameter_named_now_or_today_is_not_flagged() -> None:
    source = (
        "from datetime import date, datetime\n\n\n"
        "def f(now: datetime, today: date) -> datetime:\n    return now.astimezone()\n"
    )
    assert find_purity_violations(source, "planted.py") == []


def test_getattr_with_harmless_literal_attribute_is_not_flagged() -> None:
    source = 'def f(obj):\n    return getattr(obj, "losses")\n'
    assert find_purity_violations(source, "planted.py") == []


def test_from_catan_bot_domain_import_is_not_flagged() -> None:
    source = "from catan_bot.domain.errors import DomainValidationError\n"
    assert find_purity_violations(source, "planted.py") == []


def test_relative_import_within_domain_package_is_not_flagged() -> None:
    source = "from .errors import DomainValidationError\n"
    assert find_purity_violations(source, "planted.py") == []


def test_import_catan_bot_domain_itself_is_not_flagged() -> None:
    source = "import catan_bot.domain\n"
    assert find_purity_violations(source, "planted.py") == []


# ---------------------------------------------------------------------------
# Resolution-logic unit tests (the pieces `find_purity_violations` composes).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel_path", "expected"),
    [
        pytest.param("dates.py", "catan_bot.domain", id="module-directly-under-root"),
        pytest.param("helpers/__init__.py", "catan_bot.domain.helpers", id="package-init"),
        pytest.param("helpers/utils.py", "catan_bot.domain.helpers", id="module-in-subpackage"),
    ],
)
def test_package_for_rel_path(rel_path: str, expected: str) -> None:
    assert _package_for_rel_path(rel_path) == expected


@pytest.mark.parametrize(
    ("package", "level", "module", "expected"),
    [
        pytest.param("catan_bot.domain", 0, "collections.abc", "collections.abc", id="absolute"),
        pytest.param("catan_bot.domain", 1, None, "catan_bot.domain", id="dot-only"),
        pytest.param("catan_bot.domain", 1, "errors", "catan_bot.domain.errors", id="dot-module"),
        pytest.param("catan_bot.domain", 2, "db", "catan_bot.db", id="dotdot-db"),
        pytest.param(
            "catan_bot.domain",
            2,
            "db.repositories",
            "catan_bot.db.repositories",
            id="dotdot-dotted",
        ),
        pytest.param("catan_bot.domain", 2, None, "catan_bot", id="dotdot-only"),
    ],
)
def test_resolve_relative_base(package: str, level: int, module: str | None, expected: str) -> None:
    assert _resolve_relative_base(package, level, module) == expected
