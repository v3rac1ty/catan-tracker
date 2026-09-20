"""Static parameter-type guard: self-tests + the real-tree scan.

See `tests/static/param_type_guard.py`'s module docstring for the rule
this enforces and its deliberate scope limits. This is the same
self-testing shape as `tests/static/test_no_dynamic_sql.py`: a real-tree
scan that must be non-vacuous and clean, plus a catalogue of planted-bad
and known-good samples run straight through the checker function.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from param_type_guard import (
    REPOSITORIES_ROOT,
    find_param_type_violations,
    find_violations_in_source,
    scan_tree,
)

# ---------------------------------------------------------------------------
# The real tree: must be scanned (non-vacuously) and clean.
# ---------------------------------------------------------------------------


def test_real_repositories_tree_is_scanned_and_clean() -> None:
    assert REPOSITORIES_ROOT.is_dir()
    result = scan_tree(REPOSITORIES_ROOT)
    # Non-vacuous on two axes: every repository module was actually looked
    # at, and there were actually statements inside them to check -- a
    # regression that silently made `_sql_constants` match nothing would
    # still "pass" the violations-list assertion below for the wrong
    # reason without this.
    assert len(result.scanned_files) >= 6, result.scanned_files
    assert "catan_bot/db/repositories/score_requests.py" in result.scanned_files
    assert "catan_bot/db/repositories/guilds.py" in result.scanned_files
    assert result.scanned_constants >= 60, result.scanned_constants
    assert result.violations == [], "\n" + "\n".join(str(v) for v in result.violations)


def test_scan_tree_of_a_missing_directory_scans_nothing() -> None:
    result = scan_tree(Path("/nonexistent/repositories"))
    assert result.scanned_files == []
    assert result.violations == []


def test_scan_tree_catches_a_planted_violation(tmp_path: Path) -> None:
    copy = tmp_path / "repositories"
    shutil.copytree(REPOSITORIES_ROOT, copy, ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "planted.py").write_text(
        '_PLANTED_SQL = """\n'
        "UPDATE t SET expires_at = $1 + INTERVAL '1 hour' WHERE id = $2\n"
        '"""\n'
    )
    result = scan_tree(copy)
    assert result.violations != []
    assert any("planted.py" in v.path for v in result.violations)


# ---------------------------------------------------------------------------
# The exact CI-breaking shape, reconstructed verbatim (pre-fix) as planted
# single-file samples -- these must be caught by a full-module scan, not
# just by the lower-level `find_param_type_violations` unit below.
# ---------------------------------------------------------------------------

_PRE_FIX_INSERT_SCORE_REQUESTS = (
    '_INSERT_SCORE_REQUESTS_SQL = """\n'
    "INSERT INTO game_score_requests (game_id, guild_id, user_id, requested_at, next_prompt_at)\n"
    "SELECT $1, $2, u, $3, $3 + INTERVAL '24 hours'\n"
    "FROM unnest($4::bigint[]) AS t(u)\n"
    '"""\n'
)

_PRE_FIX_CLAIM_DUE_PROMPTS = (
    '_CLAIM_DUE_PROMPTS_SQL = """\n'
    "WITH due AS (\n"
    "    SELECT game_id, user_id\n"
    "    FROM game_score_requests\n"
    "    WHERE submitted_at IS NULL\n"
    "          AND next_prompt_at IS NOT NULL\n"
    "          AND next_prompt_at <= $1\n"
    "          AND prompts_sent < $3\n"
    "    ORDER BY next_prompt_at\n"
    "    LIMIT $2\n"
    "    FOR UPDATE SKIP LOCKED\n"
    ")\n"
    "UPDATE game_score_requests r\n"
    "SET prompts_sent = r.prompts_sent + 1,\n"
    "    next_prompt_at = $1 + INTERVAL '24 hours'\n"
    "FROM due\n"
    "WHERE r.game_id = due.game_id AND r.user_id = due.user_id\n"
    'RETURNING r.game_id\n"""\n'
)

