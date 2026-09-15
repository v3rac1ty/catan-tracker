"""`players` repository.

Only one function: registering players is purely additive bookkeeping (a
player row's only purpose is to give `games`/`season_results` an FK target),
so there's nothing to read back or update here.
"""

from __future__ import annotations

from collections.abc import Sequence

import asyncpg

from catan_bot.db.repositories._params import require_id

_INSERT_PLAYERS_SQL = """
INSERT INTO players (guild_id, user_id)
SELECT $1, unnest($2::bigint[])
ON CONFLICT (guild_id, user_id) DO NOTHING
"""


async def ensure_players(conn: asyncpg.Connection, guild_id: int, user_ids: Sequence[int]) -> None:
    """Register every id in `user_ids` as a player of `guild_id`, idempotently.

    Callers (e.g. `games.create_game`) must call this before inserting rows
    that FK to `players`, so an unregistered id fails loudly with
    `asyncpg.ForeignKeyViolationError` instead of silently.
    """
    require_id(guild_id, name="guild_id")
    # Materialize exactly once: `user_ids` may be a one-shot iterable (e.g. a
    # generator), and validating it here must not be the thing that consumes
    # it before the SQL call ever sees it (N1 audit finding).
    user_ids = list(user_ids)
    for i, user_id in enumerate(user_ids):
        require_id(user_id, name=f"user_ids[{i}]")
    await conn.execute(_INSERT_PLAYERS_SQL, guild_id, user_ids)
