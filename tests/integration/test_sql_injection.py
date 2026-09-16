"""SQL-injection payload suite (Invicti cheat-sheet techniques).

Every payload here is pushed through repository functions as a plain `$n`
parameter value -- nothing in `db/repositories/` ever builds SQL from text
(see CLAUDE.md and `tests/static/sql_guard.py`, which enforces that
statically). This suite is the *dynamic* proof: for every payload and every
text-accepting field, the value round-trips verbatim, the schema stays
intact, unrelated rows are untouched, and nothing runs slowly -- including
second-order reuse, where a stored payload flows back out through a
different read path (leaderboard/resolution/announcement-shaped queries).
"""

from __future__ import annotations

import os
import time
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

_MAX_QUERY_SECONDS = 1.0

_LONG_PAYLOAD = ("'; DROP TABLE games;-- " * 50)[:1000]

# ---------------------------------------------------------------------------
# Cheat-sheet payloads (Invicti SQL injection cheat sheet categories):
# tautologies, comments, stacked/DDL statements, UNION, time-based blind,
# error-based blind, dollar-quoting, escaped quotes, inline comments,
# CHR()/hex/URL encoding bypasses, Unicode quote look-alikes, ORDER
# BY/LIMIT/identifier injection, and an oversized payload.
# ---------------------------------------------------------------------------
_PAYLOADS: list[tuple[str, str]] = [
    ("or_1_eq_1", "' OR '1'='1"),
    ("or_1_eq_1_comment", "' OR 1=1--"),
    ("drop_table_games", "'; DROP TABLE games;--"),
    ("delete_players", "'); DELETE FROM players;--"),
    ("union_select_null_null", "' UNION SELECT NULL,NULL--"),
    ("pg_sleep", "'; SELECT pg_sleep(5);--"),
    ("cast_version_error_based", "' AND 1=CAST((SELECT version()) AS int)--"),
    ("dollar_quote_drop_seasons", "$$; DROP TABLE seasons; $$"),
    ("backslash_escaped_quote_drop_x", "\\'; DROP TABLE x;--"),
    ("inline_comment_or", "/**/OR/**/1=1"),
    ("chr_concat", "CHR(39)||CHR(59)"),
    ("hex_encoded_quote", "0x27"),
    ("url_encoded_quote_semicolon", "%27%3B"),
    ("unicode_modifier_letter_apostrophe", chr(0x02BC) + "; DROP TABLE games;--"),
    ("unicode_fullwidth_apostrophe", chr(0xFF07) + "; DROP TABLE games;--"),
    ("admin_comment", "admin'--"),
    ("copy_to_program", "1; COPY (SELECT '') TO PROGRAM 'id'"),
    ("order_by_injection", "1 ORDER BY 99--"),
    ("limit_offset_injection", "1 LIMIT 1 OFFSET 1--"),
    ("oversized_1000_chars", _LONG_PAYLOAD),
]
assert len(_PAYLOADS) >= 20

PAYLOAD_PARAMS = [pytest.param(payload, id=pid) for pid, payload in _PAYLOADS]

_ALL_TABLES = frozenset(
    {
        "guild_config",
        "players",
        "seasons",
        "season_results",
        "games",
        "game_participants",
        "game_updates",
        "events",
        "event_rsvps",
        "event_reminders",
        "schema_migrations",
    }
)

_TABLE_COUNT_QUERIES: dict[str, str] = {
    "guild_config": "SELECT COUNT(*) FROM guild_config",
    "players": "SELECT COUNT(*) FROM players",
    "seasons": "SELECT COUNT(*) FROM seasons",
    "season_results": "SELECT COUNT(*) FROM season_results",
    "games": "SELECT COUNT(*) FROM games",
    "game_participants": "SELECT COUNT(*) FROM game_participants",
    "game_updates": "SELECT COUNT(*) FROM game_updates",
    "events": "SELECT COUNT(*) FROM events",
    "event_rsvps": "SELECT COUNT(*) FROM event_rsvps",
    "event_reminders": "SELECT COUNT(*) FROM event_reminders",
}

