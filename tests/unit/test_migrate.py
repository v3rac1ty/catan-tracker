"""Unit tests for the migration runner's filename/entry validation.

No Docker/DB required: `_discover_migrations` accepts a plain directory
(`tmp_path`). Whether a migration file illegally manipulates the surrounding
transaction (COMMIT/ROLLBACK/END/ABORT/...) is checked at runtime against
real Postgres state instead of by pattern-matching SQL text, so that guard
is exercised by the integration suite (see
`tests/integration/test_migrate_integration.py`), not here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from catan_bot.db.migrate import _FILENAME_PATTERN, _discover_migrations


def test_filename_pattern_rejects_unicode_digits() -> None:
    # Arabic-Indic digits satisfy the bare `\d` shorthand without re.ASCII,
    # but must never satisfy our filename rule.
    assert _FILENAME_PATTERN.fullmatch("٠١٢٣_init.sql") is None
    assert _FILENAME_PATTERN.fullmatch("0001_init.sql") is not None


def test_discover_migrations_finds_and_sorts_valid_files(tmp_path: Path) -> None:
    (tmp_path / "0002_second.sql").write_text("SELECT 2;\n")
    (tmp_path / "0001_first.sql").write_text("SELECT 1;\n")
    (tmp_path / "__init__.py").write_text("")
    (tmp_path / "__pycache__").mkdir()

    found = _discover_migrations(tmp_path)

    assert [name for name, _ in found] == ["0001_first.sql", "0002_second.sql"]


def test_discover_migrations_rejects_uppercase_extension(tmp_path: Path) -> None:
    (tmp_path / "0001_init.sql").write_text("SELECT 1;\n")
    (tmp_path / "0002_bad.SQL").write_text("SELECT 2;\n")

    with pytest.raises(ValueError, match="Unexpected entry"):
        _discover_migrations(tmp_path)


def test_discover_migrations_rejects_double_extension(tmp_path: Path) -> None:
    (tmp_path / "0001_init.sql.bak").write_text("SELECT 1;\n")

    with pytest.raises(ValueError, match="Unexpected entry"):
        _discover_migrations(tmp_path)


def test_discover_migrations_rejects_unrelated_file(tmp_path: Path) -> None:
    (tmp_path / "0001_init.sql").write_text("SELECT 1;\n")
    (tmp_path / "notes.txt").write_text("todo\n")

    with pytest.raises(ValueError, match="Unexpected entry"):
        _discover_migrations(tmp_path)


def test_discover_migrations_rejects_stray_directory(tmp_path: Path) -> None:
    (tmp_path / "0001_init.sql").write_text("SELECT 1;\n")
    (tmp_path / "extra_dir").mkdir()

    with pytest.raises(ValueError, match="Unexpected entry"):
        _discover_migrations(tmp_path)


def test_discover_migrations_skips_pycache_directory(tmp_path: Path) -> None:
    (tmp_path / "0001_init.sql").write_text("SELECT 1;\n")
    (tmp_path / "__pycache__").mkdir()

    found = _discover_migrations(tmp_path)

    assert [name for name, _ in found] == ["0001_init.sql"]


def test_discover_migrations_rejects_pycache_as_a_file(tmp_path: Path) -> None:
    # __pycache__ is only ever skipped when it's a directory.
    (tmp_path / "0001_init.sql").write_text("SELECT 1;\n")
    (tmp_path / "__pycache__").write_text("not actually a bytecode cache dir\n")

    with pytest.raises(ValueError, match="Unexpected entry"):
        _discover_migrations(tmp_path)


def test_discover_migrations_skips_init_py_file(tmp_path: Path) -> None:
    (tmp_path / "0001_init.sql").write_text("SELECT 1;\n")
    (tmp_path / "__init__.py").write_text("")

    found = _discover_migrations(tmp_path)

    assert [name for name, _ in found] == ["0001_init.sql"]


def test_discover_migrations_rejects_init_py_as_a_directory(tmp_path: Path) -> None:
    # __init__.py is only ever skipped when it's a file.
    (tmp_path / "0001_init.sql").write_text("SELECT 1;\n")
    (tmp_path / "__init__.py").mkdir()

    with pytest.raises(ValueError, match="Unexpected entry"):
        _discover_migrations(tmp_path)


def test_discover_migrations_ignores_ds_store(tmp_path: Path) -> None:
    (tmp_path / "0001_init.sql").write_text("SELECT 1;\n")
    (tmp_path / ".DS_Store").write_bytes(b"\x00\x01binary junk")

    found = _discover_migrations(tmp_path)

    assert [name for name, _ in found] == ["0001_init.sql"]


def test_discover_migrations_rejects_symlinked_migration(tmp_path: Path) -> None:
    real = tmp_path / "0001_init.sql"
    real.write_text("SELECT 1;\n")
    link = tmp_path / "0002_linked.sql"
    link.symlink_to(real)

    with pytest.raises(ValueError, match="symlink"):
        _discover_migrations(tmp_path)


def test_discover_migrations_rejects_symlinked_directory(tmp_path: Path) -> None:
    real_dir = tmp_path / "__pycache__"
    real_dir.mkdir()
    (tmp_path / "0001_init.sql").write_text("SELECT 1;\n")
    link = tmp_path / "linked_dir"
    link.symlink_to(real_dir, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        _discover_migrations(tmp_path)
