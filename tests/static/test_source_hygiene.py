"""Static hygiene guard: no raw invisible/control/bidi/filler bytes anywhere
in the project's tracked source, SQL, shell, config, and doc files.

Reads every scanned file as UTF-8 bytes and fails on any RAW occurrence (an
actual codepoint sitting in the file's bytes -- not a `\\uXXXX` *escape
sequence*, which is plain ASCII text) of:
  - a C0 control character other than '\\n', '\\r', '\\t'
  - DEL (U+007F), and any C1 control character (U+0080-U+009F)
  - every character in Unicode general categories Cf (format), Co (private
    use), Cs (surrogate), Zl (line separator), Zp (paragraph separator),
    Mn (nonspacing mark), and Me (enclosing mark)
  - every Zs (space separator) character *except* the ordinary ASCII space
    U+0020 -- this bans NBSP (U+00A0), the ideographic space (U+3000), etc.
  - a noncharacter: U+FDD0-U+FDEF, or any codepoint ending in FFFE/FFFF in
    any plane
  - Default_Ignorable codepoints not already covered by the categories
    above: the Hangul/halfwidth fillers U+115F, U+1160, U+3164, U+FFA0; the
    Mongolian/Khmer variation-selector ranges U+180B-U+180F and
    U+17B4-U+17B5; the emoji variation selectors U+FE00-U+FE0F; and the tag
    characters U+E0000-U+E0FFF
  - the braille "blank" pattern U+2800

Ordinary *visible* non-ASCII text is fine and deliberately never flagged:
accented Latin, CJK, RTL letters, em-dashes, arrows, checkmarks, and emoji
base symbols (category So, other than the braille blank above) all pass.

This is a stricter, broader relative of the character policy enforced at
*runtime* on user-supplied text in `catan_bot.domain.validation.clean_text`
-- that function legitimately allows a couple of these (ZWJ/ZWNJ) in
*user* input. This guard is about what's allowed in our *own* tracked
files, which have no legitimate use for any of it.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Scan scope
# ---------------------------------------------------------------------------

_SCAN_DIRS = ("src", "tests", "db")

# Root-level files with no scannable extension (or an atypical one) that we
# still want covered.
_ROOT_FILES = (
    "pyproject.toml",
    "Dockerfile",
    "docker-compose.yml",
    ".env.example",
    ".dockerignore",
    ".gitignore",
    "README.md",
)

_SCAN_EXTENSIONS = frozenset({".py", ".sql", ".sh", ".toml", ".yml", ".yaml", ".md", ".txt"})

_SKIP_DIR_NAMES = frozenset({"__pycache__", ".venv", ".git"})

# Private, git-excluded planning docs -- never scanned, wherever they live.
_EXCLUDED_FILENAMES = frozenset({"CLAUDE.md", "DESIGN.md"})


def _is_skipped_dir_part(part: str) -> bool:
    return part in _SKIP_DIR_NAMES or part.endswith(".egg-info")


def iter_scanned_files() -> list[Path]:
    """Every file under the project's scan scope, per the module docstring."""
    files: set[Path] = set()
    for dirname in _SCAN_DIRS:
        root = REPO_ROOT / dirname
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(_is_skipped_dir_part(part) for part in path.relative_to(root).parts[:-1]):
                continue
            if path.name in _EXCLUDED_FILENAMES:
                continue
            if path.suffix in _SCAN_EXTENSIONS:
                files.add(path)
    for filename in _ROOT_FILES:
        path = REPO_ROOT / filename
        if path.is_file() and path.name not in _EXCLUDED_FILENAMES:
            files.add(path)
    return sorted(files)


# ---------------------------------------------------------------------------
# Character policy
# ---------------------------------------------------------------------------

_ALLOWED_C0 = frozenset({0x09, 0x0A, 0x0D})  # \t \n \r
_ALLOWED_ZS = 0x20  # ordinary ASCII space

_BANNED_CATEGORIES = frozenset({"Cf", "Co", "Cs", "Zl", "Zp", "Mn", "Me"})

# Default_Ignorable codepoints not already covered by _BANNED_CATEGORIES.
_EXTRA_IGNORABLE_SINGLES = frozenset({0x115F, 0x1160, 0x3164, 0xFFA0})
_EXTRA_IGNORABLE_RANGES = (
    (0x180B, 0x180F),
    (0x17B4, 0x17B5),
    (0xFE00, 0xFE0F),
    (0xE0000, 0xE0FFF),
)

_BRAILLE_BLANK = 0x2800


def _is_noncharacter(code: int) -> bool:
    # U+FDD0-U+FDEF, plus U+xFFFE/U+xFFFF in every plane.
    return (0xFDD0 <= code <= 0xFDEF) or (code & 0xFFFE) == 0xFFFE


def _is_extra_default_ignorable(code: int) -> bool:
    if code in _EXTRA_IGNORABLE_SINGLES:
        return True
    return any(lo <= code <= hi for lo, hi in _EXTRA_IGNORABLE_RANGES)


