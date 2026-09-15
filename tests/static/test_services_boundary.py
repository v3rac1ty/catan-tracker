"""Static boundary guard for `catan_bot.services` (an allowlist, not a blocklist).

AST-scans every module under `src/catan_bot/services/` (via `rglob`, so a
future subpackage is covered too) and fails on:

  - any import whose resolved target isn't one of a short, fixed allowlist:
    `__future__`, `logging`, `dataclasses`, `datetime`, `typing`,
    `collections.abc`, `asyncpg`, plus `catan_bot.db`/`catan_bot.domain`/
    `catan_bot.services` and their submodules -- relative imports are
    resolved against each file's own package using `node.level`, exactly
    as Python's import system would (mirrors `tests/static/
    test_domain_purity.py`). This is what actually flags `discord`,
    `catan_bot.bot`, `catan_bot.cogs`, `catan_bot.views`,
    `catan_bot.scheduler`, `time`, `sys`, `os`, `importlib`, and `builtins`
    -- none of those are on the allowlist, so importing (or relative-
    importing) any of them is caught here without a dedicated rule per
    module;
  - a call or bare attribute access named `now`, `today`, `utcnow`,
    `time`, `time_ns`, `monotonic`, `perf_counter`, `localtime`, `gmtime`,
    or `process_time`, regardless of receiver -- services never read the
    system clock; every function that needs "now"/"today" takes it as an
    explicit parameter. Unlike the domain layer's purity guard, `.time()`
    is *not* exempted here: no module under `services/` calls it
    legitimately (checked directly -- see the `grep` note in the M3b audit
    report), so there's nothing to exempt, and leaving it in closes a
    `time.time()` / `loop.time()` bypass the domain guard has to special-
    case around instead;
  - `getattr` with a missing/non-literal/clock-named attribute argument,
    `__import__` (as a bare call or an attribute reference such as
    `builtins.__import__`), `open`, `exec`, `eval`, `compile`, and
    `sys.modules` subscription/attribute access.

Not an adversarial guard like `tests/static/sql_guard.py`, but self-tested
against a bypass catalogue below anyway, matching `test_domain_purity.py`'s
style.
"""

from __future__ import annotations

from ast import (
    Attribute,
    Call,
    Constant,
    Import,
    ImportFrom,
    Name,
    parse,
    walk,
)
from pathlib import Path

import pytest

SERVICES_ROOT = Path(__file__).resolve().parents[2] / "src" / "catan_bot" / "services"

_EXPECTED_RELATIVE_PATHS = frozenset(
    {
        "__init__.py",
        "errors.py",
        "context.py",
        "results.py",
        "config_service.py",
        "season_service.py",
        "game_service.py",
        "stats_service.py",
        "event_service.py",
    }
)

# Every top-level (dotted-exact) import target the services layer may use.
# "catan_bot.db"/"catan_bot.domain"/"catan_bot.services" and anything under
# them are allowed separately, below.
_ALLOWED_EXACT_IMPORTS = frozenset(
    {
        "__future__",
        "logging",
        "dataclasses",
        "datetime",
        "typing",
        "collections.abc",
        "asyncpg",
    }
)

_ALLOWED_PACKAGES = ("catan_bot.db", "catan_bot.domain", "catan_bot.services")

# No receiver is exempted (unlike the domain guard, which excuses `.time()`
# on a `datetime` -- nothing under `services/` calls it, so there's no
# legitimate use to carve out here).
_CLOCK_ATTR_NAMES = frozenset(
    {
        "now",
        "today",
        "utcnow",
        "time",
        "time_ns",
        "monotonic",
        "perf_counter",
        "localtime",
        "gmtime",
        "process_time",
    }
)

# Attribute names that are dangerous regardless of receiver, call or not.
# These names are common stepping stones from an otherwise permitted object
# to imports, process modules, or hidden runtime state.
_DANGEROUS_ATTR_NAMES = _CLOCK_ATTR_NAMES | {
    "__import__",
    "__builtins__",
    "__dict__",
    "sys",
    "os",
    "modules",
}

_FORBIDDEN_BUILTIN_CALLS = frozenset(
    {"open", "__import__", "exec", "eval", "compile", "breakpoint"}
)


# ---------------------------------------------------------------------------
# Import resolution: turn any Import/ImportFrom node into the dotted
# module path(s) it actually reaches, honoring relative-import `level`.
# Mirrors `tests/static/test_domain_purity.py` exactly, retargeted at the
# services package.
# ---------------------------------------------------------------------------


def _package_for_rel_path(rel_path: str) -> str:
    parts = Path(rel_path).parts[:-1]
    return ".".join(("catan_bot", "services", *parts))


def _resolve_relative_base(package: str, level: int, module: str | None) -> str:
    """Mirrors `importlib._bootstrap._resolve_name`'s level handling."""
    if level == 0:
        return module or ""
    bits = package.rsplit(".", level - 1)
    base = bits[0]
    return f"{base}.{module}" if module else base


