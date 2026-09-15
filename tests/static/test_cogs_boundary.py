"""Static boundary guard for Discord-facing modules.

Commands and views are deliberately thin: they turn an interaction into an
``Actor``, call a service, and render the result.  This test keeps database
execution behind the service/repository layers by rejecting direct asyncpg,
repository, and migration imports, as well as the dynamic import and module
mutation tricks that could hide one.
"""

from __future__ import annotations

from ast import Attribute, Call, Constant, Import, ImportFrom, Name, Store, Subscript, parse, walk
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "catan_bot"
COGS_ROOT = SRC_ROOT / "cogs"
VIEWS_ROOT = SRC_ROOT / "views"
_EXPECTED_FILES = frozenset(
    {
        "cogs/__init__.py",
        "cogs/config_cog.py",
        "cogs/event_cog.py",
        "cogs/game_cog.py",
        "cogs/help_cog.py",
        "cogs/season_cog.py",
        "cogs/stats_cog.py",
        "views/__init__.py",
        "views/game_confirm.py",
        "views/event_rsvp.py",
        "errors.py",
        "formatting.py",
        "permissions.py",
        "scheduler.py",
    }
)

_FORBIDDEN_EXACT_IMPORTS = frozenset(
    {
        "asyncpg",
        "builtins",
        "importlib",
        "sys",
        "catan_bot.db",
        "catan_bot.db.migrate",
    }
)
_FORBIDDEN_PREFIX_IMPORTS = ("asyncpg.", "catan_bot.db.repositories.")
_FORBIDDEN_IMPORT_PREFIX = "catan_bot.db.repositories"


def _package_for_path(rel_path: str) -> str:
    parts = Path(rel_path).parts
    if parts[0] in {"cogs", "views"}:
        return f"catan_bot.{parts[0]}"
    return "catan_bot"


def _resolve_relative_base(package: str, level: int, module: str | None) -> str:
    if level == 0:
        return module or ""
    bits = package.rsplit(".", level - 1)
    base = bits[0]
    return f"{base}.{module}" if module else base


def _is_forbidden_import(path: str) -> bool:
    return (
        path in _FORBIDDEN_EXACT_IMPORTS
        or path.startswith(_FORBIDDEN_PREFIX_IMPORTS)
        or path == _FORBIDDEN_IMPORT_PREFIX
        or path.startswith(_FORBIDDEN_IMPORT_PREFIX + ".")
    )


def _import_violations(node: Import | ImportFrom, rel_path: str, package: str) -> list[str]:
    violations: list[str] = []
    if isinstance(node, Import):
        for alias in node.names:
            if _is_forbidden_import(alias.name):
                violations.append(f"{rel_path}:{node.lineno}: forbidden import {alias.name!r}")
        return violations

    base = _resolve_relative_base(package, node.level, node.module)
    # ``from catan_bot.db import models`` is the one intentionally allowed
    # package-level form; the imported child is checked below.  Importing
    # ``catan_bot.db`` as a whole would make repository access easy to hide.
    if base and base != "catan_bot.db" and _is_forbidden_import(base):
        violations.append(f"{rel_path}:{node.lineno}: forbidden import from {base!r}")
        return violations

    # ``from catan_bot.db import repositories`` has an innocuous base but a
    # forbidden imported child.  db.models remains an intentional allowlist
    # entry for frozen display dataclasses.
    for alias in node.names:
        if alias.name == "*":
            violations.append(f"{rel_path}:{node.lineno}: star imports are forbidden")
            continue
        candidate = f"{base}.{alias.name}" if base else alias.name
        if _is_forbidden_import(candidate):
            violations.append(f"{rel_path}:{node.lineno}: forbidden import {candidate!r}")
    return violations


def _module_aliases(tree: object) -> set[str]:
    aliases: set[str] = set()
    for node in walk(tree):
        if isinstance(node, Import):
            for alias in node.names:
                aliases.add(alias.asname or alias.name.split(".")[0])
    return aliases


def _sys_modules_access(node: Attribute) -> bool:
    return node.attr == "modules" and isinstance(node.value, Name) and node.value.id == "sys"


def _is_sys_modules_subscript(node: object) -> bool:
    return (
        isinstance(node, Subscript)
        and isinstance(node.value, Attribute)
        and _sys_modules_access(node.value)
    )


def _getattr_static_call(func: object) -> bool:
    if isinstance(func, Name):
        return func.id == "getattr_static"
    return isinstance(func, Attribute) and func.attr == "getattr_static"