_CI_BREAKING_SAMPLES = [
    pytest.param(_PRE_FIX_INSERT_SCORE_REQUESTS, id="pre-fix-insert-score-requests"),
    pytest.param(_PRE_FIX_CLAIM_DUE_PROMPTS, id="pre-fix-claim-due-prompts"),
]


@pytest.mark.parametrize("source", _CI_BREAKING_SAMPLES)
def test_pre_fix_ci_breaking_statement_is_flagged(source: str) -> None:
    violations = find_violations_in_source(source, "catan_bot/db/repositories/planted.py")
    assert violations, f"expected a violation for:\n{source}"


def test_post_fix_statements_from_score_requests_are_clean() -> None:
    """The two statements above, with the load-bearing casts this task added."""
    fixed = (
        '_INSERT_SCORE_REQUESTS_SQL = """\n'
        "INSERT INTO game_score_requests "
        "(game_id, guild_id, user_id, requested_at, next_prompt_at)\n"
        "SELECT $1, $2, u, $3::timestamptz, $3::timestamptz + INTERVAL '24 hours'\n"
        "FROM unnest($4::bigint[]) AS t(u)\n"
        '"""\n\n'
        '_CLAIM_DUE_PROMPTS_SQL = """\n'
        "WITH due AS (\n"
        "    SELECT game_id, user_id\n"
        "    FROM game_score_requests\n"
        "    WHERE submitted_at IS NULL\n"
        "          AND next_prompt_at IS NOT NULL\n"
        "          AND next_prompt_at <= $1::timestamptz\n"
        "          AND prompts_sent < $3\n"
        "    ORDER BY next_prompt_at\n"
        "    LIMIT $2\n"
        "    FOR UPDATE SKIP LOCKED\n"
        ")\n"
        "UPDATE game_score_requests r\n"
        "SET prompts_sent = r.prompts_sent + 1,\n"
        "    next_prompt_at = $1::timestamptz + INTERVAL '24 hours'\n"
        "FROM due\n"
        "WHERE r.game_id = due.game_id AND r.user_id = due.user_id\n"
        'RETURNING r.game_id\n"""\n'
    )
    assert find_violations_in_source(fixed, "catan_bot/db/repositories/planted.py") == []


# ---------------------------------------------------------------------------
# Unit-level bad samples against `find_param_type_violations` directly:
# every arithmetic operator, on both sides of the parameter, uncast.
# ---------------------------------------------------------------------------

_BAD_FRAGMENTS = [
    pytest.param("SET expires_at = $1 + INTERVAL '1 day'", id="plus-after-param"),
    pytest.param("SET expires_at = INTERVAL '1 day' + $1", id="plus-before-param"),
    pytest.param("SET expires_at = $1 - INTERVAL '1 day'", id="minus-after-param"),
    pytest.param("WHERE balance = $2 * 2", id="star-after-param"),
    pytest.param("WHERE balance = 2 * $2", id="star-before-param"),
    pytest.param("WHERE rate = $3 / 100", id="slash-after-param"),
    pytest.param("WHERE rate = 100 / $3", id="slash-before-param"),
    pytest.param("SET total = $1 + $2", id="param-plus-param-both-flagged"),
    pytest.param(
        "SET expires_at =    $1    +    INTERVAL '1 day'", id="extra-whitespace-both-sides"
    ),
    pytest.param("SET expires_at = $1\n+ INTERVAL '1 day'", id="operator-on-next-line-after-param"),
]


@pytest.mark.parametrize("fragment", _BAD_FRAGMENTS)
def test_bad_fragment_is_flagged(fragment: str) -> None:
    violations = find_param_type_violations(fragment, name="_X_SQL", path="x.py", base_line=1)
    assert violations, f"expected a violation for:\n{fragment}"


def test_param_plus_param_flags_both_occurrences() -> None:
    violations = find_param_type_violations(
        "SET total = $1 + $2", name="_X_SQL", path="x.py", base_line=1
    )
    assert len(violations) == 2


