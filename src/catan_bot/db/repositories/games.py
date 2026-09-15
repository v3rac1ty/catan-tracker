"""`games` / `game_participants` repository.

Every SQL statement here is a module-level string constant, bound exactly
once, with values passed only as `$n` arguments (see CLAUDE.md and
`tests/static/sql_guard.py`).

Status transitions (`confirm_game`, `reject_game`, `void_game`) are each a
single guarded atomic `UPDATE ... WHERE <allowed prior state> ... RETURNING`.
When the UPDATE affects zero rows, a follow-up read classifies *why*, using
a separate constant per transition (never a shared dynamic query).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import asyncpg

from catan_bot.db.models import Game, GameWithParticipants, TransitionResult
from catan_bot.db.repositories._params import require_id, require_limit, require_optional_id

_INSERT_GAME_SQL = """
INSERT INTO games (guild_id, season_id, played_on, reported_by)
VALUES ($1, $2, $3, $4)
RETURNING game_id, guild_id, season_id, played_on, status, reported_by, confirmed_by,
          confirmed_at, voided_by, voided_at, void_reason, rejected_by, rejected_at,
          channel_id, message_id, created_at
"""

_INSERT_GAME_PARTICIPANTS_SQL = """
INSERT INTO game_participants (game_id, user_id, guild_id, is_winner)
SELECT $1, u, $2, w
FROM unnest($3::bigint[], $4::boolean[]) AS t(u, w)
"""

_UPDATE_GAME_MESSAGE_SQL = """
UPDATE games
SET channel_id = $3, message_id = $4
WHERE guild_id = $1 AND game_id = $2
RETURNING game_id, guild_id, season_id, played_on, status, reported_by, confirmed_by,
          confirmed_at, voided_by, voided_at, void_reason, rejected_by, rejected_at,
          channel_id, message_id, created_at
"""

_SELECT_GAME_SQL = """
SELECT game_id, guild_id, season_id, played_on, status, reported_by, confirmed_by,
       confirmed_at, voided_by, voided_at, void_reason, rejected_by, rejected_at,
       channel_id, message_id, created_at
FROM games
WHERE guild_id = $1 AND game_id = $2
"""

_SELECT_GAME_PARTICIPANTS_SQL = """
SELECT user_id, is_winner
FROM game_participants
WHERE guild_id = $1 AND game_id = $2
"""

_CONFIRM_GAME_SQL = """
UPDATE games
SET status = 'confirmed', confirmed_by = $3, confirmed_at = now()
WHERE game_id = $1 AND guild_id = $2 AND status = 'pending' AND reported_by <> $3
      AND EXISTS (
          SELECT 1 FROM game_participants
          WHERE game_id = $1 AND guild_id = $2 AND user_id = $3
      )
RETURNING game_id
"""

_SELECT_GAME_FOR_CONFIRM_CLASSIFY_SQL = """
SELECT status, reported_by,
       EXISTS (
           SELECT 1 FROM game_participants
           WHERE game_id = $1 AND guild_id = $2 AND user_id = $3
       ) AS is_participant
FROM games
WHERE guild_id = $2 AND game_id = $1
"""

_REJECT_GAME_SQL = """
UPDATE games
SET status = 'rejected', rejected_by = $3, rejected_at = now()
WHERE game_id = $1 AND guild_id = $2 AND status = 'pending'
      AND (
          reported_by = $3
          OR EXISTS (
              SELECT 1 FROM game_participants
              WHERE game_id = $1 AND guild_id = $2 AND user_id = $3
          )
      )
RETURNING game_id
"""

_SELECT_GAME_FOR_REJECT_CLASSIFY_SQL = """
SELECT status, reported_by,
       EXISTS (
           SELECT 1 FROM game_participants
           WHERE game_id = $1 AND guild_id = $2 AND user_id = $3
       ) AS is_participant
FROM games
WHERE guild_id = $2 AND game_id = $1
"""

_VOID_GAME_SQL = """
UPDATE games
SET status = 'voided', voided_by = $3, voided_at = now(), void_reason = $4
WHERE game_id = $1 AND guild_id = $2 AND status IN ('pending', 'confirmed')
RETURNING game_id
"""

_SELECT_GAME_STATUS_FOR_VOID_CLASSIFY_SQL = """
SELECT status
FROM games
WHERE guild_id = $1 AND game_id = $2
"""

_LIST_RECENT_GAMES_SQL = """
SELECT game_id, guild_id, season_id, played_on, status, reported_by, confirmed_by,
       confirmed_at, voided_by, voided_at, void_reason, rejected_by, rejected_at,
       channel_id, message_id, created_at
