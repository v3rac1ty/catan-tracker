"""Unit coverage for the two-phase game-report service workflow."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.scoring import PlayerScore, ScoreEntry
from catan_bot.domain.validation import ParticipantRef
from catan_bot.services import game_service
from catan_bot.services.context import Actor
from catan_bot.services.errors import NotFoundError, PermissionDeniedError
from catan_bot.services.results import PreparedGameReport

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


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
        self.acquire_calls = 0

    def acquire(self) -> _Acquire:
        self.acquire_calls += 1
        return _Acquire(self.connection)


def _actor(user_id: int = 10) -> Actor:
    return Actor(user_id=user_id, has_manage_guild=False, role_ids=frozenset())


def _refs(*user_ids: int) -> tuple[ParticipantRef, ...]:
    return tuple(ParticipantRef(user_id=user_id, is_bot=False) for user_id in user_ids)


def _config(timezone: str = "America/Chicago") -> SimpleNamespace:
    return SimpleNamespace(timezone=timezone)


@pytest.fixture
def mocked_repositories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(game_service.guilds, "ensure_guild", AsyncMock(return_value=_config()))
    monkeypatch.setattr(game_service.seasons, "get_active_season", AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_prepare_resolves_local_time_without_creating_players_or_games(
    mocked_repositories: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _Pool()
    ensure_players = AsyncMock()
    create_game = AsyncMock()
    monkeypatch.setattr(game_service.players, "ensure_players", ensure_players)
    monkeypatch.setattr(game_service.games, "create_game", create_game)

    prepared = await game_service.prepare_game_report(
        pool,
        123,
        _actor(),
        winner=_refs(1)[0],
        losers=list(_refs(2)),
        date_text="2026-09-13",
        time_text="7:30pm",
        now=NOW,
    )

    assert prepared.played_on == date(2026, 9, 13)
    assert prepared.played_at is not None
    assert prepared.played_at.utcoffset() == datetime(2026, 1, 1, tzinfo=UTC).utcoffset()
    assert prepared.played_timezone == "America/Chicago"
    ensure_players.assert_not_awaited()
    create_game.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_auto_enables_extension_for_five_players(
    mocked_repositories: None,
) -> None:
    pool = _Pool()
    refs = _refs(1, 2, 3, 4, 5)
    prepared = await game_service.prepare_game_report(
        pool,
        123,
        _actor(),
        winner=refs[0],
        losers=refs[1:],
        date_text=None,
        time_text=None,
        now=NOW,
    )
    assert prepared.rules.extension_5_6 is True

    with pytest.raises(DomainValidationError, match="require the 5–6 Player Extension"):
        await game_service.prepare_game_report(
            pool,
            123,
            _actor(),
            winner=refs[0],
            losers=refs[1:],
            date_text=None,
            time_text=None,
            now=NOW,
            extension_5_6=False,
        )


@pytest.mark.asyncio
async def test_prepare_keeps_resolved_date_when_submission_clock_moves_forward(
    mocked_repositories: None,
) -> None:
    pool = _Pool()
    prepared = await game_service.prepare_game_report(
        pool,
        123,
        _actor(),
        winner=_refs(1)[0],
        losers=list(_refs(2)),
        date_text=None,
        time_text=None,
        now=datetime(2026, 9, 15, 4, 30, tzinfo=UTC),
    )
    assert prepared.played_on == date(2026, 9, 14)


@pytest.mark.asyncio
async def test_submit_rejects_wrong_reporter_before_acquiring_connection(
    mocked_repositories: None,
) -> None:
    pool = _Pool()
    prepared = PreparedGameReport(
        guild_id=123,
        reporter_id=10,
        winner_id=1,
        loser_ids=(2,),
        played_on=date(2026, 9, 13),
        played_at=None,
        played_timezone=None,
        rules=game_service.build_rules("normal", player_count=2),
    )

    with pytest.raises(PermissionDeniedError):
        await game_service.submit_game_report(
            pool, 123, _actor(11), prepared, scores=None, now=NOW
        )
    assert pool.acquire_calls == 0


@pytest.mark.asyncio
async def test_submit_empty_scores_persists_null_score_sheet(
    mocked_repositories: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _Pool()
    prepared = await game_service.prepare_game_report(
        pool,
        123,
        _actor(),
        winner=_refs(1)[0],
        losers=list(_refs(2)),
        date_text=None,
        time_text=None,
        now=NOW,
    )
    created = SimpleNamespace(game_id=7)
    create_game = AsyncMock(return_value=created)
    ensure_players = AsyncMock()
    monkeypatch.setattr(game_service.games, "create_game", create_game)
    monkeypatch.setattr(game_service.players, "ensure_players", ensure_players)

    result = await game_service.submit_game_report(
        pool, 123, _actor(), prepared, scores=None, now=NOW
    )

    assert result.scores == ()
    ensure_players.assert_awaited_once_with(pool.connection, 123, (1, 2))
    assert create_game.await_args.kwargs["scores"] == ()


@pytest.mark.asyncio
async def test_submit_rejects_partial_scores_without_acquiring_connection(
    mocked_repositories: None,
) -> None:
    pool = _Pool()
    prepared = PreparedGameReport(
        guild_id=123,
        reporter_id=10,
        winner_id=1,
        loser_ids=(2,),
        played_on=date(2026, 9, 13),
        played_at=None,
        played_timezone=None,
        rules=game_service.build_rules("normal", player_count=2),
    )
    partial = [PlayerScore(1, 10, (ScoreEntry("settlements", 10),))]

    with pytest.raises(DomainValidationError, match="every participant"):
        await game_service.submit_game_report(
            pool, 123, _actor(), prepared, scores=partial, now=NOW
        )
    assert pool.acquire_calls == 0


@pytest.mark.asyncio
async def test_submit_accepts_a_complete_score_table(
    mocked_repositories: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = _Pool()
    prepared = PreparedGameReport(
        guild_id=123,
        reporter_id=10,
        winner_id=1,
        loser_ids=(2,),
        played_on=date(2026, 9, 13),
        played_at=None,
        played_timezone=None,
        rules=game_service.build_rules("normal", player_count=2),
    )
    sources = (
        ("settlements", 3),
        ("cities", 4),
        ("longest_road", 2),
        ("largest_army", 0),
        ("vp_cards", 1),
    )
    winner_score = PlayerScore(
        1, 10, tuple(ScoreEntry(key, points) for key, points in sources)
    )
    loser_score = PlayerScore(
        2,
        3,
        tuple(
            ScoreEntry(key, points if key == "settlements" else 0)
            for key, points in sources
        ),
    )
    monkeypatch.setattr(game_service.players, "ensure_players", AsyncMock())
    monkeypatch.setattr(
        game_service.games, "create_game", AsyncMock(return_value=SimpleNamespace(game_id=8))
    )

    result = await game_service.submit_game_report(
        pool, 123, _actor(), prepared, scores=[winner_score, loser_score], now=NOW
    )

    assert result.scores == (winner_score, loser_score)


@pytest.mark.asyncio
async def test_get_game_raises_not_found_for_unknown_game(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _Pool()
    get_game = AsyncMock(return_value=None)
    monkeypatch.setattr(game_service.games, "get_game", get_game)

    with pytest.raises(NotFoundError):
        await game_service.get_game(pool, 123, 99)
    get_game.assert_awaited_once_with(pool.connection, 123, 99)
