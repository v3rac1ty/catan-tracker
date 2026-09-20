"""Game reporting and the confirm/reject/void lifecycle.

Status-transition repository calls (`confirm_game`, `reject_game`,
`void_game`) return a `TransitionResult` rather than raising; each function
here maps that result to either a `GameWithParticipants` (success) or a
fixed-message `ServiceError` (failure) -- never a raw repository outcome.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import cast

import asyncpg

from catan_bot.db.models import Game, GameWithParticipants, TransitionResult
from catan_bot.db.repositories import games, guilds, players, score_requests, seasons
from catan_bot.domain.dates import (
    combine_local,
    parse_date,
    parse_time,
    preserve_local_time,
    today_in_timezone,
)
from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.scoring import (
    GameType,
    PlayerScore,
    build_player_score,
    build_rules,
    validate_game_scores,
)
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
from catan_bot.services.results import (
    DueScorePrompt,
    PlayerScoreState,
    PreparedGameReport,
    PreparedGameUpdate,
    ScoreCollectionStatus,
)

logger = logging.getLogger(__name__)

_HISTORY_MIN_LIMIT, _HISTORY_MAX_LIMIT = 1, 25
# `games.confirm_game`/`reject_game` still guard `pending`; score collection
# additionally tolerates `confirmed` (an admin may confirm before every
# participant has submitted -- e.g. after a manual chase outside Discord),
# but never `rejected`/`voided`.
_SCORE_COLLECTION_OPEN_STATUSES = ("pending", "confirmed")

_GAME_NOT_FOUND = "That game report doesn't exist."
_GAME_NOT_PENDING = "That game has already been confirmed, rejected, or voided."
_REPORTER_CANNOT_CONFIRM = "You reported this game, so another player has to confirm it."
_CONFIRM_NOT_PARTICIPANT = "Only a player in that game can confirm it."
_REJECT_NOT_PARTICIPANT = "Only a player in that game (or the original reporter) can reject it."
_GAME_NOT_PENDING_OR_CONFIRMED = "That game has already been rejected or voided."
_UPDATE_NOT_CONFIRMED = "Only confirmed games can be updated."
_UPDATE_STALE = "That game was changed by someone else. Reload it and try again."
_UPDATE_NOOP = "No changes to save."
_UPDATE_COMPLETED_SEASON = "Games in a completed season can't change their players or winner."
_UPDATE_NOT_FOUND = "That game report doesn't exist."
_SCORE_GAME_NOT_OPEN = "This game is no longer accepting scores."
_SCORE_NOT_PARTICIPANT = "Only a participant in this game can submit a score for it."


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
    """Validate game metadata ahead of creating the game and its participants.

    This operation deliberately creates no player or game rows -- the game
    is created by the immediately-following `submit_game_report` call, not
    by anything shown back to the reporter first (there is no longer a
    reporter-facing draft to display; see `views/game_scores.py`'s
    docstring for how Phase 2 moved that to per-player DMs). Guild
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


async def get_game(pool: asyncpg.Pool, guild_id: int, game_id: int) -> GameWithParticipants:
    """Load one game through the guild-scoped repository query."""
    async with pool.acquire() as conn:
        loaded = await games.get_game(conn, guild_id, game_id)
    if loaded is None:
        raise NotFoundError(_GAME_NOT_FOUND)
    return loaded


def _refs(winner_id: int, loser_ids: Sequence[int]) -> tuple[ParticipantRef, ...]:
    return tuple(
        ParticipantRef(user_id=user_id, is_bot=False) for user_id in (winner_id, *loser_ids)
    )


def _same_roster(left: GameWithParticipants, winner_id: int, loser_ids: Sequence[int]) -> bool:
    return left.winner_id == winner_id and set(left.loser_ids) == set(loser_ids)