FROM games
WHERE guild_id = $1
ORDER BY game_id DESC
LIMIT $2
"""

_LIST_RECENT_GAMES_FOR_PLAYER_SQL = """
SELECT g.game_id, g.guild_id, g.season_id, g.played_on, g.status, g.reported_by,
       g.confirmed_by, g.confirmed_at, g.voided_by, g.voided_at, g.void_reason,
       g.rejected_by, g.rejected_at, g.channel_id, g.message_id, g.created_at
FROM games g
JOIN game_participants p ON p.game_id = g.game_id AND p.guild_id = g.guild_id
WHERE g.guild_id = $1 AND p.user_id = $2
ORDER BY g.game_id DESC
LIMIT $3
"""


def _row_to_game(row: asyncpg.Record) -> Game:
    return Game(
        game_id=row["game_id"],
        guild_id=row["guild_id"],
        season_id=row["season_id"],
        played_on=row["played_on"],
        status=row["status"],
        reported_by=row["reported_by"],
        confirmed_by=row["confirmed_by"],
        confirmed_at=row["confirmed_at"],
        voided_by=row["voided_by"],
        voided_at=row["voided_at"],
        void_reason=row["void_reason"],
        rejected_by=row["rejected_by"],
        rejected_at=row["rejected_at"],
        channel_id=row["channel_id"],
        message_id=row["message_id"],
        created_at=row["created_at"],
    )


async def create_game(
    conn: asyncpg.Connection,
    guild_id: int,
    season_id: int | None,
    played_on: date,
    reported_by: int,
    winner_id: int,
    loser_ids: Sequence[int],
) -> Game:
    """Insert a pending game and its participants in one transaction.

    Callers must `players.ensure_players` the winner and every loser first;
    an unregistered id surfaces as `asyncpg.ForeignKeyViolationError` here.
    `conn.transaction()` nests as a savepoint if the caller already holds
    one.
    """
    require_id(guild_id, name="guild_id")
    require_optional_id(season_id, name="season_id")
    require_id(reported_by, name="reported_by")
    require_id(winner_id, name="winner_id")
    # Materialize exactly once: `loser_ids` may be a one-shot iterable (e.g. a
    # generator), and validating it here must not be the thing that consumes
    # it before `len(loser_ids)`/the SQL call below ever see it (N1 audit
    # finding -- a generator previously raised TypeError from `len()`).
    loser_ids = list(loser_ids)
    for i, loser_id in enumerate(loser_ids):
        require_id(loser_id, name=f"loser_ids[{i}]")
    async with conn.transaction():
        game_row = await conn.fetchrow(
            _INSERT_GAME_SQL, guild_id, season_id, played_on, reported_by
        )
        if game_row is None:  # pragma: no cover -- INSERT ... RETURNING always returns a row.
            raise RuntimeError("INSERT INTO games did not return a row")
        game = _row_to_game(game_row)

        user_ids = [winner_id, *loser_ids]
        is_winner_flags = [True, *([False] * len(loser_ids))]
        await conn.execute(
            _INSERT_GAME_PARTICIPANTS_SQL, game.game_id, guild_id, user_ids, is_winner_flags
        )
    return game


async def set_game_message(
    conn: asyncpg.Connection, guild_id: int, game_id: int, channel_id: int, message_id: int
) -> Game | None:
    require_id(guild_id, name="guild_id")
    require_id(game_id, name="game_id")
    require_id(channel_id, name="channel_id")
    require_id(message_id, name="message_id")
    row = await conn.fetchrow(_UPDATE_GAME_MESSAGE_SQL, guild_id, game_id, channel_id, message_id)
    return _row_to_game(row) if row is not None else None


async def get_game(
    conn: asyncpg.Connection, guild_id: int, game_id: int
) -> GameWithParticipants | None:
    require_id(guild_id, name="guild_id")
    require_id(game_id, name="game_id")
    game_row = await conn.fetchrow(_SELECT_GAME_SQL, guild_id, game_id)
    if game_row is None:
        return None
    game = _row_to_game(game_row)

    participant_rows = await conn.fetch(_SELECT_GAME_PARTICIPANTS_SQL, guild_id, game_id)
    winner_id: int | None = None
    loser_ids: list[int] = []
    for row in participant_rows:
        if row["is_winner"]:
            winner_id = row["user_id"]
        else:
            loser_ids.append(row["user_id"])
    if winner_id is None:  # pragma: no cover -- a game always has exactly one winner.
        raise RuntimeError(f"game {game_id} in guild {guild_id} has no winner participant")
    return GameWithParticipants(game=game, winner_id=winner_id, loser_ids=tuple(loser_ids))


async def confirm_game(
    conn: asyncpg.Connection, guild_id: int, game_id: int, user_id: int
) -> TransitionResult:
    """`pending -> confirmed`: `user_id` must be a participant, and not the reporter.

    On success: `"confirmed"`. On failure, a follow-up SELECT classifies
    which condition the guarded UPDATE's WHERE clause failed:
    `"not_found"`, `"not_pending"`, `"reporter_cannot_confirm"`, or
    `"not_participant"`.
    """
    require_id(guild_id, name="guild_id")
    require_id(game_id, name="game_id")
    require_id(user_id, name="user_id")
    updated = await conn.fetchrow(_CONFIRM_GAME_SQL, game_id, guild_id, user_id)
    if updated is not None:
        return "confirmed"

    classify_row = await conn.fetchrow(
        _SELECT_GAME_FOR_CONFIRM_CLASSIFY_SQL, game_id, guild_id, user_id
    )
    if classify_row is None:
        return "not_found"
    if classify_row["status"] != "pending":
        return "not_pending"
    if classify_row["reported_by"] == user_id:
        return "reporter_cannot_confirm"
    if not classify_row["is_participant"]:
        return "not_participant"
    # Unreachable barring a concurrent change between the failed UPDATE and
    # this SELECT (e.g. another actor confirmed/voided it in between).
    return "not_pending"  # pragma: no cover


async def reject_game(
    conn: asyncpg.Connection, guild_id: int, game_id: int, user_id: int
) -> TransitionResult:
    """`pending -> rejected`: `user_id` must be one of: the game's
    participants, or the reporter retracting their own report.

    On success: `"rejected"`. On failure: `"not_found"`, `"not_pending"`, or
    `"not_participant"`.
    """
    require_id(guild_id, name="guild_id")
    require_id(game_id, name="game_id")
    require_id(user_id, name="user_id")
    updated = await conn.fetchrow(_REJECT_GAME_SQL, game_id, guild_id, user_id)
    if updated is not None:
        return "rejected"

    classify_row = await conn.fetchrow(
        _SELECT_GAME_FOR_REJECT_CLASSIFY_SQL, game_id, guild_id, user_id
    )
    if classify_row is None:
        return "not_found"
    if classify_row["status"] != "pending":
        return "not_pending"
    if classify_row["reported_by"] != user_id and not classify_row["is_participant"]:
        return "not_participant"
    return "not_pending"  # pragma: no cover -- see confirm_game's matching branch.


async def void_game(
    conn: asyncpg.Connection, guild_id: int, game_id: int, admin_id: int, reason: str | None
) -> TransitionResult:
    """`pending|confirmed -> voided`, keeping confirmation fields as an audit trail.

    On success: `"voided"`. On failure: `"not_found"` or
    `"not_pending_or_confirmed"` (already rejected or voided).
    """
    require_id(guild_id, name="guild_id")
    require_id(game_id, name="game_id")
    require_id(admin_id, name="admin_id")
    updated = await conn.fetchrow(_VOID_GAME_SQL, game_id, guild_id, admin_id, reason)
    if updated is not None:
        return "voided"

    status_row = await conn.fetchrow(_SELECT_GAME_STATUS_FOR_VOID_CLASSIFY_SQL, guild_id, game_id)
    if status_row is None:
        return "not_found"
    return "not_pending_or_confirmed"


async def list_recent_games(conn: asyncpg.Connection, guild_id: int, limit: int) -> list[Game]:
    require_id(guild_id, name="guild_id")
    require_limit(limit)
    rows = await conn.fetch(_LIST_RECENT_GAMES_SQL, guild_id, limit)
    return [_row_to_game(row) for row in rows]


async def list_recent_games_for_player(
    conn: asyncpg.Connection, guild_id: int, user_id: int, limit: int
) -> list[Game]:
    require_id(guild_id, name="guild_id")
    require_id(user_id, name="user_id")
    require_limit(limit)
    rows = await conn.fetch(_LIST_RECENT_GAMES_FOR_PLAYER_SQL, guild_id, user_id, limit)
    return [_row_to_game(row) for row in rows]
