"""AST-based SQL-injection static guard.

Enforces the project's hard SQL rules (see CLAUDE.md) against any Python
module's source text. Three independent layers, each meant to fail *closed*
(when in doubt, flag it):

1. **Sink calls** (`execute`, `fetch`, `copy_to_table`, ..., including
   private asyncpg internals reachable off a `conn`/`pool` object such as
   `_execute` -- see `SQL_METHOD_NAMES`) may only appear in
   `catan_bot/db/repositories/*.py` or `catan_bot/db/migrate.py`, and every
   query-carrying / identifier-carrying argument they take must be a *sound*
   string constant: a module-level `NAME = "literal"` that is bound exactly
   once, anywhere, in any form (see `_sound_module_constants`). A resolved
   constant may also not contain a stacked statement (`;` before the end),
   since asyncpg's simple-protocol `execute()` runs those. Any `**`
   keyword-unpacking passed into a sink call is rejected outright, since its
   contents can't be inspected statically (`conn.fetch(**{"query": q})`
   would otherwise smuggle a dynamic query past the `query=`/`command=`
   keyword check). `catan_bot/db/migrate.py` gets exactly one
   narrowly-scoped exception for executing the contents of a trusted
   migration file.
2. **Indirect references** to a sink (aliasing `conn.execute`, reaching it
   via `getattr`/`operator.methodcaller`/`operator.attrgetter`, or via
   `__import__`/`importlib.import_module` under its own name *or* any
   alias) are flagged everywhere, since they can't be argument-checked
   statically. This layer also bans every other way Python lets code reach
   an attribute or a module without a literal, static name that
   `_check_sink_call`'s soundness check could inspect: `setattr()` onto a
   module object or `sys.modules[...]`, a direct attribute *store* on
   `sys.modules[...]`, `obj.__dict__[...]` keyed by a sink name or a
   non-literal key, `inspect.getattr_static(...)`, `obj.__getattribute__`
   (called or merely referenced), `._protocol` access (asyncpg's internal
   wire protocol -- one hop from every private sink this file knows about),
   `from x import *`, `builtins.__import__` / `import builtins`, and
   `__builtins__` access in any form (`__builtins__["open"]` or
   `__builtins__.open`). None of these have a legitimate use in this
   project's discord.py/asyncpg application code, so they're banned
   outright, anywhere in `src/`, with no attempt to scope them to a
   "risky" receiver -- see the comment on the `SQL_METHOD_NAMES`-as-value
   check below for why that's true even for the pre-existing alias rule.
3. **Build-time detection**: anywhere in the tree, string construction
   (f-string, `.format()`, `%`, `+`, `str.join` over a list/tuple literal,
   `str.__add__`, `string.Template`) whose literal text is SQL-*shaped* is
   flagged, so a query built outside a repository still gets caught even
   though it isn't a sink call itself.

   This layer is a **heuristic tuned for precision on ordinary English**,
   not recall on every conceivable SQL fragment -- Discord cog copy is full
   of words like "select", "order", "table", "delete", "drop", "truncate",
   "grant", and "values" used in their normal English sense, and an earlier
   version of this layer flagged all of them. The patterns below instead
   require actual SQL *structure*: two-or-more clause keywords in valid SQL
   order with SQL-ish tokens between them (`SELECT ... FROM`, `UPDATE t SET
   x =`, `DELETE FROM t WHERE`, ...), or a single clause keyword written in
   UPPERCASE sitting directly against an interpolation placeholder (an
   f-string's `{}`, a `.format()`/`%``-style token, or the empty seam left
   behind when a `+`/`.join()` concatenation's dynamic operand resolves to
   `""` -- see `_literal_text`). Requiring the keyword's *case* to match SQL
   convention is deliberate: real SQL text is written `ORDER BY`/`LIMIT`/
   `TRUNCATE`, while the equivalent English words in bot copy are lowercase
   or Title Case ("Order of play", "Limit reached", "Truncate the
   description"), so this one cheap signal resolves an otherwise-genuine
   ambiguity for free.

   **This means layer 3 can miss a contrived shape** -- e.g. a column list
   built by concatenation with literal text surviving on *both* sides of
   the dynamic part (`"SELECT " + cols + " FROM t"` is still caught,
   because the dynamic part vanishes entirely and leaves "SELECT" touching
   "FROM" with nothing between; a column list that's only *partially*
   dynamic, like `f"SELECT id, {extra} FROM t"`, is caught too, because the
   placeholder is accepted as a column token -- but not every partial shape
   is guaranteed to be). **That asymmetry is intentional and safe**: layers
   1 and 2 above are the real, structural control -- they gate every actual
   sink call at the argument level and never rely on pattern-matching text.
   Layer 3 exists only to catch SQL text assembled *outside* of a sink call
   (see rule 1), as defense in depth; a miss here can never let untrusted
   input reach the database, because rule 1 still gates the call itself.
   Given that trade-off, this file deliberately chases down false positives
   on English aggressively and does not chase every possible false
   negative in hand-built SQL fragments -- and per the project's rules,
   there is no `noqa` escape hatch to paper over either direction.

   `GRANT`/`REVOKE ... ON` and `COPY ... TO/FROM` were dropped from this
   layer entirely (they used to be bare-keyword patterns): they matched
   ordinary sentences like "Grant {user} admin on the server?" and "Copy
   the link to {x} from the event" far more often than real SQL, no
   required test exercises them, and any real dynamic GRANT/COPY reaching
   an actual sink call is still caught by layer 1 (the argument to that
   call would never be a sound constant).
"""

from __future__ import annotations