def find_boundary_violations(source: str, rel_path: str) -> list[str]:
    tree = parse(source, filename=rel_path)
    module_aliases = _module_aliases(tree)
    violations: list[str] = []

    for node in walk(tree):
        if isinstance(node, Import | ImportFrom):
            violations.extend(_import_violations(node, rel_path, _package_for_path(rel_path)))
            continue

        if isinstance(node, Call):
            func = node.func
            if isinstance(func, Name) and func.id in {
                "__import__",
                "globals",
                "locals",
                "vars",
                "open",
                "exec",
                "eval",
                "compile",
            }:
                violations.append(f"{rel_path}:{node.lineno}: forbidden builtin {func.id}()")
            if (
                isinstance(func, Name)
                and func.id == "getattr"
                and (
                    len(node.args) < 2
                    or not (
                        isinstance(node.args[1], Constant) and isinstance(node.args[1].value, str)
                    )
                )
            ):
                violations.append(f"{rel_path}:{node.lineno}: dynamic getattr() is forbidden")
            if _getattr_static_call(func):
                violations.append(f"{rel_path}:{node.lineno}: getattr_static() is forbidden")
            if isinstance(func, Attribute) and func.attr == "import_module":
                violations.append(f"{rel_path}:{node.lineno}: dynamic import_module() is forbidden")
            if isinstance(func, Name) and func.id == "setattr" and node.args:
                target = node.args[0]
                if (
                    isinstance(target, Name)
                    and target.id in module_aliases
                    or _is_sys_modules_subscript(target)
                ):
                    violations.append(
                        f"{rel_path}:{node.lineno}: setattr() on a module is forbidden"
                    )

        elif isinstance(node, Attribute):
            if node.attr in {"__import__", "__getattribute__", "__dict__"}:
                violations.append(
                    f"{rel_path}:{node.lineno}: dynamic attribute .{node.attr} is forbidden"
                )
            if _sys_modules_access(node):
                violations.append(f"{rel_path}:{node.lineno}: sys.modules access is forbidden")

        elif isinstance(node, Name) and node.id in {"__builtins__", "__import__"}:
            violations.append(f"{rel_path}:{node.lineno}: dynamic builtin access is forbidden")

    # Attribute stores such as ``sys.modules['x'].fetch = fn`` have the
    # subscript as the attribute's receiver, so they are not Call nodes.
    for node in walk(tree):
        if (
            isinstance(node, Attribute)
            and isinstance(node.ctx, Store)
            and _is_sys_modules_subscript(node.value)
        ):
            violations.append(
                f"{rel_path}:{node.lineno}: assignment on sys.modules[...] is forbidden"
            )

    return violations


def _boundary_files() -> list[Path]:
    files = list(COGS_ROOT.rglob("*.py")) + list(VIEWS_ROOT.rglob("*.py"))
    files.extend(
        SRC_ROOT / name for name in ("errors.py", "formatting.py", "permissions.py", "scheduler.py")
    )
    return sorted(files)


def test_boundary_tree_is_scanned_and_clean() -> None:
    assert COGS_ROOT.is_dir()
    assert VIEWS_ROOT.is_dir()
    paths = _boundary_files()
    scanned = {path.relative_to(SRC_ROOT).as_posix() for path in paths}
    assert scanned >= _EXPECTED_FILES, scanned

    violations = [
        violation
        for path in paths
        for violation in find_boundary_violations(
            path.read_text(encoding="utf-8"), path.relative_to(SRC_ROOT).as_posix()
        )
    ]
    assert violations == [], "\n" + "\n".join(violations)


@pytest.mark.parametrize(
    "source",
    [
        "import asyncpg\n",
        "from catan_bot.db.repositories import games\n",
        "from catan_bot.db import migrate\n",
        "import importlib\nimportlib.import_module('asyncpg')\n",
        "from importlib import import_module as im\nim('asyncpg')\n",
        "from builtins import __import__ as imp\nimp('asyncpg')\n",
        "import sys as s\ns.modules['x'].fetch = fn\n",
        "from sys import modules as registry\nregistry['x'].fetch = fn\n",
        "def f(obj, field):\n    return getattr(obj, field)\n",
        "def f(obj):\n    return obj.__dict__['execute']\n",
    ],
)
def test_indirect_database_escape_is_flagged(source: str) -> None:
    assert find_boundary_violations(source, "cogs/planted.py")


def test_models_dataclasses_and_normal_discord_imports_are_allowed() -> None:
    source = (
        "import discord\n"
        "from catan_bot.db.models import GuildConfig\n\n"
        "def render(config: GuildConfig) -> discord.Embed:\n"
        "    return discord.Embed(title=getattr(config, 'timezone', 'UTC'))\n"
    )
    assert find_boundary_violations(source, "formatting.py") == []
