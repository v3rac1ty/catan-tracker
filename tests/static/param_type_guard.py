"""Static guard: no bare `$n` parameter as an arithmetic operand in repository SQL.

This guards against exactly the class of bug that broke CI (see the M4
postmortem): `score_requests.py`'s `_INSERT_SCORE_REQUESTS_SQL` bound
`requested_at` with `SELECT $1, $2, u, $3, $3 + INTERVAL '24 hours'`. That
`+` matches *two* asyncpg/Postgres operator overloads for an otherwise
untyped parameter -- `interval + interval -> interval` and
`timestamptz + interval -> timestamptz` -- and with `$3` uncast, Postgres
has no way to pick one. It fails at *prepare* time with
`asyncpg.exceptions.AmbiguousParameterError`, before a single row is
touched, so every caller of the statement broke identically. This is a
narrow, deliberately mechanical rule -- not a general SQL type checker --
tuned to catch exactly that shape and nothing more:

    A `$n` placeholder is a violation when an arithmetic operator
    (`+`, `-`, `*`, `/`) sits directly against it (only whitespace
    between, either before or after) and that specific occurrence does
    not carry an explicit `::type` cast immediately after it.

Scope and what this deliberately does NOT check:

  - Only module-level `_..._SQL` string constants in
    `catan_bot/db/repositories/*.py` are scanned -- matching where every
    real SQL statement in this codebase lives (see `sql_guard.py`, which
    already enforces that no other location may hold a SQL sink argument).
  - A cast makes that *occurrence* safe regardless of what touches it on
    the other side: `$1::timestamptz + INTERVAL '24 hours'` is fine even
    though `+` still sits directly against `$1`, because the parameter's
    type is no longer in question. Conversely, casting only some
    occurrences of a repeated `$n` (e.g. only the arithmetic one) does not
    help if the *other*, uncast occurrence is what a reader might later
    "simplify" -- Postgres resolves one type per parameter number for the
    whole statement, so this checker is deliberately per-occurrence: every
    touch of `$n` against an operator must be individually cast.
  - This is a textual/local check, not a SQL parser: it does not
    understand precedence, parentheses, or string-literal boundaries. It
    has no false negatives on any statement actually written the way this
    codebase writes SQL (see the module docstrings in
    `db/repositories/*.py`: plain, hand-written, multi-line string
    constants, never built from fragments), and it is intentionally blind
    to arithmetic between two *columns* (`revision + 1`,
    `r.prompts_sent + 1`) or between a column and a literal -- those never
    involve an untyped `$n` parameter, so Postgres has nothing to be
    ambiguous about.
  - It does not (and cannot, without a real Postgres catalog) catch every
    possible parameter-typing ambiguity -- e.g. a value parameter that
    only reaches its type through a `CASE ... ELSE <column>` branch, which
    is a different, generally-non-ambiguous inference path (see
    `guilds.py::_SET_LEADERBOARD_SETTINGS_SQL` and its own comment for why
    that one was hardened by hand instead of mechanically).
"""

from __future__ import annotations

import re
from ast import AnnAssign, Assign, Constant, Module, Name, parse
from dataclasses import dataclass, field
from pathlib import Path

REPOSITORIES_ROOT = (
    Path(__file__).resolve().parents[2] / "src" / "catan_bot" / "db" / "repositories"
)

_ARITH_CHARS = frozenset("+-*/")

# A `$n` placeholder immediately (ignoring whitespace) followed by a
# `::type` cast, e.g. `$3::timestamptz` or `$5 :: bigint`. Matched at the
# *specific occurrence* being checked, not once per statement. No `^`/`\A`
# anchor: `Pattern.match(string, pos)` already requires the match to start
# exactly at `pos` -- `\A` would instead (and wrongly) demand `pos == 0`,
# i.e. it would only ever "see" a cast on a parameter at the very start of
# the SQL text.
_CAST_AFTER_RE = re.compile(r"\s*::\s*[A-Za-z_]\w*")

_PARAM_RE = re.compile(r"\$(\d+)")


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


@dataclass(frozen=True)
class ScanResult:
    violations: list[Violation] = field(default_factory=list)
    scanned_files: list[str] = field(default_factory=list)
    scanned_constants: int = 0


