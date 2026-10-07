"""Read-only analytics repository: one row per player per confirmed game.

Every SQL statement here is a module-level string constant, bound exactly
once, with values passed only as `$n` arguments (see CLAUDE.md and
`tests/static/sql_guard.py`).
"""

from __future__ import annotations

import json
from typing import get_args

import asyncpg

from catan_bot.db.repositories._params import require_id, require_optional_id
from catan_bot.domain.participation import ParticipationRecord
from catan_bot.domain.scoring import GameType

_GAME_TYPES = frozenset(get_args(GameType))

# The two optional filters are parameterized predicates inside this one
# statement (not a second query text chosen in Python), mirroring
# `games._LIST_RECENT_GAMES_SQL`. Both parameters carry explicit casts so
# `$n IS NULL` is never ambiguous at prepare time.
#
# `player_count` is a window count over the *active* participants of each
# game: the `p.is_active` predicate is part of the WHERE, so inactive rows
# (e.g. players removed by a roster update) never reach the window. The
# optional filters only restrict whole games, never part of a game's
# roster, so the count is always the game's true active roster size.
#
# Ordering is exactly the reverse of the history order (played_on DESC,
# played_at DESC NULLS LAST, game_id DESC): unknown times sort first within
# a date, then game_id and user_id break remaining ties deterministically.
_LIST_PARTICIPATIONS_SQL = """
SELECT g.game_id, g.played_on, g.played_at, g.game_type, g.extension_5_6,
       g.target_points, g.season_id, g.played_timezone, p.user_id, p.is_winner, p.total_points,
       p.score_breakdown::text AS score_breakdown,
       count(*) OVER (PARTITION BY g.game_id) AS player_count
FROM games AS g
JOIN game_participants AS p
    ON p.game_id = g.game_id AND p.guild_id = g.guild_id
WHERE g.guild_id = $1
      AND g.status = 'confirmed'
      AND p.is_active
      AND ($2::bigint IS NULL OR g.season_id = $2::bigint)
      AND ($3::text IS NULL OR g.game_type = $3::text)
ORDER BY g.played_on ASC, g.played_at ASC NULLS FIRST, g.game_id ASC, p.user_id ASC
"""


def _require_optional_game_type(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _GAME_TYPES:
        raise ValueError(f"game_type must be one of {sorted(_GAME_TYPES)!r}, got {value!r}")
    return value


def _decode_breakdown(total_points: object, score_breakdown: object) -> dict[str, int] | None:
    if total_points is None and score_breakdown is None:
        return None
    if total_points is None or score_breakdown is None:
        raise RuntimeError("game participant has incomplete score data")  # pragma: no cover
    if type(total_points) is not int or not isinstance(score_breakdown, str):
        raise RuntimeError("game participant has invalid score data")
    decoded = json.loads(score_breakdown)
    if not isinstance(decoded, dict):  # pragma: no cover -- DB CHECK enforces JSON object.
        raise RuntimeError("game participant score_breakdown is not a JSON object")
    breakdown: dict[str, int] = {}
    for key, points in decoded.items():
        if not isinstance(key, str) or type(points) is not int:
            raise RuntimeError("game participant score_breakdown has invalid entries")
        breakdown[key] = points
    return breakdown


def _row_to_participation(row: asyncpg.Record) -> ParticipationRecord:
    return ParticipationRecord(
        game_id=row["game_id"],
        played_on=row["played_on"],
        played_at=row["played_at"],
        game_type=row["game_type"],
        extension_5_6=row["extension_5_6"],
        target_points=row["target_points"],
        player_count=row["player_count"],
        user_id=row["user_id"],
        is_winner=row["is_winner"],
        total_points=row["total_points"],
        breakdown=_decode_breakdown(row["total_points"], row["score_breakdown"]),
        season_id=row["season_id"],
        played_timezone=row["played_timezone"],
    )


async def list_participations(
    conn: asyncpg.Connection,
    guild_id: int,
    *,
    season_id: int | None = None,
    game_type: str | None = None,
) -> list[ParticipationRecord]:
    """Every active player's result in every confirmed game, oldest first.

    Voided, rejected and pending games are excluded, as are participants a
    roster update deactivated. `player_count` is the number of active
    participants in the game. Optionally restricted to one season and/or
    one game type. Rows with no recorded score carry
    `total_points=None, breakdown=None`.
    """
    require_id(guild_id, name="guild_id")
    require_optional_id(season_id, name="season_id")
    game_type = _require_optional_game_type(game_type)
    rows = await conn.fetch(_LIST_PARTICIPATIONS_SQL, guild_id, season_id, game_type)
    return [_row_to_participation(row) for row in rows]
