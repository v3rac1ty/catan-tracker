"""Cross-guild isolation: every guild-scoped function, called with the
*wrong* guild's id against the *right* guild's resource, must act as if the
resource doesn't exist (`None`/`False`/a `not_found`-shaped `TransitionResult`)
and must never mutate the resource's real guild's data.

The four documented exceptions (`seasons.lock_due_seasons`,
`seasons.list_unannounced_completed`, `events.claim_due_reminders`,
`events.complete_past_events`) are system-wide scheduler queries and are
deliberately not guild-scoped -- see DESIGN.md and each function's
docstring -- so they're out of scope here.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta

import asyncpg
import pytest

from catan_bot.db.models import SeasonResultRow
from catan_bot.db.repositories import events, games, guilds, players, seasons

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

STARTS_ON = date(2026, 1, 1)
ENDS_ON = date(2026, 12, 31)
ENDS_AT = datetime(2027, 1, 1, tzinfo=UTC)
PLAYED_ON = date(2026, 2, 1)
NOW = datetime(2026, 6, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# guilds.py
#
# M2: a tautological `WHERE` (e.g. one that always matches regardless of
# `guild_id`) is only detectable with a *second* guild's row actually
# present -- every test below relies on both `guild_id` and
# `other_guild_id`'s `guild_config` rows existing (their fixtures ensure
# them), not just on `other_guild_id` being some arbitrary unused number.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "setter",
    [
        pytest.param(
            lambda conn, gid: guilds.set_timezone(conn, gid, "Europe/Paris"),
            id="set_timezone",
        ),
        pytest.param(
            lambda conn, gid: guilds.set_announce_channel(conn, gid, 123456),
            id="set_announce_channel",
        ),
        pytest.param(
            lambda conn, gid: guilds.set_admin_role(conn, gid, 654321),
            id="set_admin_role",
        ),
        pytest.param(
            lambda conn, gid: guilds.set_default_min_games(conn, gid, 7),
            id="set_default_min_games",
        ),
    ],
)
async def test_guild_config_setter_does_not_mutate_other_guild(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int, setter
) -> None:
    """M2: each setter's `WHERE guild_id = $1` must only ever touch its own
    guild's row -- comparing every column of `other_guild_id`'s row
    (`GuildConfig` is a frozen dataclass, so `==` is field-by-field) proves
    the `WHERE` isn't tautological."""
    before = await guilds.get_guild(app_conn, other_guild_id)
    assert before is not None

    updated = await setter(app_conn, guild_id)
    assert updated is not None
    assert updated.guild_id == guild_id

    after = await guilds.get_guild(app_conn, other_guild_id)
    assert after == before


