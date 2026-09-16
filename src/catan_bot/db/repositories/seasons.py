"""`seasons` / `season_results` repository.

Every SQL statement here is a module-level string constant, bound exactly
once, with values passed only as `$n` arguments (see CLAUDE.md and
`tests/static/sql_guard.py`).

`lock_due_seasons` and `list_unannounced_completed` are the two documented
exceptions to "every query is guild-scoped": they are system-wide scheduler
queries that must see every guild's due/unannounced seasons in one pass.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

import asyncpg

from catan_bot.db.errors import ActiveSeasonExistsError
from catan_bot.db.models import Season, SeasonResultRow
from catan_bot.db.repositories._params import require_aware, require_id, require_int, require_limit
from catan_bot.domain.ranking import PlayerStats

# `min_games` is a small, human-set threshold (never a computed/user-count
# value), so 100 is a generous, sane ceiling rather than the column's real
# (much larger) INT range.
_MIN_GAMES_MIN = 1
_MIN_GAMES_MAX = 100

# `season_results.rank`/`.games` are Postgres `INT` (32-bit signed) columns
# (see `0001_init.sql`); this is that column's real range, not an arbitrary
# app-level ceiling.
_INT_MAX = 2**31 - 1

_SELECT_ACTIVE_SEASON_SQL = """
SELECT season_id, guild_id, name, starts_on, ends_on, ends_at, min_games, status,
       resolved_at, announced_at, created_by, created_at
FROM seasons
WHERE guild_id = $1 AND status = 'active'
"""

# L2 audit finding: `end_season_now` must hold this guild's active-season
# row locked for the rest of its transaction, so a concurrent
# `set_active_min_games` can't change `min_games` between this read and
# `complete_season` freezing results computed from it -- it blocks on this
# row lock until the locking transaction commits or rolls back.
_LOCK_ACTIVE_SEASON_SQL = """
SELECT season_id, guild_id, name, starts_on, ends_on, ends_at, min_games, status,
       resolved_at, announced_at, created_by, created_at
FROM seasons
WHERE guild_id = $1 AND status = 'active'
FOR UPDATE
"""

# L3 audit finding: the scheduler must be able to lock *one* due season at
# a time (excluding ones it already knows have failed this pass) instead of
# every due season across every guild in one transaction -- so a slow or
# paused resolution never also holds an unrelated guild's due season
# locked. `SKIP LOCKED` lets a concurrent caller move on to the next
# candidate instead of blocking on a row this query's caller is still
# holding.
_LOCK_NEXT_DUE_SEASON_SQL = """
SELECT season_id, guild_id, name, starts_on, ends_on, ends_at, min_games, status,
       resolved_at, announced_at, created_by, created_at
FROM seasons
WHERE status = 'active' AND ends_at <= $1 AND NOT (season_id = ANY($2::bigint[]))
ORDER BY season_id
LIMIT 1
FOR UPDATE SKIP LOCKED
"""

_INSERT_SEASON_SQL = """
INSERT INTO seasons (guild_id, name, starts_on, ends_on, ends_at, min_games, created_by)
VALUES ($1, $2, $3, $4, $5, $6, $7)
RETURNING season_id, guild_id, name, starts_on, ends_on, ends_at, min_games, status,
          resolved_at, announced_at, created_by, created_at
"""

_UPDATE_ACTIVE_MIN_GAMES_SQL = """
UPDATE seasons
SET min_games = $2
WHERE guild_id = $1 AND status = 'active'
RETURNING season_id, guild_id, name, starts_on, ends_on, ends_at, min_games, status,
          resolved_at, announced_at, created_by, created_at
"""

_UPDATE_ACTIVE_END_SQL = """
UPDATE seasons
SET ends_on = $2, ends_at = $3
WHERE guild_id = $1 AND status = 'active'
RETURNING season_id, guild_id, name, starts_on, ends_on, ends_at, min_games, status,
          resolved_at, announced_at, created_by, created_at
"""

_CANCEL_ACTIVE_SEASON_SQL = """
UPDATE seasons
SET status = 'cancelled', resolved_at = now()
WHERE guild_id = $1 AND status = 'active'
RETURNING season_id, guild_id, name, starts_on, ends_on, ends_at, min_games, status,
          resolved_at, announced_at, created_by, created_at
"""

_LIST_SEASONS_SQL = """
SELECT season_id, guild_id, name, starts_on, ends_on, ends_at, min_games, status,
       resolved_at, announced_at, created_by, created_at
