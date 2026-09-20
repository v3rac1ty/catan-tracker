"""Recurring leaderboard posts: a daily digest sweep and a per-game trigger.

Two very different delivery-semantics decisions live side by side here,
mirroring the contrast `scheduler.py`'s module docstring draws between
season announcements (retried, so at-least-once) and event reminders
(claimed before delivery, so at-most-once):

  - `due_daily_leaderboards` claims each guild's digest (via
    `guilds.claim_daily_leaderboard`) *before* handing anything back to the
    scheduler to send. That makes the digest at-most-once: a crash, or a
    second concurrent tick, after the claim commits can never send the same
    guild's digest twice for the same local day -- but it *can* silently
    drop one if the process dies between the claim and the actual Discord
    send. That is the right tradeoff for a digest: a missed "here's today's
    standings" post is a shrug (tomorrow's digest, or the next `/leaderboard`
    call, still shows the truth), while a duplicate one is just noise in the
    channel, every single day it happens.
  - Season announcements (`season_service.pending_announcements` /
    `scheduler._send_announcement`) are the deliberate opposite: marked
    *after* a successful send, so a crash mid-send can duplicate one, but a
    transient failure is always retried until it goes through -- appropriate
    there because a season's final result is a one-time event that must
    eventually be seen, and re-announcing it once in a great while is a far
    smaller cost than silently losing it forever.

`leaderboard_after_game` has no claim to make at all: it is triggered
synchronously right after one specific game is confirmed (see
`views/game_confirm.py`), never swept across guilds by the scheduler, so
there is no restart/concurrency window here to protect against in the first
place -- at most one caller ever asks "what should this guild's per-game
post look like" for a given confirmation.
"""

from __future__ import annotations

import logging
from datetime import datetime

import asyncpg

from catan_bot.db.models import GameWithParticipants, GuildConfig
from catan_bot.db.repositories import games as games_repo
from catan_bot.db.repositories import guilds
from catan_bot.domain.dates import local_time_in_timezone, today_in_timezone
from catan_bot.domain.ranking import compute_movement
from catan_bot.services.results import LeaderboardPost
from catan_bot.services.stats_service import leaderboard as _read_leaderboard

logger = logging.getLogger(__name__)

_DAILY_LEADERBOARD_MIN_LIMIT, _DAILY_LEADERBOARD_MAX_LIMIT = 1, 50


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


async def _finalize_post(
    pool: asyncpg.Pool, config: GuildConfig, *, games: tuple[GameWithParticipants, ...] = ()
) -> LeaderboardPost:
    """Read the live board, compute movement, and persist the new ranking.

    Shared by the daily sweep and the per-game trigger. Reading the board
    (its own `pool.acquire()`, inside `stats_service.leaderboard`) and
    persisting the new ranking are deliberately two separate round trips
    rather than one held connection, matching how the rest of the services
    layer keeps a single guild's read/write pair from holding a connection
    open across unrelated work.
    """
    board = await _read_leaderboard(pool, config.guild_id, config.leaderboard_scope)
    ranking = [player.user_id for player in board.ranked]
    movements = compute_movement(config.leaderboard_last_ranking, ranking)
    async with pool.acquire() as conn:
        await guilds.set_leaderboard_ranking(conn, config.guild_id, ranking)
    if config.leaderboard_channel_id is None:  # pragma: no cover -- callers only reach here
        # with a channel configured; see due_daily_leaderboards/
        # leaderboard_after_game, both of which check this first.
        raise RuntimeError(f"guild {config.guild_id} has no leaderboard channel configured")
    return LeaderboardPost(
        guild_id=config.guild_id,
        channel_id=config.leaderboard_channel_id,
        board=board,
        movements=movements,
        games=games,
    )


async def leaderboard_after_game(pool: asyncpg.Pool, guild_id: int) -> LeaderboardPost | None:
    """What to post right after one game is confirmed, or `None` if nothing should post.

    Returns `None` (never raises) for every guild that isn't actively
    configured for a per-game post right now: `off`/`daily` mode, or
    `per_game` mode with no channel configured yet. The caller
    (`views/game_confirm.py`) treats this whole path as best-effort --
    the confirmation itself is already durably committed by the time this
    runs, so nothing here is allowed to make a confirmation appear to fail.
    """
    async with pool.acquire() as conn:
        config = await guilds.get_guild(conn, guild_id)
    if config is None or config.leaderboard_mode != "per_game":
        return None
    if config.leaderboard_channel_id is None:
        return None
    return await _finalize_post(pool, config)


