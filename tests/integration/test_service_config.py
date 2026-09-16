"""Integration tests for `catan_bot.services.config_service`."""

from __future__ import annotations

import os

import asyncpg
import pytest

from catan_bot.services import config_service
from catan_bot.services.context import Actor
from catan_bot.services.errors import PermissionDeniedError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]


def _admin(user_id: int = 1) -> Actor:
    return Actor(user_id=user_id, has_manage_guild=True, role_ids=frozenset())


def _non_admin_with_role(user_id: int, role_id: int) -> Actor:
    return Actor(user_id=user_id, has_manage_guild=False, role_ids=frozenset({role_id}))


async def test_get_config_creates_row_on_first_use(pool: asyncpg.Pool, guild_id: int) -> None:
    config = await config_service.get_config(pool, guild_id)
    assert config.guild_id == guild_id
    assert config.timezone == "UTC"
    assert config.default_min_games == 2


async def test_set_timezone_by_manage_guild_holder(pool: asyncpg.Pool, guild_id: int) -> None:
    updated = await config_service.set_timezone(pool, guild_id, _admin(), "America/Chicago")
    assert updated.timezone == "America/Chicago"


async def test_set_timezone_rejects_invalid_zone(pool: asyncpg.Pool, guild_id: int) -> None:
    from catan_bot.domain.errors import DomainValidationError

    with pytest.raises(DomainValidationError):
        await config_service.set_timezone(pool, guild_id, _admin(), "Not/A/Zone")


async def test_set_timezone_rejects_admin_role_holder_without_manage_guild(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    """Config commands require Manage Server specifically -- the admin role
    is not enough, so an admin-role holder can't reassign it."""
    await config_service.set_admin_role(pool, guild_id, _admin(), role_id=42)
    actor = _non_admin_with_role(2, role_id=42)

    with pytest.raises(PermissionDeniedError):
        await config_service.set_timezone(pool, guild_id, actor, "America/Chicago")

    unchanged = await config_service.get_config(pool, guild_id)
    assert unchanged.timezone == "UTC"


async def test_set_announce_channel_success_and_clear(pool: asyncpg.Pool, guild_id: int) -> None:
    updated = await config_service.set_announce_channel(pool, guild_id, _admin(), 555)
    assert updated.announce_channel_id == 555

    cleared = await config_service.set_announce_channel(pool, guild_id, _admin(), None)
    assert cleared.announce_channel_id is None


async def test_set_announce_channel_rejects_non_manage_guild(
    pool: asyncpg.Pool, guild_id: int
) -> None:
    actor = Actor(user_id=2, has_manage_guild=False, role_ids=frozenset())
    with pytest.raises(PermissionDeniedError):
        await config_service.set_announce_channel(pool, guild_id, actor, 555)


async def test_set_admin_role_success_and_clear(pool: asyncpg.Pool, guild_id: int) -> None:
    updated = await config_service.set_admin_role(pool, guild_id, _admin(), 999)
    assert updated.admin_role_id == 999

    cleared = await config_service.set_admin_role(pool, guild_id, _admin(), None)
    assert cleared.admin_role_id is None


async def test_set_admin_role_rejects_non_manage_guild(pool: asyncpg.Pool, guild_id: int) -> None:
    actor = Actor(user_id=2, has_manage_guild=False, role_ids=frozenset())
    with pytest.raises(PermissionDeniedError):
        await config_service.set_admin_role(pool, guild_id, actor, 999)


async def test_set_player_role_success_and_clear(pool: asyncpg.Pool, guild_id: int) -> None:
    updated = await config_service.set_player_role(pool, guild_id, _admin(), 999)
    assert updated.player_role_id == 999

    cleared = await config_service.set_player_role(pool, guild_id, _admin(), None)
    assert cleared.player_role_id is None


async def test_set_player_role_rejects_non_manage_guild(pool: asyncpg.Pool, guild_id: int) -> None:
    actor = Actor(user_id=2, has_manage_guild=False, role_ids=frozenset())
    with pytest.raises(PermissionDeniedError):
        await config_service.set_player_role(pool, guild_id, actor, 999)
