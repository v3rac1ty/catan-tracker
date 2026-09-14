"""AST-based SQL-injection static guard.

Enforces the project's hard SQL rules (see CLAUDE.md) against any Python
module's source text:

1. Every call to an asyncpg-style query method (`execute`, `executemany`,
   `fetch`, `fetchrow`, `fetchval`, `prepare`, `cursor`, `copy_from_query`)
   must pass a query that is either a string literal, or a `Name` that
   resolves to a module-level `NAME = "literal"` assignment in the same
   module. Anything else (f-string, `.format()`, `%`, `+`, a call result, an
   attribute, or a local variable) fails.
2. Such calls may only appear in `catan_bot/db/repositories/*.py` or in
   `catan_bot/db/migrate.py`. The single allowed exception is the migration
   runner executing the *contents* of a trusted, filename-validated
   migration file — identified narrowly by requiring the call to be
   `.execute(...)` inside `catan_bot/db/migrate.py` on a source line tagged
   with the `_APPLY_MIGRATION_FILE_SQL_ALLOWLISTED` marker comment.
3. Anywhere in the scanned tree, f-string / `.format()` / `%` / `+` string
   building whose literal parts contain a SQL keyword (case-insensitive,
   whole word) is flagged, regardless of where it appears.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

SQL_METHOD_NAMES = frozenset(
    {
        "execute",
        "executemany",
        "fetch",
        "fetchrow",
        "fetchval",
        "prepare",
        "cursor",
        "copy_from_query",
    }
)

SQL_KEYWORD_RE = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|WHERE|FROM|UNION)\b",
    re.IGNORECASE,
)

ALLOWED_REPOSITORY_PREFIX = "catan_bot/db/repositories/"
ALLOWED_MIGRATE_FILE = "catan_bot/db/migrate.py"
MIGRATION_EXEC_ALLOWLIST_MARKER = "_APPLY_MIGRATION_FILE_SQL_ALLOWLISTED"


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _module_level_string_constants(tree: ast.Module) -> dict[str, str]:
    """Map NAME -> literal value for module-level `NAME = "literal"` (or annotated) assignments."""
    consts: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            value = stmt.value
            if (
                isinstance(target, ast.Name)
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                consts[target.id] = value.value
        elif isinstance(stmt, ast.AnnAssign):
            target = stmt.target
            value = stmt.value
            if (
                isinstance(target, ast.Name)
                and value is not None
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                consts[target.id] = value.value
    return consts


def _literal_text(node: ast.AST, module_consts: dict[str, str]) -> str:
    """Best-effort recursive extraction of the literal string content of an expression.

    Resolves plain string constants, module-level constant name references,
    f-string literal segments, and `+` concatenation trees. Dynamic pieces
    (function calls, non-constant names, formatted f-string values, etc.)
    contribute nothing, so this only ever grows the string we scan for SQL
    keywords -- it never hides one.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in module_consts:
        return module_consts[node.id]
    if isinstance(node, ast.JoinedStr):
        return "".join(
            _literal_text(value, module_consts)
            for value in node.values
            if isinstance(value, ast.Constant)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_text(node.left, module_consts) + _literal_text(node.right, module_consts)
    return ""


def _contains_sql_keyword(text: str) -> bool:
    return bool(SQL_KEYWORD_RE.search(text))


def _location_allows_sql_calls(rel_path: str) -> bool:
    return rel_path.startswith(ALLOWED_REPOSITORY_PREFIX) or rel_path == ALLOWED_MIGRATE_FILE


def _get_query_arg(call: ast.Call) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == "query":
            return kw.value
    if call.args:
        return call.args[0]
    return None


def _is_constant_query_arg(node: ast.AST | None, module_consts: dict[str, str]) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    return isinstance(node, ast.Name) and node.id in module_consts


def _is_allowlisted_migration_exec(
    call: ast.Call, attr: str, rel_path: str, source_lines: list[str]
) -> bool:
    if rel_path != ALLOWED_MIGRATE_FILE or attr != "execute":
        return False
    lineno = call.lineno
    if lineno < 1 or lineno > len(source_lines):
        return False
    return MIGRATION_EXEC_ALLOWLIST_MARKER in source_lines[lineno - 1]


def find_violations_in_source(source: str, rel_path: str) -> list[Violation]:
    """Check one module's source text. `rel_path` is POSIX-style, relative to `src/`."""
    tree = ast.parse(source, filename=rel_path)
    module_consts = _module_level_string_constants(tree)
    source_lines = source.splitlines()
    violations: list[Violation] = []
    flagged_binop_lines: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr

            if attr in SQL_METHOD_NAMES:
                if not _location_allows_sql_calls(rel_path):
                    violations.append(
                        Violation(
                            rel_path,
                            node.lineno,
                            f"call to .{attr}() is only allowed in "
                            f"{ALLOWED_REPOSITORY_PREFIX} or {ALLOWED_MIGRATE_FILE}",
                        )
                    )
                else:
                    query_arg = _get_query_arg(node)
                    is_allowlisted = _is_allowlisted_migration_exec(
                        node, attr, rel_path, source_lines
                    )
                    if not _is_constant_query_arg(query_arg, module_consts) and not is_allowlisted:
                        violations.append(
                            Violation(
                                rel_path,
                                node.lineno,
                                f"call to .{attr}() must use a string literal or "
                                "module-level string constant as its query",
                            )
                        )

            if attr == "format":
                text = _literal_text(node.func.value, module_consts)
                if _contains_sql_keyword(text):
                    violations.append(
                        Violation(
                            rel_path,
                            node.lineno,
                            "str.format() used to build a string containing a SQL keyword",
                        )
                    )

        elif isinstance(node, ast.JoinedStr):
            text = _literal_text(node, module_consts)
            if _contains_sql_keyword(text):
                violations.append(
                    Violation(
                        rel_path,
                        node.lineno,
                        "f-string contains a SQL keyword in its literal text",
                    )
                )

        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            text = _literal_text(node.left, module_consts)
            if _contains_sql_keyword(text):
                violations.append(
                    Violation(
                        rel_path,
                        node.lineno,
                        "%-formatting used to build a string containing a SQL keyword",
                    )
                )

        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            if node.lineno not in flagged_binop_lines:
                text = _literal_text(node, module_consts)
                if _contains_sql_keyword(text):
                    violations.append(
                        Violation(
                            rel_path,
                            node.lineno,
                            "'+' concatenation used to build a string containing a SQL keyword",
                        )
                    )
                    flagged_binop_lines.add(node.lineno)

    return violations


def scan_tree(src_root: Path) -> list[Violation]:
    """Scan every `.py` file under `src_root`, returning all violations found."""
    violations: list[Violation] = []
    for path in sorted(src_root.rglob("*.py")):
        rel_path = path.relative_to(src_root).as_posix()
        source = path.read_text(encoding="utf-8")
        violations.extend(find_violations_in_source(source, rel_path))
    return violations