def _is_banned_codepoint(code: int) -> bool:
    if code in _ALLOWED_C0:
        return False
    if code <= 0x1F or code == 0x7F:  # other C0 controls, DEL
        return True
    if 0x80 <= code <= 0x9F:  # C1 controls
        return True
    if _is_noncharacter(code):
        return True
    if _is_extra_default_ignorable(code):
        return True
    if code == _BRAILLE_BLANK:
        return True
    category = unicodedata.category(chr(code))
    if category in _BANNED_CATEGORIES:
        return True
    return category == "Zs" and code != _ALLOWED_ZS


@dataclass(frozen=True)
class HygieneViolation:
    path: str
    line: int
    col: int
    codepoint: str
    name: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}:{self.col} {self.codepoint} {self.name}"


def find_hygiene_violations(text: str, rel_path: str) -> list[HygieneViolation]:
    """Scan `text` (one file's full content) for banned raw codepoints.

    Deliberately walks character-by-character with manual line/column
    tracking, rather than `str.splitlines()` or `text.split("\\n")`
    combined with an index lookup -- using either of those here would risk
    the exact bug this guard exists to catch (U+2028/U+2029 silently
    acting as line breaks).
    """
    violations: list[HygieneViolation] = []
    line = 1
    col = 0
    for ch in text:
        col += 1
        code = ord(ch)
        if _is_banned_codepoint(code):
            name = unicodedata.name(ch, "UNNAMED")
            violations.append(HygieneViolation(rel_path, line, col, f"U+{code:04X}", name))
        if ch == "\n":
            line += 1
            col = 0
    return violations


def scan_file(path: Path) -> list[HygieneViolation]:
    """Read and scan one file, failing closed on invalid UTF-8.

    Uses `encoding="utf-8"` (never `"utf-8-sig"`) so a leading BOM survives
    decoding as a literal U+FEFF character instead of being silently
    stripped -- it must be flagged like any other raw Cf character.
    """
    try:
        rel_path = str(path.relative_to(REPO_ROOT))
    except ValueError:
        rel_path = str(path)  # outside REPO_ROOT (e.g. a test fixture in tmp_path)
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [
            HygieneViolation(
                rel_path, 1, exc.start + 1, "INVALID-UTF-8", f"undecodable byte(s): {exc.reason}"
            )
        ]
    return find_hygiene_violations(text, rel_path)


# ---------------------------------------------------------------------------
# The real tree: must be scanned (non-vacuously) and clean.
# ---------------------------------------------------------------------------

_MIN_SCANNED_FILES = 44  # the current count under the scan scope above


def test_project_tree_is_scanned_and_clean() -> None:
    files = iter_scanned_files()
    assert len(files) >= _MIN_SCANNED_FILES, len(files)  # non-vacuous

    rel_paths = {str(p.relative_to(REPO_ROOT)) for p in files}
    for required in (
        "src/catan_bot/db/migrations/0001_init.sql",
        "db/roles.sql",
        "docker-compose.yml",
        "README.md",
    ):
        assert required in rel_paths, f"expected {required} to be included in the scan"

    violations: list[HygieneViolation] = []
    for path in files:
        violations.extend(scan_file(path))
    assert violations == [], "\n" + "\n".join(str(v) for v in violations)


def test_excluded_planning_docs_are_never_scanned() -> None:
    files = iter_scanned_files()
    names = {p.name for p in files}
    assert "CLAUDE.md" not in names
    assert "DESIGN.md" not in names


def test_scan_tree_of_a_missing_directory_returns_no_files() -> None:
    assert list(Path("/nonexistent/root").rglob("*")) == []


# ---------------------------------------------------------------------------
# Self-tests: prove the checker itself catches (and doesn't over-catch).
#
# Every fixture string below is built with `chr()`, never a raw character
# or an escape sequence typed directly into this file's own source.
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_bytes(content.encode("utf-8"))
    return path


def test_hygiene_checker_flags_raw_line_separator_in_sql_file(tmp_path: Path) -> None:
    content = "SELECT 1;" + chr(0x2028) + "SELECT 2;\n"
    path = _write(tmp_path, "bad.sql", content)
    violations = scan_file(path)
    assert len(violations) == 1
    assert violations[0].codepoint == "U+2028"
    assert violations[0].line == 1


def test_hygiene_checker_flags_hangul_filler_in_python_string(tmp_path: Path) -> None:
    content = 'x = "safe' + chr(0x3164) + 'text"\n'
    path = _write(tmp_path, "bad.py", content)
    violations = scan_file(path)
    assert any(v.codepoint == "U+3164" for v in violations)


def test_hygiene_checker_flags_private_use_character(tmp_path: Path) -> None:
    content = "x = 1  # " + chr(0xE000) + "\n"
    path = _write(tmp_path, "bad.py", content)
    violations = scan_file(path)
    assert any(v.codepoint == "U+E000" for v in violations)


def test_hygiene_checker_flags_noncharacter(tmp_path: Path) -> None:
    content = "x = 1  # " + chr(0xFDD0) + "\n"
    path = _write(tmp_path, "bad.py", content)
    violations = scan_file(path)
    assert any(v.codepoint == "U+FDD0" for v in violations)