def _log_daily_leaderboard_failure(guild_id: int, exc: Exception) -> None:
    """Log a failed daily-digest attempt without leaking row contents.

    Same no-DETAIL rule as `season_service._log_resolution_failure` /
    `event_service._log_reminder_read_failure`: never `str(exc)`, never a
    `PostgresError`'s DETAIL/HINT text, and no traceback -- only the guild
    id and the exception's type name, plus `sqlstate` when it's a
    `PostgresError` (a fixed 5-character error class code, not
    server-supplied text).
    """
    if isinstance(exc, asyncpg.PostgresError):
        logger.error(
            "Daily leaderboard digest failed for guild_id=%s: %s (sqlstate=%s)",
            guild_id,
            type(exc).__name__,
            exc.sqlstate,
        )
    else:
        logger.error(
            "Daily leaderboard digest failed for guild_id=%s: %s",
            guild_id,
            type(exc).__name__,
        )


async def _due_daily_post(
    pool: asyncpg.Pool, config: GuildConfig, now: datetime
) -> LeaderboardPost | None:
    """Claim and prepare one guild's digest, if it's actually due.

    Every failure here (a bad stored timezone, a transient connection
    error, ...) is caught and logged rather than propagated, so one guild's
    problem can never block -- or lose -- another guild's digest in the
    same sweep. `list_daily_leaderboard_guilds` already filters to
    `mode = 'daily'` with a channel configured, so this only has to decide
    *timing*: has today's configured post time passed locally, is there
    anything to report, and has this local day already been claimed.
    """
    try:
        local_date = today_in_timezone(config.timezone, now=now)
        local_time = local_time_in_timezone(config.timezone, now=now)
        if local_time < config.leaderboard_daily_time:
            return None
        async with pool.acquire() as conn:
            game_count = await games_repo.count_games_on_date(conn, config.guild_id, local_date)
        if game_count == 0:
            # A quiet local day: nothing to post, so nothing is claimed --
            # a game confirmed later the same day is still picked up by a
            # later tick, since `leaderboard_last_posted_on` was never set.
            return None

        # Claim *before* anything is read for rendering or sent: see this
        # module's docstring for why that ordering is what makes the digest
        # at-most-once. `claimed` reflects the guild's config as of the
        # claim (not the possibly-stale `config` passed in), so a
        # concurrent `/config leaderboard` change is never read stale.
        async with pool.acquire() as conn, conn.transaction():
            claimed = await guilds.claim_daily_leaderboard(conn, config.guild_id, local_date)
        if claimed is None:
            # Already posted for this local day -- a previous tick (or a
            # pre-restart run of this same tick) already claimed it.
            return None

        async with pool.acquire() as conn:
            day_games = await games_repo.list_games_on_date(conn, config.guild_id, local_date)
            # `list_games_on_date` (Phase 1) returns bare `Game` rows -- enough
            # to decide "is there anything to report" above -- but the digest
            # needs each game's winner/losers to say "who beat whom", so each
            # one is re-read with its roster. The day's game count is always
            # small (a handful at most), so the extra round trips are cheap.
            with_participants: list[GameWithParticipants] = []
            for game in day_games:
                full = await games_repo.get_game(conn, config.guild_id, game.game_id)
                if full is not None:
                    with_participants.append(full)
        return await _finalize_post(pool, claimed, games=tuple(with_participants))
    except Exception as exc:
        _log_daily_leaderboard_failure(config.guild_id, exc)
        return None


async def due_daily_leaderboards(
    pool: asyncpg.Pool, now: datetime, limit: int
) -> list[LeaderboardPost]:
    """Every daily-mode guild's digest that is due to post at `now`.

    Cheap when nothing is due: a guild that isn't in daily mode (or has no
    channel configured) never reaches this module at all --
    `list_daily_leaderboard_guilds` filters that at the SQL layer -- and a
    daily-mode guild whose configured time hasn't passed yet costs nothing
    beyond that one list query (`_due_daily_post` returns before any further
    read). `limit` bounds how many daily-mode guilds one sweep considers,
    matching `season_service.pending_announcements`'s
    `_ANNOUNCEMENTS_MAX_LIMIT` pattern -- the repository call itself has no
    `LIMIT` (Phase 1's `list_daily_leaderboard_guilds` is unconditional, like
    `events.claim_due_reminders`'s guild-wide sweep), so it's applied here.
    """
    n = _clamp(limit, _DAILY_LEADERBOARD_MIN_LIMIT, _DAILY_LEADERBOARD_MAX_LIMIT)
    async with pool.acquire() as conn:
        candidates = await guilds.list_daily_leaderboard_guilds(conn)

    posts: list[LeaderboardPost] = []
    for config in candidates[:n]:
        post = await _due_daily_post(pool, config, now)
        if post is not None:
            posts.append(post)
    return posts


__all__ = ["due_daily_leaderboards", "leaderboard_after_game"]
