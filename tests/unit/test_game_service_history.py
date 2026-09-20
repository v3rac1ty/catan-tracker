"""Unit coverage for `game_service.game_history`'s `include_voided` option."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from catan_bot.services import game_service


class _Connection:
    pass


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


@pytest.fixture
def repositories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(game_service.games, "list_recent_games", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        game_service.games, "list_recent_games_for_player", AsyncMock(return_value=[])
    )


async def test_game_history_defaults_include_voided_to_false(repositories: None) -> None:
    pool = _Pool()

    await game_service.game_history(pool, 20, user_id=None, limit=10)  # type: ignore[arg-type]

    game_service.games.list_recent_games.assert_awaited_once_with(
        pool.connection, 20, 10, include_voided=False
    )


async def test_game_history_threads_include_voided_true(repositories: None) -> None:
    pool = _Pool()

    await game_service.game_history(
        pool,
        20,
        user_id=None,
        limit=10,
        include_voided=True,  # type: ignore[arg-type]
    )

    game_service.games.list_recent_games.assert_awaited_once_with(
        pool.connection, 20, 10, include_voided=True
    )


async def test_game_history_for_a_player_threads_include_voided(repositories: None) -> None:
    pool = _Pool()

    await game_service.game_history(
        pool,
        20,
        user_id=7,
        limit=10,
        include_voided=True,  # type: ignore[arg-type]
    )

    game_service.games.list_recent_games_for_player.assert_awaited_once_with(
        pool.connection, 20, 7, 10, include_voided=True
    )
    game_service.games.list_recent_games.assert_not_awaited()