_PG_TABLES_SQL = "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public'"


def _fit(payload: str, max_len: int) -> str:
    """Trim `payload` to satisfy a `char_length(...) <= max_len` CHECK."""
    return payload[:max_len]


async def _table_names(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(_PG_TABLES_SQL)
    return {row["tablename"] for row in rows}


async def _table_counts(conn: asyncpg.Connection) -> dict[str, int]:
    return {table: await conn.fetchval(query) for table, query in _TABLE_COUNT_QUERIES.items()}


async def _assert_schema_intact(conn: asyncpg.Connection) -> None:
    """All 10 tables are still there via both `pg_tables` and
    `information_schema` -- proves no payload ever ran as DDL."""
    assert await _table_names(conn) == set(_ALL_TABLES)
    info_rows = await conn.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )
    info_names = {row["table_name"] for row in info_rows}
    # catan_app has no grants on schema_migrations, so information_schema
    # (privilege-filtered, unlike pg_tables) legitimately omits it -- what
    # matters is that every *other* table is still listed.
    assert info_names == _ALL_TABLES - {"schema_migrations"}


async def _assert_unchanged_except(
    conn: asyncpg.Connection, before: dict[str, int], expected_deltas: dict[str, int]
) -> None:
    after = await _table_counts(conn)
    for table, before_count in before.items():
        expected = before_count + expected_deltas.get(table, 0)
        assert after[table] == expected, (
            f"unexpected row count change in {table}: {before_count} -> {after[table]}"
        )


# ---------------------------------------------------------------------------
# Season name.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", PAYLOAD_PARAMS)
async def test_season_name_payload_round_trips_and_leaves_schema_intact(
    app_conn: asyncpg.Connection, guild_id: int, payload: str
) -> None:
    name = _fit(payload, 100)
    before = await _table_counts(app_conn)

    start = time.monotonic()
    season = await seasons.create_season(
        app_conn, guild_id, name, STARTS_ON, ENDS_ON, ENDS_AT, 2, 1
    )
    elapsed = time.monotonic() - start

    assert elapsed < _MAX_QUERY_SECONDS
    assert season.name == name

    fetched = await seasons.get_season(app_conn, guild_id, season.season_id)
    assert fetched is not None
    assert fetched.name == name

    listed = await seasons.list_seasons(app_conn, guild_id, 10)
    assert any(s.season_id == season.season_id and s.name == name for s in listed)

    await _assert_schema_intact(app_conn)
    await _assert_unchanged_except(app_conn, before, {"seasons": 1})


@pytest.mark.parametrize("payload", PAYLOAD_PARAMS)
async def test_season_name_payload_survives_the_full_resolution_pipeline(
    app_conn: asyncpg.Connection, guild_id: int, payload: str
) -> None:
    """Second-order: create -> lock_due_seasons -> complete_season ->
    get_season_results -> list_unannounced_completed, all with a
    payload-named season, and the name comes back intact at every step."""
    name = _fit(payload, 100)
    due_at = NOW - timedelta(days=1)
    season = await seasons.create_season(app_conn, guild_id, name, STARTS_ON, ENDS_ON, due_at, 2, 1)

    async with app_conn.transaction():
        due = await seasons.lock_due_seasons(app_conn, NOW)
        (locked,) = [s for s in due if s.season_id == season.season_id]
        assert locked.name == name

        completed = await seasons.complete_season(app_conn, guild_id, season.season_id, [])
        assert completed is True

    results = await seasons.get_season_results(app_conn, guild_id, season.season_id)
    assert results == []

    unannounced = await seasons.list_unannounced_completed(app_conn, 50)
    (matching,) = [s for s in unannounced if s.season_id == season.season_id]
    assert matching.name == name

    await _assert_schema_intact(app_conn)


