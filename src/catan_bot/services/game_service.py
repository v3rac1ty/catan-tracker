"""Game reporting and the confirm/reject/void lifecycle.

Status-transition repository calls (`confirm_game`, `reject_game`,
`void_game`) return a `TransitionResult` rather than raising; each function
here maps that result to either a `GameWithParticipants` (success) or a
fixed-message `ServiceError` (failure) -- never a raw repository outcome.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import asyncpg

from catan_bot.db.models import Game, GameWithParticipants, TransitionResult
from catan_bot.db.repositories import games, guilds, players, seasons
from catan_bot.domain.dates import combine_local, parse_date, parse_time, today_in_timezone
from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.scoring import GameType, PlayerScore, build_rules, validate_game_scores
from catan_bot.domain.validation import (
    VOID_REASON_MAX,
    ParticipantRef,
    clean_text,
    validate_game_date,
    validate_game_in_season,
    validate_participants,
)
from catan_bot.services.context import Actor, require_admin, require_valid_timezone
from catan_bot.services.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ServiceError,
)
from catan_bot.services.results import PreparedGameReport

_HISTORY_MIN_LIMIT, _HISTORY_MAX_LIMIT = 1, 25

_GAME_NOT_FOUND = "That game report doesn't exist."
_GAME_NOT_PENDING = "That game has already been confirmed, rejected, or voided."
_REPORTER_CANNOT_CONFIRM = "You reported this game, so another player has to confirm it."
_CONFIRM_NOT_PARTICIPANT = "Only a player in that game can confirm it."
_REJECT_NOT_PARTICIPANT = "Only a player in that game (or the original reporter) can reject it."
_GAME_NOT_PENDING_OR_CONFIRMED = "That game has already been rejected or voided."


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, n))


async def report_game(
    pool: asyncpg.Pool,
    guild_id: int,
    actor: Actor,
    *,
    winner: ParticipantRef,
    losers: Sequence[ParticipantRef],
    date_text: str | None,
    now: datetime,
) -> GameWithParticipants:
    """Validate, then create a pending game and its participants in one transaction.

    The reporter (`actor.user_id`) need not be one of `winner`/`losers`.
    """
    prepared = await prepare_game_report(
        pool,
        guild_id,
        actor,
        winner=winner,
        losers=losers,
        date_text=date_text,
        time_text=None,
        now=now,
    )
    return await submit_game_report(pool, guild_id, actor, prepared, scores=None, now=now)


async def prepare_game_report(
    pool: asyncpg.Pool,
    guild_id: int,
    actor: Actor,
    *,
    winner: ParticipantRef,
    losers: Sequence[ParticipantRef],
    date_text: str | None,
    time_text: str | None,
    now: datetime,
    game_type: GameType = "normal",
    extension_5_6: bool | None = None,
    scenario: str | None = None,
    target_points: int | None = None,
) -> PreparedGameReport:
    """Validate game metadata before displaying a score sheet.

    This operation deliberately creates no player or game rows.  Guild
    configuration may be initialized for a first-time guild, matching the
    other services' ``ensure_guild`` behavior.
    """
    winner_id, loser_ids = validate_participants(winner, losers)
    player_count = 1 + len(loser_ids)
    if extension_5_6 is None:
        extension_5_6 = player_count >= 5
    elif player_count >= 5 and extension_5_6 is False:
        raise DomainValidationError("Games with 5 or 6 players require the 5–6 Player Extension.")
    clean_scenario = clean_text(scenario, field="Scenario", max_len=100, min_len=0)
    rules = build_rules(
        game_type,
        extension_5_6=extension_5_6,
        scenario=clean_scenario,
        target_points=target_points,
        player_count=player_count,
    )
    async with pool.acquire() as conn, conn.transaction():
        config = await guilds.ensure_guild(conn, guild_id)
        timezone = require_valid_timezone(config)
        today = today_in_timezone(timezone, now=now)
        played_on = parse_date(date_text, today=today)
        validate_game_date(played_on, today=today)

        active_season = await seasons.get_active_season(conn, guild_id)
        if active_season is not None:
            validate_game_in_season(
                played_on, starts_on=active_season.starts_on, ends_on=active_season.ends_on
            )
        played_at = None
        played_timezone = None
        if time_text is not None and time_text.strip():
            played_at = combine_local(played_on, parse_time(time_text), timezone)
            played_timezone = timezone
    return PreparedGameReport(
        guild_id=guild_id,
        reporter_id=actor.user_id,
        winner_id=winner_id,
        loser_ids=loser_ids,
        played_on=played_on,
        played_at=played_at,
        played_timezone=played_timezone,
        rules=rules,
    )


async def submit_game_report(
    pool: asyncpg.Pool,
    guild_id: int,
    actor: Actor,
    prepared: PreparedGameReport,
    *,
    scores: Sequence[PlayerScore] | None,
    now: datetime,
) -> GameWithParticipants:
    """Commit a prepared report and its optional complete score sheet."""
    if type(prepared) is not PreparedGameReport:
        raise ValueError("prepared must be a PreparedGameReport")
    if guild_id != prepared.guild_id or actor.user_id != prepared.reporter_id:
        raise PermissionDeniedError("Only the player who started this game report can submit it.")
    participant_ids = (prepared.winner_id, *prepared.loser_ids)
    validated_scores = validate_game_scores(
        prepared.rules,
        scores,
        participant_ids=participant_ids,
        winner_id=prepared.winner_id,
    )
    async with pool.acquire() as conn, conn.transaction():
        config = await guilds.ensure_guild(conn, guild_id)
        timezone = require_valid_timezone(config)
        today = today_in_timezone(timezone, now=now)
        validate_game_date(prepared.played_on, today=today)

        active_season = await seasons.get_active_season(conn, guild_id)
        season_id: int | None = None
        if active_season is not None:
            validate_game_in_season(
                prepared.played_on,
                starts_on=active_season.starts_on,
                ends_on=active_season.ends_on,
            )
            season_id = active_season.season_id

        await players.ensure_players(conn, guild_id, participant_ids)
        game = await games.create_game(
            conn,
            guild_id,
            season_id,
            prepared.played_on,
            actor.user_id,
            prepared.winner_id,
            prepared.loser_ids,
            game_type=prepared.rules.game_type,
            extension_5_6=prepared.rules.extension_5_6,
            scenario=prepared.rules.scenario,
            target_points=prepared.rules.target_points,
            played_at=prepared.played_at,
            played_timezone=prepared.played_timezone,
            scores=validated_scores,
        )
        return GameWithParticipants(
            game=game,
            winner_id=prepared.winner_id,
            loser_ids=prepared.loser_ids,
            scores=validated_scores,
        )


async def get_game(
    pool: asyncpg.Pool, guild_id: int, game_id: int
) -> GameWithParticipants:
    """Load one game through the guild-scoped repository query."""
    async with pool.acquire() as conn:
        loaded = await games.get_game(conn, guild_id, game_id)
    if loaded is None:
        raise NotFoundError(_GAME_NOT_FOUND)
    return loaded


def _confirm_result_error(result: TransitionResult) -> ServiceError:
    if result == "not_found":
        return NotFoundError(_GAME_NOT_FOUND)
    if result == "not_pending":
        return ConflictError(_GAME_NOT_PENDING)
    if result == "reporter_cannot_confirm":
        return PermissionDeniedError(_REPORTER_CANNOT_CONFIRM)
    if result == "not_participant":
        return PermissionDeniedError(_CONFIRM_NOT_PARTICIPANT)
    raise AssertionError(f"unexpected confirm_game result: {result!r}")  # pragma: no cover


def _reject_result_error(result: TransitionResult) -> ServiceError:
    if result == "not_found":
        return NotFoundError(_GAME_NOT_FOUND)
    if result == "not_pending":
        return ConflictError(_GAME_NOT_PENDING)
    if result == "not_participant":
        return PermissionDeniedError(_REJECT_NOT_PARTICIPANT)
    raise AssertionError(f"unexpected reject_game result: {result!r}")  # pragma: no cover


def _void_result_error(result: TransitionResult) -> ServiceError:
    if result == "not_found":
        return NotFoundError(_GAME_NOT_FOUND)
    if result == "not_pending_or_confirmed":
        return ConflictError(_GAME_NOT_PENDING_OR_CONFIRMED)
    raise AssertionError(f"unexpected void_game result: {result!r}")  # pragma: no cover


async def _load_or_die(
    conn: asyncpg.Connection, guild_id: int, game_id: int
) -> GameWithParticipants:
    loaded = await games.get_game(conn, guild_id, game_id)
    if loaded is None:  # pragma: no cover -- the guarded UPDATE above just succeeded on this row.
        raise RuntimeError(f"game {game_id} in guild {guild_id} vanished after a successful update")
    return loaded


async def confirm_game(
    pool: asyncpg.Pool, guild_id: int, game_id: int, actor: Actor
) -> GameWithParticipants:
    async with pool.acquire() as conn, conn.transaction():
        result = await games.confirm_game(conn, guild_id, game_id, actor.user_id)
        if result != "confirmed":
            raise _confirm_result_error(result)
        return await _load_or_die(conn, guild_id, game_id)


async def reject_game(
    pool: asyncpg.Pool, guild_id: int, game_id: int, actor: Actor
) -> GameWithParticipants:
    async with pool.acquire() as conn, conn.transaction():
        result = await games.reject_game(conn, guild_id, game_id, actor.user_id)
        if result != "rejected":
            raise _reject_result_error(result)
        return await _load_or_die(conn, guild_id, game_id)


async def void_game(
    pool: asyncpg.Pool, guild_id: int, game_id: int, actor: Actor, reason_text: str | None
) -> GameWithParticipants:
    """Void a game (admin only). `reason_text` may be empty."""
    reason = clean_text(reason_text, field="Void reason", max_len=VOID_REASON_MAX, min_len=0)
    async with pool.acquire() as conn, conn.transaction():
        config = await guilds.ensure_guild(conn, guild_id)
        require_admin(actor, config)
        result = await games.void_game(conn, guild_id, game_id, actor.user_id, reason)
        if result != "voided":
            raise _void_result_error(result)
        return await _load_or_die(conn, guild_id, game_id)


async def record_game_message(
    pool: asyncpg.Pool, guild_id: int, game_id: int, channel_id: int, message_id: int
) -> None:
    async with pool.acquire() as conn:
        await games.set_game_message(conn, guild_id, game_id, channel_id, message_id)


async def game_history(
    pool: asyncpg.Pool, guild_id: int, *, user_id: int | None, limit: int
) -> list[Game]:
    n = _clamp(limit, _HISTORY_MIN_LIMIT, _HISTORY_MAX_LIMIT)
    async with pool.acquire() as conn:
        if user_id is not None:
            return await games.list_recent_games_for_player(conn, guild_id, user_id, n)
        return await games.list_recent_games(conn, guild_id, n)