def _score_semantics(scores: Sequence[PlayerScore]) -> tuple[tuple[object, ...], ...]:
    """Return a stable, order-independent representation of score rows.

    Score rows are identified by player, while a breakdown is identified by
    source key.  Sorting both levels prevents a reordered loser list or form
    fields from looking like a real edit.  An empty tuple remains distinct
    from any explicit score rows, including rows whose totals are all zero.
    """
    return tuple(
        sorted(
            (
                score.user_id,
                score.total_points,
                tuple(sorted((entry.key, entry.points) for entry in score.breakdown)),
            )
            for score in scores
        )
    )


def _same_scores(left: Sequence[PlayerScore], right: Sequence[PlayerScore]) -> bool:
    """Compare score sheets by player and content, ignoring row/entry order."""
    return _score_semantics(left) == _score_semantics(right)


def _preserve_local_time(
    original: GameWithParticipants, played_on: date
) -> tuple[datetime | None, str | None]:
    if original.game.played_at is None or original.game.played_timezone is None:
        return None, None
    timezone = original.game.played_timezone
    return preserve_local_time(original.game.played_at, played_on, timezone), timezone


async def prepare_game_update(
    pool: asyncpg.Pool,
    guild_id: int,
    actor: Actor,
    *,
    game_id: int,
    winner: ParticipantRef | None,
    losers: Sequence[ParticipantRef] | None,
    date_text: str | None,
    time_text: str | None,
    game_type: GameType | None,
    extension_5_6: bool | None,
    scenario: str | None,
    target_points: int | None,
    reason: str | None,
    clear_time: bool = False,
    clear_scenario: bool = False,
    now: datetime,
) -> PreparedGameUpdate:
    """Prepare an admin-only edit of a confirmed game without writing it."""
    if clear_time and time_text is not None and time_text.strip():
        raise DomainValidationError(
            "Choose either a new time or clear the existing time, not both."
        )
    if clear_scenario and scenario is not None and scenario.strip():
        raise DomainValidationError(
            "Choose either a new scenario or clear the existing scenario, not both."
        )
    clean_reason = clean_text(reason, field="Update reason", max_len=200, min_len=0)

    async with pool.acquire() as conn, conn.transaction():
        config = await guilds.ensure_guild(conn, guild_id)
        require_admin(actor, config)
        original = await games.get_game(conn, guild_id, game_id)
        if original is None:
            raise NotFoundError(_UPDATE_NOT_FOUND)
        if original.game.status != "confirmed":
            raise ConflictError(_UPDATE_NOT_CONFIRMED)

        loser_refs = None if losers is None else tuple(losers)
        winner_id = original.winner_id if winner is None else winner.user_id
        existing_ids = (original.winner_id, *original.loser_ids)
        if loser_refs is None:
            if winner is not None:
                if winner_id not in existing_ids:
                    raise DomainValidationError(
                        "A replacement winner requires an explicit full loser list."
                    )
                loser_ids = tuple(user_id for user_id in existing_ids if user_id != winner_id)
            else:
                loser_ids = original.loser_ids
        else:
            loser_ids = tuple(ref.user_id for ref in loser_refs)
        effective_loser_refs = (
            loser_refs if loser_refs is not None else _refs(winner_id, loser_ids)[1:]
        )
        validate_participants(
            ParticipantRef(winner_id, winner.is_bot if winner is not None else False),
            effective_loser_refs,
        )
        if winner is not None and winner_id not in existing_ids and loser_refs is None:
            raise DomainValidationError(
                "A replacement winner requires an explicit full loser list."
            )

        player_count = 1 + len(loser_ids)
        effective_extension = extension_5_6
        if effective_extension is None:
            effective_extension = True if player_count >= 5 else original.game.extension_5_6
        if player_count >= 5 and effective_extension is False:
            raise DomainValidationError(
                "Games with 5 or 6 players require the 5–6 Player Extension."
            )

        effective_type = cast(
            GameType, game_type if game_type is not None else original.game.game_type
        )
        if clear_scenario:
            effective_scenario = None
        elif scenario is None:
            effective_scenario = original.game.scenario
        else:
            effective_scenario = clean_text(scenario, field="Scenario", max_len=100, min_len=0)
        rules_changed = (
            effective_type != original.game.game_type
            or effective_scenario != original.game.scenario
        )
        if target_points is None:
            if rules_changed:
                # Deliberately changing the rules must not silently carry a
                # scenario/Seafarers target forward; those modes require an
                # explicit target. Ordinary modes receive build_rules' 10/13
                # default and that default is persisted below.
                effective_target_input = None
                target_points_to_store = None
            else:
                effective_target_input = original.game.target_points
                target_points_to_store = original.game.target_points
        else:
            effective_target_input = target_points
            target_points_to_store = target_points
        rules = build_rules(
            effective_type,
            extension_5_6=effective_extension,
            scenario=effective_scenario,
            target_points=effective_target_input,
            player_count=player_count,
        )
        if target_points is None and rules_changed and target_points_to_store is None:
            # A legacy NULL target remains NULL only when all rule metadata is
            # retained. Once the rules are deliberately changed, persist the
            # ordinary default so the edit is self-describing in history.
            target_points_to_store = rules.target_points

        timezone = require_valid_timezone(config)
        today = today_in_timezone(timezone, now=now)
        played_on = (
            original.game.played_on if date_text is None else parse_date(date_text, today=today)
        )
        validate_game_date(played_on, today=today)
        original_season = None
        if original.game.season_id is not None:
            original_season = await seasons.get_season(conn, guild_id, original.game.season_id)
            if original_season is None:
                raise ConflictError("The season for this game no longer exists.")
            validate_game_in_season(
                played_on, starts_on=original_season.starts_on, ends_on=original_season.ends_on
            )
            if original_season.status == "completed" and not _same_roster(
                original, winner_id, loser_ids
            ):
                raise ConflictError(_UPDATE_COMPLETED_SEASON)

        if clear_time:
            played_at, played_timezone = None, None
        elif time_text is not None and time_text.strip():
            played_at = combine_local(played_on, parse_time(time_text), timezone)
            played_timezone = timezone
        elif date_text is not None and original.game.played_at is not None:
            played_at, played_timezone = _preserve_local_time(original, played_on)
        else:
            played_at, played_timezone = original.game.played_at, original.game.played_timezone

    return PreparedGameUpdate(
        guild_id=guild_id,
        game_id=game_id,
        editor_id=actor.user_id,
        expected_revision=getattr(original.game, "revision", 0),
        original=original,
        winner_id=winner_id,
        loser_ids=tuple(loser_ids),
        played_on=played_on,
        played_at=played_at,
        played_timezone=played_timezone,
        rules=rules,
        target_points_to_store=target_points_to_store,
        initial_scores=original.scores,
        update_reason=clean_reason,
        season_id=original.game.season_id,
    )


