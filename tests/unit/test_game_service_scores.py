"""Unit coverage for Phase 2's per-player DM score-collection service functions:
`open_score_collection`, `record_score_request_delivery`, `record_player_score`,
`clear_player_score`, `score_collection_status`, `find_open_score_request_game_id`,
`get_game_for_player`, Phase 5's `confirm_preflight`, and Phase 6's `due_score_prompts`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.scoring import PlayerScore, ScoreEntry
from catan_bot.services import game_service
from catan_bot.services.context import Actor
from catan_bot.services.errors import ConflictError, NotFoundError, PermissionDeniedError

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


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


def _game(
    *,
    status: str = "pending",
    winner_id: int = 1,
    loser_ids: tuple[int, ...] = (2,),
    scores: tuple[PlayerScore, ...] = (),
    reported_by: int = 999,
) -> SimpleNamespace:
    game = SimpleNamespace(
        game_id=7,
        game_type="normal",
        extension_5_6=False,
        scenario=None,
        target_points=10,
        status=status,
        reported_by=reported_by,
    )
    return SimpleNamespace(game=game, winner_id=winner_id, loser_ids=loser_ids, scores=scores)


def _score(user_id: int, *, longest_road: int = 0, settlements: int = 8) -> PlayerScore:
    return PlayerScore(
        user_id=user_id,
        total_points=settlements + longest_road,
        breakdown=(
            ScoreEntry("settlements", settlements),
            ScoreEntry("cities", 0),
            ScoreEntry("longest_road", longest_road),
            ScoreEntry("largest_army", 0),
            ScoreEntry("vp_cards", 0),
        ),
    )


# ---------------------------------------------------------------------------
# open_score_collection / record_score_request_delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_score_collection_delegates_to_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    create = AsyncMock()
    monkeypatch.setattr(game_service.score_requests, "create_score_requests", create)

    await game_service.open_score_collection(pool, 123, 7, (1, 2, 3), NOW)

    create.assert_awaited_once_with(pool.connection, 123, 7, (1, 2, 3), NOW)


@pytest.mark.asyncio
async def test_record_score_request_delivery_marks_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    mark_delivered = AsyncMock()
    monkeypatch.setattr(game_service.score_requests, "mark_delivered", mark_delivered)

    await game_service.record_score_request_delivery(
        pool, 123, 7, 1, channel_id=555, message_id=999, delivered=True
    )

    mark_delivered.assert_awaited_once_with(pool.connection, 123, 7, 1, 555, 999)


@pytest.mark.asyncio
async def test_record_score_request_delivery_marks_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    mark_blocked = AsyncMock()
    monkeypatch.setattr(game_service.score_requests, "mark_blocked", mark_blocked)

    await game_service.record_score_request_delivery(
        pool, 123, 7, 1, channel_id=None, message_id=None, delivered=False
    )

    mark_blocked.assert_awaited_once_with(pool.connection, 123, 7, 1)


@pytest.mark.asyncio
async def test_record_score_request_delivery_requires_ids_when_delivered() -> None:
    pool = _Pool()
    with pytest.raises(ValueError, match="channel_id and message_id are required"):
        await game_service.record_score_request_delivery(
            pool, 123, 7, 1, channel_id=None, message_id=None, delivered=True
        )


# ---------------------------------------------------------------------------
# record_player_score
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_player_score_builds_validates_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    monkeypatch.setattr(game_service.games, "lock_game", AsyncMock(return_value=_game()))
    set_score = AsyncMock(return_value=True)
    monkeypatch.setattr(game_service.games, "set_player_score", set_score)
    mark_submitted = AsyncMock()
    monkeypatch.setattr(game_service.score_requests, "mark_submitted", mark_submitted)
    final = _game(scores=(_score(1),))
    monkeypatch.setattr(game_service.games, "get_game", AsyncMock(return_value=final))

    result = await game_service.record_player_score(
        pool,
        123,
        7,
        1,
        numeric={"settlements": 8, "cities": 0, "vp_cards": 0},
        awards=["longest_road"],
        now=NOW,
    )

    assert result is final
    saved_score = set_score.await_args.args[-1]
    assert saved_score.user_id == 1
    assert saved_score.total_points == 10  # 8 settlements + 2 longest_road
    mark_submitted.assert_awaited_once_with(pool.connection, 123, 7, 1, NOW)


@pytest.mark.asyncio
async def test_record_player_score_rejects_non_participant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    monkeypatch.setattr(game_service.games, "lock_game", AsyncMock(return_value=_game()))
    set_score = AsyncMock()
    monkeypatch.setattr(game_service.games, "set_player_score", set_score)

    with pytest.raises(PermissionDeniedError, match="participant"):
        await game_service.record_player_score(
            pool,
            123,
            7,
            99,
            numeric={"settlements": 8, "cities": 0, "vp_cards": 0},
            awards=[],
            now=NOW,
        )
    set_score.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "voided"])
async def test_record_player_score_rejects_closed_game(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    pool = _Pool()
    monkeypatch.setattr(
        game_service.games, "lock_game", AsyncMock(return_value=_game(status=status))
    )

    with pytest.raises(ConflictError, match="no longer accepting"):
        await game_service.record_player_score(
            pool,
            123,
            7,
            1,
            numeric={"settlements": 8, "cities": 0, "vp_cards": 0},
            awards=[],
            now=NOW,
        )


@pytest.mark.asyncio
async def test_record_player_score_missing_game_raises_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    monkeypatch.setattr(game_service.games, "lock_game", AsyncMock(return_value=None))

    with pytest.raises(NotFoundError):
        await game_service.record_player_score(
            pool,
            123,
            7,
            1,
            numeric={"settlements": 8, "cities": 0, "vp_cards": 0},
            awards=[],
            now=NOW,
        )


@pytest.mark.asyncio
async def test_record_player_score_detects_exclusive_award_conflict_against_stored_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Player 2 already claimed Longest Road; player 1 trying to claim it too
    must fail with a message naming the conflicting award, before any write."""
    pool = _Pool()
    existing = _game(scores=(_score(2, longest_road=2),))
    monkeypatch.setattr(game_service.games, "lock_game", AsyncMock(return_value=existing))
    set_score = AsyncMock()
    monkeypatch.setattr(game_service.games, "set_player_score", set_score)

    with pytest.raises(DomainValidationError, match="Longest Road"):
        await game_service.record_player_score(
            pool,
            123,
            7,
            1,
            numeric={"settlements": 8, "cities": 0, "vp_cards": 0},
            awards=["longest_road"],
            now=NOW,
        )
    set_score.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_player_score_resubmission_replaces_only_that_players_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-validating against the whole set must exclude the submitter's own
    previous row, or a player fixing a typo would conflict with themselves."""
    pool = _Pool()
    existing = _game(scores=(_score(1, longest_road=2),))
    monkeypatch.setattr(game_service.games, "lock_game", AsyncMock(return_value=existing))
    set_score = AsyncMock(return_value=True)
    monkeypatch.setattr(game_service.games, "set_player_score", set_score)
    monkeypatch.setattr(game_service.score_requests, "mark_submitted", AsyncMock())
    monkeypatch.setattr(game_service.games, "get_game", AsyncMock(return_value=existing))

    await game_service.record_player_score(
        pool,
        123,
        7,
        1,
        numeric={"settlements": 9, "cities": 0, "vp_cards": 0},
        awards=["longest_road"],
        now=NOW,
    )

    set_score.assert_awaited_once()


# ---------------------------------------------------------------------------
# clear_player_score
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_player_score_writes_none_and_returns_refreshed_game(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    monkeypatch.setattr(
        game_service.games, "lock_game", AsyncMock(return_value=_game(scores=(_score(1),)))
    )
    set_score = AsyncMock(return_value=True)
    monkeypatch.setattr(game_service.games, "set_player_score", set_score)
    cleared = _game()
    monkeypatch.setattr(game_service.games, "get_game", AsyncMock(return_value=cleared))

    result = await game_service.clear_player_score(pool, 123, 7, 1, NOW)

    assert result is cleared
    set_score.assert_awaited_once_with(pool.connection, 123, 7, 1, None)


@pytest.mark.asyncio
async def test_clear_player_score_rejects_non_participant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    monkeypatch.setattr(game_service.games, "lock_game", AsyncMock(return_value=_game()))

    with pytest.raises(PermissionDeniedError):
        await game_service.clear_player_score(pool, 123, 7, 99, NOW)


# ---------------------------------------------------------------------------
# score_collection_status / find_open_score_request_game_id / get_game_for_player
# ---------------------------------------------------------------------------


def _request(user_id: int, *, status: str = "pending", submitted: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id,
        delivery_status=status,
        submitted_at=NOW if submitted else None,
    )


@pytest.mark.asyncio
async def test_score_collection_status_bundles_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _Pool()
    game = _game(winner_id=1, loser_ids=(2, 3))
    monkeypatch.setattr(game_service.games, "get_game", AsyncMock(return_value=game))
    requests = [
        _request(1, submitted=True),
        _request(2, status="blocked"),
        _request(3, status="pending"),
    ]
    monkeypatch.setattr(
        game_service.score_requests, "list_score_requests", AsyncMock(return_value=requests)
    )

    status = await game_service.score_collection_status(pool, 123, 7)

    assert status.submitted_ids == (1,)
    assert status.outstanding_ids == (2, 3)
    assert status.blocked_ids == (2,)
    assert status.complete is False


@pytest.mark.asyncio
async def test_score_collection_status_complete_when_every_request_submitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    monkeypatch.setattr(game_service.games, "get_game", AsyncMock(return_value=_game()))
    monkeypatch.setattr(
        game_service.score_requests,
        "list_score_requests",
        AsyncMock(return_value=[_request(1, submitted=True), _request(2, submitted=True)]),
    )

    status = await game_service.score_collection_status(pool, 123, 7)

    assert status.complete is True
    assert status.outstanding_ids == ()


@pytest.mark.asyncio
async def test_score_collection_status_missing_game_raises_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    monkeypatch.setattr(game_service.games, "get_game", AsyncMock(return_value=None))

    with pytest.raises(NotFoundError):
        await game_service.score_collection_status(pool, 123, 999)


@pytest.mark.asyncio
async def test_find_open_score_request_game_id_returns_none_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    monkeypatch.setattr(
        game_service.score_requests, "get_latest_open_request", AsyncMock(return_value=None)
    )

    assert await game_service.find_open_score_request_game_id(pool, 123, 1) is None


@pytest.mark.asyncio
async def test_find_open_score_request_game_id_returns_the_request_game(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    monkeypatch.setattr(
        game_service.score_requests,
        "get_latest_open_request",
        AsyncMock(return_value=SimpleNamespace(game_id=42)),
    )

    assert await game_service.find_open_score_request_game_id(pool, 123, 1) == 42


@pytest.mark.asyncio
async def test_get_game_for_player_returns_game_for_a_participant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    game = _game()
    monkeypatch.setattr(game_service.games, "get_game", AsyncMock(return_value=game))

    result = await game_service.get_game_for_player(pool, 123, 7, 1)
    assert result is game


@pytest.mark.asyncio
async def test_get_game_for_player_rejects_non_participant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    monkeypatch.setattr(game_service.games, "get_game", AsyncMock(return_value=_game()))

    with pytest.raises(PermissionDeniedError):
        await game_service.get_game_for_player(pool, 123, 7, 99)


# ---------------------------------------------------------------------------
# confirm_preflight (Phase 5): the pre-dialog permission check ahead of
# `views/game_confirm.py`'s "confirm anyway" second step. Deliberately
# mirrors `db.repositories.games.confirm_game`'s own classification order
# (not found -> not pending -> reporter -> not a participant).
# ---------------------------------------------------------------------------


def _actor(user_id: int) -> Actor:
    return Actor(user_id=user_id, has_manage_guild=False, role_ids=frozenset())


@pytest.mark.asyncio
async def test_confirm_preflight_returns_status_for_an_eligible_participant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    game = _game(winner_id=1, loser_ids=(2, 3), reported_by=9)
    monkeypatch.setattr(game_service.games, "get_game", AsyncMock(return_value=game))
    monkeypatch.setattr(
        game_service.score_requests,
        "list_score_requests",
        AsyncMock(
            return_value=[
                _request(1, submitted=True),
                _request(2, submitted=True),
                _request(3),
            ]
        ),
    )

    status = await game_service.confirm_preflight(pool, 123, 7, _actor(2))

    assert status.complete is False
    assert status.outstanding_ids == (3,)


@pytest.mark.asyncio
async def test_confirm_preflight_returns_complete_status_when_every_row_is_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    game = _game(winner_id=1, loser_ids=(2,), reported_by=9)
    monkeypatch.setattr(game_service.games, "get_game", AsyncMock(return_value=game))
    monkeypatch.setattr(
        game_service.score_requests,
        "list_score_requests",
        AsyncMock(return_value=[_request(1, submitted=True), _request(2, submitted=True)]),
    )

    status = await game_service.confirm_preflight(pool, 123, 7, _actor(2))

    assert status.complete is True


@pytest.mark.asyncio
async def test_confirm_preflight_rejects_the_reporter(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _Pool()
    game = _game(winner_id=1, loser_ids=(2,), reported_by=1)
    monkeypatch.setattr(game_service.games, "get_game", AsyncMock(return_value=game))
    monkeypatch.setattr(
        game_service.score_requests, "list_score_requests", AsyncMock(return_value=[])
    )

    with pytest.raises(PermissionDeniedError, match="reported this game"):
        await game_service.confirm_preflight(pool, 123, 7, _actor(1))


@pytest.mark.asyncio
async def test_confirm_preflight_rejects_a_non_participant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    game = _game(winner_id=1, loser_ids=(2,), reported_by=9)
    monkeypatch.setattr(game_service.games, "get_game", AsyncMock(return_value=game))
    monkeypatch.setattr(
        game_service.score_requests, "list_score_requests", AsyncMock(return_value=[])
    )

    with pytest.raises(PermissionDeniedError, match="Only a player"):
        await game_service.confirm_preflight(pool, 123, 7, _actor(99))


@pytest.mark.asyncio
async def test_confirm_preflight_rejects_a_non_pending_game(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    game = _game(status="confirmed", winner_id=1, loser_ids=(2,), reported_by=9)
    monkeypatch.setattr(game_service.games, "get_game", AsyncMock(return_value=game))
    monkeypatch.setattr(
        game_service.score_requests, "list_score_requests", AsyncMock(return_value=[])
    )

    with pytest.raises(ConflictError):
        await game_service.confirm_preflight(pool, 123, 7, _actor(2))


@pytest.mark.asyncio
async def test_confirm_preflight_missing_game_raises_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    monkeypatch.setattr(game_service.games, "get_game", AsyncMock(return_value=None))

    with pytest.raises(NotFoundError):
        await game_service.confirm_preflight(pool, 123, 999, _actor(1))


# ---------------------------------------------------------------------------
# due_score_prompts (Phase 6): claims via `score_requests.claim_due_prompts`,
# then pairs each claimed row with its live game -- dropping anything whose
# game has since left `pending`/`confirmed`.
# ---------------------------------------------------------------------------


def _claimed(
    game_id: int,
    guild_id: int,
    user_id: int,
    *,
    dm_channel_id: int | None = 555,
    dm_message_id: int | None = 777,
    delivery_status: str = "delivered",
) -> SimpleNamespace:
    return SimpleNamespace(
        game_id=game_id,
        guild_id=guild_id,
        user_id=user_id,
        dm_channel_id=dm_channel_id,
        dm_message_id=dm_message_id,
        delivery_status=delivery_status,
    )


@pytest.mark.asyncio
async def test_due_score_prompts_claims_then_pairs_with_its_live_game(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    claim = AsyncMock(return_value=[_claimed(7, 123, 2)])
    monkeypatch.setattr(game_service.score_requests, "claim_due_prompts", claim)
    game = _game()
    get_game = AsyncMock(return_value=game)
    monkeypatch.setattr(game_service.games, "get_game", get_game)

    due = await game_service.due_score_prompts(pool, NOW, 50)

    claim.assert_awaited_once_with(pool.connection, NOW, 50)
    get_game.assert_awaited_once_with(pool.connection, 123, 7)
    assert len(due) == 1
    assert due[0].game is game
    assert due[0].user_id == 2
    assert due[0].dm_channel_id == 555
    assert due[0].dm_message_id == 777
    assert due[0].delivery_status == "delivered"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "voided"])
async def test_due_score_prompts_drops_a_claim_for_a_dead_game(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """The claim query has no idea about game status, so it still claims (and
    reschedules) a since-rejected/voided game's row -- harmless, since the
    row is simply never claimed again. This proves the dead game produces no
    prompt for the scheduler to act on."""
    pool = _Pool()
    monkeypatch.setattr(
        game_service.score_requests,
        "claim_due_prompts",
        AsyncMock(return_value=[_claimed(7, 123, 2)]),
    )
    monkeypatch.setattr(
        game_service.games, "get_game", AsyncMock(return_value=_game(status=status))
    )

    due = await game_service.due_score_prompts(pool, NOW, 50)

    assert due == []


@pytest.mark.asyncio
async def test_due_score_prompts_reads_each_game_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three outstanding players in the same game claim three rows, but the
    game itself should only be read once, not once per row."""
    pool = _Pool()
    monkeypatch.setattr(
        game_service.score_requests,
        "claim_due_prompts",
        AsyncMock(return_value=[_claimed(7, 123, 2), _claimed(7, 123, 3), _claimed(7, 123, 4)]),
    )
    game = _game(winner_id=2, loser_ids=(3, 4))
    get_game = AsyncMock(return_value=game)
    monkeypatch.setattr(game_service.games, "get_game", get_game)

    due = await game_service.due_score_prompts(pool, NOW, 50)

    get_game.assert_awaited_once()
    assert [prompt.user_id for prompt in due] == [2, 3, 4]
    assert all(prompt.game is game for prompt in due)


@pytest.mark.asyncio
async def test_due_score_prompts_drops_only_the_game_whose_read_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read failure for one claimed game's row must not cost prompts already
    claimed for a different game in the same sweep."""
    pool = _Pool()
    monkeypatch.setattr(
        game_service.score_requests,
        "claim_due_prompts",
        AsyncMock(return_value=[_claimed(7, 123, 2), _claimed(9, 123, 5)]),
    )
    good_game = _game(winner_id=5, loser_ids=())
    get_game = AsyncMock(side_effect=[RuntimeError("boom"), good_game])
    monkeypatch.setattr(game_service.games, "get_game", get_game)

    due = await game_service.due_score_prompts(pool, NOW, 50)

    assert len(due) == 1
    assert due[0].user_id == 5
    assert due[0].game is good_game


@pytest.mark.asyncio
async def test_due_score_prompts_returns_nothing_when_nothing_is_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool()
    claim = AsyncMock(return_value=[])
    monkeypatch.setattr(game_service.score_requests, "claim_due_prompts", claim)
    get_game = AsyncMock()
    monkeypatch.setattr(game_service.games, "get_game", get_game)

    due = await game_service.due_score_prompts(pool, NOW, 50)

    assert due == []
    get_game.assert_not_awaited()