def _sql_constants(tree: Module) -> list[tuple[str, str, int]]:
    """Every module-level `_..._SQL` string constant: (name, text, lineno).

    Mirrors `sql_guard.py`'s `_module_level_constant_candidates` shape, but
    this guard is a correctness lint, not an adversarial security control,
    so it doesn't need that file's "bound exactly once anywhere in the
    module" soundness proof -- picking up every module-level `NAME = "..."`
    /`NAME: T = "..."` assignment whose name matches the repository SQL
    naming convention is enough to scan every statement that convention
    covers, which is all of them (see each repository module's own
    docstring).
    """
    out: list[tuple[str, str, int]] = []
    for stmt in tree.body:
        target: Name | None = None
        value = None
        if (
            isinstance(stmt, Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], Name)
        ):
            target = stmt.targets[0]
            value = stmt.value
        elif (
            isinstance(stmt, AnnAssign) and isinstance(stmt.target, Name) and stmt.value is not None
        ):
            target = stmt.target
            value = stmt.value
        if target is None:
            continue
        name = target.id
        if not (name.startswith("_") and name.endswith("_SQL")):
            continue
        if isinstance(value, Constant) and isinstance(value.value, str):
            out.append((name, value.value, value.lineno))
    return out


def find_param_type_violations(
    sql: str, *, name: str, path: str, base_line: int
) -> list[Violation]:
    """Every uncast `$n` occurrence touching `+`/`-`/`*`/`/` in one SQL constant's text."""
    violations: list[Violation] = []
    for match in _PARAM_RE.finditer(sql):
        start, end = match.span()
        num = match.group(1)

        if _CAST_AFTER_RE.match(sql, end):
            continue  # this occurrence carries its own cast -- safe regardless of context.

        before = sql[:start].rstrip()
        after = sql[end:].lstrip()
        touches_before = bool(before) and before[-1] in _ARITH_CHARS
        touches_after = bool(after) and after[0] in _ARITH_CHARS
        if not (touches_before or touches_after):
            continue

        line = base_line + sql[:start].count("\n")
        violations.append(
            Violation(
                path,
                line,
                f"{name}: ${num} is used as an operand of an arithmetic operator "
                "(+, -, *, /) without an explicit '::type' cast at this occurrence -- "
                "Postgres can deduce more than one type for an uncast parameter used "
                "this way (e.g. both 'interval + interval' and 'timestamptz + "
                "interval' match '$n + INTERVAL ...') and raises "
                "asyncpg.exceptions.AmbiguousParameterError at prepare time; add an "
                "explicit cast to every occurrence of this parameter, not just this one",
            )
        )
    return violations


def find_violations_in_source(source: str, rel_path: str) -> list[Violation]:
    """Check one repository module's source text for the guard's rule.

    `rel_path` is POSIX-style, relative to `src/`, matching `sql_guard.py`'s
    convention (even though this guard only ever looks inside
    `db/repositories/`, so `rel_path` will always start with that prefix
    for anything actually flagged).
    """
    tree = parse(source, filename=rel_path)
    violations: list[Violation] = []
    for name, text, lineno in _sql_constants(tree):
        violations.extend(
            find_param_type_violations(text, name=name, path=rel_path, base_line=lineno)
        )
    return violations


def scan_tree(repositories_root: Path) -> ScanResult:
    """Scan every `.py` file directly under `repositories_root`."""
    if not repositories_root.is_dir():
        return ScanResult(violations=[], scanned_files=[])
    scanned: list[str] = []
    violations: list[Violation] = []
    constants = 0
    for path in sorted(repositories_root.glob("*.py")):
        rel_path = f"catan_bot/db/repositories/{path.name}"
        scanned.append(rel_path)
        source = path.read_text(encoding="utf-8")
        tree = parse(source, filename=rel_path)
        found = _sql_constants(tree)
        constants += len(found)
        for name, text, lineno in found:
            violations.extend(
                find_param_type_violations(text, name=name, path=rel_path, base_line=lineno)
            )
    return ScanResult(violations=violations, scanned_files=scanned, scanned_constants=constants)


__all__ = [
    "REPOSITORIES_ROOT",
    "ScanResult",
    "Violation",
    "find_param_type_violations",
    "find_violations_in_source",
    "scan_tree",
]