async def submit_game_update(
    pool: asyncpg.Pool,
    guild_id: int,
    actor: Actor,
    prepared: PreparedGameUpdate,
    *,
    scores: Sequence[PlayerScore] | None,
    now: datetime,
) -> GameWithParticipants:
    """Commit a prepared confirmed-game edit using optimistic concurrency."""
    if type(prepared) is not PreparedGameUpdate:
        raise ValueError("prepared must be a PreparedGameUpdate")
    if guild_id != prepared.guild_id or actor.user_id != prepared.editor_id:
        raise PermissionDeniedError("Only the administrator who started this update can submit it.")
    proposed_ids = (prepared.winner_id, *prepared.loser_ids)
    # `allow_partial=True`: an admin correction can land while a game's
    # per-player DM score collection (Phase 2) is still in progress, so this
    # must tolerate the same "some rows present, some not" shape
    # `record_player_score` does -- never force every participant's row to
    # be re-supplied just to fix one player's total.
    validated_scores = validate_game_scores(
        prepared.rules,
        scores,
        participant_ids=proposed_ids,
        winner_id=prepared.winner_id,
        allow_partial=True,
    )
    async with pool.acquire() as conn, conn.transaction():
        config = await guilds.ensure_guild(conn, guild_id)
        require_admin(actor, config)
        locked_season = None
        if prepared.season_id is not None:
            locked_season = await seasons.lock_season(conn, guild_id, prepared.season_id)
            if locked_season is None:
                raise ConflictError("The season for this game no longer exists.")
        current = await games.lock_game(conn, guild_id, prepared.game_id)
        if current is None:
            raise NotFoundError(_UPDATE_NOT_FOUND)
        if current.game.status != "confirmed":
            raise ConflictError(_UPDATE_NOT_CONFIRMED)
        if getattr(current.game, "revision", 0) != prepared.expected_revision:
            raise ConflictError(_UPDATE_STALE)
        if locked_season is not None:
            validate_game_in_season(
                prepared.played_on,
                starts_on=locked_season.starts_on,
                ends_on=locked_season.ends_on,
            )
            if locked_season.status == "completed" and not _same_roster(
                current, prepared.winner_id, prepared.loser_ids
            ):
                raise ConflictError(_UPDATE_COMPLETED_SEASON)
        timezone = require_valid_timezone(config)
        validate_game_date(prepared.played_on, today=today_in_timezone(timezone, now=now))
        if current.game.season_id != prepared.season_id:
            raise ConflictError(_UPDATE_STALE)

        if (
            current.winner_id == prepared.winner_id
            and set(current.loser_ids) == set(prepared.loser_ids)
            and current.game.played_on == prepared.played_on
            and current.game.game_type == prepared.rules.game_type
            and current.game.extension_5_6 == prepared.rules.extension_5_6
            and current.game.scenario == prepared.rules.scenario
            and current.game.target_points == prepared.target_points_to_store
            and current.game.played_at == prepared.played_at
            and current.game.played_timezone == prepared.played_timezone
            and _same_scores(current.scores, validated_scores)
        ):
            raise ConflictError(_UPDATE_NOOP)

        new_ids = tuple(
            user_id
            for user_id in proposed_ids
            if user_id not in (current.winner_id, *current.loser_ids)
        )
        if new_ids:
            await players.ensure_players(conn, guild_id, new_ids)
        result = await games.update_confirmed_game(
            conn,
            guild_id,
            prepared.game_id,
            expected_revision=prepared.expected_revision,
            played_on=prepared.played_on,
            winner_id=prepared.winner_id,
            loser_ids=prepared.loser_ids,
            game_type=prepared.rules.game_type,
            extension_5_6=prepared.rules.extension_5_6,
            scenario=prepared.rules.scenario,
            target_points=prepared.target_points_to_store,
            played_at=prepared.played_at,
            played_timezone=prepared.played_timezone,
            scores=validated_scores,
            allow_partial=True,
            updated_by=actor.user_id,
            reason=prepared.update_reason,
        )
        if result == "not_found":
            raise NotFoundError(_UPDATE_NOT_FOUND)
        if result == "not_confirmed":
            raise ConflictError(_UPDATE_NOT_CONFIRMED)
        if result == "stale":
            raise ConflictError(_UPDATE_STALE)
        return result


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


