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

import json
from collections.abc import Sequence
from datetime import date, datetime

import asyncpg

from catan_bot.db.models import Game, GameWithParticipants, TransitionResult
from catan_bot.db.repositories._params import (
    require_aware,
    require_id,
    require_int,
    require_limit,
    require_optional_id,
)
from catan_bot.domain.scoring import GameRules, PlayerScore, ScoreEntry, score_sources

_GAME_TYPES = frozenset({"normal", "seafarers", "cities_knights", "seafarers_cities_knights"})

_INSERT_GAME_SQL = """
INSERT INTO games (
    guild_id, season_id, played_on, reported_by, game_type, extension_5_6,
    scenario, target_points, played_at, played_timezone
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
RETURNING game_id, guild_id, season_id, played_on, status, reported_by, confirmed_by,
          confirmed_at, voided_by, voided_at, void_reason, rejected_by, rejected_at,
          channel_id, message_id, created_at, game_type, extension_5_6, scenario,
          target_points, played_at, played_timezone, revision, updated_by, updated_at,
          update_reason
"""

_INSERT_GAME_PARTICIPANTS_SQL = """
INSERT INTO game_participants (
    game_id, user_id, guild_id, is_winner, total_points, score_breakdown
)
SELECT $1, u, $2, w, points, breakdown::jsonb
FROM unnest($3::bigint[], $4::boolean[], $5::smallint[], $6::text[])
    AS t(u, w, points, breakdown)
"""

_UPDATE_GAME_MESSAGE_SQL = """
UPDATE games
SET channel_id = $3, message_id = $4
WHERE guild_id = $1 AND game_id = $2
RETURNING game_id, guild_id, season_id, played_on, status, reported_by, confirmed_by,
          confirmed_at, voided_by, voided_at, void_reason, rejected_by, rejected_at,
          channel_id, message_id, created_at, game_type, extension_5_6, scenario,
          target_points, played_at, played_timezone, revision, updated_by, updated_at,
          update_reason
"""

_SELECT_GAME_SQL = """
SELECT g.game_id, g.guild_id, g.season_id, g.played_on, g.status, g.reported_by,
       g.confirmed_by, g.confirmed_at, g.voided_by, g.voided_at, g.void_reason,
       g.rejected_by, g.rejected_at, g.channel_id, g.message_id, g.created_at,
       g.game_type, g.extension_5_6, g.scenario, g.target_points, g.played_at,
       g.played_timezone, g.revision, g.updated_by, g.updated_at, g.update_reason,
       COALESCE(
           array_agg(p.user_id ORDER BY p.is_winner DESC, p.user_id)
               FILTER (WHERE p.user_id IS NOT NULL),
           ARRAY[]::bigint[]
       ) AS participant_user_ids,
       COALESCE(
           array_agg(p.is_winner ORDER BY p.is_winner DESC, p.user_id)
               FILTER (WHERE p.user_id IS NOT NULL),
           ARRAY[]::boolean[]
       ) AS participant_winner_flags,
       COALESCE(
           array_agg(p.total_points ORDER BY p.is_winner DESC, p.user_id)
               FILTER (WHERE p.user_id IS NOT NULL),
           ARRAY[]::smallint[]
       ) AS participant_total_points,
       COALESCE(
           array_agg(p.score_breakdown::text ORDER BY p.is_winner DESC, p.user_id)
               FILTER (WHERE p.user_id IS NOT NULL),
           ARRAY[]::text[]
       ) AS participant_score_breakdowns
FROM games AS g
LEFT JOIN game_participants AS p
    ON p.game_id = g.game_id AND p.guild_id = g.guild_id AND p.is_active
WHERE g.guild_id = $1 AND g.game_id = $2
GROUP BY g.game_id
"""

_SELECT_GAME_PARTICIPANTS_SQL = """
SELECT user_id, is_winner, total_points, score_breakdown
FROM game_participants
WHERE guild_id = $1 AND game_id = $2 AND is_active
ORDER BY is_winner DESC, user_id ASC
"""