import re
import tokenize
from ast import (
    AST,
    AnnAssign,
    Assign,
    AsyncFunctionDef,
    Attribute,
    BinOp,
    Call,
    ClassDef,
    Constant,
    Del,
    ExceptHandler,
    FunctionDef,
    Global,
    Import,
    ImportFrom,
    JoinedStr,
    Lambda,
    List,
    MatchAs,
    MatchMapping,
    MatchStar,
    Module,
    Name,
    Nonlocal,
    Store,
    Subscript,
    Tuple,
    arguments,
    iter_child_nodes,
    parse,
    walk,
)
from ast import Add as AstAdd
from ast import Mod as AstMod
from collections import Counter
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

# --------------------------------------------------------------------------
# Sink inventory (asyncpg 0.31 `Connection` / `Pool`)
# --------------------------------------------------------------------------

# Methods whose query-carrying argument(s) must resolve to a sound constant.
QUERY_SINK_NAMES = frozenset(
    {
        "execute",
        "executemany",
        "fetch",
        "fetchrow",
        "fetchval",
        "fetchmany",
        "prepare",
        "cursor",
        "copy_from_query",
        # Private asyncpg.Connection internals (asyncpg 0.31) that still
        # carry raw SQL text and are reachable straight off a `conn`/`pool`
        # object, e.g. `conn._execute(...)`. Not public API, but a sink is a
        # sink -- see test_asyncpg_sinks_cover_every_query_bearing_method,
        # which fails if a future asyncpg upgrade adds another one of these.
        "_execute",
        "_executemany",
        "_do_execute",
        "_get_statement",
        "_prepare",
        "_time_and_log",
        "_Connection__execute",  # name-mangled form of Connection.__execute
        "_copy_in",  # takes `copy_stmt`, not `query`, but is the same sink
        "_copy_out",  # shape: a raw SQL/COPY statement string, no params
    }
)

# Methods that carry raw SQL identifiers/predicates as `table_name`,
# `schema_name`, `where`, or `columns` instead of a `query` string.
TABLE_SINK_NAMES = frozenset(
    {
        "copy_from_table",
        "copy_to_table",
        "copy_records_to_table",
        # Private: asyncpg.Connection's internal COPY ... WHERE formatter.
        "_format_copy_where",
    }
)

# Union, used for the location rule and for indirect-reference detection.
SQL_METHOD_NAMES = QUERY_SINK_NAMES | TABLE_SINK_NAMES

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


@dataclass(frozen=True)
class ScanResult:
    """Result of scanning a source tree: what was found, and what was looked at.

    Keeping `scanned_files` alongside `violations` is what lets the real-tree
    test prove it isn't vacuously passing over zero files (see rule 8).
    """

    violations: list[Violation] = field(default_factory=list)
    scanned_files: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# SQL-shaped text patterns (build-time detection, rule 7)
# --------------------------------------------------------------------------

# --- Multi-clause SQL: two-or-more real clause keywords in valid SQL order,
# with SQL-ish tokens (identifiers, '*', placeholders) between them. These
# are case-INSENSITIVE: real SQL is still SQL in lowercase, and none of
# these shapes occur in ordinary English by accident (they all require a
# specific keyword *pair* in a specific order, not just one keyword).

# The column list accepts a placeholder token ('{}' from an f-string's
# FormattedValue, or a literal '%s') as well as real identifiers, so a
# partially-dynamic column list (`f"SELECT id, {extra} FROM t"`) is still
# caught, not just a fully-static or fully-empty one.
_SELECT_FROM_RE = re.compile(
    r"\bSELECT\s+(?:\*|(?:DISTINCT\s+)?(?:[\w.*()]+|\{\}|%s)"
    r"(?:\s*,\s*(?:[\w.*()]+|\{\}|%s))*)\s+FROM\b",
    re.IGNORECASE,
)
# A bare 'INSERT INTO' matches ordinary English nowhere near as often as
# 'SELECT'/'FROM' do, but still require the shape to continue toward
# VALUES/SELECT so a stray "insert into" phrase can't match alone.
_INSERT_INTO_RE = re.compile(
    r"\bINSERT\s+INTO\s+[A-Za-z_][\w.]*\b.{0,120}?\b(?:VALUES|SELECT)\b",
    re.IGNORECASE | re.DOTALL,
)
_UPDATE_SET_RE = re.compile(
    r"\bUPDATE\s+[A-Za-z_][\w.]*\s+SET\s+[A-Za-z_][\w.]*\s*=", re.IGNORECASE
)
# Anchored so an English continuation ("Delete from your calendar: ...")
# can't qualify: the identifier right after FROM must be followed
# *immediately* (only whitespace between) by WHERE/RETURNING/';'/end-of-
# string -- real SQL, not a sentence that happens to keep going.
_DELETE_FROM_RE = re.compile(
    r"\bDELETE\s+FROM\s+[A-Za-z_][\w.]*\s*(?:WHERE\b|RETURNING\b|;|$)", re.IGNORECASE
)
_UNION_SELECT_RE = re.compile(r"\bUNION\b(?:\s+ALL)?\s+SELECT\b", re.IGNORECASE)
_WHERE_COND_RE = re.compile(
    r"\bWHERE\s+[A-Za-z_][\w.]*\s*(?:=|<|>|\bIN\b|\bIS\b|\bLIKE\b|\bBETWEEN\b)",
    re.IGNORECASE,
)
# CREATE|ALTER|DROP <object> [IF (NOT)? EXISTS] <ident>?, anchored the same
# way as DELETE FROM: whatever follows the (optional) identifier must
# immediately be '(', ';', 'IF EXISTS', 'CASCADE', a placeholder, or
# end-of-string. "Drop table tennis night? React below" fails here because
# real words ("night? React...") follow the identifier instead.
_DDL_OBJECT_RE = re.compile(
    r"\b(?:CREATE|ALTER|DROP)\s+(?:TABLE|ROLE|DATABASE|SCHEMA|INDEX|FUNCTION|EXTENSION|VIEW)\b"
    r"\s*(?:IF\s+(?:NOT\s+)?EXISTS\b\s*)?"
    r"(?:[A-Za-z_][\w.]*\s*)?"
    r"(?:\(|;|IF\s+EXISTS\b|CASCADE\b|\{\}|%s|$)",
    re.IGNORECASE,
)

