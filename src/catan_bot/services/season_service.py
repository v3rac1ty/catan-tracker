"""Season lifecycle: start, adjust, cancel, resolve, and read back.

`_resolve` is the single resolution helper shared by `end_season_now` (an
admin acting now) and `resolve_due_seasons` (the scheduler, driven by
`lock_due_seasons`), so both paths compute standings and freeze results
identically.
"""

from __future__ import annotations

import logging
from datetime import datetime

import asyncpg

from catan_bot.db.errors import ActiveSeasonExistsError
from catan_bot.db.models import GuildConfig, Season, SeasonResultRow
from catan_bot.db.repositories import guilds, seasons
from catan_bot.domain.bet import outcome_for, resolve_bet
from catan_bot.domain.dates import parse_date, season_end_instant, today_in_timezone
from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.ranking import rank_players
from catan_bot.domain.validation import (
    SEASON_NAME_MAX,
    clean_text,
    validate_min_games,
    validate_season_window,
)
from catan_bot.services.context import Actor, require_admin, require_valid_timezone
from catan_bot.services.errors import ConflictError
from catan_bot.services.results import Announcement, SeasonInfo, SeasonResolution

logger = logging.getLogger(__name__)

_HISTORY_MIN_LIMIT, _HISTORY_MAX_LIMIT = 1, 25
_ANNOUNCEMENTS_MIN_LIMIT, _ANNOUNCEMENTS_MAX_LIMIT = 1, 50

_NO_ACTIVE_SEASON = "There's no active season."
_ACTIVE_SEASON_EXISTS = "A season is already active. End or cancel it first."
_ALREADY_RESOLVED = "This season has already been resolved."
_END_DATE_REQUIRED = "A season needs an end date."


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


async def start_season(
    pool: asyncpg.Pool,
    guild_id: int,
    actor: Actor,
    *,
    name: str,
    end_date_text: str | None,
    start_date_text: str | None,
    min_games: int | None,
    now: datetime,
) -> Season:
    async with pool.acquire() as conn, conn.transaction():
        config = await guilds.ensure_guild(conn, guild_id)
        require_admin(actor, config)

        clean_name = clean_text(name, field="Season name", max_len=SEASON_NAME_MAX, min_len=1)
        timezone = require_valid_timezone(config)
        today = today_in_timezone(timezone, now=now)
        starts_on = parse_date(start_date_text, today=today)
        if end_date_text is None or not end_date_text.strip():
            raise DomainValidationError(_END_DATE_REQUIRED)
        ends_on = parse_date(end_date_text, today=today)
        validate_season_window(starts_on, ends_on, today=today)

        n = min_games if min_games is not None else config.default_min_games
        n = validate_min_games(n)
        ends_at = season_end_instant(ends_on, timezone)

        try:
            return await seasons.create_season(
                conn, guild_id, clean_name, starts_on, ends_on, ends_at, n, actor.user_id
            )
        except ActiveSeasonExistsError as exc:
            raise ConflictError(_ACTIVE_SEASON_EXISTS) from exc


async def set_min_games(
    pool: asyncpg.Pool, guild_id: int, actor: Actor, n: int
) -> tuple[Season | None, GuildConfig]:
    """Update the guild default *and* the active season's threshold, atomically.

    Both writes happen in the same transaction: if either fails, neither
    takes effect.
    """
    validated = validate_min_games(n)
    async with pool.acquire() as conn, conn.transaction():
        config = await guilds.ensure_guild(conn, guild_id)
        require_admin(actor, config)
        updated_config = await guilds.set_default_min_games(conn, guild_id, validated)
        if updated_config is None:  # pragma: no cover -- ensure_guild guarantees the row exists.
            raise RuntimeError(
                f"guild_config row for guild {guild_id} vanished during set_min_games"
            )
        updated_season = await seasons.set_active_min_games(conn, guild_id, validated)
        return updated_season, updated_config


