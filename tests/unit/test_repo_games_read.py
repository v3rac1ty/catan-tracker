"""Unit coverage for the atomic game-details read."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from catan_bot.db.repositories import games
from catan_bot.domain.scoring import PlayerScore, ScoreEntry


class _OneStatementConnection:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.fetchrow_calls: list[tuple[object, tuple[object, ...]]] = []

    async def fetchrow(self, query: object, *args: object) -> dict[str, object] | None:
        self.fetchrow_calls.append((query, args))
        return self.row

    async def fetch(self, *args: object, **kwargs: object) -> Any:
        raise AssertionError("get_game must not perform a second participant fetch")


def _game_row() -> dict[str, object]:
    return {
        "game_id": 40,
        "guild_id": 20,
        "season_id": None,
        "played_on": date(2026, 3, 4),
        "status": "confirmed",
        "reported_by": 1,
        "confirmed_by": 2,
        "confirmed_at": datetime(2026, 3, 4, 22, tzinfo=UTC),
        "voided_by": None,
        "voided_at": None,
        "void_reason": None,
        "rejected_by": None,
        "rejected_at": None,
        "channel_id": 30,
        "message_id": 31,
        "created_at": datetime(2026, 3, 4, 20, tzinfo=UTC),
        "game_type": "normal",
        "extension_5_6": False,
        "scenario": None,
        "target_points": 10,
        "played_at": None,
        "played_timezone": None,
        "revision": 2,
        "updated_by": 9,
        "updated_at": datetime(2026, 3, 5, tzinfo=UTC),
        "update_reason": "corrected roster",
        # These arrays are emitted by the SQL statement in winner-first,
        # then user-id order.  None preserves the intentionally omitted score
        # sheet independently of the explicit zero-value rows.
        "participant_user_ids": [3, 1, 2],
        "participant_winner_flags": [True, False, False],
        "participant_total_points": [10, None, 0],
        "participant_score_breakdowns": [
            '{"cities":6,"settlements":4}',
            None,
            '{"longest_road":0,"settlements":0}',
        ],
    }


async def test_get_game_uses_one_aggregated_fetchrow_and_maps_active_roster() -> None:
    conn = _OneStatementConnection(_game_row())

    result = await games.get_game(conn, 20, 40)  # type: ignore[arg-type]

    assert result is not None
    assert conn.fetchrow_calls == [(games._SELECT_GAME_SQL, (20, 40))]
    assert result.game.revision == 2
    assert result.winner_id == 3
    assert result.loser_ids == (1, 2)
    assert result.scores == (
        PlayerScore(
            user_id=3,
            total_points=10,
            breakdown=(ScoreEntry("settlements", 4), ScoreEntry("cities", 6)),
        ),
        PlayerScore(
            user_id=2,
            total_points=0,
            breakdown=(ScoreEntry("settlements", 0), ScoreEntry("longest_road", 0)),
        ),
    )


async def test_get_game_unknown_game_uses_one_fetchrow() -> None:
    conn = _OneStatementConnection(None)

    assert await games.get_game(conn, 20, 404) is None  # type: ignore[arg-type]
    assert conn.fetchrow_calls == [(games._SELECT_GAME_SQL, (20, 404))]