# --- Single-clause fragment directly touching an interpolation placeholder.
# Deliberately case-SENSITIVE (no re.IGNORECASE): real SQL text is written
# in these exact keyword forms by convention, while the same words in
# ordinary bot copy show up lowercase or Title-Case ("Truncate the
# description", "Values ({a}, {b})", "Limit reached"). Matching only the
# literal uppercase spelling is a free, cheap way to tell them apart -- it
# costs nothing on real SQL (which is written this way anyway) and rules
# out prose without needing a second signal.
_CLAUSE_KEYWORD_RE = r"(?:ORDER\s+BY|GROUP\s+BY|LIMIT|OFFSET|RETURNING|TRUNCATE|VALUES)"
_PLACEHOLDER_RE = r"(?:\{\}|%s)"
_SINGLE_CLAUSE_PLACEHOLDER_RE = re.compile(
    rf"\b{_CLAUSE_KEYWORD_RE}\b\s*\(?\s*(?:{_PLACEHOLDER_RE}|$)"
)

# --- Dynamic column list: SELECT ... FROM with *nothing but* whitespace
# and/or placeholders between them -- the shape left behind when
# `"SELECT " + cols + " FROM t"` or `" ".join(["SELECT", c, "FROM", t])`
# collapses its dynamic operand to "" (see `_literal_text`). Case-sensitive
# for the same reason as above; ordinary English never spells out
# "SELECT ... FROM" fully uppercase with nothing but a placeholder between.
_DYNAMIC_SELECT_FROM_RE = re.compile(r"\bSELECT\b(?:\s|\{\}|%s)*\bFROM\b")

_SQL_SHAPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    _SELECT_FROM_RE,
    _INSERT_INTO_RE,
    _UPDATE_SET_RE,
    _DELETE_FROM_RE,
    _DDL_OBJECT_RE,
    _UNION_SELECT_RE,
    _WHERE_COND_RE,
    _SINGLE_CLAUSE_PLACEHOLDER_RE,
    _DYNAMIC_SELECT_FROM_RE,
)


def _looks_like_sql(text: str) -> bool:
    return bool(text) and any(p.search(text) for p in _SQL_SHAPE_PATTERNS)


# A resolved constant may carry exactly one trailing ';' (a normal statement
# terminator); anything before that is a stacked statement.
def _has_stacked_statement(sql_text: str) -> bool:
    stripped = sql_text.rstrip()
    if not stripped:
        return False
    body = stripped[:-1] if stripped.endswith(";") else stripped
    return ";" in body


# --------------------------------------------------------------------------
# Binding analysis: "is this Name a sound module-level string constant?"
# --------------------------------------------------------------------------


def _arg_names(args: arguments) -> set[str]:
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _binding_counts(nodes: list[AST]) -> Counter[str]:
    """Count every *binding* occurrence of every name across `nodes`.

    Deliberately broad: assignment/augassign/annassign targets (incl. tuple
    unpacking), for/async-for targets, with-as, walrus, function/lambda
    parameters, import aliases, global/nonlocal, except-as, def/class names,
    and match capture names all count. A name is only a sound constant if
    its *total* count across the module is exactly one.
    """
    counts: Counter[str] = Counter()
    for node in nodes:
        if isinstance(node, Name) and isinstance(node.ctx, Store | Del):
            counts[node.id] += 1
        elif isinstance(node, FunctionDef | AsyncFunctionDef):
            counts[node.name] += 1
            for name in _arg_names(node.args):
                counts[name] += 1
        elif isinstance(node, Lambda):
            for name in _arg_names(node.args):
                counts[name] += 1
        elif isinstance(node, ClassDef):
            counts[node.name] += 1
        elif isinstance(node, Import | ImportFrom):
            for alias in node.names:
                counts[alias.asname or alias.name.split(".")[0]] += 1
        elif isinstance(node, Global | Nonlocal):
            for name in node.names:
                counts[name] += 1
        elif (
            isinstance(node, ExceptHandler)
            and node.name
            or isinstance(node, MatchAs)
            and node.name
            or isinstance(node, MatchStar)
            and node.name
        ):
            counts[node.name] += 1
        elif isinstance(node, MatchMapping) and node.rest:
            counts[node.rest] += 1
    return counts


def _module_level_constant_candidates(tree: Module) -> dict[str, str]:
    """Name -> literal, for a *direct* module-level `Name = "lit"` / AnnAssign.

    Only looks at `tree.body` (top-level statements), matching "assigned...
    at module level" in the hardening rule. Soundness (uniqueness) is
    layered on separately via `_binding_counts`.
    """
    candidates: dict[str, str] = {}
    for stmt in tree.body:
        target_name: str | None = None
        value = None
        if (
            isinstance(stmt, Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], Name)
        ):
            target_name = stmt.targets[0].id
            value = stmt.value
        elif (
            isinstance(stmt, AnnAssign) and isinstance(stmt.target, Name) and stmt.value is not None
        ):
            target_name = stmt.target.id
            value = stmt.value
        if target_name is None:
            continue
        if isinstance(value, Constant) and isinstance(value.value, str):
            candidates[target_name] = value.value
        else:
            candidates.pop(target_name, None)
    return candidates


