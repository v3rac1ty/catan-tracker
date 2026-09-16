"""`guild_config` repository.

Every SQL statement here is a module-level string constant, bound exactly
once, and every value reaches Postgres only as a `$n` argument -- see
CLAUDE.md and `tests/static/sql_guard.py` for the enforced rules.
"""

from __future__ import annotations

import asyncpg

from catan_bot.db.models import GuildConfig
from catan_bot.db.repositories._params import require_id, require_int, require_optional_id

# `min_games` is a small, human-set threshold (never a computed/user-count
# value), so 100 is a generous, sane ceiling rather than the column's real
# (much larger) INT range.
_MIN_GAMES_MIN = 1
_MIN_GAMES_MAX = 100

_INSERT_GUILD_IF_MISSING_SQL = """
INSERT INTO guild_config (guild_id)
VALUES ($1)
ON CONFLICT (guild_id) DO NOTHING
"""

_SELECT_GUILD_SQL = """
SELECT guild_id, timezone, announce_channel_id, admin_role_id, player_role_id,
       default_min_games,
       created_at, updated_at
FROM guild_config
WHERE guild_id = $1
"""

_UPDATE_TIMEZONE_SQL = """
UPDATE guild_config
SET timezone = $2, updated_at = now()
WHERE guild_id = $1
RETURNING guild_id, timezone, announce_channel_id, admin_role_id, player_role_id,
          default_min_games, created_at, updated_at
"""

_UPDATE_ANNOUNCE_CHANNEL_SQL = """
UPDATE guild_config
SET announce_channel_id = $2, updated_at = now()
WHERE guild_id = $1
RETURNING guild_id, timezone, announce_channel_id, admin_role_id, player_role_id,
          default_min_games, created_at, updated_at
"""

_UPDATE_ADMIN_ROLE_SQL = """
UPDATE guild_config
SET admin_role_id = $2, updated_at = now()
WHERE guild_id = $1
RETURNING guild_id, timezone, announce_channel_id, admin_role_id, player_role_id,
          default_min_games, created_at, updated_at
"""

_UPDATE_DEFAULT_MIN_GAMES_SQL = """
UPDATE guild_config
SET default_min_games = $2, updated_at = now()
WHERE guild_id = $1
RETURNING guild_id, timezone, announce_channel_id, admin_role_id, player_role_id,
          default_min_games, created_at, updated_at
"""

_UPDATE_PLAYER_ROLE_SQL = """
UPDATE guild_config
SET player_role_id = $2, updated_at = now()
WHERE guild_id = $1
RETURNING guild_id, timezone, announce_channel_id, admin_role_id, player_role_id,
          default_min_games, created_at, updated_at
"""


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