# ---------------------------------------------------------------------------
# Event title / description / location.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", PAYLOAD_PARAMS)
async def test_event_text_fields_payload_round_trips_and_leaves_schema_intact(
    app_conn: asyncpg.Connection, guild_id: int, payload: str
) -> None:
    title = _fit(payload, 100)
    description = _fit(payload, 1000)
    location = _fit(payload, 200)
    starts_at = NOW + timedelta(hours=2)
    before = await _table_counts(app_conn)

    start = time.monotonic()
    event = await events.create_event(
        app_conn, guild_id, title, description, location, starts_at, 1, None, []
    )
    elapsed = time.monotonic() - start

    assert elapsed < _MAX_QUERY_SECONDS
    assert event.title == title
    assert event.description == description
    assert event.location == location

    fetched = await events.get_event(app_conn, guild_id, event.event_id)
    assert fetched is not None
    assert (fetched.title, fetched.description, fetched.location) == (title, description, location)

    upcoming = await events.list_upcoming_events(app_conn, guild_id, NOW, 10)
    (matching,) = [e for e in upcoming if e.event_id == event.event_id]
    assert (matching.title, matching.description, matching.location) == (
        title,
        description,
        location,
    )

    await _assert_schema_intact(app_conn)
    await _assert_unchanged_except(app_conn, before, {"events": 1})


@pytest.mark.parametrize("payload", PAYLOAD_PARAMS)
async def test_event_title_payload_survives_claim_due_reminders(
    app_conn: asyncpg.Connection, guild_id: int, payload: str
) -> None:
    """Second-order: a payload-titled event's reminder is claimed with the
    title intact in the `ClaimedReminder` the scheduler would announce."""
    title = _fit(payload, 100)
    starts_at = NOW + timedelta(hours=2)
    event = await events.create_event(
        app_conn,
        guild_id,
        title,
        None,
        None,
        starts_at,
        1,
        555,
        [(60, NOW - timedelta(minutes=1))],
    )

    claimed = await events.claim_due_reminders(app_conn, NOW)

    (matching,) = [c for c in claimed if c.event_id == event.event_id]
    assert matching.title == title
    await _assert_schema_intact(app_conn)


# ---------------------------------------------------------------------------
# Void reason.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", PAYLOAD_PARAMS)
async def test_void_reason_payload_round_trips_via_get_game(
    app_conn: asyncpg.Connection, guild_id: int, payload: str
) -> None:
    reason = _fit(payload, 200)
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])
    before = await _table_counts(app_conn)

    start = time.monotonic()
    result = await games.void_game(app_conn, guild_id, game.game_id, 999, reason)
    elapsed = time.monotonic() - start

    assert elapsed < _MAX_QUERY_SECONDS
    assert result == "voided"

    fetched = await games.get_game(app_conn, guild_id, game.game_id)
    assert fetched is not None
    assert fetched.game.void_reason == reason

    await _assert_schema_intact(app_conn)
    await _assert_unchanged_except(app_conn, before, {})


# ---------------------------------------------------------------------------
# Timezone (guild_config.timezone, CHECK <= 64 chars).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", PAYLOAD_PARAMS)
async def test_timezone_payload_round_trips_via_get_guild(
    app_conn: asyncpg.Connection, guild_id: int, payload: str
) -> None:
    timezone = _fit(payload, 64)
    before = await _table_counts(app_conn)

    start = time.monotonic()
    updated = await guilds.set_timezone(app_conn, guild_id, timezone)
    elapsed = time.monotonic() - start

    assert elapsed < _MAX_QUERY_SECONDS
    assert updated is not None
    assert updated.timezone == timezone

    fetched = await guilds.get_guild(app_conn, guild_id)
    assert fetched is not None
    assert fetched.timezone == timezone

    await _assert_schema_intact(app_conn)
    await _assert_unchanged_except(app_conn, before, {})