_CONFIRM_GAME_SQL = """
UPDATE games
SET status = 'confirmed', confirmed_by = $3, confirmed_at = now()
WHERE game_id = $1 AND guild_id = $2 AND status = 'pending' AND reported_by <> $3
      AND EXISTS (
          SELECT 1 FROM game_participants
          WHERE game_id = $1 AND guild_id = $2 AND user_id = $3 AND is_active
      )
RETURNING game_id
"""

_SELECT_GAME_FOR_CONFIRM_CLASSIFY_SQL = """
SELECT status, reported_by,
       EXISTS (
           SELECT 1 FROM game_participants
           WHERE game_id = $1 AND guild_id = $2 AND user_id = $3 AND is_active
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
              WHERE game_id = $1 AND guild_id = $2 AND user_id = $3 AND is_active
          )
      )
RETURNING game_id
"""

_SELECT_GAME_FOR_REJECT_CLASSIFY_SQL = """
SELECT status, reported_by,
       EXISTS (
           SELECT 1 FROM game_participants
           WHERE game_id = $1 AND guild_id = $2 AND user_id = $3 AND is_active
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
       channel_id, message_id, created_at, game_type, extension_5_6, scenario,
       target_points, played_at, played_timezone, revision, updated_by, updated_at,
       update_reason
FROM games
WHERE guild_id = $1
ORDER BY played_on DESC, played_at DESC NULLS LAST, game_id DESC
LIMIT $2
"""

_LIST_RECENT_GAMES_FOR_PLAYER_SQL = """
SELECT g.game_id, g.guild_id, g.season_id, g.played_on, g.status, g.reported_by,
       g.confirmed_by, g.confirmed_at, g.voided_by, g.voided_at, g.void_reason,
       g.rejected_by, g.rejected_at, g.channel_id, g.message_id, g.created_at,
       g.game_type, g.extension_5_6, g.scenario, g.target_points, g.played_at,
       g.played_timezone, g.revision, g.updated_by, g.updated_at, g.update_reason
FROM games g
JOIN game_participants p ON p.game_id = g.game_id AND p.guild_id = g.guild_id
WHERE g.guild_id = $1 AND p.user_id = $2 AND p.is_active
ORDER BY g.played_on DESC, g.played_at DESC NULLS LAST, g.game_id DESC
LIMIT $3
"""

_LOCK_GAME_SQL = """
SELECT game_id, guild_id, season_id, played_on, status, reported_by, confirmed_by,
       confirmed_at, voided_by, voided_at, void_reason, rejected_by, rejected_at,
       channel_id, message_id, created_at, game_type, extension_5_6, scenario,
       target_points, played_at, played_timezone, revision, updated_by, updated_at,
       update_reason
FROM games
WHERE guild_id = $1 AND game_id = $2
FOR UPDATE
"""

_UPDATE_CONFIRMED_GAME_SQL = """
UPDATE games
SET played_on = $3, game_type = $4, extension_5_6 = $5, scenario = $6,
    target_points = $7, played_at = $8, played_timezone = $9,
    revision = revision + 1, updated_by = $10, updated_at = now(), update_reason = $11
WHERE guild_id = $1 AND game_id = $2 AND status = 'confirmed' AND revision = $12
RETURNING game_id, guild_id, season_id, played_on, status, reported_by, confirmed_by,
          confirmed_at, voided_by, voided_at, void_reason, rejected_by, rejected_at,
          channel_id, message_id, created_at, game_type, extension_5_6, scenario,
          target_points, played_at, played_timezone, revision, updated_by, updated_at,
          update_reason
"""

_DEACTIVATE_GAME_PARTICIPANTS_SQL = """
UPDATE game_participants
SET is_active = false, is_winner = false
WHERE guild_id = $1 AND game_id = $2 AND is_active
"""

_UPSERT_GAME_PARTICIPANTS_SQL = """
INSERT INTO game_participants (
    game_id, user_id, guild_id, is_winner, total_points, score_breakdown, is_active
)
SELECT $1, u, $2, w, points, breakdown::jsonb, true
FROM unnest($3::bigint[], $4::boolean[], $5::smallint[], $6::text[])
    AS t(u, w, points, breakdown)
ON CONFLICT (game_id, user_id) DO UPDATE
SET guild_id = EXCLUDED.guild_id,
    is_winner = EXCLUDED.is_winner,
    total_points = EXCLUDED.total_points,
    score_breakdown = EXCLUDED.score_breakdown,
    is_active = true
"""