def test_hygiene_checker_flags_soft_hyphen(tmp_path: Path) -> None:
    content = "word" + chr(0x00AD) + "break\n"
    path = _write(tmp_path, "bad.txt", content)
    violations = scan_file(path)
    assert any(v.codepoint == "U+00AD" for v in violations)


def test_hygiene_checker_flags_invisible_separator(tmp_path: Path) -> None:
    content = "1" + chr(0x2063) + "000\n"
    path = _write(tmp_path, "bad.md", content)
    violations = scan_file(path)
    assert any(v.codepoint == "U+2063" for v in violations)


def test_hygiene_checker_flags_variation_selector_16(tmp_path: Path) -> None:
    content = "status" + chr(0xFE0F) + "\n"
    path = _write(tmp_path, "bad.md", content)
    violations = scan_file(path)
    assert any(v.codepoint == "U+FE0F" for v in violations)


def test_hygiene_checker_flags_combining_grapheme_joiner(tmp_path: Path) -> None:
    content = "a" + chr(0x034F) + "b\n"
    path = _write(tmp_path, "bad.py", content)
    violations = scan_file(path)
    assert any(v.codepoint == "U+034F" for v in violations)


def test_hygiene_checker_flags_tag_character(tmp_path: Path) -> None:
    content = "flag" + chr(0xE0041) + "\n"
    path = _write(tmp_path, "bad.py", content)
    violations = scan_file(path)
    assert any(v.codepoint == "U+E0041" for v in violations)


def test_hygiene_checker_flags_no_break_space(tmp_path: Path) -> None:
    content = "a" + chr(0x00A0) + "b\n"
    path = _write(tmp_path, "bad.yml", content)
    violations = scan_file(path)
    assert any(v.codepoint == "U+00A0" for v in violations)


def test_hygiene_checker_flags_combining_acute_accent(tmp_path: Path) -> None:
    content = "e" + chr(0x0301) + "\n"
    path = _write(tmp_path, "bad.py", content)
    violations = scan_file(path)
    assert any(v.codepoint == "U+0301" for v in violations)


def test_hygiene_checker_flags_bom_at_file_start(tmp_path: Path) -> None:
    content = chr(0xFEFF) + "x = 1\n"
    path = _write(tmp_path, "bad.py", content)
    violations = scan_file(path)
    assert any(v.codepoint == "U+FEFF" for v in violations)
    assert violations[0].line == 1
    assert violations[0].col == 1


def test_hygiene_checker_flags_invalid_utf8_bytes(tmp_path: Path) -> None:
    path = tmp_path / "bad.py"
    path.write_bytes(b"x = 1  # \xff\xfe not valid utf-8\n")
    violations = scan_file(path)
    assert len(violations) == 1
    assert violations[0].codepoint == "INVALID-UTF-8"


def test_hygiene_checker_flags_braille_blank(tmp_path: Path) -> None:
    content = chr(0x2800) + "\n"
    path = _write(tmp_path, "bad.py", content)
    violations = scan_file(path)
    assert any(v.codepoint == "U+2800" for v in violations)


def test_hygiene_checker_allows_tab_newline_and_carriage_return() -> None:
    text = "def f():\n\tpass\r\n"
    assert find_hygiene_violations(text, "ok.py") == []


def test_hygiene_checker_allows_ordinary_unicode_and_emoji_base() -> None:
    # Em-dash, right arrow, an accented Latin letter, and an emoji base
    # symbol (category So, distinct from the banned braille blank above)
    # are all fine -- only the specific banned categories/codepoints above
    # are rejected.
    em_dash = chr(0x2014)
    arrow = chr(0x2192)
    e_acute = chr(0x00E9)
    emoji_base = chr(0x1F3B2)  # GAME DIE
    text = f"caf{e_acute} {em_dash} left {arrow} right {emoji_base}\n"
    assert find_hygiene_violations(text, "ok.py") == []


def test_hygiene_checker_allows_ordinary_space() -> None:
    text = "x = 1 + 2\n"
    assert find_hygiene_violations(text, "ok.py") == []


def test_hygiene_checker_does_not_flag_escape_sequence_text() -> None:
    # The literal 6 ASCII characters '\\', 'u', '2', '0', '2', '8' (a Python
    # escape *sequence* in source text) are not the raw codepoint itself.
    text = 'x = "' + chr(0x5C) + 'u2028"\n'
    assert find_hygiene_violations(text, "ok.py") == []


def test_hygiene_checker_reports_correct_line_and_column(tmp_path: Path) -> None:
    content = "line one\nsecond" + chr(0xFEFF) + "line\n"
    path = _write(tmp_path, "bad.py", content)
    violations = scan_file(path)
    assert len(violations) == 1
    assert violations[0].line == 2
    assert violations[0].col == 7  # "second" is 6 chars, the BOM is the 7th


def test_hygiene_violation_str_format_is_path_line_col_codepoint_name() -> None:
    content = chr(0x3164)
    violations = find_hygiene_violations(content, "some/file.py")
    assert str(violations[0]) == "some/file.py:1:1 U+3164 HANGUL FILLER"