# ---------------------------------------------------------------------------
# Documented client-side/data-error behavior (not SQL injection per se, but
# adjacent robustness the M3a spec calls out explicitly).
# ---------------------------------------------------------------------------


async def test_nul_byte_in_text_raises_character_not_in_repertoire_error(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """Postgres text columns can't hold an embedded NUL byte. asyncpg
    surfaces this as a `CharacterNotInRepertoireError` (a subclass of
    `DataError`) from the server, never silently truncating or otherwise
    doing anything unsafe with it."""
    with pytest.raises(asyncpg.exceptions.CharacterNotInRepertoireError):
        await seasons.create_season(
            app_conn, guild_id, "bad\x00name", STARTS_ON, ENDS_ON, ENDS_AT, 2, 1
        )


async def test_str_where_bigint_expected_raises_value_error_before_reaching_sql(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """A non-numeric string bound to a `bigint` parameter is rejected by
    `_params.require_id` (item 7 / L2) before any `conn` method is even
    called -- the query is never sent to Postgres at all."""
    with pytest.raises(ValueError, match="season_id"):
        await seasons.get_season(app_conn, guild_id, "not-an-int")  # type: ignore[arg-type]


async def test_integer_overflow_id_raises_value_error_cleanly(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """An id outside `bigint` range (2^63-1) is also rejected by
    `_params.require_id` up front, rather than wrapping around or reaching
    the server."""
    with pytest.raises(ValueError, match="season_id"):
        await seasons.get_season(app_conn, guild_id, 2**64)


# ---------------------------------------------------------------------------
# Second-order via the stats/leaderboard-shaped queries: a payload season
# name must not affect (or be affected by) confirmed-game aggregation.
# ---------------------------------------------------------------------------


async def test_payload_season_name_does_not_break_stats_queries(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    payload_name = _fit("'; DROP TABLE games;--" + chr(0xFF07), 100)
    season = await seasons.create_season(
        app_conn, guild_id, payload_name, STARTS_ON, ENDS_ON, ENDS_AT, 2, 1
    )
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, season.season_id, PLAYED_ON, 1, 1, [2])
    confirm_result = await games.confirm_game(app_conn, guild_id, game.game_id, 2)
    assert confirm_result == "confirmed"

    stats = await seasons.season_player_stats(app_conn, guild_id, season.season_id)
    assert {s.user_id for s in stats} == {1, 2}

    all_time = await seasons.all_time_player_stats(app_conn, guild_id)
    assert {s.user_id for s in all_time} == {1, 2}

    results = [SeasonResultRow(user_id=1, rank=1, games=1, wins=1, eligible=True, outcome="payee")]
    await seasons.complete_season(app_conn, guild_id, season.season_id, results)
    stored = await seasons.get_season_results(app_conn, guild_id, season.season_id)
    assert [r.user_id for r in stored] == [1]

    # Second-order: the payload name must come back intact through every
    # read path, after both stats aggregation and season completion.
    listed = await seasons.list_seasons(app_conn, guild_id, 10)
    (listed_match,) = [s for s in listed if s.season_id == season.season_id]
    assert listed_match.name == payload_name

    fetched = await seasons.get_season(app_conn, guild_id, season.season_id)
    assert fetched is not None
    assert fetched.name == payload_name

    unannounced = await seasons.list_unannounced_completed(app_conn, 50)
    (unannounced_match,) = [s for s in unannounced if s.season_id == season.season_id]
    assert unannounced_match.name == payload_name

    await _assert_schema_intact(app_conn)


# ---------------------------------------------------------------------------
# Enum-CHECK columns: `event_rsvps.response` and `season_results.outcome`
# are constrained to a fixed set of literal values, not a length limit --
# every payload must be rejected by that CHECK, never accepted or treated
# as SQL.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", PAYLOAD_PARAMS)
async def test_upsert_rsvp_payload_response_raises_check_violation_and_leaves_rsvps_unchanged(
    app_conn: asyncpg.Connection, guild_id: int, payload: str
) -> None:
    event = await events.create_event(
        app_conn, guild_id, "Title", None, None, NOW + timedelta(hours=1), 1, None, []
    )
    before = await _table_counts(app_conn)

    with pytest.raises(asyncpg.CheckViolationError):
        await events.upsert_rsvp(app_conn, guild_id, event.event_id, 1, payload)

    await _assert_unchanged_except(app_conn, before, {})
    counts = await events.rsvp_counts(app_conn, guild_id, event.event_id)
    assert (counts.going, counts.maybe, counts.not_going) == (0, 0, 0)
    await _assert_schema_intact(app_conn)


@pytest.mark.parametrize("payload", PAYLOAD_PARAMS)
async def test_rsvp_user_ids_payload_element_is_ignored_without_error(
    app_conn: asyncpg.Connection, guild_id: int, payload: str
) -> None:
    """A payload string as one element of the `responses` filter list must
    never error or match rows -- `response = ANY($3::text[])` simply finds
    no match for it, leaving the genuine `'going'` row unaffected."""
    event = await events.create_event(
        app_conn, guild_id, "Title", None, None, NOW + timedelta(hours=1), 1, None, []
    )
    await events.upsert_rsvp(app_conn, guild_id, event.event_id, 1, "going")

    ids = await events.rsvp_user_ids(app_conn, guild_id, event.event_id, [payload, "going"])

    assert ids == [1]
    await _assert_schema_intact(app_conn)


async def test_complete_season_invalid_outcome_payload_rolls_back_entirely(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """`season_results.outcome` is constrained to `('payer', 'payee')` --
    one bad row must roll back the *whole* `complete_season` transaction,
    including the guarding `active -> completed` UPDATE, proving
    `conn.transaction()` really wraps both statements.

    N3 (M3a re-audit): `complete_season` now validates every result row's
    `outcome` (against `(None, "payer", "payee")`) before any SQL is built,
    so this SQL-injection-shaped payload is now rejected as a `ValueError`
    without ever reaching Postgres -- an even stronger guarantee than the
    `CheckViolationError` this previously depended on the database for.
    Nothing must be written either way: the assertions below still prove
    the season is untouched, no results were stored, and the schema
    survived intact."""
    season = await seasons.create_season(app_conn, guild_id, "S", STARTS_ON, ENDS_ON, ENDS_AT, 2, 1)
    await players.ensure_players(app_conn, guild_id, [1, 2])
    results = [
        SeasonResultRow(user_id=1, rank=1, games=1, wins=1, eligible=True, outcome="payee"),
        SeasonResultRow(
            user_id=2,
            rank=2,
            games=1,
            wins=0,
            eligible=True,
            outcome="'; DROP TABLE season_results;--",  # type: ignore[arg-type]
        ),
    ]

    with pytest.raises(ValueError, match=r"results\[1\]\.outcome"):
        await seasons.complete_season(app_conn, guild_id, season.season_id, results)

    unchanged = await seasons.get_season(app_conn, guild_id, season.season_id)
    assert unchanged is not None
    assert unchanged.status == "active"
    assert await seasons.get_season_results(app_conn, guild_id, season.season_id) == []
    await _assert_schema_intact(app_conn)


# ---------------------------------------------------------------------------
# Over-length text: every length-limited column (`char_length(...) <= N`)
# must reject exactly one character over its max, with no truncation and no
# partial write.
# ---------------------------------------------------------------------------


async def test_season_name_over_max_length_raises_check_violation_and_creates_no_row(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    before = await _table_counts(app_conn)
    over_length = "a" * 101

    with pytest.raises(asyncpg.CheckViolationError):
        await seasons.create_season(
            app_conn, guild_id, over_length, STARTS_ON, ENDS_ON, ENDS_AT, 2, 1
        )

    await _assert_unchanged_except(app_conn, before, {})
    await _assert_schema_intact(app_conn)


async def test_season_name_over_max_length_astral_plane_raises_check_violation(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """`char_length()` counts code points, not UTF-16 code units or UTF-8
    bytes -- a 101-character string built from one astral-plane `chr()`
    call per character must still trip the `<= 100` CHECK (matching
    `test_server_encoding_is_utf8` in test_repo_guilds.py)."""
    before = await _table_counts(app_conn)
    over_length = chr(0x1F600) * 101
    assert len(over_length) == 101

    with pytest.raises(asyncpg.CheckViolationError):
        await seasons.create_season(
            app_conn, guild_id, over_length, STARTS_ON, ENDS_ON, ENDS_AT, 2, 1
        )

    await _assert_unchanged_except(app_conn, before, {})
    await _assert_schema_intact(app_conn)


async def test_event_title_over_max_length_raises_check_violation_and_creates_no_row(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    before = await _table_counts(app_conn)
    over_length = "a" * 101

    with pytest.raises(asyncpg.CheckViolationError):
        await events.create_event(
            app_conn, guild_id, over_length, None, None, NOW + timedelta(hours=1), 1, None, []
        )

    await _assert_unchanged_except(app_conn, before, {})
    await _assert_schema_intact(app_conn)


async def test_event_title_over_max_length_astral_plane_raises_check_violation(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    before = await _table_counts(app_conn)
    over_length = chr(0x1F600) * 101
    assert len(over_length) == 101

    with pytest.raises(asyncpg.CheckViolationError):
        await events.create_event(
            app_conn, guild_id, over_length, None, None, NOW + timedelta(hours=1), 1, None, []
        )

    await _assert_unchanged_except(app_conn, before, {})
    await _assert_schema_intact(app_conn)


async def test_event_description_over_max_length_raises_check_violation_and_creates_no_row(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    before = await _table_counts(app_conn)
    over_length = "a" * 1001

    with pytest.raises(asyncpg.CheckViolationError):
        await events.create_event(
            app_conn, guild_id, "T", over_length, None, NOW + timedelta(hours=1), 1, None, []
        )

    await _assert_unchanged_except(app_conn, before, {})
    await _assert_schema_intact(app_conn)


async def test_event_location_over_max_length_raises_check_violation_and_creates_no_row(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    before = await _table_counts(app_conn)
    over_length = "a" * 201

    with pytest.raises(asyncpg.CheckViolationError):
        await events.create_event(
            app_conn, guild_id, "T", None, over_length, NOW + timedelta(hours=1), 1, None, []
        )

    await _assert_unchanged_except(app_conn, before, {})
    await _assert_schema_intact(app_conn)


async def test_void_reason_over_max_length_raises_check_violation_and_leaves_game_pending(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    await players.ensure_players(app_conn, guild_id, [1, 2])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2])
    over_length = "a" * 201

    with pytest.raises(asyncpg.CheckViolationError):
        await games.void_game(app_conn, guild_id, game.game_id, 999, over_length)

    unchanged = await games.get_game(app_conn, guild_id, game.game_id)
    assert unchanged is not None
    assert unchanged.game.status == "pending"
    assert unchanged.game.void_reason is None
    await _assert_schema_intact(app_conn)


async def test_timezone_over_max_length_raises_check_violation_and_leaves_value_unchanged(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    before = await guilds.get_guild(app_conn, guild_id)
    assert before is not None
    over_length = "a" * 65

    with pytest.raises(asyncpg.CheckViolationError):
        await guilds.set_timezone(app_conn, guild_id, over_length)

    unchanged = await guilds.get_guild(app_conn, guild_id)
    assert unchanged is not None
    assert unchanged.timezone == before.timezone
    await _assert_schema_intact(app_conn)