async def confirm_preflight(
    pool: asyncpg.Pool, guild_id: int, game_id: int, actor: Actor
) -> ScoreCollectionStatus:
    """Whether `actor` may confirm `game_id`, plus its live score-collection status.

    `views/game_confirm.py` calls this *before* it ever decides whether to
    show the "confirm anyway" dialog for a partially-scored game: a
    reporter or a non-participant must see the same permission error
    `confirm_game` would raise, not a confirmation prompt for an action
    they were never allowed to take in the first place. This deliberately
    mirrors `db.repositories.games.confirm_game`'s own classification
    order -- not found, then not pending, then reporter, then not a
    participant -- so the two can never disagree about *why* someone can't
    confirm.

    This is read-only and purely advisory: it never mutates the game, and
    `confirm_game` re-runs its own checks -- inside a locked transaction --
    the moment a confirmation is actually attempted, so nothing here is
    ever trusted as the real authorization decision. Nothing about the
    dialog UI is decided here either; that's the caller's job, based on
    `ScoreCollectionStatus.complete`.
    """
    status = await score_collection_status(pool, guild_id, game_id)
    game = status.game
    if game.game.status != "pending":
        raise ConflictError(_GAME_NOT_PENDING)
    if game.game.reported_by == actor.user_id:
        raise PermissionDeniedError(_REPORTER_CANNOT_CONFIRM)
    participant_ids = (game.winner_id, *game.loser_ids)
    if actor.user_id not in participant_ids:
        raise PermissionDeniedError(_CONFIRM_NOT_PARTICIPANT)
    return status


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