# ---------------------------------------------------------------------------
# Good samples: casts neutralize arithmetic; every other real shape in the
# repository package stays clean.
# ---------------------------------------------------------------------------

_GOOD_FRAGMENTS = [
    pytest.param("SET expires_at = $1::timestamptz + INTERVAL '1 day'", id="cast-then-plus"),
    pytest.param("SET expires_at = INTERVAL '1 day' + $1::timestamptz", id="plus-then-cast"),
    pytest.param(
        "SET expires_at = $1 :: timestamptz + INTERVAL '1 day'", id="cast-with-spaces-around-colons"
    ),
    pytest.param("WHERE rate = $3::numeric / 100", id="cast-before-slash"),
    pytest.param("FROM unnest($4::bigint[]) AS t(u)", id="array-cast-no-arithmetic"),
    pytest.param("WHERE next_prompt_at <= $1", id="bare-comparison-not-arithmetic"),
    pytest.param("WHERE reported_by <> $3", id="not-equal-operator-not-arithmetic"),
    pytest.param(
        "WHERE leaderboard_last_posted_on IS DISTINCT FROM $2", id="is-distinct-from-not-arithmetic"
    ),
    pytest.param(
        "SET prompts_sent = r.prompts_sent + 1, next_prompt_at = $1::timestamptz",
        id="column-plus-literal-with-unrelated-cast-param",
    ),
    pytest.param(
        "SET revision = revision + 1 WHERE revision = $12", id="column-arithmetic-no-param-involved"
    ),
    pytest.param(
        "SET leaderboard_mode = CASE WHEN $2 THEN $3::text ELSE leaderboard_mode END",
        id="case-when-then-cast-else-column",
    ),
    pytest.param("WHERE status <> 'voided' OR $3", id="boolean-or-not-arithmetic"),
    pytest.param("SELECT COUNT(*) AS games", id="count-star-no-param"),
]


@pytest.mark.parametrize("fragment", _GOOD_FRAGMENTS)
def test_good_fragment_is_not_flagged(fragment: str) -> None:
    assert find_param_type_violations(fragment, name="_X_SQL", path="x.py", base_line=1) == []


def test_repeated_param_only_flags_the_uncast_occurrence() -> None:
    """One occurrence cast, the other one bare and touching '+': only the bare one is flagged."""
    violations = find_param_type_violations(
        "SELECT $3, $3 + INTERVAL '1 day'", name="_X_SQL", path="x.py", base_line=1
    )
    assert len(violations) == 1


# ---------------------------------------------------------------------------
# Sanity: `find_violations_in_source` only ever looks at `_..._SQL`-named
# module-level constants, and reports the right file/line.
# ---------------------------------------------------------------------------


def test_non_sql_named_constant_is_ignored() -> None:
    source = "_NOT_SQL_SOMETHING = \"$1 + INTERVAL '1 day'\"\n"
    assert find_violations_in_source(source, "catan_bot/db/repositories/x.py") == []


def test_local_variable_is_ignored_only_module_level_constants_scanned() -> None:
    source = "def f():\n    _LOCAL_SQL = \"SET x = $1 + INTERVAL '1 day'\"\n    return _LOCAL_SQL\n"
    assert find_violations_in_source(source, "catan_bot/db/repositories/x.py") == []


def test_violation_reports_correct_line_number() -> None:
    source = '_X_SQL = """\nSELECT 1\nWHERE expires_at = $1 + INTERVAL \'1 day\'\n"""\n'
    violations = find_violations_in_source(source, "catan_bot/db/repositories/x.py")
    assert len(violations) == 1
    # Line 1 is `_X_SQL = """`, line 3 is the offending arithmetic.
    assert violations[0].line == 3


def test_violation_str_format_is_path_line_message() -> None:
    source = "_X_SQL = \"SET x = $1 + INTERVAL '1 day'\"\n"
    violations = find_violations_in_source(source, "catan_bot/db/repositories/x.py")
    assert len(violations) == 1
    assert str(violations[0]).startswith("catan_bot/db/repositories/x.py:1: ")
