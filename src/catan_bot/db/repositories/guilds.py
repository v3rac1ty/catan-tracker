"""`guild_config` repository.

Every SQL statement here is a module-level string constant, bound exactly
once, and every value reaches Postgres only as a `$n` argument -- see
CLAUDE.md and `tests/static/sql_guard.py` for the enforced rules.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, time

import asyncpg

from catan_bot.db.models import GuildConfig
from catan_bot.db.repositories._params import require_id, require_int, require_optional_id

# `min_games` is a small, human-set threshold (never a computed/user-count
# value), so 100 is a generous, sane ceiling rather than the column's real
# (much larger) INT range.
_MIN_GAMES_MIN = 1
_MIN_GAMES_MAX = 100

_LEADERBOARD_MODES = frozenset({"off", "per_game", "daily"})
_LEADERBOARD_SCOPES = frozenset({"season", "all_time"})


class _Unset:
    """Sentinel meaning "the caller didn't pass this argument at all."

    Distinct from `None`, which is itself a valid value for a nullable
    field like `channel_id` -- see `set_leaderboard_settings`, the one
    function that uses this.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid only.
        return "UNSET"


_UNSET = _Unset()

_INSERT_GUILD_IF_MISSING_SQL = """
INSERT INTO guild_config (guild_id)
VALUES ($1)
ON CONFLICT (guild_id) DO NOTHING
"""

# Every SELECT/RETURNING column list below is the same set, spelled out in
# full each time rather than shared via string interpolation: the SQL guard
# (tests/static/sql_guard.py) only ever treats a plain literal `NAME = "..."`
# as a sound constant, so an f-string built from two constants -- even two
# perfectly sound ones -- is flagged as "building a SQL-shaped string" and
# rejected outright. Verbatim repetition is the only form the guard accepts;
# adding/removing a 0005 column means updating all eight statements below.
_SELECT_GUILD_SQL = """
SELECT guild_id, timezone, announce_channel_id, admin_role_id, player_role_id,
       default_min_games, created_at, updated_at,
       leaderboard_mode, leaderboard_channel_id, leaderboard_scope,
       leaderboard_daily_time, leaderboard_last_posted_on, leaderboard_last_ranking
FROM guild_config
WHERE guild_id = $1
"""

_UPDATE_TIMEZONE_SQL = """
UPDATE guild_config
SET timezone = $2, updated_at = now()
WHERE guild_id = $1
RETURNING guild_id, timezone, announce_channel_id, admin_role_id, player_role_id,
          default_min_games, created_at, updated_at,
          leaderboard_mode, leaderboard_channel_id, leaderboard_scope,
          leaderboard_daily_time, leaderboard_last_posted_on, leaderboard_last_ranking
"""

_UPDATE_ANNOUNCE_CHANNEL_SQL = """
UPDATE guild_config
SET announce_channel_id = $2, updated_at = now()
WHERE guild_id = $1
RETURNING guild_id, timezone, announce_channel_id, admin_role_id, player_role_id,
          default_min_games, created_at, updated_at,
          leaderboard_mode, leaderboard_channel_id, leaderboard_scope,
          leaderboard_daily_time, leaderboard_last_posted_on, leaderboard_last_ranking
"""

_UPDATE_ADMIN_ROLE_SQL = """
UPDATE guild_config
SET admin_role_id = $2, updated_at = now()
WHERE guild_id = $1
RETURNING guild_id, timezone, announce_channel_id, admin_role_id, player_role_id,
          default_min_games, created_at, updated_at,
          leaderboard_mode, leaderboard_channel_id, leaderboard_scope,
          leaderboard_daily_time, leaderboard_last_posted_on, leaderboard_last_ranking
"""

_UPDATE_DEFAULT_MIN_GAMES_SQL = """
UPDATE guild_config
SET default_min_games = $2, updated_at = now()
WHERE guild_id = $1
RETURNING guild_id, timezone, announce_channel_id, admin_role_id, player_role_id,
          default_min_games, created_at, updated_at,
          leaderboard_mode, leaderboard_channel_id, leaderboard_scope,
          leaderboard_daily_time, leaderboard_last_posted_on, leaderboard_last_ranking
"""

_UPDATE_PLAYER_ROLE_SQL = """
UPDATE guild_config
SET player_role_id = $2, updated_at = now()
WHERE guild_id = $1
RETURNING guild_id, timezone, announce_channel_id, admin_role_id, player_role_id,
          default_min_games, created_at, updated_at,
          leaderboard_mode, leaderboard_channel_id, leaderboard_scope,
          leaderboard_daily_time, leaderboard_last_posted_on, leaderboard_last_ranking
"""