def _sound_module_constants(tree: Module) -> dict[str, str]:
    """Names that are safe to treat as SQL constants anywhere in the module."""
    counts = _binding_counts(list(walk(tree)))
    candidates = _module_level_constant_candidates(tree)
    return {name: value for name, value in candidates.items() if counts[name] == 1}


# --------------------------------------------------------------------------
# Best-effort *local* constant resolution (rule 7 text extraction only --
# never used to gate a sink call, only to widen what build-time detection
# can see; a miss here can never hide a sink-argument violation).
# --------------------------------------------------------------------------


def _iter_own_scope(root: AST) -> list[AST]:
    """`root` and all descendants, stopping at nested function/class/lambda bodies."""
    out: list[AST] = []
    stack = [root]
    while stack:
        current = stack.pop()
        out.append(current)
        if current is not root and isinstance(
            current, FunctionDef | AsyncFunctionDef | Lambda | ClassDef
        ):
            continue
        stack.extend(iter_child_nodes(current))
    return out


def _scope_string_constants(scope_root: AST) -> dict[str, str]:
    nodes = _iter_own_scope(scope_root)
    counts = _binding_counts(nodes)
    candidates: dict[str, str] = {}
    for node in nodes:
        target_name: str | None = None
        value = None
        if (
            isinstance(node, Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], Name)
        ):
            target_name = node.targets[0].id
            value = node.value
        elif (
            isinstance(node, AnnAssign) and isinstance(node.target, Name) and node.value is not None
        ):
            target_name = node.target.id
            value = node.value
        if target_name is None:
            continue
        if isinstance(value, Constant) and isinstance(value.value, str):
            candidates[target_name] = value.value
        else:
            candidates.pop(target_name, None)
    return {name: value for name, value in candidates.items() if counts[name] == 1}


def _build_parent_map(tree: Module) -> dict[int, AST]:
    parents: dict[int, AST] = {}
    for node in walk(tree):
        for child in iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _enclosing_scope(node: AST, parents: dict[int, AST], module: Module) -> AST:
    current = node
    while id(current) in parents:
        current = parents[id(current)]
        if isinstance(current, FunctionDef | AsyncFunctionDef | Lambda):
            return current
    return module


class _NameResolver:
    """Caches, per enclosing function (or module), a merged constant map."""

    def __init__(self, tree: Module, module_consts: dict[str, str]) -> None:
        self._tree = tree
        self._module_consts = module_consts
        self._parents = _build_parent_map(tree)
        self._cache: dict[int, dict[str, str]] = {}

    def for_node(self, node: AST) -> dict[str, str]:
        scope = _enclosing_scope(node, self._parents, self._tree)
        key = id(scope)
        if key not in self._cache:
            if scope is self._tree:
                self._cache[key] = self._module_consts
            else:
                merged = dict(self._module_consts)
                merged.update(_scope_string_constants(scope))
                self._cache[key] = merged
        return self._cache[key]

    @property
    def parents(self) -> dict[int, AST]:
        return self._parents


# --------------------------------------------------------------------------
# Literal-text extraction (best-effort, "only ever grows the scanned text")
# --------------------------------------------------------------------------


def _literal_text(node: AST, names: dict[str, str]) -> str:
    if isinstance(node, Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, Name):
        return names.get(node.id, "")
    if isinstance(node, JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, Constant):
                parts.append(_literal_text(value, names))
            else:
                # FormattedValue: substitute a placeholder, per rule 7.
                parts.append("{}")
        return "".join(parts)
    if isinstance(node, BinOp) and isinstance(node.op, AstAdd):
        return _literal_text(node.left, names) + _literal_text(node.right, names)
    if isinstance(node, Call) and isinstance(node.func, Attribute):
        attr = node.func.attr
        if attr == "format":
            return _literal_text(node.func.value, names)
        if attr == "join" and len(node.args) == 1 and isinstance(node.args[0], List | Tuple):
            sep = _literal_text(node.func.value, names)
            return sep.join(_literal_text(elt, names) for elt in node.args[0].elts)
        if attr == "__add__":
            if (
                isinstance(node.func.value, Name)
                and node.func.value.id == "str"
                and len(node.args) >= 2
            ):
                return _literal_text(node.args[0], names) + _literal_text(node.args[1], names)
            if node.args:
                return _literal_text(node.func.value, names) + _literal_text(node.args[0], names)
    return ""


def _is_string_template_call(func: AST) -> bool:
    if isinstance(func, Name) and func.id == "Template":
        return True
    return (
        isinstance(func, Attribute)
        and func.attr == "Template"
        and isinstance(func.value, Name)
        and func.value.id == "string"
    )


# --------------------------------------------------------------------------
# Indirect-reference detection (rule 2)
# --------------------------------------------------------------------------


def _is_dynamic_import_call(func: AST) -> bool:
    if isinstance(func, Name) and func.id in ("__import__", "import_module"):
        return True
    return isinstance(func, Attribute) and func.attr == "import_module"


def _is_introspection_call(func: AST) -> bool:
    return isinstance(func, Name) and func.id in ("globals", "locals", "vars")


def _is_operator_call(func: AST, name: str) -> bool:
    if isinstance(func, Name) and func.id == name:
        return True
    return (
        isinstance(func, Attribute)
        and func.attr == name
        and isinstance(func.value, Name)
        and func.value.id == "operator"
    )


def _dynamic_attr_name_message(node: AST, via: str) -> str | None:
    if isinstance(node, Constant) and isinstance(node.value, str):
        if node.value in SQL_METHOD_NAMES:
            return f"{via}() names a SQL sink method ({node.value!r}) indirectly"
        return None
    return f"{via}() uses a non-literal attribute/method name, which could resolve to a SQL sink"