async def test_get_guild_returns_own_row_not_other_guilds_with_both_present(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    """M2: with both guilds' rows present and both modified, `get_guild(B)`
    must return B's own row, never A's."""
    await guilds.set_timezone(app_conn, guild_id, "Europe/Paris")
    await guilds.set_timezone(app_conn, other_guild_id, "America/Chicago")

    found = await guilds.get_guild(app_conn, other_guild_id)

    assert found is not None
    assert found.guild_id == other_guild_id
    assert found.timezone == "America/Chicago"


async def test_ensure_guild_on_existing_row_returns_own_values_after_other_guild_modified(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    """M2: `ensure_guild`'s `ON CONFLICT DO NOTHING` + read-back must return
    B's own values, unaffected by A having just been modified."""
    await guilds.set_timezone(app_conn, other_guild_id, "Asia/Tokyo")
    await guilds.set_timezone(app_conn, guild_id, "Europe/Paris")

    result = await guilds.ensure_guild(app_conn, other_guild_id)

    assert result.guild_id == other_guild_id
    assert result.timezone == "Asia/Tokyo"


# ---------------------------------------------------------------------------
# seasons.py
# ---------------------------------------------------------------------------


async def test_get_active_season_is_none_when_only_other_guild_has_one(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    """M2: guild B has no active season even though guild A does -- both
    guilds' `guild_config` rows exist, so this isn't just "no row at all"."""
    await seasons.create_season(app_conn, guild_id, "A", STARTS_ON, ENDS_ON, ENDS_AT, 2, 1)

    assert await seasons.get_active_season(app_conn, other_guild_id) is None


async def test_list_seasons_is_empty_when_only_other_guild_has_seasons(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    await seasons.create_season(app_conn, guild_id, "A", STARTS_ON, ENDS_ON, ENDS_AT, 2, 1)

    assert await seasons.list_seasons(app_conn, other_guild_id, 10) == []


async def test_get_season_wrong_guild_returns_none(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    season = await seasons.create_season(app_conn, guild_id, "S", STARTS_ON, ENDS_ON, ENDS_AT, 2, 1)
    assert await seasons.get_season(app_conn, other_guild_id, season.season_id) is None


async def test_set_active_min_games_wrong_guild_returns_none_and_does_not_mutate(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    season = await seasons.create_season(app_conn, guild_id, "S", STARTS_ON, ENDS_ON, ENDS_AT, 2, 1)
    assert await seasons.set_active_min_games(app_conn, other_guild_id, 9) is None

    unchanged = await seasons.get_season(app_conn, guild_id, season.season_id)
    assert unchanged is not None
    assert unchanged.min_games == 2


async def test_set_active_end_wrong_guild_returns_none_and_does_not_mutate(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    season = await seasons.create_season(app_conn, guild_id, "S", STARTS_ON, ENDS_ON, ENDS_AT, 2, 1)
    new_end = date(2026, 3, 1)
    assert (
        await seasons.set_active_end(
            app_conn, other_guild_id, new_end, datetime(2026, 3, 2, tzinfo=UTC)
        )
        is None
    )

    unchanged = await seasons.get_season(app_conn, guild_id, season.season_id)
    assert unchanged is not None
    assert unchanged.ends_on == ENDS_ON


async def test_cancel_active_season_wrong_guild_returns_none_and_does_not_mutate(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    season = await seasons.create_season(app_conn, guild_id, "S", STARTS_ON, ENDS_ON, ENDS_AT, 2, 1)
    assert await seasons.cancel_active_season(app_conn, other_guild_id) is None

    unchanged = await seasons.get_season(app_conn, guild_id, season.season_id)
    assert unchanged is not None
    assert unchanged.status == "active"


async def test_complete_season_wrong_guild_returns_false_and_inserts_no_results(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    season = await seasons.create_season(app_conn, guild_id, "S", STARTS_ON, ENDS_ON, ENDS_AT, 2, 1)
    await players.ensure_players(app_conn, guild_id, [1])
    results = [SeasonResultRow(user_id=1, rank=1, games=2, wins=2, eligible=True, outcome=None)]

    result = await seasons.complete_season(app_conn, other_guild_id, season.season_id, results)

    assert result is False
    unchanged = await seasons.get_season(app_conn, guild_id, season.season_id)
    assert unchanged is not None
    assert unchanged.status == "active"
    assert await seasons.get_season_results(app_conn, guild_id, season.season_id) == []


async def test_mark_announced_wrong_guild_does_not_mutate(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    season = await seasons.create_season(app_conn, guild_id, "S", STARTS_ON, ENDS_ON, ENDS_AT, 2, 1)
    await seasons.complete_season(app_conn, guild_id, season.season_id, [])

    await seasons.mark_announced(app_conn, other_guild_id, season.season_id)

    unchanged = await seasons.get_season(app_conn, guild_id, season.season_id)
    assert unchanged is not None
    assert unchanged.announced_at is None


async def test_get_season_results_wrong_guild_returns_empty(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    season = await seasons.create_season(app_conn, guild_id, "S", STARTS_ON, ENDS_ON, ENDS_AT, 2, 1)
    await players.ensure_players(app_conn, guild_id, [1])
    results = [SeasonResultRow(user_id=1, rank=1, games=2, wins=2, eligible=True, outcome=None)]
    await seasons.complete_season(app_conn, guild_id, season.season_id, results)

    assert await seasons.get_season_results(app_conn, other_guild_id, season.season_id) == []


async def test_season_and_all_time_stats_are_isolated_per_guild(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    season_a = await seasons.create_season(
        app_conn, guild_id, "A", STARTS_ON, ENDS_ON, ENDS_AT, 2, 1
    )
    await seasons.create_season(app_conn, other_guild_id, "B", STARTS_ON, ENDS_ON, ENDS_AT, 2, 1)
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, season_a.season_id, PLAYED_ON, 1, 1, [2])
    await games.confirm_game(app_conn, guild_id, game.game_id, 2)

    # Same user_id (1) has confirmed games only in `guild_id`.
    assert await seasons.season_player_stats(app_conn, other_guild_id, season_a.season_id) == []
    assert await seasons.all_time_player_stats(app_conn, other_guild_id) == []

    other_season, other_all_time = await seasons.player_stats(app_conn, other_guild_id, 1)
    assert other_season is not None  # other_guild_id has its own active season
    assert other_season.games == 0
    assert other_all_time.games == 0


# ---------------------------------------------------------------------------
# games.py
# ---------------------------------------------------------------------------


async def _create_pending_game(conn: asyncpg.Connection, guild_id: int) -> games.Game:
    await players.ensure_players(conn, guild_id, [1, 2])
    return await games.create_game(conn, guild_id, None, PLAYED_ON, 1, 1, [2])


async def test_get_game_wrong_guild_returns_none(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    game = await _create_pending_game(app_conn, guild_id)
    assert await games.get_game(app_conn, other_guild_id, game.game_id) is None


async def test_set_game_message_wrong_guild_returns_none_and_does_not_mutate(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    game = await _create_pending_game(app_conn, guild_id)
    assert await games.set_game_message(app_conn, other_guild_id, game.game_id, 1, 2) is None

    unchanged = await games.get_game(app_conn, guild_id, game.game_id)
    assert unchanged is not None
    assert unchanged.game.channel_id is None


async def test_confirm_game_wrong_guild_is_not_found_and_does_not_mutate(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    game = await _create_pending_game(app_conn, guild_id)

    result = await games.confirm_game(app_conn, other_guild_id, game.game_id, 2)

    assert result == "not_found"
    unchanged = await games.get_game(app_conn, guild_id, game.game_id)
    assert unchanged is not None
    assert unchanged.game.status == "pending"


async def test_reject_game_wrong_guild_is_not_found_and_does_not_mutate(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    game = await _create_pending_game(app_conn, guild_id)

    result = await games.reject_game(app_conn, other_guild_id, game.game_id, 2)

    assert result == "not_found"
    unchanged = await games.get_game(app_conn, guild_id, game.game_id)
    assert unchanged is not None
    assert unchanged.game.status == "pending"


async def test_reject_game_wrong_guild_by_the_original_reporter_is_not_found(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    """M2, targeting `_REJECT_GAME_SQL`'s `reported_by = $3` branch
    specifically: even guild A's own game reporter can't retract it by
    calling through guild B (both guilds' rows exist) -- `guild_id = $2`
    must scope the guarded UPDATE *and* the follow-up classify SELECT."""
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])

    result = await games.reject_game(app_conn, other_guild_id, game.game_id, 1)

    assert result == "not_found"
    unchanged = await games.get_game(app_conn, guild_id, game.game_id)
    assert unchanged is not None
    assert unchanged.game.status == "pending"


async def test_void_game_wrong_guild_is_not_found_and_does_not_mutate(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    game = await _create_pending_game(app_conn, guild_id)

    result = await games.void_game(app_conn, other_guild_id, game.game_id, 999, "n/a")

    assert result == "not_found"
    unchanged = await games.get_game(app_conn, guild_id, game.game_id)
    assert unchanged is not None
    assert unchanged.game.status == "pending"


async def test_list_recent_games_wrong_guild_returns_empty(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    await _create_pending_game(app_conn, guild_id)
    assert await games.list_recent_games(app_conn, other_guild_id, 10) == []


async def test_list_recent_games_for_player_wrong_guild_returns_empty(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    await _create_pending_game(app_conn, guild_id)
    assert await games.list_recent_games_for_player(app_conn, other_guild_id, 1, 10) == []


# ---------------------------------------------------------------------------
# events.py
# ---------------------------------------------------------------------------


async def _create_scheduled_event(conn: asyncpg.Connection, guild_id: int) -> events.Event:
    return await events.create_event(
        conn, guild_id, "Title", None, None, NOW + timedelta(hours=1), 1, None, []
    )


async def test_get_event_wrong_guild_returns_none(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    event = await _create_scheduled_event(app_conn, guild_id)
    assert await events.get_event(app_conn, other_guild_id, event.event_id) is None


async def test_set_event_message_wrong_guild_returns_none_and_does_not_mutate(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    event = await _create_scheduled_event(app_conn, guild_id)
    assert await events.set_event_message(app_conn, other_guild_id, event.event_id, 1, 2) is None

    unchanged = await events.get_event(app_conn, guild_id, event.event_id)
    assert unchanged is not None
    assert unchanged.channel_id is None


async def test_cancel_event_wrong_guild_is_not_found_and_does_not_mutate(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    event = await _create_scheduled_event(app_conn, guild_id)

    result = await events.cancel_event(app_conn, other_guild_id, event.event_id, 1, True)

    assert result == "not_found"
    unchanged = await events.get_event(app_conn, guild_id, event.event_id)
    assert unchanged is not None
    assert unchanged.status == "scheduled"


async def test_upsert_rsvp_wrong_guild_returns_false_and_does_not_mutate(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    event = await _create_scheduled_event(app_conn, guild_id)

    result = await events.upsert_rsvp(app_conn, other_guild_id, event.event_id, 5, "going")

    assert result is False
    assert await events.rsvp_user_ids(app_conn, guild_id, event.event_id, ["going"]) == []


async def test_rsvp_counts_wrong_guild_returns_zero(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    event = await _create_scheduled_event(app_conn, guild_id)
    await events.upsert_rsvp(app_conn, guild_id, event.event_id, 5, "going")

    counts = await events.rsvp_counts(app_conn, other_guild_id, event.event_id)

    assert (counts.going, counts.maybe, counts.not_going) == (0, 0, 0)


async def test_rsvp_user_ids_wrong_guild_returns_empty(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    event = await _create_scheduled_event(app_conn, guild_id)
    await events.upsert_rsvp(app_conn, guild_id, event.event_id, 5, "going")

    assert await events.rsvp_user_ids(app_conn, other_guild_id, event.event_id, ["going"]) == []


async def test_list_upcoming_events_wrong_guild_returns_empty(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    await _create_scheduled_event(app_conn, guild_id)
    assert await events.list_upcoming_events(app_conn, other_guild_id, NOW, 10) == []
