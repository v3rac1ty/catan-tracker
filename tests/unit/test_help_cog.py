"""Help inventory and operator-documentation checks."""

from pathlib import Path

from catan_bot.cogs.help_cog import _COMMANDS


def test_help_includes_confirmed_game_update_and_current_source_of_truth() -> None:
    descriptions = dict(_COMMANDS)

    assert "/game update" in descriptions
    assert "confirmed" in descriptions["/game update"]
    assert "clear_time" in descriptions["/game update"]
    assert "clear_scenario" in descriptions["/game update"]
    assert "revision" in descriptions["/game update"]
    assert "/game show" in descriptions
    assert "current source of truth" in descriptions["/game show"]


def test_operator_docs_cover_update_migration_and_command_sync() -> None:
    root = Path(__file__).parents[2]
    readme = (root / "README.md").read_text(encoding="utf-8")
    deployment = (root / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")

    assert "/game update" in readme
    assert "NULL" in readme
    assert "current source of truth" in readme
    assert "0004_game_updates.sql" in deployment
    assert "command sync" in deployment