def _is_getattr_static_call(
    func: AST, inspect_module_aliases: set[str], getattr_static_aliases: set[str]
) -> bool:
    if isinstance(func, Name) and func.id == "getattr_static":
        return True
    if isinstance(func, Name) and func.id in getattr_static_aliases:
        return True
    return (
        isinstance(func, Attribute)
        and func.attr == "getattr_static"
        and isinstance(func.value, Name)
        and func.value.id in inspect_module_aliases
    )


def _indirect_name_call_message(call: Call) -> str | None:
    func = call.func
    if isinstance(func, Name) and func.id == "getattr":
        if len(call.args) >= 2:
            return _dynamic_attr_name_message(call.args[1], "getattr")
        return None
    if _is_operator_call(func, "methodcaller"):
        if call.args:
            return _dynamic_attr_name_message(call.args[0], "operator.methodcaller")
        return None
    if _is_operator_call(func, "attrgetter"):
        for arg in call.args:
            msg = _dynamic_attr_name_message(arg, "operator.attrgetter")
            if msg:
                return msg
    return None


# --------------------------------------------------------------------------
# Sink-reachability backlog (rule 2 continued): every other way Python lets
# code reach an attribute or a module by something other than a static,
# literal name. Unlike `getattr`/`operator.methodcaller`/`attrgetter` above,
# none of these have a legitimate use in this project's discord.py/asyncpg
# application code, so each is banned outright, anywhere in `src/`, with no
# attempt to check whether the *particular* call site looks dangerous.
# --------------------------------------------------------------------------