# One guarded UPDATE handles every combination of "which leaderboard fields
# did the caller actually supply" without composing SQL text per call: each
# column's SET clause is a `CASE WHEN <field provided> THEN <new value> ELSE
# <existing column> END`, so the statement itself never changes -- only
# which boolean/value pairs get bound to it (see `set_leaderboard_settings`).
# A plain `COALESCE(new, existing)` sentinel doesn't work here: it treats a
# caller-supplied NULL the same as "not supplied", which would make
# `leaderboard_channel_id` (itself nullable) impossible to explicitly clear
# through this function.
#
# $3/$5/$7/$9 (the "new value" side of each CASE) are each explicitly cast
# rather than left for Postgres to infer from the ELSE branch's column
# reference. Ordinary CASE type resolution isn't the same ambiguity as
# score_requests.py's `$n + INTERVAL` bug (there's no competing operator
# overload here), so this would likely still resolve correctly without a
# cast -- but every one of these parameters can be NULL at bind time
# (`channel_id=None` is a legitimate explicit "clear it", and mode/scope/
# daily_time are all sent as `None` on the branch where the caller didn't
# supply them), and a NULL with no cast has nothing but the CASE's other
# branch to anchor its type to. Casting explicitly removes any dependence
# on that inference succeeding, matching every other value-carrying `$n` in
# this file.
_SET_LEADERBOARD_SETTINGS_SQL = """
UPDATE guild_config
SET leaderboard_mode = CASE WHEN $2 THEN $3::text ELSE leaderboard_mode END,
    leaderboard_channel_id = CASE WHEN $4 THEN $5::bigint ELSE leaderboard_channel_id END,
    leaderboard_scope = CASE WHEN $6 THEN $7::text ELSE leaderboard_scope END,
    leaderboard_daily_time = CASE WHEN $8 THEN $9::time ELSE leaderboard_daily_time END,
    updated_at = now()
WHERE guild_id = $1
RETURNING guild_id, timezone, announce_channel_id, admin_role_id, player_role_id,
          default_min_games, created_at, updated_at,
          leaderboard_mode, leaderboard_channel_id, leaderboard_scope,
          leaderboard_daily_time, leaderboard_last_posted_on, leaderboard_last_ranking
"""

# System-wide (no guild_id): the scheduler needs every `daily`-mode guild in
# one pass, matching `events.claim_due_reminders`'s style.  A guild with no
# channel configured yet is left out -- there's nowhere to post, so it would
# just be dead weight in every scheduler tick's list.
_LIST_DAILY_LEADERBOARD_GUILDS_SQL = """
SELECT guild_id, timezone, announce_channel_id, admin_role_id, player_role_id,
       default_min_games, created_at, updated_at,
       leaderboard_mode, leaderboard_channel_id, leaderboard_scope,
       leaderboard_daily_time, leaderboard_last_posted_on, leaderboard_last_ranking
FROM guild_config
WHERE leaderboard_mode = 'daily' AND leaderboard_channel_id IS NOT NULL
"""

# `IS DISTINCT FROM` (not `<>`, which is NULL-unsafe) so a guild that has
# never posted -- `leaderboard_last_posted_on IS NULL` -- still claims
# successfully the first time.
_CLAIM_DAILY_LEADERBOARD_SQL = """
UPDATE guild_config
SET leaderboard_last_posted_on = $2, updated_at = now()
WHERE guild_id = $1 AND leaderboard_last_posted_on IS DISTINCT FROM $2
RETURNING guild_id, timezone, announce_channel_id, admin_role_id, player_role_id,
          default_min_games, created_at, updated_at,
          leaderboard_mode, leaderboard_channel_id, leaderboard_scope,
          leaderboard_daily_time, leaderboard_last_posted_on, leaderboard_last_ranking
"""

_SET_LEADERBOARD_RANKING_SQL = """
UPDATE guild_config
SET leaderboard_last_ranking = $2::jsonb, updated_at = now()
WHERE guild_id = $1
RETURNING guild_id, timezone, announce_channel_id, admin_role_id, player_role_id,
          default_min_games, created_at, updated_at,
          leaderboard_mode, leaderboard_channel_id, leaderboard_scope,
          leaderboard_daily_time, leaderboard_last_posted_on, leaderboard_last_ranking
"""