FROM seasons
WHERE guild_id = $1
ORDER BY season_id DESC
LIMIT $2
"""

_SELECT_SEASON_SQL = """
SELECT season_id, guild_id, name, starts_on, ends_on, ends_at, min_games, status,
       resolved_at, announced_at, created_by, created_at
FROM seasons
WHERE guild_id = $1 AND season_id = $2
"""

_LOCK_SEASON_SQL = """
SELECT season_id, guild_id, name, starts_on, ends_on, ends_at, min_games, status,
       resolved_at, announced_at, created_by, created_at
FROM seasons
WHERE guild_id = $1 AND season_id = $2
FOR UPDATE
"""

_SELECT_SEASON_PLAYER_STATS_SQL = """
SELECT p.user_id AS user_id, COUNT(*) AS games, COUNT(*) FILTER (WHERE p.is_winner) AS wins
FROM game_participants p
JOIN games g ON g.game_id = p.game_id
WHERE g.guild_id = $1 AND g.season_id = $2 AND g.status = 'confirmed' AND p.is_active
GROUP BY p.user_id
"""

_SELECT_ALL_TIME_PLAYER_STATS_SQL = """
SELECT p.user_id AS user_id, COUNT(*) AS games, COUNT(*) FILTER (WHERE p.is_winner) AS wins
FROM game_participants p
JOIN games g ON g.game_id = p.game_id
WHERE g.guild_id = $1 AND g.status = 'confirmed' AND p.is_active
GROUP BY p.user_id
"""

_SELECT_SEASON_PLAYER_STATS_FOR_USER_SQL = """
SELECT COUNT(*) AS games, COUNT(*) FILTER (WHERE p.is_winner) AS wins
FROM game_participants p
JOIN games g ON g.game_id = p.game_id
WHERE g.guild_id = $1 AND g.season_id = $2 AND g.status = 'confirmed'
  AND p.user_id = $3 AND p.is_active
"""

_SELECT_ALL_TIME_PLAYER_STATS_FOR_USER_SQL = """
SELECT COUNT(*) AS games, COUNT(*) FILTER (WHERE p.is_winner) AS wins
FROM game_participants p
JOIN games g ON g.game_id = p.game_id
WHERE g.guild_id = $1 AND g.status = 'confirmed' AND p.user_id = $2 AND p.is_active
"""

# System-wide (no guild_id): the scheduler must see every guild's due
# seasons in one pass. Callers hold a transaction so FOR UPDATE SKIP LOCKED
# actually locks the rows until they're resolved.
_LOCK_DUE_SEASONS_SQL = """
SELECT season_id, guild_id, name, starts_on, ends_on, ends_at, min_games, status,
       resolved_at, announced_at, created_by, created_at
FROM seasons
WHERE status = 'active' AND ends_at <= $1
FOR UPDATE SKIP LOCKED
"""

_COMPLETE_SEASON_GUARDED_UPDATE_SQL = """
UPDATE seasons
SET status = 'completed', resolved_at = now()
WHERE guild_id = $1 AND season_id = $2 AND status = 'active'
RETURNING season_id
"""

_INSERT_SEASON_RESULTS_SQL = """
INSERT INTO season_results (season_id, guild_id, user_id, rank, games, wins, eligible, outcome)
SELECT $1, $2, u, r, g, w, e, o
FROM unnest($3::bigint[], $4::int[], $5::int[], $6::int[], $7::boolean[], $8::text[])
    AS t(u, r, g, w, e, o)
"""

# System-wide (no guild_id): the scheduler must find every guild's
# unannounced completed seasons in one pass.
_LIST_UNANNOUNCED_COMPLETED_SQL = """
SELECT season_id, guild_id, name, starts_on, ends_on, ends_at, min_games, status,
       resolved_at, announced_at, created_by, created_at