def _module_bound_names(tree: Module) -> set[str]:
    """Names bound by a plain `import x` / `import y as x` anywhere in the
    module -- i.e. names that are statically known to refer to a module
    object. Used by `_is_dangerous_setattr_target`: `setattr()` onto one of
    these can silently rebind an attribute (including an imported sink
    alias) on a live module.
    """
    names: set[str] = set()
    for node in walk(tree):
        if isinstance(node, Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _import_module_aliases(tree: Module) -> set[str]:
    """Local names bound to `importlib.import_module` under an alias other
    than its own name, e.g. `from importlib import import_module as im`
    binds `im`. A call through that alias (`im("os")`) is exactly as
    dynamic as calling `import_module` directly, but `_is_dynamic_import_call`
    only recognizes the literal name -- this is what lets the guard follow
    the alias too (see DESIGN.md's guard backlog).
    """
    names: set[str] = set()
    for node in walk(tree):
        if isinstance(node, ImportFrom) and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module" and alias.asname:
                    names.add(alias.asname)
    return names


def _inspect_aliases(tree: Module) -> tuple[set[str], set[str]]:
    """Return local aliases for ``inspect`` and ``getattr_static``.

    Imports are cheap to reject at the boundary, but recognizing aliases here
    keeps the SQL guard fail-closed even when a caller hides the access behind
    ``import inspect as i`` or ``from inspect import getattr_static as gs``.
    """
    module_names: set[str] = {"inspect"}
    function_names: set[str] = {"getattr_static"}
    for node in walk(tree):
        if isinstance(node, Import):
            for alias in node.names:
                if alias.name == "inspect":
                    module_names.add(alias.asname or "inspect")
        elif isinstance(node, ImportFrom) and node.module == "inspect":
            for alias in node.names:
                if alias.name == "getattr_static":
                    function_names.add(alias.asname or "getattr_static")
    return module_names, function_names


def _sys_modules_names(tree: Module) -> set[str]:
    """Names that refer to the live ``sys.modules`` registry.

    Include both module aliases (``import sys as s`` → ``s.modules``) and
    direct registry imports (``from sys import modules as registry`` →
    ``registry[...]``).
    """
    module_names: set[str] = {"sys"}
    registry_names: set[str] = set()
    for node in walk(tree):
        if isinstance(node, Import):
            for alias in node.names:
                if alias.name == "sys":
                    module_names.add(alias.asname or "sys")
        elif isinstance(node, ImportFrom) and node.module == "sys":
            for alias in node.names:
                if alias.name == "modules":
                    registry_names.add(alias.asname or "modules")
    return module_names | registry_names


def _is_import_module_alias_call(func: AST, aliases: set[str]) -> bool:
    return isinstance(func, Name) and func.id in aliases


def _is_sys_modules_subscript(node: AST, sys_modules_names: set[str]) -> bool:
    """`sys.modules[...]` -- the live module registry, keyed by module name.

    Both `setattr(sys.modules[...], ...)` and a direct attribute store
    (`sys.modules[...].attr = value`) reach the same live object as
    importing that module normally would.
    """
    if not isinstance(node, Subscript):
        return False
    if isinstance(node.value, Name):
        return node.value.id in sys_modules_names
    return (
        isinstance(node.value, Attribute)
        and node.value.attr == "modules"
        and isinstance(node.value.value, Name)
        and node.value.value.id in sys_modules_names
    )


def _is_dangerous_setattr_target(
    node: AST, module_names: set[str], sys_modules_names: set[str]
) -> bool:
    if _is_sys_modules_subscript(node, sys_modules_names):
        return True
    return isinstance(node, Name) and node.id in module_names


def _dict_subscript_violation_message(node: Subscript) -> str | None:
    """`obj.__dict__[...]` -- flags a literal sink name or any non-literal
    key. A literal key that isn't a sink name is left alone: plain
    `obj.__dict__["some_field"]` has no bearing on SQL sinks.
    """
    if not (isinstance(node.value, Attribute) and node.value.attr == "__dict__"):
        return None
    key = node.slice
    if isinstance(key, Constant) and isinstance(key.value, str):
        if key.value in SQL_METHOD_NAMES:
            return f"__dict__[{key.value!r}] names a SQL sink method indirectly"
        return None
    return "__dict__[...] uses a non-literal key, which could resolve to a SQL sink"


# --------------------------------------------------------------------------
# Migration-file allowlist (rule 6)
# --------------------------------------------------------------------------


def _comment_marker_lines(source: str, marker: str) -> set[int]:
    """Line numbers where `marker` appears in a real `#` comment token.

    Uses `tokenize` rather than substring search on raw lines, so a marker
    sitting inside a string literal (not a comment) doesn't count.
    """
    lines: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(StringIO(source).readline):
            if tok.type == tokenize.COMMENT and marker in tok.string:
                lines.add(tok.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return set()
    return lines


def _resolve_migration_allowlist(tree: Module, rel_path: str, comment_lines: set[int]) -> set[int]:
    """id()s of the (at most one) `.execute(sql_text)` call the allowlist exempts.

    Exactly one marker-tagged `.execute(...)` call is permitted, and only
    when its first argument is precisely `sql_text`. If there are zero, two,
    or more marked calls, or the shape doesn't match, nothing is exempted --
    every marked call then falls through to the normal soundness check
    (which flags it, since its argument is never a sound constant).
    """
    if rel_path != ALLOWED_MIGRATE_FILE:
        return set()
    candidates = [
        node
        for node in walk(tree)
        if isinstance(node, Call)
        and isinstance(node.func, Attribute)
        and node.func.attr == "execute"
        and node.lineno in comment_lines
    ]
    if len(candidates) != 1:
        return set()
    (call,) = candidates
    if call.args and isinstance(call.args[0], Name) and call.args[0].id == "sql_text":
        return {id(call)}
    return set()


def _location_allows_sql_calls(rel_path: str) -> bool:
    return rel_path.startswith(ALLOWED_REPOSITORY_PREFIX) or rel_path == ALLOWED_MIGRATE_FILE


# --------------------------------------------------------------------------
# Sink-argument soundness (rule 1 / rule 3 / rule 4)
# --------------------------------------------------------------------------


def _is_sound_string_constant(node: AST | None, names: dict[str, str]) -> bool:
    if node is None:
        return False
    if isinstance(node, Constant) and isinstance(node.value, str):
        return True
    return isinstance(node, Name) and node.id in names


def _is_sound_columns_value(node: AST | None, names: dict[str, str]) -> bool:
    if _is_sound_string_constant(node, names):
        return True
    if isinstance(node, List | Tuple):
        return all(_is_sound_string_constant(elt, names) for elt in node.elts)
    return False


def _resolved_text(node: AST, names: dict[str, str]) -> str:
    if isinstance(node, Constant):
        return node.value
    if isinstance(node, Name):
        return names[node.id]
    return ""


def _query_sink_arg_nodes(call: Call) -> list[tuple[str, AST]]:
    """Every query-carrying argument: the first positional, plus
    query=/command=/copy_stmt= (the latter is what `_copy_in`/`_copy_out`
    name their raw-SQL parameter)."""
    nodes: list[tuple[str, AST]] = []
    if call.args:
        nodes.append(("positional", call.args[0]))
    for kw in call.keywords:
        if kw.arg in ("query", "command", "copy_stmt"):
            nodes.append((kw.arg, kw.value))
    return nodes


# `_format_copy_where`'s only parameter is `where`, not `table_name`; every
# other table-style sink's first positional parameter is `table_name`.
_TABLE_SINK_POSITIONAL_LABEL = {"_format_copy_where": "where"}


def _table_sink_arg_nodes(call: Call, attr: str) -> list[tuple[str, AST]]:
    nodes: list[tuple[str, AST]] = []
    if call.args:
        label = _TABLE_SINK_POSITIONAL_LABEL.get(attr, "table_name")
        nodes.append((label, call.args[0]))
    for kw in call.keywords:
        if kw.arg in ("table_name", "schema_name", "where", "columns"):
            nodes.append((kw.arg, kw.value))
    return nodes


def _check_sink_call(
    call: Call, attr: str, rel_path: str, names: dict[str, str], is_allowlisted: bool
) -> list[Violation]:
    out: list[Violation] = []
    if not _location_allows_sql_calls(rel_path):
        out.append(
            Violation(
                rel_path,
                call.lineno,
                f"call to .{attr}() is only allowed in {ALLOWED_REPOSITORY_PREFIX} "
                f"or {ALLOWED_MIGRATE_FILE}",
            )
        )
        return out
    star_kwargs = [kw for kw in call.keywords if kw.arg is None]
    if star_kwargs:
        # A `**mapping` keyword can smuggle a `query=`/`command=`/`where=`/
        # etc. key past every other check here -- its contents (whatever
        # they are) can never be resolved statically, so it's rejected
        # outright, for query-style and table-style sinks alike.
        out.append(
            Violation(
                rel_path,
                call.lineno,
                f"call to .{attr}() uses ** keyword-unpacking; its contents can't be "
                "checked statically, so it's not allowed on a SQL sink",
            )
        )
    if is_allowlisted:
        return out
    if attr in QUERY_SINK_NAMES:
        for label, node in _query_sink_arg_nodes(call):
            if not _is_sound_string_constant(node, names):
                out.append(
                    Violation(
                        rel_path,
                        call.lineno,
                        f"call to .{attr}() must use a string literal or module-level "
                        f"string constant for its {label} argument",
                    )
                )
            elif _has_stacked_statement(_resolved_text(node, names)):
                out.append(
                    Violation(
                        rel_path,
                        call.lineno,
                        f"call to .{attr}() query constant contains a stacked statement "
                        "(';' appears before the end)",
                    )
                )
    elif attr in TABLE_SINK_NAMES:
        for label, node in _table_sink_arg_nodes(call, attr):
            sound = (
                _is_sound_columns_value(node, names)
                if label == "columns"
                else _is_sound_string_constant(node, names)
            )
            if not sound:
                out.append(
                    Violation(
                        rel_path,
                        call.lineno,
                        f"call to .{attr}() must use a sound constant for its {label} argument",
                    )
                )
    return out


# --------------------------------------------------------------------------
# Main entry points
# --------------------------------------------------------------------------


def find_violations_in_source(source: str, rel_path: str) -> list[Violation]:
    """Check one module's source text. `rel_path` is POSIX-style, relative to `src/`."""
    tree = parse(source, filename=rel_path)
    module_consts = _sound_module_constants(tree)
    resolver = _NameResolver(tree, module_consts)
    comment_lines = _comment_marker_lines(source, MIGRATION_EXEC_ALLOWLIST_MARKER)
    allowlisted_ids = _resolve_migration_allowlist(tree, rel_path, comment_lines)
    module_bound_names = _module_bound_names(tree)
    import_module_aliases = _import_module_aliases(tree)
    inspect_module_aliases, getattr_static_aliases = _inspect_aliases(tree)
    sys_modules_names = _sys_modules_names(tree)

    call_func_attr_ids = {
        id(node.func)
        for node in walk(tree)
        if isinstance(node, Call) and isinstance(node.func, Attribute)
    }

    violations: list[Violation] = []
    flagged_add_ids: set[int] = set()
    parents = resolver.parents

    for node in walk(tree):
        if isinstance(node, Call):
            if _is_dynamic_import_call(node.func):
                violations.append(
                    Violation(
                        rel_path,
                        node.lineno,
                        "dynamic import via __import__()/importlib.import_module() is "
                        "not allowed anywhere in src/",
                    )
                )
            if _is_introspection_call(node.func):
                violations.append(
                    Violation(
                        rel_path,
                        node.lineno,
                        "call to globals()/locals()/vars() is not allowed anywhere in "
                        "src/ (it can rebind a 'constant' at runtime)",
                    )
                )
            indirect_msg = _indirect_name_call_message(node)
            if indirect_msg:
                violations.append(Violation(rel_path, node.lineno, indirect_msg))

            if _is_getattr_static_call(node.func, inspect_module_aliases, getattr_static_aliases):
                violations.append(
                    Violation(
                        rel_path,
                        node.lineno,
                        "inspect.getattr_static() is not allowed anywhere in src/ -- it "
                        "reads any attribute, including a SQL sink, bypassing the normal "
                        "attribute-access machinery this guard can otherwise see through",
                    )
                )

            if _is_import_module_alias_call(node.func, import_module_aliases):
                violations.append(
                    Violation(
                        rel_path,
                        node.lineno,
                        "dynamic import via an aliased importlib.import_module() is not "
                        "allowed anywhere in src/",
                    )
                )

            if (
                isinstance(node.func, Name)
                and node.func.id == "setattr"
                and node.args
                and _is_dangerous_setattr_target(
                    node.args[0], module_bound_names, sys_modules_names
                )
            ):
                violations.append(
                    Violation(
                        rel_path,
                        node.lineno,
                        "setattr() onto a module or sys.modules[...] is not allowed "
                        "anywhere in src/ -- it can silently rebind an imported SQL sink "
                        "or import hook",
                    )
                )

            if isinstance(node.func, Attribute):
                attr = node.func.attr
                if attr in SQL_METHOD_NAMES:
                    violations.extend(
                        _check_sink_call(
                            node,
                            attr,
                            rel_path,
                            resolver.for_node(node),
                            id(node) in allowlisted_ids,
                        )
                    )
                if attr == "format":
                    text = _literal_text(node.func.value, resolver.for_node(node))
                    if _looks_like_sql(text):
                        violations.append(
                            Violation(
                                rel_path,
                                node.lineno,
                                "str.format() used to build a SQL-shaped string",
                            )
                        )
                if (
                    attr == "join"
                    and len(node.args) == 1
                    and isinstance(node.args[0], List | Tuple)
                ):
                    text = _literal_text(node, resolver.for_node(node))
                    if _looks_like_sql(text):
                        violations.append(
                            Violation(
                                rel_path,
                                node.lineno,
                                "str.join() over a list/tuple literal builds a SQL-shaped string",
                            )
                        )
                if attr == "__add__":
                    text = _literal_text(node, resolver.for_node(node))
                    if _looks_like_sql(text):
                        violations.append(
                            Violation(
                                rel_path,
                                node.lineno,
                                "str.__add__() used to build a SQL-shaped string",
                            )
                        )

            if _is_string_template_call(node.func) and node.args:
                text = _literal_text(node.args[0], resolver.for_node(node))
                if _looks_like_sql(text):
                    violations.append(
                        Violation(
                            rel_path,
                            node.lineno,
                            "string.Template(...) used to build a SQL-shaped string",
                        )
                    )

        elif isinstance(node, Attribute):
            # Fail-closed and deliberately *unscoped*: this flags `.execute`/
            # `.fetch`/`.cursor`/`.prepare` used as a value anywhere in src/,
            # not just on asyncpg-typed receivers. An earlier audit flagged
            # this as a possible false positive on discord.py objects that
            # use attributes of the same name as callbacks -- but discord.py
            # doesn't expose attributes named exactly `fetch`, `execute`,
            # `cursor`, or `prepare` (checked against discord.py 2.7's public
            # API), so there is nothing to scope this to and no accuracy
            # gained by trying. Narrowing it to "asyncpg-typed receivers"
            # would also require type inference this file doesn't do, and
            # would only reduce coverage for no measured benefit.
            if node.attr in SQL_METHOD_NAMES and id(node) not in call_func_attr_ids:
                violations.append(
                    Violation(
                        rel_path,
                        node.lineno,
                        f"reference to .{node.attr} as a value (alias/callback/partial) is not "
                        "allowed -- SQL sinks must be called directly so their arguments "
                        "can be checked",
                    )
                )
            if node.attr == "__getattribute__":
                violations.append(
                    Violation(
                        rel_path,
                        node.lineno,
                        ".__getattribute__ is not allowed anywhere in src/ -- it can "
                        "resolve to any attribute, including a SQL sink, whether it's "
                        "called directly or just referenced as a value",
                    )
                )
            if node.attr == "_protocol":
                violations.append(
                    Violation(
                        rel_path,
                        node.lineno,
                        "._protocol access is not allowed anywhere in src/ -- it reaches "
                        "asyncpg's internal wire protocol, one hop from every private "
                        "sink this guard knows about",
                    )
                )
            if node.attr == "__import__":
                violations.append(
                    Violation(
                        rel_path,
                        node.lineno,
                        ".__import__ access is not allowed anywhere in src/ (e.g. "
                        "builtins.__import__), whether it's called or just referenced",
                    )
                )
            if isinstance(node.ctx, Store) and _is_sys_modules_subscript(
                node.value, sys_modules_names
            ):
                violations.append(
                    Violation(
                        rel_path,
                        node.lineno,
                        "attribute assignment on sys.modules[...] is not allowed anywhere "
                        "in src/ -- it can rebind anything on a live module, including an "
                        "imported SQL sink alias",
                    )
                )

        elif isinstance(node, Subscript):
            dict_msg = _dict_subscript_violation_message(node)
            if dict_msg:
                violations.append(Violation(rel_path, node.lineno, dict_msg))

        elif isinstance(node, Name):
            if node.id == "__builtins__":
                violations.append(
                    Violation(
                        rel_path,
                        node.lineno,
                        "__builtins__ access is not allowed anywhere in src/ (covers both "
                        "__builtins__['name'] and __builtins__.name forms)",
                    )
                )

        elif isinstance(node, ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                violations.append(
                    Violation(
                        rel_path, node.lineno, "'from x import *' is not allowed anywhere in src/"
                    )
                )
            if node.module == "importlib" and any(a.name == "import_module" for a in node.names):
                violations.append(
                    Violation(
                        rel_path,
                        node.lineno,
                        "importing importlib.import_module (aliased or not) is not "
                        "allowed anywhere in src/ -- calling it can resolve any "
                        "module/attribute, including a SQL sink",
                    )
                )
            if node.module == "builtins" and any(a.name == "__import__" for a in node.names):
                violations.append(
                    Violation(
                        rel_path,
                        node.lineno,
                        "importing builtins.__import__ is not allowed anywhere in src/",
                    )
                )
            if node.module == "inspect" and any(a.name == "getattr_static" for a in node.names):
                violations.append(
                    Violation(
                        rel_path,
                        node.lineno,
                        "importing inspect.getattr_static is not allowed anywhere in src/",
                    )
                )

        elif isinstance(node, Import):
            if any(alias.name in {"builtins", "importlib"} for alias in node.names):
                violations.append(
                    Violation(
                        rel_path,
                        node.lineno,
                        "'import builtins/importlib' is not allowed anywhere in src/ "
                        "(it exposes dynamic import hooks)",
                    )
                )

        elif isinstance(node, JoinedStr):
            text = _literal_text(node, resolver.for_node(node))
            if _looks_like_sql(text):
                violations.append(
                    Violation(rel_path, node.lineno, "f-string builds a SQL-shaped string")
                )

        elif isinstance(node, BinOp) and isinstance(node.op, AstMod):
            text = _literal_text(node.left, resolver.for_node(node))
            if _looks_like_sql(text):
                violations.append(
                    Violation(rel_path, node.lineno, "%-formatting builds a SQL-shaped string")
                )

        elif isinstance(node, BinOp) and isinstance(node.op, AstAdd):
            parent = parents.get(id(node))
            if isinstance(parent, BinOp) and isinstance(parent.op, AstAdd):
                continue  # handled by the outermost '+' node in this chain
            if id(node) not in flagged_add_ids:
                text = _literal_text(node, resolver.for_node(node))
                if _looks_like_sql(text):
                    violations.append(
                        Violation(
                            rel_path, node.lineno, "'+' concatenation builds a SQL-shaped string"
                        )
                    )
                    flagged_add_ids.add(id(node))

    return violations


def scan_tree(src_root: Path) -> ScanResult:
    """Scan every `.py` file under `src_root`, returning violations and files scanned."""
    if not src_root.is_dir():
        return ScanResult(violations=[], scanned_files=[])
    scanned: list[str] = []
    violations: list[Violation] = []
    for path in sorted(src_root.rglob("*.py")):
        rel_path = path.relative_to(src_root).as_posix()
        scanned.append(rel_path)
        source = path.read_text(encoding="utf-8")
        violations.extend(find_violations_in_source(source, rel_path))
    return ScanResult(violations=violations, scanned_files=scanned)


__all__ = [
    "ALLOWED_MIGRATE_FILE",
    "ALLOWED_REPOSITORY_PREFIX",
    "MIGRATION_EXEC_ALLOWLIST_MARKER",
    "QUERY_SINK_NAMES",
    "SQL_METHOD_NAMES",
    "TABLE_SINK_NAMES",
    "ScanResult",
    "Violation",
    "find_violations_in_source",
    "scan_tree",
]