# ---------------------------------------------------------------------------
# Phase 2: per-player DM score collection.
# ---------------------------------------------------------------------------


async def open_score_collection(
    pool: asyncpg.Pool, guild_id: int, game_id: int, user_ids: Sequence[int], now: datetime
) -> None:
    """Seed one pending `game_score_requests` row per participant.

    Called once, right after a freshly reported game's public message is
    sent -- the cog fans a DM out to each `user_id` and records delivery
    separately via `record_score_request_delivery`, but every participant
    gets a tracked row here regardless of whether their DM ever lands.
    """
    async with pool.acquire() as conn:
        await score_requests.create_score_requests(conn, guild_id, game_id, user_ids, now)


async def record_score_request_delivery(
    pool: asyncpg.Pool,
    guild_id: int,
    game_id: int,
    user_id: int,
    *,
    channel_id: int | None,
    message_id: int | None,
    delivered: bool,
) -> None:
    """Record one participant's DM outcome: delivered (with its ids) or blocked.

    `delivered=False` covers `discord.Forbidden` (closed DMs) and any other
    reason the DM couldn't be sent -- the cog's fan-out must keep going for
    the rest of the roster either way, so this never raises on an unknown
    guild/game/user; it's simply a no-op update in that case, matching the
    underlying repository calls.
    """
    async with pool.acquire() as conn:
        if delivered:
            if channel_id is None or message_id is None:
                raise ValueError("channel_id and message_id are required when delivered=True")
            await score_requests.mark_delivered(
                conn, guild_id, game_id, user_id, channel_id, message_id
            )
        else:
            await score_requests.mark_blocked(conn, guild_id, game_id, user_id)


async def record_player_score(
    pool: asyncpg.Pool,
    guild_id: int,
    game_id: int,
    user_id: int,
    *,
    numeric: Mapping[str, int],
    awards: Iterable[str],
    now: datetime,
) -> GameWithParticipants:
    """Build, validate, and persist one participant's own score row.

    Locks the game row for the duration of the write (matching
    `submit_game_update`'s optimistic-concurrency pattern, minus the
    revision token -- there's no draft to go stale here, just a race between
    two participants submitting at nearly the same moment) so the exclusive-
    award check below always sees every row already on file. Only `pending`
    or `confirmed` games accept a score; a rejected or voided game's roster
    is final and never gets a new row, however the game got there.
    """
    async with pool.acquire() as conn, conn.transaction():
        current = await games.lock_game(conn, guild_id, game_id)
        if current is None:
            raise NotFoundError(_GAME_NOT_FOUND)
        if current.game.status not in _SCORE_COLLECTION_OPEN_STATUSES:
            raise ConflictError(_SCORE_GAME_NOT_OPEN)
        participant_ids = (current.winner_id, *current.loser_ids)
        if user_id not in participant_ids:
            raise PermissionDeniedError(_SCORE_NOT_PARTICIPANT)
        rules = build_rules(
            current.game.game_type,
            extension_5_6=current.game.extension_5_6,
            scenario=current.game.scenario,
            target_points=current.game.target_points,
            player_count=len(participant_ids),
        )
        score = build_player_score(rules, user_id, numeric=numeric, awards=awards)
        # Re-validate the *whole* set -- this row plus every other row
        # already on file -- so an exclusive-award conflict (e.g. two
        # players both claiming Longest Road) is caught before either write
        # lands, with a message that names the conflicting award. The
        # winner's-target check only fires once the winner's own row is
        # among these, exactly like every other `allow_partial=True` caller.
        other_scores = tuple(s for s in current.scores if s.user_id != user_id)
        validate_game_scores(
            rules,
            (*other_scores, score),
            participant_ids=participant_ids,
            winner_id=current.winner_id,
            allow_partial=True,
        )
        await games.set_player_score(conn, guild_id, game_id, user_id, score)
        await score_requests.mark_submitted(conn, guild_id, game_id, user_id, now)
        return await _load_or_die(conn, guild_id, game_id)


