"""`game_score_requests` repository.

Every SQL statement here is a module-level string constant, bound exactly
once, with values passed only as `$n` arguments (see CLAUDE.md and
`tests/static/sql_guard.py`).

One row per (game, participant), created once when a game is reported and
updated as that participant's DM is delivered/blocked and, eventually,
submitted. `claim_due_prompts` is the scheduler's system-wide sweep for
players who haven't submitted yet -- see its docstring for how it differs
from `events.claim_due_reminders`'s plain single-statement claim.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import asyncpg

from catan_bot.db.models import ScoreRequest
from catan_bot.db.repositories._params import require_aware, require_id, require_limit

# The scheduler re-prompts a player at most 3 times total before giving up
# and leaving the request outstanding for a human to chase manually.
_MAX_PROMPTS = 3

_INSERT_SCORE_REQUESTS_SQL = """
INSERT INTO game_score_requests (game_id, guild_id, user_id, requested_at, next_prompt_at)
SELECT $1, $2, u, $3, $3 + INTERVAL '24 hours'
FROM unnest($4::bigint[]) AS t(u)
"""

_MARK_DELIVERED_SQL = """
UPDATE game_score_requests
SET delivery_status = 'delivered', dm_channel_id = $4, dm_message_id = $5
WHERE guild_id = $1 AND game_id = $2 AND user_id = $3 AND submitted_at IS NULL
RETURNING game_id
"""

_MARK_BLOCKED_SQL = """
UPDATE game_score_requests
SET delivery_status = 'blocked'
WHERE guild_id = $1 AND game_id = $2 AND user_id = $3 AND submitted_at IS NULL
RETURNING game_id
"""

_MARK_SUBMITTED_SQL = """
UPDATE game_score_requests
SET submitted_at = $4, next_prompt_at = NULL
WHERE guild_id = $1 AND game_id = $2 AND user_id = $3 AND submitted_at IS NULL
RETURNING game_id
"""

_SELECT_REQUESTS_FOR_GAME_SQL = """
SELECT game_id, guild_id, user_id, dm_channel_id, dm_message_id, delivery_status,
       requested_at, next_prompt_at, prompts_sent, submitted_at
FROM game_score_requests
WHERE guild_id = $1 AND game_id = $2
ORDER BY user_id
"""

# Backs `/game scores` (no game id given): a player's own ephemeral fallback
# sheet for whichever game most recently asked them for a score and hasn't
# gotten one yet. Joined against `games` (not just `game_score_requests`)
# so a request left dangling by a since-rejected/voided game -- which never
# gets an explicit score_requests cleanup -- doesn't surface a sheet that
# can no longer accept a write; `record_player_score`/`clear_player_score`
# re-check the game's live status anyway, but there's no reason to hand a
# player a dead sheet in the first place.
_SELECT_LATEST_OPEN_REQUEST_FOR_USER_SQL = """
SELECT r.game_id, r.guild_id, r.user_id, r.dm_channel_id, r.dm_message_id, r.delivery_status,
       r.requested_at, r.next_prompt_at, r.prompts_sent, r.submitted_at
FROM game_score_requests r
JOIN games g ON g.game_id = r.game_id AND g.guild_id = r.guild_id
WHERE r.guild_id = $1 AND r.user_id = $2 AND r.submitted_at IS NULL
      AND g.status IN ('pending', 'confirmed')
ORDER BY r.requested_at DESC
LIMIT 1
"""

# System-wide (no guild_id): the scheduler must see every guild's due
# re-prompts in one pass, matching `events.claim_due_reminders`. Unlike that
# query, this one is bounded by a caller-supplied `limit`, and Postgres has
# no `UPDATE ... LIMIT` -- so the candidate rows are chosen first, in a CTE,
# with `FOR UPDATE SKIP LOCKED`, and only those are updated. SKIP LOCKED
# means two concurrent scheduler ticks split the due backlog between them
# instead of one blocking on rows the other is mid-claim on; the tradeoff is
# that a due row already locked elsewhere is simply left for the next tick
# rather than waited for, which is fine here since ticks run frequently and
# every claimed row is still re-armed (never lost, just possibly delayed by
# one tick). Claiming and rescheduling happen in the same statement, so the
# caller never has to separately re-arm a row after deciding to send.
_CLAIM_DUE_PROMPTS_SQL = """
WITH due AS (
    SELECT game_id, user_id
    FROM game_score_requests
    WHERE submitted_at IS NULL
          AND next_prompt_at IS NOT NULL
          AND next_prompt_at <= $1
          AND prompts_sent < $3
    ORDER BY next_prompt_at
    LIMIT $2
    FOR UPDATE SKIP LOCKED
)
UPDATE game_score_requests r
SET prompts_sent = r.prompts_sent + 1,
    next_prompt_at = $1 + INTERVAL '24 hours'
FROM due
WHERE r.game_id = due.game_id AND r.user_id = due.user_id
RETURNING r.game_id, r.guild_id, r.user_id, r.dm_channel_id, r.dm_message_id,
          r.delivery_status, r.requested_at, r.next_prompt_at, r.prompts_sent,
          r.submitted_at