def _is_allowed_module_path(path: str) -> bool:
    if path in _ALLOWED_EXACT_IMPORTS:
        return True
    return any(path == pkg or path.startswith(pkg + ".") for pkg in _ALLOWED_PACKAGES)


def _import_violations(node: Import | ImportFrom, rel_path: str, package: str) -> list[str]:
    violations: list[str] = []
    if isinstance(node, Import):
        for alias in node.names:
            if not _is_allowed_module_path(alias.name):
                violations.append(
                    f"{rel_path}:{node.lineno}: import of {alias.name!r} is not on the "
                    "services layer's allowlist"
                )
        return violations

    base = _resolve_relative_base(package, node.level, node.module)
    candidates = [base] if base else []
    if base.startswith("catan_bot"):
        candidates.extend(f"{base}.{alias.name}" for alias in node.names)

    for candidate in candidates:
        if not _is_allowed_module_path(candidate):
            violations.append(
                f"{rel_path}:{node.lineno}: import resolves to {candidate!r}, which is "
                "not on the services layer's allowlist"
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
    if attr_arg.value in _DANGEROUS_ATTR_NAMES:
        return f"getattr() names a forbidden attribute ({attr_arg.value!r}) indirectly"
    return None


def _call_violation(call: Call) -> str | None:
    func = call.func
    if isinstance(func, Name):
        if func.id == "getattr":
            return _getattr_violation(call)
        if func.id in _FORBIDDEN_BUILTIN_CALLS:
            return f"call to builtin {func.id}() is not allowed in the services layer"
        if func.id in _CLOCK_ATTR_NAMES:
            return f"call to '{func.id}()' reads the system clock -- take it as a parameter instead"
    return None


def _is_sys_modules_access(node: Attribute) -> bool:
    return node.attr == "modules" and isinstance(node.value, Name) and node.value.id == "sys"


def find_boundary_violations(source: str, rel_path: str) -> list[str]:
    """Every boundary violation in one module's source text.

    `rel_path` is POSIX-style, relative to `catan_bot/services/`.
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
        elif isinstance(node, Attribute):
            if node.attr in _DANGEROUS_ATTR_NAMES:
                violations.append(
                    f"{rel_path}:{node.lineno}: reference to '.{node.attr}' is not allowed "
                    "in the services layer"
                )
            elif _is_sys_modules_access(node):
                violations.append(f"{rel_path}:{node.lineno}: 'sys.modules' access is not allowed")
        elif isinstance(node, Name) and node.id in {"__builtins__", "__import__"}:
            violations.append(
                f"{rel_path}:{node.lineno}: reference to {node.id!r} is not allowed "
                "in the services layer"
            )

    return violations


# ---------------------------------------------------------------------------
# The real tree: must be scanned (non-vacuously) and clean.
# ---------------------------------------------------------------------------


def test_services_tree_is_scanned_and_clean() -> None:
    assert SERVICES_ROOT.is_dir()
    py_files = sorted(SERVICES_ROOT.rglob("*.py"))
    scanned_rel_paths = {path.relative_to(SERVICES_ROOT).as_posix() for path in py_files}

    # Proves the scan isn't vacuous: every owned module, no more, no fewer.
    assert scanned_rel_paths == _EXPECTED_RELATIVE_PATHS, scanned_rel_paths

    violations: list[str] = []
    for path in py_files:
        rel_path = path.relative_to(SERVICES_ROOT).as_posix()
        violations.extend(find_boundary_violations(path.read_text(encoding="utf-8"), rel_path))
    assert violations == [], "\n" + "\n".join(violations)


# ---------------------------------------------------------------------------
# Self-tests: every bad sample must be flagged, single file per sample.
# ---------------------------------------------------------------------------

_BAD_SAMPLES = [
    pytest.param("import discord\n", id="01-import-discord"),
    pytest.param("import discord.ext.commands\n", id="02-import-discord-submodule"),
    pytest.param("from discord.ext import commands\n", id="03-from-discord-ext-import-commands"),
    pytest.param("from discord import Interaction\n", id="04-from-discord-import-interaction"),
    pytest.param(
        "import datetime\n\n\ndef f():\n    return datetime.datetime.now()\n",
        id="05-datetime-now-via-chain",
    ),
    pytest.param(
        "import datetime\n\n\ndef f():\n    return datetime.date.today()\n",
        id="06-date-today-via-chain",
    ),
    pytest.param(
        "from datetime import datetime as D\n\n\ndef f():\n    return D.utcnow()\n",
        id="07-aliased-class-utcnow",
    ),
    pytest.param("NOW = datetime.now\n", id="08-clock-attribute-bound-to-name"),
    pytest.param('open("x")\n', id="09-open-call"),
    pytest.param('__import__("os")\n', id="10-dunder-import-call"),
    pytest.param("import time\n\ntime.time()\n", id="11-time-time-via-chain"),
    pytest.param("time.time()\n", id="12-bare-time-time"),
    pytest.param("perf_counter()\n", id="13-bare-perf-counter-call"),
    pytest.param("time_ns()\n", id="14-bare-time-ns-call"),
    pytest.param("loop.time()\n", id="15-loop-dot-time"),
    pytest.param("from time import time\n", id="16-from-time-import-time"),
    pytest.param("from time import monotonic as m\n", id="17-from-time-import-monotonic-as-m"),
    pytest.param(
        'import datetime\n\n\ndef f():\n    return getattr(datetime, "now")()\n',
        id="18-getattr-now",
    ),
    pytest.param("from catan_bot import bot\n", id="19-from-catan-bot-import-bot"),
    pytest.param("from ..cogs import help_cog\n", id="20-relative-dotdot-cogs-help-cog"),
    pytest.param("import catan_bot.bot\n", id="21-import-catan-bot-bot"),
    pytest.param("import catan_bot.cogs\n", id="22-import-catan-bot-cogs"),
    pytest.param("import catan_bot.views\n", id="23-import-catan-bot-views"),
    pytest.param("import catan_bot.scheduler\n", id="24-import-catan-bot-scheduler"),
    # No `import sys`/`import builtins` on these two: the point is that the
    # attribute-access rule catches `sys.modules`/`.__import__` on its own,
    # independent of the (separately-tested) import allowlist.
    pytest.param("sys.modules['discord']\n", id="25-sys-modules-subscript"),
    pytest.param("import os\n", id="26-import-os"),
    pytest.param(
        "from importlib import import_module as im\n", id="27-from-importlib-import-module-as-im"
    ),
    pytest.param("builtins.__import__\n", id="28-builtins-dunder-import-ref"),
    pytest.param("import exceptions\n", id="29-import-of-unknown-module"),
    pytest.param("breakpoint()\n", id="30-breakpoint-call"),
    pytest.param("obj.__builtins__\n", id="31-dunder-builtins-attribute"),
    pytest.param("obj.__dict__\n", id="32-dunder-dict-attribute"),
    pytest.param("obj.sys\n", id="33-sys-hop-attribute"),
    pytest.param("obj.os\n", id="34-os-hop-attribute"),
    pytest.param("obj.modules\n", id="35-modules-hop-attribute"),
    pytest.param('getattr(obj, "__dict__")\n', id="36-getattr-dunder-dict"),
    pytest.param('getattr(obj, "modules")\n', id="37-getattr-modules"),
]


@pytest.mark.parametrize("source", _BAD_SAMPLES)
def test_bypass_sample_is_flagged(source: str) -> None:
    assert find_boundary_violations(source, "planted.py") != []


# ---------------------------------------------------------------------------
# Control cases: legitimate service code must stay clean.
# ---------------------------------------------------------------------------


def test_clean_module_is_not_flagged() -> None:
    source = (
        "from __future__ import annotations\n\n"
        "from datetime import datetime\n\n\n"
        "async def f(pool, now: datetime) -> None:\n"
        "    today = now.astimezone().date()\n"
        "    return today\n"
    )
    assert find_boundary_violations(source, "planted.py") == []


def test_ordinary_parameter_named_now_or_today_is_not_flagged() -> None:
    source = (
        "from datetime import date, datetime\n\n\n"
        "def f(now: datetime, today: date) -> datetime:\n    return now.astimezone()\n"
    )
    assert find_boundary_violations(source, "planted.py") == []


def test_import_of_asyncpg_is_not_flagged() -> None:
    source = "import asyncpg\n"
    assert find_boundary_violations(source, "planted.py") == []


def test_import_of_logging_is_not_flagged() -> None:
    source = "import logging\n\nlogger = logging.getLogger(__name__)\n"
    assert find_boundary_violations(source, "planted.py") == []


def test_relative_import_within_services_package_is_not_flagged() -> None:
    source = "from .errors import ServiceError\n"
    assert find_boundary_violations(source, "planted.py") == []


def test_import_of_repositories_is_not_flagged() -> None:
    source = "from catan_bot.db.repositories import guilds\n"
    assert find_boundary_violations(source, "planted.py") == []


def test_import_of_db_models_is_not_flagged() -> None:
    source = "from catan_bot.db.models import Season\n"
    assert find_boundary_violations(source, "planted.py") == []


def test_import_of_domain_is_not_flagged() -> None:
    source = "from catan_bot.domain.dates import validate_timezone\n"
    assert find_boundary_violations(source, "planted.py") == []


def test_keyword_argument_named_now_is_not_flagged() -> None:
    source = (
        "def plan(starts_at, now):\n"
        "    from catan_bot.domain.reminders import plan_reminders\n"
        "    return plan_reminders(starts_at, now=now)\n"
    )
    assert find_boundary_violations(source, "planted.py") == []


def test_getattr_with_harmless_literal_attribute_is_not_flagged() -> None:
    source = 'def f(obj):\n    return getattr(obj, "user_message")\n'
    assert find_boundary_violations(source, "planted.py") == []