async def set_end_date(
    pool: asyncpg.Pool, guild_id: int, actor: Actor, end_date_text: str, now: datetime
) -> Season:
    async with pool.acquire() as conn, conn.transaction():
        config = await guilds.ensure_guild(conn, guild_id)
        require_admin(actor, config)
        active = await seasons.get_active_season(conn, guild_id)
        if active is None:
            raise ConflictError(_NO_ACTIVE_SEASON)

        if end_date_text is None or not end_date_text.strip():
            raise DomainValidationError(_END_DATE_REQUIRED)
        timezone = require_valid_timezone(config)
        today = today_in_timezone(timezone, now=now)
        ends_on = parse_date(end_date_text, today=today)
        validate_season_window(active.starts_on, ends_on, today=today)
        ends_at = season_end_instant(ends_on, timezone)

        updated = await seasons.set_active_end(conn, guild_id, ends_on, ends_at)
        if updated is None:
            raise ConflictError(_NO_ACTIVE_SEASON)
        return updated


async def cancel_season(pool: asyncpg.Pool, guild_id: int, actor: Actor) -> Season:
    async with pool.acquire() as conn, conn.transaction():
        config = await guilds.ensure_guild(conn, guild_id)
        require_admin(actor, config)
        cancelled = await seasons.cancel_active_season(conn, guild_id)
        if cancelled is None:
            raise ConflictError(_NO_ACTIVE_SEASON)
        return cancelled


async def _resolve(conn: asyncpg.Connection, season: Season) -> SeasonResolution:
    """Compute standings, resolve the bet, freeze results, and complete the season.

    Shared by `end_season_now` and `resolve_due_seasons`. Must run inside a
    transaction the caller holds: `seasons.complete_season` nests its own
    transaction as a savepoint.
    """
    stats = await seasons.season_player_stats(conn, season.guild_id, season.season_id)
    ranked = rank_players(stats, min_games=season.min_games)
    outcome = resolve_bet(ranked)
    results = [
        SeasonResultRow(
            user_id=p.user_id,
            rank=p.rank,
            games=p.games,
            wins=p.wins,
            eligible=p.eligible,
            outcome=outcome_for(p.user_id, outcome),
        )
        for p in ranked
    ]
    completed = await seasons.complete_season(conn, season.guild_id, season.season_id, results)
    if not completed:
        raise ConflictError(_ALREADY_RESOLVED)
    return SeasonResolution(season=season, ranked=ranked, outcome=outcome, completed=completed)


async def end_season_now(
    pool: asyncpg.Pool, guild_id: int, actor: Actor, now: datetime
) -> SeasonResolution:
    async with pool.acquire() as conn, conn.transaction():
        config = await guilds.ensure_guild(conn, guild_id)
        require_admin(actor, config)
        # `lock_active_season` (FOR UPDATE), not `get_active_season`: holding
        # the row lock for the rest of this transaction means a concurrent
        # `set_min_games` can't change `min_games` between this read and
        # `_resolve` freezing results computed from it -- it blocks until
        # this transaction commits, by which point the season is no longer
        # active and its own update becomes a no-op (see season_service's
        # test suite for the race).
        season = await seasons.lock_active_season(conn, guild_id)
        if season is None:
            raise ConflictError(_NO_ACTIVE_SEASON)
        return await _resolve(conn, season)


def _log_resolution_failure(season_id: int, exc: Exception) -> None:
    """Log a season-resolution failure without leaking row contents.

    `asyncpg.PostgresError.__str__` (`PostgresMessage.__str__`) appends the
    server's DETAIL/HINT text, which can contain row contents (e.g. a
    season name via a CHECK violation message) -- so this never calls
    `str(exc)` or passes `exc_info` (a chained traceback can end with that
    same leaky `str()`). `sqlstate` is always safe to log: a fixed 5-character error
    class code, not server-supplied text.
    """
    if isinstance(exc, asyncpg.PostgresError):
        logger.error(
            "Season resolution failed for season_id=%s: %s (sqlstate=%s)",
            season_id,
            type(exc).__name__,
            exc.sqlstate,
        )
    else:
        logger.error(
            "Season resolution failed for season_id=%s: %s",
            season_id,
            type(exc).__name__,
        )