_INSERT_GAME_UPDATE_SQL = """
INSERT INTO game_updates (
    game_id, revision, guild_id, updated_by, updated_at, reason, before_snapshot, after_snapshot
)
VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb)
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
        game_type=row["game_type"],
        extension_5_6=row["extension_5_6"],
        scenario=row["scenario"],
        target_points=row["target_points"],
        played_at=row["played_at"],
        played_timezone=row["played_timezone"],
        revision=row["revision"],
        updated_by=row["updated_by"],
        updated_at=row["updated_at"],
        update_reason=row["update_reason"],
    )


def _require_game_type(value: object) -> str:
    if not isinstance(value, str) or value not in _GAME_TYPES:
        raise ValueError(f"game_type must be one of {sorted(_GAME_TYPES)!r}, got {value!r}")
    return value


def _require_extension(value: object) -> bool:
    if type(value) is not bool:
        raise ValueError(f"extension_5_6 must be a bool, got {value!r} ({type(value).__name__})")
    return value


def _require_optional_bounded_str(
    value: object, *, name: str, min_length: int, max_length: int
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not min_length <= len(value) <= max_length:
        raise ValueError(
            f"{name} must be a str with length between {min_length} and {max_length}, got {value!r}"
        )
    return value


def _validate_played_time(
    played_at: datetime | None, played_timezone: str | None
) -> tuple[datetime | None, str | None]:
    if (played_at is None) != (played_timezone is None):
        raise ValueError("played_at and played_timezone must either both be set or both be None")
    if played_at is None:
        return None, None
    return (
        require_aware(played_at, name="played_at"),
        _require_optional_bounded_str(
            played_timezone, name="played_timezone", min_length=1, max_length=64
        ),
    )


def _score_breakdown_json(value: object, *, name: str) -> str:
    """Turn the immutable domain representation into a JSON object string.

    asyncpg's JSONB codec returns text by default, and accepting only
    string-keyed, integer-valued pairs here makes persistence explicit and
    prevents a mutable caller mapping from being passed through implicitly.
    Rule-specific keys and totals are deliberately validated by the domain
    layer; this repository only enforces the durable JSONB-object boundary.
    """
    if not isinstance(value, tuple):
        raise ValueError(f"{name} must be a tuple of ScoreEntry values")
    breakdown: dict[str, int] = {}
    for index, item in enumerate(value):
        if type(item) is not ScoreEntry:
            raise ValueError(f"{name}[{index}] must be a ScoreEntry")
        key, points = item.key, item.points
        if points > 99:
            raise ValueError(f"{name}[{index}].points must be an int between 0 and 99")
        if key in breakdown:
            raise ValueError(f"{name} contains duplicate key {key!r}")
        breakdown[key] = points
    return json.dumps(breakdown, separators=(",", ":"), ensure_ascii=False)


def _scores_for_participants(
    scores: Sequence[PlayerScore] | None, user_ids: Sequence[int]
) -> tuple[list[int | None], list[str | None]]:
    if scores is None:
        return [None] * len(user_ids), [None] * len(user_ids)
    if not isinstance(scores, (list, tuple)):
        raise ValueError("scores must be a list or tuple of PlayerScore")
    # An unfinished/omitted score sheet has no durable score data.  This is
    # intentionally different from a full sheet whose rows all happen to be
    # zero: that one is represented by non-NULL totals and JSON objects.
    if not scores:
        return [None] * len(user_ids), [None] * len(user_ids)

    by_user_id: dict[int, PlayerScore] = {}
    for index, score in enumerate(scores):
        if not isinstance(score, PlayerScore):
            raise ValueError(f"scores[{index}] must be a PlayerScore")
        user_id = require_id(score.user_id, name=f"scores[{index}].user_id")
        if user_id in by_user_id:
            raise ValueError(f"scores contains duplicate user_id {user_id}")
        require_int(
            score.total_points,
            name=f"scores[{index}].total_points",
            min_value=0,
            max_value=99,
        )
        _score_breakdown_json(score.breakdown, name=f"scores[{index}].breakdown")
        by_user_id[user_id] = score

    if set(by_user_id) != set(user_ids) or len(by_user_id) != len(user_ids):
        raise ValueError("scores must contain exactly one score for every game participant")

    return (
        [by_user_id[user_id].total_points for user_id in user_ids],
        [
            _score_breakdown_json(by_user_id[user_id].breakdown, name="score.breakdown")
            for user_id in user_ids
        ],
    )


def _player_score_from_values(
    user_id: int, total_points: object, score_breakdown: object, game: Game
) -> PlayerScore | None:
    if total_points is None and score_breakdown is None:
        return None
    # pragma: no cover -- DB CHECK enforces pairing.
    if total_points is None or score_breakdown is None:
        raise RuntimeError("game participant has incomplete score data")
    if type(total_points) is not int or not isinstance(score_breakdown, str):
        raise RuntimeError("game participant has invalid score data")
    decoded = json.loads(score_breakdown)
    if not isinstance(decoded, dict):  # pragma: no cover -- DB CHECK enforces JSON object.
        raise RuntimeError("game participant score_breakdown is not a JSON object")
    source_order = {
        source.key: index
        for index, source in enumerate(
            score_sources(
                GameRules(
                    game_type=game.game_type,
                    extension_5_6=game.extension_5_6,
                    scenario=game.scenario,
                    target_points=game.target_points,
                )
            )
        )
    }
    entries = sorted(
        decoded.items(),
        key=lambda item: (source_order.get(item[0], len(source_order)), item[0]),
    )
    breakdown: list[ScoreEntry] = []
    for key, points in entries:
        if not isinstance(key, str) or type(points) is not int:
            raise RuntimeError("game participant score_breakdown has invalid entries")
        breakdown.append(ScoreEntry(key=key, points=points))
    return PlayerScore(user_id=user_id, total_points=total_points, breakdown=tuple(breakdown))


def _row_to_player_score(row: asyncpg.Record, game: Game) -> PlayerScore | None:
    return _player_score_from_values(
        row["user_id"], row["total_points"], row["score_breakdown"], game
    )


def _game_with_participants_from_row(row: asyncpg.Record) -> GameWithParticipants:
    """Map the one-statement game/roster read into immutable domain rows."""
    game = _row_to_game(row)
    user_ids = row["participant_user_ids"]
    winner_flags = row["participant_winner_flags"]
    total_points = row["participant_total_points"]
    score_breakdowns = row["participant_score_breakdowns"]
    try:
        participant_values = zip(
            user_ids, winner_flags, total_points, score_breakdowns, strict=True
        )
        participants = list(participant_values)
    except (TypeError, ValueError) as exc:  # pragma: no cover -- aggregate query invariant.
        raise RuntimeError("game participant aggregates are inconsistent") from exc

    winner_id: int | None = None
    loser_ids: list[int] = []
    scores: list[PlayerScore] = []
    for user_id, is_winner, player_points, score_breakdown in participants:
        if type(user_id) is not int or type(is_winner) is not bool:
            raise RuntimeError("game participant aggregates have invalid values")
        if is_winner:
            winner_id = user_id
        else:
            loser_ids.append(user_id)
        score = _player_score_from_values(user_id, player_points, score_breakdown, game)
        if score is not None:
            scores.append(score)
    if winner_id is None:  # pragma: no cover -- a game always has exactly one winner.
        raise RuntimeError(
            f"game {game.game_id} in guild {game.guild_id} has no winner participant"
        )
    return GameWithParticipants(
        game=game, winner_id=winner_id, loser_ids=tuple(loser_ids), scores=tuple(scores)
    )


async def create_game(
    conn: asyncpg.Connection,
    guild_id: int,
    season_id: int | None,
    played_on: date,
    reported_by: int,
    winner_id: int,
    loser_ids: Sequence[int],
    *,
    game_type: str = "normal",
    extension_5_6: bool = False,
    scenario: str | None = None,
    target_points: int | None = None,
    played_at: datetime | None = None,
    played_timezone: str | None = None,
    scores: Sequence[PlayerScore] | None = None,
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
    game_type = _require_game_type(game_type)
    extension_5_6 = _require_extension(extension_5_6)
    scenario = _require_optional_bounded_str(
        scenario, name="scenario", min_length=1, max_length=100
    )
    if target_points is not None:
        target_points = require_int(
            target_points, name="target_points", min_value=1, max_value=99
        )
    played_at, played_timezone = _validate_played_time(played_at, played_timezone)
    # Materialize exactly once: `loser_ids` may be a one-shot iterable (e.g. a
    # generator), and validating it here must not be the thing that consumes
    # it before `len(loser_ids)`/the SQL call below ever see it (N1 audit
    # finding -- a generator previously raised TypeError from `len()`).
    loser_ids = list(loser_ids)
    for i, loser_id in enumerate(loser_ids):
        require_id(loser_id, name=f"loser_ids[{i}]")
    user_ids = [winner_id, *loser_ids]
    total_points, score_breakdowns = _scores_for_participants(scores, user_ids)
    async with conn.transaction():
        game_row = await conn.fetchrow(
            _INSERT_GAME_SQL,
            guild_id,
            season_id,
            played_on,
            reported_by,
            game_type,
            extension_5_6,
            scenario,
            target_points,
            played_at,
            played_timezone,
        )
        if game_row is None:  # pragma: no cover -- INSERT ... RETURNING always returns a row.
            raise RuntimeError("INSERT INTO games did not return a row")
        game = _row_to_game(game_row)

        is_winner_flags = [True, *([False] * len(loser_ids))]
        await conn.execute(
            _INSERT_GAME_PARTICIPANTS_SQL,
            game.game_id,
            guild_id,
            user_ids,
            is_winner_flags,
            total_points,
            score_breakdowns,
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
    """Read game details and its active roster from one statement snapshot."""
    require_id(guild_id, name="guild_id")
    require_id(game_id, name="game_id")
    row = await conn.fetchrow(_SELECT_GAME_SQL, guild_id, game_id)
    if row is None:
        return None
    return _game_with_participants_from_row(row)


async def lock_game(
    conn: asyncpg.Connection, guild_id: int, game_id: int
) -> GameWithParticipants | None:
    """Lock one game and return its current active roster.

    The caller must keep a transaction open for the duration of any decision
    based on this result.  This mirrors ``seasons.lock_active_season`` and is
    deliberately guild-scoped before ``FOR UPDATE``.
    """
    require_id(guild_id, name="guild_id")
    require_id(game_id, name="game_id")
    row = await conn.fetchrow(_LOCK_GAME_SQL, guild_id, game_id)
    if row is None:
        return None
    game = _row_to_game(row)
    participants = await conn.fetch(_SELECT_GAME_PARTICIPANTS_SQL, guild_id, game_id)
    winner_id: int | None = None
    losers: list[int] = []
    scores: list[PlayerScore] = []
    for participant in participants:
        if participant["is_winner"]:
            winner_id = participant["user_id"]
        else:
            losers.append(participant["user_id"])
        score = _row_to_player_score(participant, game)
        if score is not None:
            scores.append(score)
    if winner_id is None:  # pragma: no cover -- schema/application invariant.
        raise RuntimeError(f"game {game_id} in guild {guild_id} has no active winner")
    return GameWithParticipants(game, winner_id, tuple(losers), tuple(scores))


def _snapshot(game: GameWithParticipants) -> str:
    """A deterministic, bounded representation of an authoritative roster."""
    g = game.game
    score_by_user = {score.user_id: score for score in game.scores}
    users = [game.winner_id, *game.loser_ids]
    participants: list[dict[str, object]] = []
    for user_id in users:
        score = score_by_user.get(user_id)
        participants.append(
            {
                "user_id": user_id,
                "is_winner": user_id == game.winner_id,
                "total_points": score.total_points if score is not None else None,
                "score_breakdown": (
                    {entry.key: entry.points for entry in score.breakdown}
                    if score is not None
                    else None
                ),
            }
        )
    payload = {
        "game": {
            "game_id": g.game_id,
            "guild_id": g.guild_id,
            "season_id": g.season_id,
            "played_on": g.played_on.isoformat(),
            "game_type": g.game_type,
            "extension_5_6": g.extension_5_6,
            "scenario": g.scenario,
            "target_points": g.target_points,
            "played_at": g.played_at.isoformat() if g.played_at is not None else None,
            "played_timezone": g.played_timezone,
            "revision": g.revision,
        },
        "participants": participants,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


async def update_confirmed_game(
    conn: asyncpg.Connection,
    guild_id: int,
    game_id: int,
    *,
    expected_revision: int,
    updated_by: int,
    reason: str | None,
    played_on: date,
    winner_id: int,
    loser_ids: Sequence[int],
    game_type: str,
    extension_5_6: bool,
    scenario: str | None,
    target_points: int | None,
    played_at: datetime | None,
    played_timezone: str | None,
    scores: Sequence[PlayerScore] | None,
) -> GameWithParticipants | str:
    """Replace the active roster/details of a confirmed game and audit it.

    Returns ``not_found``, ``not_confirmed``, or ``stale`` without mutating
    when the guarded update cannot apply.  It never changes the game season,
    report/confirmation identity, or Discord message identity.
    """
    require_id(guild_id, name="guild_id")
    require_id(game_id, name="game_id")
    expected_revision = require_int(
        expected_revision, name="expected_revision", min_value=0, max_value=2**31 - 1
    )
    require_id(updated_by, name="updated_by")
    reason = _require_optional_bounded_str(reason, name="reason", min_length=1, max_length=200)
    require_id(winner_id, name="winner_id")
    game_type = _require_game_type(game_type)
    extension_5_6 = _require_extension(extension_5_6)
    scenario = _require_optional_bounded_str(
        scenario, name="scenario", min_length=1, max_length=100
    )
    if target_points is not None:
        target_points = require_int(target_points, name="target_points", min_value=1, max_value=99)
    played_at, played_timezone = _validate_played_time(played_at, played_timezone)
    loser_ids = list(loser_ids)
    for i, loser_id in enumerate(loser_ids):
        require_id(loser_id, name=f"loser_ids[{i}]")
    user_ids = [winner_id, *loser_ids]
    if len(set(user_ids)) != len(user_ids):
        raise ValueError("winner_id and loser_ids must not contain duplicates")
    points, breakdowns = _scores_for_participants(scores, user_ids)

    async with conn.transaction():
        before = await lock_game(conn, guild_id, game_id)
        if before is None:
            return "not_found"
        if before.game.status != "confirmed":
            return "not_confirmed"
        if before.game.revision != expected_revision:
            return "stale"
        updated_row = await conn.fetchrow(
            _UPDATE_CONFIRMED_GAME_SQL,
            guild_id,
            game_id,
            played_on,
            game_type,
            extension_5_6,
            scenario,
            target_points,
            played_at,
            played_timezone,
            updated_by,
            reason,
            expected_revision,
        )
        if updated_row is None:  # pragma: no cover -- lock prevents a concurrent transition.
            return "stale"
        await conn.execute(_DEACTIVATE_GAME_PARTICIPANTS_SQL, guild_id, game_id)
        await conn.execute(
            _UPSERT_GAME_PARTICIPANTS_SQL,
            game_id,
            guild_id,
            user_ids,
            [True, *([False] * len(loser_ids))],
            points,
            breakdowns,
        )
        updated = await get_game(conn, guild_id, game_id)
        if updated is None:  # pragma: no cover -- protected by the locked game row.
            raise RuntimeError("updated game disappeared")
        await conn.execute(
            _INSERT_GAME_UPDATE_SQL,
            game_id,
            updated.game.revision,
            guild_id,
            updated_by,
            updated.game.updated_at,
            reason,
            _snapshot(before),
            _snapshot(updated),
        )
    return updated


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
