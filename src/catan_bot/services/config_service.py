"""Guild configuration: timezone, announce channel, admin role.

Every mutator here requires Manage Server (`context.require_manage_guild`)
-- not the configured admin role -- so an admin-role holder can never
reassign that role to someone else (or themselves).
"""

from __future__ import annotations

from datetime import time

import asyncpg

from catan_bot.db.models import GuildConfig
from catan_bot.db.repositories import guilds
from catan_bot.domain.dates import validate_timezone
from catan_bot.services.context import Actor, require_manage_guild


async def get_config(pool: asyncpg.Pool, guild_id: int) -> GuildConfig:
    """The guild's config, creating its `guild_config` row on first use."""
    async with pool.acquire() as conn:
        return await guilds.ensure_guild(conn, guild_id)


async def set_timezone(
    pool: asyncpg.Pool, guild_id: int, actor: Actor, tz_text: str
) -> GuildConfig:
    require_manage_guild(actor)
    timezone = validate_timezone(tz_text)
    async with pool.acquire() as conn, conn.transaction():
        await guilds.ensure_guild(conn, guild_id)
        updated = await guilds.set_timezone(conn, guild_id, timezone)
    if updated is None:  # pragma: no cover -- ensure_guild above guarantees the row exists.
        raise RuntimeError(f"guild_config row for guild {guild_id} vanished during set_timezone")
    return updated


async def set_announce_channel(
    pool: asyncpg.Pool, guild_id: int, actor: Actor, channel_id: int | None
) -> GuildConfig:
    require_manage_guild(actor)
    async with pool.acquire() as conn, conn.transaction():
        await guilds.ensure_guild(conn, guild_id)
        updated = await guilds.set_announce_channel(conn, guild_id, channel_id)
    if updated is None:  # pragma: no cover -- ensure_guild above guarantees the row exists.
        raise RuntimeError(
            f"guild_config row for guild {guild_id} vanished during set_announce_channel"
        )
    return updated


async def set_admin_role(
    pool: asyncpg.Pool, guild_id: int, actor: Actor, role_id: int | None
) -> GuildConfig:
    require_manage_guild(actor)
    async with pool.acquire() as conn, conn.transaction():
        await guilds.ensure_guild(conn, guild_id)
        updated = await guilds.set_admin_role(conn, guild_id, role_id)
    if updated is None:  # pragma: no cover -- ensure_guild above guarantees the row exists.
        raise RuntimeError(f"guild_config row for guild {guild_id} vanished during set_admin_role")
    return updated


async def set_player_role(
    pool: asyncpg.Pool, guild_id: int, actor: Actor, role_id: int | None
) -> GuildConfig:
    """Configure (or clear) the role mentioned for event notifications.

    Like the other ``/config`` mutators, this is deliberately restricted to
    Manage Server rather than the configured admin role.
    """
    require_manage_guild(actor)
    async with pool.acquire() as conn, conn.transaction():
        await guilds.ensure_guild(conn, guild_id)
        updated = await guilds.set_player_role(conn, guild_id, role_id)
    if updated is None:  # pragma: no cover -- ensure_guild above guarantees the row exists.
        raise RuntimeError(f"guild_config row for guild {guild_id} vanished during set_player_role")
    return updated


async def set_leaderboard_settings(
    pool: asyncpg.Pool,
    guild_id: int,
    actor: Actor,
    *,
    mode: str,
    channel_id: int | None,
    scope: str,
    daily_time: time,
) -> GuildConfig:
    """Replace every recurring-leaderboard setting in one call (`/config leaderboard`).

    Unlike the repository layer's `guilds.set_leaderboard_settings` (which
    supports a partial update, via its own `_UNSET` sentinel, for callers
    that only want to change one field), `/config leaderboard` is a single
    command that always submits a complete set of values: the cog resolves
    `scope`'s and `daily_time`'s defaults, and `channel_id`'s
    announce-channel fallback, before this is ever called. There is
    therefore nothing partial to represent at this layer -- every field
    below is always explicitly supplied to the repository call.
    """
    require_manage_guild(actor)
    async with pool.acquire() as conn, conn.transaction():
        await guilds.ensure_guild(conn, guild_id)
        updated = await guilds.set_leaderboard_settings(
            conn, guild_id, mode=mode, channel_id=channel_id, scope=scope, daily_time=daily_time
        )
    if updated is None:  # pragma: no cover -- ensure_guild above guarantees the row exists.
        raise RuntimeError(
            f"guild_config row for guild {guild_id} vanished during set_leaderboard_settings"
        )
    return updated
