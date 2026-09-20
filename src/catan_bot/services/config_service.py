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


class _Unset:
    """Sentinel meaning "the `/config leaderboard` cog didn't pass this
    keyword at all," distinct from an explicit `None` (which clears the
    nullable `channel_id`).

    Mirrors `db.repositories.guilds._Unset` one layer up, rather than
    importing that module's private sentinel directly -- this layer only
    calls into a repository's public functions, never reaches into its
    internals. See `set_leaderboard_settings` below for how a caller's
    "did they even pass this" decision is forwarded, one field at a time,
    to the repository's own sentinel.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover -- debugging aid only.
        return "UNSET"


_UNSET = _Unset()


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
    mode: str | _Unset = _UNSET,
    channel_id: int | None | _Unset = _UNSET,
    scope: str | _Unset = _UNSET,
    daily_time: time | _Unset = _UNSET,
) -> GuildConfig:
    """Update only whichever leaderboard fields were supplied (`/config leaderboard`).

    `mode`, `channel_id`, `scope`, and `daily_time` are all optional Discord
    command options, and an omitted one must leave that field's stored
    value exactly as it was -- not silently reset it to a default -- the
    same "omitted means unchanged" rule `/game update`'s optional fields
    already follow. The cog signals "the user didn't pass this option" by
    leaving the matching keyword argument out of its call here entirely
    (falling back to this function's own `_UNSET` default); this function
    then forwards that same per-field "was it supplied" decision straight
    through to the repository call, via its own `_UNSET` sentinel
    (`guilds.set_leaderboard_settings`).

    Clearing the channel back to unset is still reachable despite `None`
    being a valid *value* for `channel_id` here (not just "omitted"): the
    cog's `clear_channel: bool` option calls this with `channel_id=None`
    explicitly, rather than leaving it out.
    """
    require_manage_guild(actor)
    kwargs: dict[str, object] = {}
    if mode is not _UNSET:
        kwargs["mode"] = mode
    if channel_id is not _UNSET:
        kwargs["channel_id"] = channel_id
    if scope is not _UNSET:
        kwargs["scope"] = scope
    if daily_time is not _UNSET:
        kwargs["daily_time"] = daily_time
    async with pool.acquire() as conn, conn.transaction():
        await guilds.ensure_guild(conn, guild_id)
        updated = await guilds.set_leaderboard_settings(conn, guild_id, **kwargs)
    if updated is None:  # pragma: no cover -- ensure_guild above guarantees the row exists.
        raise RuntimeError(
            f"guild_config row for guild {guild_id} vanished during set_leaderboard_settings"
        )
    return updated
