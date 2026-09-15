"""Static checks for the bootstrap role credential guards."""

from __future__ import annotations

from pathlib import Path

_ROLES_SQL = Path(__file__).parents[2] / "db" / "roles.sql"


def test_role_password_guards_require_url_safe_values_and_minimum_length() -> None:
    sql = _ROLES_SQL.read_text(encoding="utf-8")

    assert (
        "length(:'migrator_password') < 16\n"
        "    OR :'migrator_password' ~ '[^A-Za-z0-9_-]' AS migrator_password_invalid"
    ) in sql
    assert (
        "length(:'app_password') < 16\n"
        "    OR :'app_password' ~ '[^A-Za-z0-9_-]' AS app_password_invalid"
    ) in sql
