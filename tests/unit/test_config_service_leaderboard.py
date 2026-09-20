"""Focused unit coverage for `config_service.set_leaderboard_settings`'s
partial-update behavior (Phase 4 fix; Phase 5 extended it to `mode` too).

`/config leaderboard` previously always submitted a complete set of
leaderboard fields, so an omitted Discord command option silently reset
that field to a default instead of leaving it alone. These tests prove the
fix one layer below the cog: an omitted keyword here must never become an
explicit value forwarded to `guilds.set_leaderboard_settings` -- these use
a mocked repository call (no real Postgres) so they run everywhere, unlike
`tests/integration/test_repo_guilds.py`'s equivalent end-to-end coverage of
the repository's own `_UNSET` sentinel. `mode` follows the exact same
`_UNSET`-sentinel rule as the other three fields now, rather than being a
required keyword.
"""

from __future__ import annotations

from datetime import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from catan_bot.services import config_service
from catan_bot.services.context import Actor
from catan_bot.services.errors import PermissionDeniedError


class _Transaction:
    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Connection:
    def transaction(self) -> _Transaction:
        return _Transaction()


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Pool:
    def __init__(self) -> None:
        self.connection = _Connection()

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


def _actor(*, admin: bool = True) -> Actor:
    return Actor(user_id=1, has_manage_guild=admin, role_ids=frozenset())


async def test_set_leaderboard_settings_with_only_mode_forwards_only_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting only `mode` must reach the repository call with none of
    channel_id/scope/daily_time supplied -- proving a previously configured
    channel, scope, and time all survive untouched, since the repository's
    own `_UNSET` sentinel (its default for each of those parameters) is
    exactly what "leave the stored value alone" means one layer down."""
    pool = _Pool()
    monkeypatch.setattr(config_service.guilds, "ensure_guild", AsyncMock())
    updated = SimpleNamespace(guild_id=7)
    set_settings = AsyncMock(return_value=updated)
    monkeypatch.setattr(config_service.guilds, "set_leaderboard_settings", set_settings)

    result = await config_service.set_leaderboard_settings(
        pool,  # type: ignore[arg-type]
        7,
        _actor(),
        mode="daily",
    )

    assert result is updated
    set_settings.assert_awaited_once_with(pool.connection, 7, mode="daily")


async def test_set_leaderboard_settings_with_only_channel_forwards_only_channel_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mode` is optional at this layer too (Phase 5): a caller that only
    wants to change the channel must reach the repository call without
    `mode`, proving the stored mode survives untouched exactly like the
    other partial-update fields already do."""
    pool = _Pool()
    monkeypatch.setattr(config_service.guilds, "ensure_guild", AsyncMock())
    updated = SimpleNamespace(guild_id=7)
    set_settings = AsyncMock(return_value=updated)
    monkeypatch.setattr(config_service.guilds, "set_leaderboard_settings", set_settings)

    result = await config_service.set_leaderboard_settings(
        pool,  # type: ignore[arg-type]
        7,
        _actor(),
        channel_id=555,
    )

    assert result is updated
    set_settings.assert_awaited_once_with(pool.connection, 7, channel_id=555)


async def test_set_leaderboard_settings_forwards_only_the_fields_actually_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    monkeypatch.setattr(config_service.guilds, "ensure_guild", AsyncMock())
    set_settings = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(config_service.guilds, "set_leaderboard_settings", set_settings)

    await config_service.set_leaderboard_settings(
        pool,  # type: ignore[arg-type]
        7,
        _actor(),
        mode="per_game",
        scope="all_time",
    )

    set_settings.assert_awaited_once_with(pool.connection, 7, mode="per_game", scope="all_time")


async def test_set_leaderboard_settings_can_still_explicitly_clear_the_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`channel_id=None` (a real value, not an omission) must still reach the
    repository call -- distinguishing "clear it" from "leave it alone" is
    the entire reason the `_UNSET` sentinel exists one layer down."""
    pool = _Pool()
    monkeypatch.setattr(config_service.guilds, "ensure_guild", AsyncMock())
    set_settings = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(config_service.guilds, "set_leaderboard_settings", set_settings)

    await config_service.set_leaderboard_settings(
        pool,  # type: ignore[arg-type]
        7,
        _actor(),
        mode="off",
        channel_id=None,
    )

    set_settings.assert_awaited_once_with(pool.connection, 7, mode="off", channel_id=None)


async def test_set_leaderboard_settings_can_forward_every_field_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    monkeypatch.setattr(config_service.guilds, "ensure_guild", AsyncMock())
    set_settings = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(config_service.guilds, "set_leaderboard_settings", set_settings)

    await config_service.set_leaderboard_settings(
        pool,  # type: ignore[arg-type]
        7,
        _actor(),
        mode="daily",
        channel_id=555,
        scope="season",
        daily_time=time(7, 30),
    )

    set_settings.assert_awaited_once_with(
        pool.connection, 7, mode="daily", channel_id=555, scope="season", daily_time=time(7, 30)
    )


async def test_set_leaderboard_settings_requires_manage_guild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    set_settings = AsyncMock()
    monkeypatch.setattr(config_service.guilds, "set_leaderboard_settings", set_settings)

    with pytest.raises(PermissionDeniedError):
        await config_service.set_leaderboard_settings(
            pool,  # type: ignore[arg-type]
            7,
            _actor(admin=False),
            mode="off",
        )

    set_settings.assert_not_awaited()