FROM seasons
WHERE status = 'completed' AND announced_at IS NULL
ORDER BY season_id
LIMIT $1
"""

_MARK_ANNOUNCED_SQL = """
UPDATE seasons
SET announced_at = now()
WHERE guild_id = $1 AND season_id = $2 AND status = 'completed' AND announced_at IS NULL
"""

_SELECT_SEASON_RESULTS_SQL = """
SELECT user_id, rank, games, wins, eligible, outcome
FROM season_results
WHERE season_id = $1 AND guild_id = $2
ORDER BY rank, user_id
"""

_ACTIVE_SEASON_UNIQUE_CONSTRAINT = "seasons_one_active_per_guild"


def _row_to_season(row: asyncpg.Record) -> Season:
    return Season(
        season_id=row["season_id"],
        guild_id=row["guild_id"],
        name=row["name"],
        starts_on=row["starts_on"],
        ends_on=row["ends_on"],
        ends_at=row["ends_at"],
        min_games=row["min_games"],
        status=row["status"],
        resolved_at=row["resolved_at"],
        announced_at=row["announced_at"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


def _row_to_player_stats(row: asyncpg.Record) -> PlayerStats:
    return PlayerStats(user_id=row["user_id"], games=row["games"], wins=row["wins"])


def _row_to_season_result(row: asyncpg.Record) -> SeasonResultRow:
    return SeasonResultRow(
        user_id=row["user_id"],
        rank=row["rank"],
        games=row["games"],
        wins=row["wins"],
        eligible=row["eligible"],
        outcome=row["outcome"],
    )


async def get_active_season(conn: asyncpg.Connection, guild_id: int) -> Season | None:
    require_id(guild_id, name="guild_id")
    row = await conn.fetchrow(_SELECT_ACTIVE_SEASON_SQL, guild_id)
    return _row_to_season(row) if row is not None else None


async def lock_active_season(conn: asyncpg.Connection, guild_id: int) -> Season | None:
    """`guild_id`'s active season, row-locked (`FOR UPDATE`) for the caller's transaction.

    Callers must hold a transaction: the lock is released on commit or
    rollback. A concurrent `set_active_min_games`/`set_active_end` on the
    same row blocks until then, rather than racing a read this transaction
    already made.
    """
    require_id(guild_id, name="guild_id")
    row = await conn.fetchrow(_LOCK_ACTIVE_SEASON_SQL, guild_id)
    return _row_to_season(row) if row is not None else None


async def lock_next_due_season(
    conn: asyncpg.Connection, now: datetime, exclude_season_ids: Sequence[int]
) -> Season | None:
    """The lowest-`season_id` active season whose `ends_at` has passed, excluding some ids.

    System-wide scheduler query (no `guild_id` scoping), matching
    `lock_due_seasons` -- but locks at most *one* row (`FOR UPDATE SKIP
    LOCKED LIMIT 1`) instead of every due season in one pass, so a resolver
    working on one candidate never also holds an unrelated guild's due
    season locked. Callers must hold a transaction, exactly like
    `lock_due_seasons`.

    `exclude_season_ids` is materialized and validated (`require_id`, one
    call per id, matching `complete_season`'s per-row validation) before
    any SQL runs; an empty sequence excludes nothing.
    """
    now = require_aware(now, name="now")
    excluded = [
        require_id(season_id, name=f"exclude_season_ids[{i}]")
        for i, season_id in enumerate(exclude_season_ids)
    ]
    row = await conn.fetchrow(_LOCK_NEXT_DUE_SEASON_SQL, now, excluded)
    return _row_to_season(row) if row is not None else None


async def create_season(
    conn: asyncpg.Connection,
    guild_id: int,
    name: str,
    starts_on: date,
    ends_on: date,
    ends_at: datetime,
    min_games: int,
    created_by: int,
) -> Season:
    """Create a new active season, raising `ActiveSeasonExistsError` if one exists.

    Maps `asyncpg.UniqueViolationError` on `seasons_one_active_per_guild`
    (the only unique constraint this INSERT can hit) to the typed error;
    anything else propagates unchanged.
    """
    require_id(guild_id, name="guild_id")
    require_id(created_by, name="created_by")
    ends_at = require_aware(ends_at, name="ends_at")
    min_games = require_int(
        min_games, name="min_games", min_value=_MIN_GAMES_MIN, max_value=_MIN_GAMES_MAX
    )
    try:
        row = await conn.fetchrow(
            _INSERT_SEASON_SQL, guild_id, name, starts_on, ends_on, ends_at, min_games, created_by
        )
    except asyncpg.UniqueViolationError as exc:
        if exc.constraint_name == _ACTIVE_SEASON_UNIQUE_CONSTRAINT:
            raise ActiveSeasonExistsError(guild_id) from exc
        raise  # pragma: no cover -- no other unique constraint can fire on this INSERT.
    if row is None:  # pragma: no cover -- INSERT ... RETURNING always returns a row on success.
        raise RuntimeError("INSERT INTO seasons did not return a row")
    return _row_to_season(row)


async def set_active_min_games(conn: asyncpg.Connection, guild_id: int, n: int) -> Season | None:
    require_id(guild_id, name="guild_id")
    n = require_int(n, name="n", min_value=_MIN_GAMES_MIN, max_value=_MIN_GAMES_MAX)
    row = await conn.fetchrow(_UPDATE_ACTIVE_MIN_GAMES_SQL, guild_id, n)
    return _row_to_season(row) if row is not None else None


async def set_active_end(
    conn: asyncpg.Connection, guild_id: int, ends_on: date, ends_at: datetime
) -> Season | None:
    require_id(guild_id, name="guild_id")
    ends_at = require_aware(ends_at, name="ends_at")
    row = await conn.fetchrow(_UPDATE_ACTIVE_END_SQL, guild_id, ends_on, ends_at)
    return _row_to_season(row) if row is not None else None


async def cancel_active_season(conn: asyncpg.Connection, guild_id: int) -> Season | None:
    require_id(guild_id, name="guild_id")
    row = await conn.fetchrow(_CANCEL_ACTIVE_SEASON_SQL, guild_id)
    return _row_to_season(row) if row is not None else None


async def list_seasons(conn: asyncpg.Connection, guild_id: int, limit: int) -> list[Season]:
    require_id(guild_id, name="guild_id")
    require_limit(limit)
    rows = await conn.fetch(_LIST_SEASONS_SQL, guild_id, limit)
    return [_row_to_season(row) for row in rows]


async def get_season(conn: asyncpg.Connection, guild_id: int, season_id: int) -> Season | None:
    require_id(guild_id, name="guild_id")
    require_id(season_id, name="season_id")
    row = await conn.fetchrow(_SELECT_SEASON_SQL, guild_id, season_id)
    return _row_to_season(row) if row is not None else None


async def lock_season(conn: asyncpg.Connection, guild_id: int, season_id: int) -> Season | None:
    """Lock one guild-scoped season regardless of status for a caller transaction."""
    require_id(guild_id, name="guild_id")
    require_id(season_id, name="season_id")
    row = await conn.fetchrow(_LOCK_SEASON_SQL, guild_id, season_id)
    return _row_to_season(row) if row is not None else None


async def season_player_stats(
    conn: asyncpg.Connection, guild_id: int, season_id: int
) -> list[PlayerStats]:
    """Confirmed-game tallies for `season_id`, one row per player who has any."""
    require_id(guild_id, name="guild_id")
    require_id(season_id, name="season_id")
    rows = await conn.fetch(_SELECT_SEASON_PLAYER_STATS_SQL, guild_id, season_id)
    return [_row_to_player_stats(row) for row in rows]


async def all_time_player_stats(conn: asyncpg.Connection, guild_id: int) -> list[PlayerStats]:
    """Confirmed-game tallies across every season (and seasonless games)."""
    require_id(guild_id, name="guild_id")
    rows = await conn.fetch(_SELECT_ALL_TIME_PLAYER_STATS_SQL, guild_id)
    return [_row_to_player_stats(row) for row in rows]


async def player_stats(
    conn: asyncpg.Connection, guild_id: int, user_id: int
) -> tuple[PlayerStats | None, PlayerStats]:
    """One player's season (if a season is active) and all-time stats.

    `season` is `None` only when there is no active season -- if one is
    active but the player has zero confirmed games in it, `season` is a
    zero-games `PlayerStats`, not `None`.
    """
    require_id(guild_id, name="guild_id")
    require_id(user_id, name="user_id")
    active = await get_active_season(conn, guild_id)
    all_time_row = await conn.fetchrow(
        _SELECT_ALL_TIME_PLAYER_STATS_FOR_USER_SQL, guild_id, user_id
    )
    if all_time_row is None:  # pragma: no cover -- a bare COUNT(*) always returns one row.
        raise RuntimeError("all-time stats query returned no row")
    all_time = PlayerStats(user_id=user_id, games=all_time_row["games"], wins=all_time_row["wins"])

    season: PlayerStats | None = None
    if active is not None:
        season_row = await conn.fetchrow(
            _SELECT_SEASON_PLAYER_STATS_FOR_USER_SQL, guild_id, active.season_id, user_id
        )
        if season_row is None:  # pragma: no cover -- same as above.
            raise RuntimeError("season stats query returned no row")
        season = PlayerStats(user_id=user_id, games=season_row["games"], wins=season_row["wins"])
    return season, all_time


async def lock_due_seasons(conn: asyncpg.Connection, now: datetime) -> list[Season]:
    """Every active season across all guilds whose `ends_at` has passed.

    System-wide scheduler query (no `guild_id` scoping): callers must hold
    a transaction so `FOR UPDATE SKIP LOCKED` actually reserves each season
    until `complete_season` (or a rollback) releases it.
    """
    now = require_aware(now, name="now")
    rows = await conn.fetch(_LOCK_DUE_SEASONS_SQL, now)
    return [_row_to_season(row) for row in rows]


_SEASON_OUTCOMES: tuple[str | None, ...] = (None, "payer", "payee")


def _validate_season_result_row(row: SeasonResultRow, *, index: int) -> None:
    """One `SeasonResultRow`, checked field-by-field before any SQL is built.

    `type(x) is int` (via `require_id`/`require_int`) and `type(eligible) is
    bool` both avoid the same class of silent coercion `require_id` guards
    against elsewhere: `True` reading as `1`, `2.9` truncating to `2` (N3
    audit finding). `wins`'s upper bound is that row's own (already
    validated) `games`, matching the `season_results_wins_le_games` CHECK
    constraint this would otherwise only fail against at the database.
    """
    require_id(row.user_id, name=f"results[{index}].user_id")
    require_int(row.rank, name=f"results[{index}].rank", min_value=1, max_value=_INT_MAX)
    games = require_int(row.games, name=f"results[{index}].games", min_value=0, max_value=_INT_MAX)
    require_int(row.wins, name=f"results[{index}].wins", min_value=0, max_value=games)
    if type(row.eligible) is not bool:
        raise ValueError(
            f"results[{index}].eligible must be a bool, got {row.eligible!r} "
            f"({type(row.eligible).__name__})"
        )
    if row.outcome not in _SEASON_OUTCOMES:
        raise ValueError(
            f"results[{index}].outcome must be one of {_SEASON_OUTCOMES!r}, got {row.outcome!r}"
        )


async def complete_season(
    conn: asyncpg.Connection,
    guild_id: int,
    season_id: int,
    results: Sequence[SeasonResultRow],
) -> bool:
    """Freeze `results` and flip the season `active -> completed`, atomically.

    The guarded UPDATE runs first: if the season isn't currently active
    (already completed/cancelled, or never existed), it updates zero rows
    and this returns `False` *without* inserting any results -- making a
    second call idempotent (no duplicate `season_results` rows). Both
    statements run in one transaction (nested as a savepoint if the caller
    is already in one, e.g. around `lock_due_seasons`).
    """
    require_id(guild_id, name="guild_id")
    require_id(season_id, name="season_id")
    # Materialize exactly once: `results` may be a one-shot iterable (e.g. a
    # generator), and validating each row here must not be the thing that
    # consumes it before the SQL call below ever sees it (N1 audit finding).
    results = list(results)
    for i, row in enumerate(results):
        _validate_season_result_row(row, index=i)
    async with conn.transaction():
        updated = await conn.fetchrow(_COMPLETE_SEASON_GUARDED_UPDATE_SQL, guild_id, season_id)
        if updated is None:
            return False

        user_ids = [r.user_id for r in results]
        ranks = [r.rank for r in results]
        games = [r.games for r in results]
        wins = [r.wins for r in results]
        eligible = [r.eligible for r in results]
        outcomes = [r.outcome for r in results]
        await conn.execute(
            _INSERT_SEASON_RESULTS_SQL,
            season_id,
            guild_id,
            user_ids,
            ranks,
            games,
            wins,
            eligible,
            outcomes,
        )
    return True


async def list_unannounced_completed(conn: asyncpg.Connection, limit: int) -> list[Season]:
    """Every completed-but-unannounced season across all guilds.

    System-wide scheduler query (no `guild_id` scoping), matching
    `lock_due_seasons`.
    """
    require_limit(limit)
    rows = await conn.fetch(_LIST_UNANNOUNCED_COMPLETED_SQL, limit)
    return [_row_to_season(row) for row in rows]


async def mark_announced(conn: asyncpg.Connection, guild_id: int, season_id: int) -> None:
    require_id(guild_id, name="guild_id")
    require_id(season_id, name="season_id")
    await conn.execute(_MARK_ANNOUNCED_SQL, guild_id, season_id)


async def get_season_results(
    conn: asyncpg.Connection, guild_id: int, season_id: int
) -> list[SeasonResultRow]:
    require_id(guild_id, name="guild_id")
    require_id(season_id, name="season_id")
    rows = await conn.fetch(_SELECT_SEASON_RESULTS_SQL, season_id, guild_id)
    return [_row_to_season_result(row) for row in rows]