async def resolve_due_seasons(pool: asyncpg.Pool, now: datetime) -> list[SeasonResolution]:
    """Resolve every due season across all guilds, isolating one guild's failure from the rest.

    Each iteration opens a fresh transaction and locks *at most one*
    candidate season via `lock_next_due_season` (excluding every id already
    in `failed`), rather than locking every currently-due season up front
    (`lock_due_seasons`) -- so a slow or paused resolution of one guild's
    season never also holds a lock on some *other* guild's due season,
    which would otherwise block that guild's own `/season end` or the next
    concurrent scheduler tick. A season whose resolution raises is logged
    (by id and exception type only -- never its name, any other
    user-supplied text, or a DETAIL/HINT message) and added to `failed`, so
    the next iteration retries the remaining candidates with a fresh
    transaction. `failed` only grows and a resolved season stops being
    `due` (it's no longer `status = 'active'`), so each iteration strictly
    shrinks the effective candidate set: the loop always terminates once
    nothing resolvable is left.
    """
    resolved: list[SeasonResolution] = []
    failed: set[int] = set()
    while True:
        candidate: Season | None = None
        try:
            async with pool.acquire() as conn, conn.transaction():
                candidate = await seasons.lock_next_due_season(conn, now, sorted(failed))
                if candidate is None:
                    return resolved
                resolution = await _resolve(conn, candidate)
        except Exception as exc:
            if candidate is None:  # pragma: no cover -- lock_next_due_season itself would fail.
                raise
            _log_resolution_failure(candidate.season_id, exc)
            failed.add(candidate.season_id)
            continue
        resolved.append(resolution)


async def pending_announcements(pool: asyncpg.Pool, limit: int) -> list[Announcement]:
    n = _clamp(limit, _ANNOUNCEMENTS_MIN_LIMIT, _ANNOUNCEMENTS_MAX_LIMIT)
    async with pool.acquire() as conn:
        due_seasons = await seasons.list_unannounced_completed(conn, n)
    announcements: list[Announcement] = []
    for season in due_seasons:
        announcement = await _read_pending_announcement(pool, season)
        if announcement is not None:
            announcements.append(announcement)
    return announcements


def _log_announcement_read_failure(season: Season, exc: Exception) -> None:
    """Log only identifiers and an exception class for one announcement read."""
    if isinstance(exc, asyncpg.PostgresError):
        logger.error(
            "Season announcement read failed for season_id=%s guild_id=%s: %s (sqlstate=%s)",
            season.season_id,
            season.guild_id,
            type(exc).__name__,
            exc.sqlstate,
        )
    else:
        logger.error(
            "Season announcement read failed for season_id=%s guild_id=%s: %s",
            season.season_id,
            season.guild_id,
            type(exc).__name__,
        )


async def _read_pending_announcement(pool: asyncpg.Pool, season: Season) -> Announcement | None:
    """Read one frozen result set so a guild failure does not block its peers."""
    try:
        async with pool.acquire() as conn:
            results = await seasons.get_season_results(conn, season.guild_id, season.season_id)
    except Exception as exc:
        _log_announcement_read_failure(season, exc)
        return None
    return Announcement(season=season, results=results)


async def mark_announced(pool: asyncpg.Pool, guild_id: int, season_id: int) -> None:
    async with pool.acquire() as conn:
        await seasons.mark_announced(conn, guild_id, season_id)


async def season_info(pool: asyncpg.Pool, guild_id: int) -> SeasonInfo | None:
    async with pool.acquire() as conn:
        season = await seasons.get_active_season(conn, guild_id)
        if season is None:
            return None
        stats = await seasons.season_player_stats(conn, guild_id, season.season_id)
        ranked = rank_players(stats, min_games=season.min_games)
        return SeasonInfo(season=season, ranked=ranked)


async def season_history(pool: asyncpg.Pool, guild_id: int, limit: int) -> list[Season]:
    n = _clamp(limit, _HISTORY_MIN_LIMIT, _HISTORY_MAX_LIMIT)
    async with pool.acquire() as conn:
        return await seasons.list_seasons(conn, guild_id, n)


async def season_results(
    pool: asyncpg.Pool, guild_id: int, season_id: int
) -> list[SeasonResultRow]:
    """A season's frozen standings. Always reads `season_results` -- never live stats."""
    async with pool.acquire() as conn:
        return await seasons.get_season_results(conn, guild_id, season_id)