async def clear_player_score(
    pool: asyncpg.Pool, guild_id: int, game_id: int, user_id: int, now: datetime
) -> GameWithParticipants:
    """Return one participant's score row to unrecorded (SQL NULL).

    `now` is threaded through even though nothing here reads it yet, for the
    same reason every other clock-taking function in this module does: a
    future revision that needs to timestamp the clear -- e.g. resetting
    `next_prompt_at` so the scheduler resumes chasing them -- shouldn't have
    to touch every caller's signature (cog, tests, ...) just to add it.
    """
    async with pool.acquire() as conn, conn.transaction():
        current = await games.lock_game(conn, guild_id, game_id)
        if current is None:
            raise NotFoundError(_GAME_NOT_FOUND)
        if current.game.status not in _SCORE_COLLECTION_OPEN_STATUSES:
            raise ConflictError(_SCORE_GAME_NOT_OPEN)
        participant_ids = (current.winner_id, *current.loser_ids)
        if user_id not in participant_ids:
            raise PermissionDeniedError(_SCORE_NOT_PARTICIPANT)
        await games.set_player_score(conn, guild_id, game_id, user_id, None)
        return await _load_or_die(conn, guild_id, game_id)


async def score_collection_status(
    pool: asyncpg.Pool, guild_id: int, game_id: int
) -> ScoreCollectionStatus:
    """Who has submitted, who hasn't, and who's blocked for one game."""
    async with pool.acquire() as conn:
        game = await games.get_game(conn, guild_id, game_id)
        if game is None:
            raise NotFoundError(_GAME_NOT_FOUND)
        requests = await score_requests.list_score_requests(conn, guild_id, game_id)
    return ScoreCollectionStatus(
        game=game,
        requests=tuple(
            PlayerScoreState(
                user_id=request.user_id,
                delivery_status=request.delivery_status,
                submitted=request.submitted_at is not None,
            )
            for request in requests
        ),
    )


def _log_score_prompt_read_failure(game_id: int, guild_id: int, exc: Exception) -> None:
    """Log a claimed prompt's failed game read, without leaking row contents.

    Same no-DETAIL rule as `event_service._log_reminder_read_failure`: never
    `str(exc)` (a `PostgresError`'s message can carry DETAIL/HINT text with
    row contents), never those fields directly, and no traceback -- only the
    ids and the exception's type name, plus `sqlstate` for a `PostgresError`
    (a fixed 5-character error class code, not server-supplied text).
    """
    if isinstance(exc, asyncpg.PostgresError):
        logger.error(
            "Score prompt read failed for game_id=%s guild_id=%s: %s (sqlstate=%s)",
            game_id,
            guild_id,
            type(exc).__name__,
            exc.sqlstate,
        )
    else:
        logger.error(
            "Score prompt read failed for game_id=%s guild_id=%s: %s",
            game_id,
            guild_id,
            type(exc).__name__,
        )