"""


def _row_to_score_request(row: asyncpg.Record) -> ScoreRequest:
    return ScoreRequest(
        game_id=row["game_id"],
        guild_id=row["guild_id"],
        user_id=row["user_id"],
        dm_channel_id=row["dm_channel_id"],
        dm_message_id=row["dm_message_id"],
        delivery_status=row["delivery_status"],
        requested_at=row["requested_at"],
        next_prompt_at=row["next_prompt_at"],
        prompts_sent=row["prompts_sent"],
        submitted_at=row["submitted_at"],
    )


async def create_score_requests(
    conn: asyncpg.Connection,
    guild_id: int,
    game_id: int,
    user_ids: Sequence[int],
    requested_at: datetime,
) -> None:
    """Bulk-insert one pending score request per participant for a new game.

    Called once, right alongside the winner+losers DM fan-out for a freshly
    reported game: every participant gets exactly one `pending` row here,
    with no DM identity yet (`mark_delivered`/`mark_blocked` fill that in
    once the DM actually sends) and its first re-prompt checkpoint already
    seeded 24 hours out, so the scheduler sweep has something to find even
    if delivery never gets recorded.
    """
    require_id(guild_id, name="guild_id")
    require_id(game_id, name="game_id")
    requested_at = require_aware(requested_at, name="requested_at")
    ids = list(user_ids)
    for i, user_id in enumerate(ids):
        require_id(user_id, name=f"user_ids[{i}]")
    if not ids:
        raise ValueError("user_ids must not be empty")
    if len(set(ids)) != len(ids):
        raise ValueError("user_ids must not contain duplicates")
    await conn.execute(_INSERT_SCORE_REQUESTS_SQL, game_id, guild_id, requested_at, ids)


async def mark_delivered(
    conn: asyncpg.Connection,
    guild_id: int,
    game_id: int,
    user_id: int,
    dm_channel_id: int,
    dm_message_id: int,
) -> bool:
    """Record a successfully-sent DM's channel/message ids.  Returns whether a row was updated."""
    require_id(guild_id, name="guild_id")
    require_id(game_id, name="game_id")
    require_id(user_id, name="user_id")
    require_id(dm_channel_id, name="dm_channel_id")
    require_id(dm_message_id, name="dm_message_id")
    row = await conn.fetchrow(
        _MARK_DELIVERED_SQL, guild_id, game_id, user_id, dm_channel_id, dm_message_id
    )
    return row is not None


async def mark_blocked(conn: asyncpg.Connection, guild_id: int, game_id: int, user_id: int) -> bool:
    """Record that a player's DMs are closed (or otherwise undeliverable).

    Left as its own status rather than folded into a boolean, since a later
    phase can use it to decide whether to keep re-prompting via DM at all or
    fall back to some other channel.  Returns whether a row was updated.
    """
    require_id(guild_id, name="guild_id")
    require_id(game_id, name="game_id")
    require_id(user_id, name="user_id")
    row = await conn.fetchrow(_MARK_BLOCKED_SQL, guild_id, game_id, user_id)
    return row is not None


async def mark_submitted(
    conn: asyncpg.Connection, guild_id: int, game_id: int, user_id: int, submitted_at: datetime
) -> bool:
    """Mark a request submitted and clear its re-prompt schedule.

    Guarded by ``submitted_at IS NULL`` like every other writer here, so a
    second submission attempt (e.g. a stale DM re-opened after the
    scheduler already re-prompted and the player answered the newer one)
    is simply a no-op instead of overwriting `submitted_at`.  Returns
    whether a row was updated.
    """
    require_id(guild_id, name="guild_id")
    require_id(game_id, name="game_id")
    require_id(user_id, name="user_id")
    submitted_at = require_aware(submitted_at, name="submitted_at")
    row = await conn.fetchrow(_MARK_SUBMITTED_SQL, guild_id, game_id, user_id, submitted_at)
    return row is not None


async def list_score_requests(
    conn: asyncpg.Connection, guild_id: int, game_id: int
) -> list[ScoreRequest]:
    """Every score request for one game, in user id order."""
    require_id(guild_id, name="guild_id")
    require_id(game_id, name="game_id")
    rows = await conn.fetch(_SELECT_REQUESTS_FOR_GAME_SQL, guild_id, game_id)
    return [_row_to_score_request(row) for row in rows]


async def get_latest_open_request(
    conn: asyncpg.Connection, guild_id: int, user_id: int
) -> ScoreRequest | None:
    """The most recently requested game this user hasn't yet submitted a score for.

    `None` when the user has no open request (nothing pending, or every open
    game's request already got a row -- see `_SELECT_LATEST_OPEN_REQUEST_FOR_USER_SQL`
    for why a rejected/voided game's leftover request never counts).
    """
    require_id(guild_id, name="guild_id")
    require_id(user_id, name="user_id")
    row = await conn.fetchrow(_SELECT_LATEST_OPEN_REQUEST_FOR_USER_SQL, guild_id, user_id)
    return _row_to_score_request(row) if row is not None else None


async def claim_due_prompts(
    conn: asyncpg.Connection, now: datetime, limit: int
) -> list[ScoreRequest]:
    """Atomically claim up to `limit` score requests due for a re-prompt.

    "Claiming" a row also reschedules it: `prompts_sent` is incremented and
    `next_prompt_at` pushed 24 hours out from `now` in the same statement,
    so the caller only has to decide whether to actually send the DM, never
    to separately re-arm the row afterward. A request with
    `prompts_sent >= 3` is left alone rather than claimed indefinitely --
    see `_MAX_PROMPTS` and this module's docstring for the at-most-once
    claim pattern and its SKIP LOCKED tradeoff.
    """
    now = require_aware(now, name="now")
    limit = require_limit(limit)
    rows = await conn.fetch(_CLAIM_DUE_PROMPTS_SQL, now, limit, _MAX_PROMPTS)
    return [_row_to_score_request(row) for row in rows]
