"""Static guards for scripts/deploy.sh and its tie to the CI workflow.

The deploy script gates on GitHub check runs by name, and it must never be able
to recreate the database container or touch volumes. These tests keep both
properties from drifting without anyone noticing.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "deploy.sh"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _required_checks() -> list[str]:
    match = re.search(r"^REQUIRED_CHECKS=\(\n(.*?)^\)", _script_text(), re.S | re.M)
    assert match, "REQUIRED_CHECKS array not found in scripts/deploy.sh"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_required_checks_match_ci_job_names() -> None:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    job_names = {job["name"] for job in workflow["jobs"].values()}
    required = _required_checks()
    assert len(required) == len(set(required)), "duplicate entries in REQUIRED_CHECKS"
    assert set(required) == job_names, (
        "scripts/deploy.sh REQUIRED_CHECKS must equal the job names in .github/workflows/ci.yml"
    )


def test_every_force_recreate_is_scoped_to_one_service() -> None:
    lines = [line for line in _script_text().splitlines() if "--force-recreate" in line]
    assert lines, "expected the scoped migrate/bot force-recreate commands"
    scoped = re.compile(r"--no-deps\s+--force-recreate\s+(migrate|bot)\b")
    unscoped = [line.strip() for line in lines if not scoped.search(line)]
    assert unscoped == [], f"unscoped --force-recreate: {unscoped}"


def test_database_is_only_started_without_recreate() -> None:
    lines = [
        line
        for line in _script_text().splitlines()
        if re.search(r"\bup\b.*\bdb\b", line) and not line.lstrip().startswith("#")
    ]
    assert lines, "expected the `up -d --no-recreate db` command"
    assert all("--no-recreate" in line for line in lines), lines


def test_script_never_removes_volumes_or_tears_the_stack_down() -> None:
    text = _script_text()
    forbidden = (
        r"down\s+-v",
        r"down\s+--volumes",
        r"compose\s+down",
        r"volume\s+rm",
        r"volume\s+prune",
        r"system\s+prune",
    )
    for pattern in forbidden:
        assert not re.search(pattern, text), f"deploy.sh must not contain {pattern!r}"


def test_script_has_lf_line_endings() -> None:
    assert b"\r" not in SCRIPT.read_bytes()
