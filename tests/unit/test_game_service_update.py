"""Focused coverage for the confirmed-game update service workflow."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.scoring import PlayerScore, ScoreEntry
from catan_bot.domain.validation import ParticipantRef
from catan_bot.services import game_service
from catan_bot.services.context import Actor
from catan_bot.services.errors import ConflictError

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

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


def _actor() -> Actor:
    return Actor(user_id=50, has_manage_guild=True, role_ids=frozenset())


def _original(
    *, winner_id: int = 1, loser_ids: tuple[int, ...] = (2,), target_points: int | None = None
) -> SimpleNamespace:
    game = SimpleNamespace(
        game_id=7,
        guild_id=123,
        season_id=None,
        played_on=date(2026, 9, 13),
        status="confirmed",
        game_type="normal",
        extension_5_6=False,
        scenario=None,
        target_points=target_points,
        played_at=None,
        played_timezone=None,
        revision=3,
    )
    return SimpleNamespace(game=game, winner_id=winner_id, loser_ids=loser_ids, scores=())


@pytest.fixture
def repositories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        game_service.guilds,
        "ensure_guild",
        AsyncMock(return_value=SimpleNamespace(timezone="America/Chicago", admin_role_id=None)),
    )
    monkeypatch.setattr(game_service.seasons, "get_season", AsyncMock(return_value=None))


async def _prepare(
    monkeypatch: pytest.MonkeyPatch,
    original: SimpleNamespace,
    **kwargs: object,
) -> object:
    monkeypatch.setattr(game_service.games, "get_game", AsyncMock(return_value=original))
    args: dict[str, object] = {
        "game_id": 7,
        "winner": None,
        "losers": None,
        "date_text": None,
        "time_text": None,
        "game_type": None,
        "extension_5_6": None,
        "scenario": None,
        "target_points": None,
        "reason": None,
        "now": NOW,
    }
    args.update(kwargs)
    return await game_service.prepare_game_update(_Pool(), 123, _actor(), **args)


@pytest.mark.asyncio
async def test_winner_only_swaps_an_existing_loser_and_keeps_roster(
    repositories: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = await _prepare(
        monkeypatch,
        _original(winner_id=1, loser_ids=(2, 3)),
        winner=ParticipantRef(2, False),
    )
    assert prepared.winner_id == 2
    assert set(prepared.loser_ids) == {1, 3}


@pytest.mark.asyncio
async def test_external_winner_requires_explicit_loser_replacement(
    repositories: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(DomainValidationError, match="explicit full loser list"):
        await _prepare(
            monkeypatch,
            _original(),
            winner=ParticipantRef(9, False),
        )
    prepared = await _prepare(
        monkeypatch,
        _original(),
        winner=ParticipantRef(9, False),
        losers=(ParticipantRef(2, False), ParticipantRef(3, False)),
    )
    assert prepared.winner_id == 9


@pytest.mark.asyncio
async def test_legacy_target_and_empty_reason_are_preserved_as_null(
    repositories: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = await _prepare(monkeypatch, _original(target_points=None))
    assert prepared.target_points_to_store is None
    assert prepared.rules.target_points == 10
    assert prepared.update_reason is None


@pytest.mark.asyncio
async def test_changed_seafarers_rules_require_explicit_target(
    repositories: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(DomainValidationError, match="require a target"):
        await _prepare(monkeypatch, _original(), game_type="seafarers")


@pytest.mark.asyncio
async def test_submit_rechecks_admin_and_maps_stale_result(
    repositories: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _original()
    prepared = await _prepare(monkeypatch, original)
    monkeypatch.setattr(game_service.games, "lock_game", AsyncMock(return_value=original))
    monkeypatch.setattr(
        game_service.games,
        "update_confirmed_game",
        AsyncMock(return_value="stale"),
    )
    prepared = replace(prepared, played_on=date(2026, 9, 12))
    with pytest.raises(ConflictError, match="changed by someone else"):
        await game_service.submit_game_update(
            _Pool(), 123, _actor(), prepared, scores=None, now=NOW
        )


@pytest.mark.asyncio
async def test_submit_rejects_noop_without_repository_update(
    repositories: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _original()
    prepared = await _prepare(monkeypatch, original)
    monkeypatch.setattr(game_service.games, "lock_game", AsyncMock(return_value=original))
    update = AsyncMock()
    monkeypatch.setattr(game_service.games, "update_confirmed_game", update)
    with pytest.raises(ConflictError, match="No changes"):
        await game_service.submit_game_update(
            _Pool(), 123, _actor(), prepared, scores=None, now=NOW
        )
    update.assert_not_awaited()


def _normal_score(user_id: int, *, settlements: int, vp_cards: int = 0) -> PlayerScore:
    return PlayerScore(
        user_id=user_id,
        total_points=settlements + vp_cards,
        breakdown=(
            ScoreEntry("settlements", settlements),
            ScoreEntry("cities", 0),
            ScoreEntry("longest_road", 0),
            ScoreEntry("largest_army", 0),
            ScoreEntry("vp_cards", vp_cards),
        ),
    )


def test_same_scores_ignores_player_and_breakdown_order() -> None:
    first = (
        _normal_score(1, settlements=4),
        _normal_score(2, settlements=3),
    )
    second = (
        PlayerScore(
            user_id=2,
            total_points=3,
            breakdown=tuple(reversed(first[1].breakdown)),
        ),
        PlayerScore(
            user_id=1,
            total_points=4,
            breakdown=tuple(reversed(first[0].breakdown)),
        ),
    )

    assert game_service._same_scores(first, second)


def test_same_scores_preserves_null_vs_explicit_zero() -> None:
    absent: tuple[PlayerScore, ...] = ()
    explicit_zero = (
        _normal_score(1, settlements=0),
        _normal_score(2, settlements=0),
    )

    assert not game_service._same_scores(absent, explicit_zero)
    assert game_service._same_scores(explicit_zero, tuple(reversed(explicit_zero)))


def test_preserve_local_time_delegates_timezone_work_to_domain_dates() -> None:
    original = _original()
    original.game.played_at = datetime(2026, 9, 13, 0, 30, tzinfo=UTC)
    original.game.played_timezone = "America/Chicago"

    preserved, timezone = game_service._preserve_local_time(
        original, date(2026, 9, 14)
    )

    assert preserved == datetime(2026, 9, 15, 0, 30, tzinfo=UTC)
    assert timezone == "America/Chicago"