def _leaderboard_ranking_json(user_ids: Sequence[int], *, name: str) -> str:
    """Encode an ordered ranking as a JSON array string of validated ids.

    Mirrors `games.py`'s `_score_breakdown_json`: the durable JSONB boundary
    (a plain array of positive bigint user ids) is enforced here, while what
    the ranking or its order *means* is entirely the caller's business.
    """
    ids = [require_id(user_id, name=f"{name}[{i}]") for i, user_id in enumerate(user_ids)]
    return json.dumps(ids, separators=(",", ":"))


def _decode_leaderboard_ranking(value: object) -> tuple[int, ...] | None:
    if value is None:
        return None
    if not isinstance(value, str):  # pragma: no cover -- DB CHECK enforces a JSON array.
        raise RuntimeError("guild_config.leaderboard_last_ranking has invalid data")
    decoded = json.loads(value)
    if not isinstance(decoded, list) or not all(type(v) is int for v in decoded):
        raise RuntimeError("guild_config.leaderboard_last_ranking is not a JSON array of ints")
    return tuple(decoded)


def _row_to_guild_config(row: asyncpg.Record) -> GuildConfig:
    return GuildConfig(
        guild_id=row["guild_id"],
        timezone=row["timezone"],
        announce_channel_id=row["announce_channel_id"],
        admin_role_id=row["admin_role_id"],
        player_role_id=row["player_role_id"],
        default_min_games=row["default_min_games"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        leaderboard_mode=row["leaderboard_mode"],
        leaderboard_channel_id=row["leaderboard_channel_id"],
        leaderboard_scope=row["leaderboard_scope"],
        leaderboard_daily_time=row["leaderboard_daily_time"],
        leaderboard_last_posted_on=row["leaderboard_last_posted_on"],
        leaderboard_last_ranking=_decode_leaderboard_ranking(row["leaderboard_last_ranking"]),
    )


async def ensure_guild(conn: asyncpg.Connection, guild_id: int) -> GuildConfig:
    """Create `guild_config` for `guild_id` if it doesn't exist yet.

    Idempotent: `ON CONFLICT (guild_id) DO NOTHING` never touches an
    existing row, so a second call never bumps `updated_at`.
    """
    require_id(guild_id, name="guild_id")
    await conn.execute(_INSERT_GUILD_IF_MISSING_SQL, guild_id)
    row = await conn.fetchrow(_SELECT_GUILD_SQL, guild_id)
    if row is None:  # pragma: no cover -- insert-or-conflict above guarantees a row exists.
        # Unreachable in practice: nothing in this codebase ever deletes a
        # guild_config row once created.
        raise RuntimeError(f"guild_config row for guild {guild_id} vanished after ensure_guild")
    return _row_to_guild_config(row)


async def get_guild(conn: asyncpg.Connection, guild_id: int) -> GuildConfig | None:
    require_id(guild_id, name="guild_id")
    row = await conn.fetchrow(_SELECT_GUILD_SQL, guild_id)
    return _row_to_guild_config(row) if row is not None else None


async def set_timezone(
    conn: asyncpg.Connection, guild_id: int, timezone: str
) -> GuildConfig | None:
    require_id(guild_id, name="guild_id")
    row = await conn.fetchrow(_UPDATE_TIMEZONE_SQL, guild_id, timezone)
    return _row_to_guild_config(row) if row is not None else None


async def set_announce_channel(
    conn: asyncpg.Connection, guild_id: int, channel_id: int | None
) -> GuildConfig | None:
    require_id(guild_id, name="guild_id")
    require_optional_id(channel_id, name="channel_id")
    row = await conn.fetchrow(_UPDATE_ANNOUNCE_CHANNEL_SQL, guild_id, channel_id)
    return _row_to_guild_config(row) if row is not None else None


async def set_admin_role(
    conn: asyncpg.Connection, guild_id: int, role_id: int | None
) -> GuildConfig | None:
    """Set (or, with `role_id=None`, clear) the configured admin role."""
    require_id(guild_id, name="guild_id")
    require_optional_id(role_id, name="role_id")
    row = await conn.fetchrow(_UPDATE_ADMIN_ROLE_SQL, guild_id, role_id)
    return _row_to_guild_config(row) if row is not None else None


async def set_player_role(
    conn: asyncpg.Connection, guild_id: int, role_id: int | None
) -> GuildConfig | None:
    """Set (or, with ``role_id=None``, clear) the event notification role."""
    require_id(guild_id, name="guild_id")
    require_optional_id(role_id, name="role_id")
    row = await conn.fetchrow(_UPDATE_PLAYER_ROLE_SQL, guild_id, role_id)
    return _row_to_guild_config(row) if row is not None else None


async def set_default_min_games(
    conn: asyncpg.Connection, guild_id: int, min_games: int
) -> GuildConfig | None:
    require_id(guild_id, name="guild_id")
    min_games = require_int(
        min_games, name="min_games", min_value=_MIN_GAMES_MIN, max_value=_MIN_GAMES_MAX
    )
    row = await conn.fetchrow(_UPDATE_DEFAULT_MIN_GAMES_SQL, guild_id, min_games)
    return _row_to_guild_config(row) if row is not None else None


async def set_leaderboard_settings(
    conn: asyncpg.Connection,
    guild_id: int,
    *,
    mode: str | _Unset = _UNSET,
    channel_id: int | None | _Unset = _UNSET,
    scope: str | _Unset = _UNSET,
    daily_time: time | _Unset = _UNSET,
) -> GuildConfig | None:
    """Update only the leaderboard fields the caller actually supplied.

    Every parameter defaults to the `_UNSET` sentinel rather than `None`,
    since `channel_id` is itself a nullable column: a caller must be able to
    explicitly clear it (`channel_id=None`) without that being
    indistinguishable from "leave it alone" (the omitted-argument case). See
    `_SET_LEADERBOARD_SETTINGS_SQL` for how the single statement behind this
    handles "only some fields supplied" without composing SQL per call.
    """
    require_id(guild_id, name="guild_id")

    mode_given = mode is not _UNSET
    if mode_given and (not isinstance(mode, str) or mode not in _LEADERBOARD_MODES):
        raise ValueError(f"mode must be one of {sorted(_LEADERBOARD_MODES)!r}, got {mode!r}")

    channel_given = channel_id is not _UNSET
    if channel_given:
        require_optional_id(channel_id, name="channel_id")  # type: ignore[arg-type]

    scope_given = scope is not _UNSET
    if scope_given and (not isinstance(scope, str) or scope not in _LEADERBOARD_SCOPES):
        raise ValueError(f"scope must be one of {sorted(_LEADERBOARD_SCOPES)!r}, got {scope!r}")

    time_given = daily_time is not _UNSET
    if time_given and type(daily_time) is not time:
        raise ValueError(f"daily_time must be a datetime.time, got {daily_time!r}")

    row = await conn.fetchrow(
        _SET_LEADERBOARD_SETTINGS_SQL,
        guild_id,
        mode_given,
        mode if mode_given else None,
        channel_given,
        channel_id if channel_given else None,
        scope_given,
        scope if scope_given else None,
        time_given,
        daily_time if time_given else None,
    )
    return _row_to_guild_config(row) if row is not None else None


async def list_daily_leaderboard_guilds(conn: asyncpg.Connection) -> list[GuildConfig]:
    """Every guild configured for a daily leaderboard post with a channel set.

    System-wide (no `guild_id` scoping), matching `events.claim_due_reminders`
    / `complete_past_events`: the scheduler sweep checks every guild's local
    time against `leaderboard_daily_time` in one pass.
    """
    rows = await conn.fetch(_LIST_DAILY_LEADERBOARD_GUILDS_SQL)
    return [_row_to_guild_config(row) for row in rows]


async def claim_daily_leaderboard(
    conn: asyncpg.Connection, guild_id: int, local_date: date
) -> GuildConfig | None:
    """At-most-once claim of one guild's daily leaderboard post for `local_date`.

    Guarded by `leaderboard_last_posted_on IS DISTINCT FROM $2`: a restart,
    or a second concurrent scheduler tick that reaches this guild after
    another tick already claimed today, returns `None` instead of posting
    twice. Returns the updated config on a successful claim, so the caller
    has everything it needs (channel, scope, ...) without a second read.
    """
    require_id(guild_id, name="guild_id")
    row = await conn.fetchrow(_CLAIM_DAILY_LEADERBOARD_SQL, guild_id, local_date)
    return _row_to_guild_config(row) if row is not None else None


async def set_leaderboard_ranking(
    conn: asyncpg.Connection, guild_id: int, user_ids: Sequence[int]
) -> GuildConfig | None:
    """Replace the ordered ranking used to compute the next post's movement arrows.

    `user_ids` is the exact posted order, top to bottom; an empty sequence
    is a valid "empty board" and is stored as `[]`, not NULL -- NULL is
    reserved for "no board has ever been posted yet" (see `GuildConfig
    .leaderboard_last_ranking`'s docstring).
    """
    require_id(guild_id, name="guild_id")
    ranking_json = _leaderboard_ranking_json(list(user_ids), name="user_ids")
    row = await conn.fetchrow(_SET_LEADERBOARD_RANKING_SQL, guild_id, ranking_json)
    return _row_to_guild_config(row) if row is not None else None