async def due_score_prompts(pool: asyncpg.Pool, now: datetime, limit: int) -> list[DueScorePrompt]:
    """Claim every score request due for a re-prompt and pair each with its live game.

    Two phases, on separate connections, mirroring `event_service.
    due_reminders`:

    1. **Claim**, in its own transaction that commits before anything else
       runs. `score_requests.claim_due_prompts` increments `prompts_sent`
       and pushes `next_prompt_at` 24 hours out in the same statement, so
       every claimed row is already durably re-armed (or, past
       `_MAX_PROMPTS`, simply no longer claimable) by the time this
       function returns -- no matter what the scheduler does with the
       result. That makes a re-prompt at-most-once: whatever happens next
       (a dropped connection, a failed DM, a crash), a claimed row is never
       claimed again for the same round, so the worst case is that a player
       loses one of their three total chases, never that they get a
       duplicate DM.
    2. **Read**, one game at a time, cached per `game_id` since a game with
       several outstanding players claims several rows in the same sweep.
       The claim query knows nothing about game status -- it happily claims
       (and reschedules) a row that belongs to a since-rejected/voided game
       too, since `game_score_requests` carries no such check. That's
       harmless: a claimed-then-dropped row is simply never claimed again
       (the same "consumed, not retried" outcome as any other claim here),
       which is exactly correct for a game whose roster is already final
       and owes nobody anything. Only `pending`/`confirmed` games --
       `_SCORE_COLLECTION_OPEN_STATUSES`, the same two statuses
       `record_player_score` still accepts -- actually produce a prompt. A
       game whose read itself fails is logged and dropped, matching
       `event_service._read_reminder`'s isolation: one bad read must never
       cost every other claimed prompt in the same sweep, including a
       different player's prompt for a *different* game.
    """
    async with pool.acquire() as conn, conn.transaction():
        claimed = await score_requests.claim_due_prompts(conn, now, limit)

    games_by_id: dict[int, GameWithParticipants | None] = {}
    due: list[DueScorePrompt] = []
    for request in claimed:
        if request.game_id not in games_by_id:
            try:
                async with pool.acquire() as conn:
                    games_by_id[request.game_id] = await games.get_game(
                        conn, request.guild_id, request.game_id
                    )
            except Exception as exc:
                _log_score_prompt_read_failure(request.game_id, request.guild_id, exc)
                games_by_id[request.game_id] = None
        game = games_by_id[request.game_id]
        if game is None or game.game.status not in _SCORE_COLLECTION_OPEN_STATUSES:
            continue
        due.append(
            DueScorePrompt(
                game=game,
                user_id=request.user_id,
                dm_channel_id=request.dm_channel_id,
                dm_message_id=request.dm_message_id,
                delivery_status=request.delivery_status,
            )
        )
    return due


async def find_open_score_request_game_id(
    pool: asyncpg.Pool, guild_id: int, user_id: int
) -> int | None:
    """The most recent game still awaiting `user_id`'s score, if any.

    Backs `/game scores` when no `game_id` is given.
    """
    async with pool.acquire() as conn:
        request = await score_requests.get_latest_open_request(conn, guild_id, user_id)
    return request.game_id if request is not None else None


async def get_game_for_player(
    pool: asyncpg.Pool, guild_id: int, game_id: int, user_id: int
) -> GameWithParticipants:
    """Load a game, but only for one of its own participants (`/game scores`).

    Keeps the "only a participant may open this game's sheet" access check
    in the services layer rather than the cog, matching every other
    permission decision in this module.
    """
    game = await get_game(pool, guild_id, game_id)
    participant_ids = (game.winner_id, *game.loser_ids)
    if user_id not in participant_ids:
        raise PermissionDeniedError(_SCORE_NOT_PARTICIPANT)
    return game


async def game_history(
    pool: asyncpg.Pool,
    guild_id: int,
    *,
    user_id: int | None,
    limit: int,
    include_voided: bool = False,
) -> list[Game]:
    """Recent games, newest first. Voided games stay in the database but are
    excluded from this listing unless `include_voided` is set."""
    n = _clamp(limit, _HISTORY_MIN_LIMIT, _HISTORY_MAX_LIMIT)
    async with pool.acquire() as conn:
        if user_id is not None:
            return await games.list_recent_games_for_player(
                conn, guild_id, user_id, n, include_voided=include_voided
            )
        return await games.list_recent_games(conn, guild_id, n, include_voided=include_voided)
