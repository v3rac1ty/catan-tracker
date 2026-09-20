"""Admin-only score-correction sheet for a confirmed game (`/game update`).

Phase 2 moved the reporter-facing score sheet to per-player DMs
(`views/score_entry.py`): `/game report` no longer builds one big paginated
table here, and this module now serves exactly one workflow -- an
administrator correcting a *confirmed* game's roster/rules/scores.

Like its predecessor, this sheet deliberately has no database identity of
its own: it's a short-lived (30-minute) in-memory Discord draft owned by the
editor, and only a successful Save update mutates anything. It differs from
`views/score_entry.py` in that it is NOT a `discord.ui.DynamicItem` set: an
admin session doesn't need to survive 24 hours or a bot restart the way
per-player score collection does, so a plain, stateful `discord.ui.View`
(this file's `GameScoreSheet`) is the right tool here, exactly like the
Confirm/Reject buttons are the wrong tool for something that needs to
survive a restart and DM sheets are the wrong tool for something this
short-lived.

One page, one player at a time: a player picker (there's a roster to
choose from -- unlike `views/score_entry.py`, which only ever edits the one
player it was DM'd to), a numeric modal (`entry_fields`, <=5 inputs), and an
award multi-select for whichever player is currently selected. Blank cells
remain distinct from an explicit zero exactly as before, just tracked per
player instead of per page: a player's row must be entirely filled or
entirely blank before Save update will include (or omit) it.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from time import monotonic
from typing import TYPE_CHECKING, Any

import discord

from catan_bot import formatting
from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.scoring import PlayerScore, ScoreEntry, entry_fields, score_sources
from catan_bot.errors import handle_interaction_error
from catan_bot.services import game_service
from catan_bot.services.errors import ServiceError

if TYPE_CHECKING:
    from catan_bot.services.context import Actor

_BLANK = "—"
_MAX_VALUE = 99
_TIMEOUT_SECONDS = 1800
_MAX_UPDATE_REFRESH_ATTEMPTS = 2


def parse_score_cell(value: str) -> int | None:
    """Parse a modal cell without silently converting an omitted value to zero."""
    stripped = value.strip()
    if not stripped:
        return None
    if not stripped.isascii() or not stripped.isdecimal():
        raise ValueError("Enter a whole number from 0 to 99, or leave the cell blank.")
    result = int(stripped)
    if result > _MAX_VALUE:
        raise ValueError("Points must be between 0 and 99.")
    return result


def _mention(user_id: int) -> str:
    return f"<@{user_id}>"


def _table_label(value: str, width: int = 28) -> str:
    """Keep the score sheet aligned and within Discord's embed description limit."""
    return value if len(value) <= width else value[: width - 1] + "…"


class _PlayerSelect(discord.ui.Select[Any]):
    def __init__(self, sheet: GameScoreSheet) -> None:
        self.sheet = sheet
        options = [
            discord.SelectOption(label=f"P{index + 1}: {user_id}", value=str(user_id))
            for index, user_id in enumerate(sheet.participant_ids)
        ]
        super().__init__(placeholder="Choose player", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.sheet.ensure_editable(interaction):
            return
        self.sheet.selected_player_id = int(self.values[0])
        self.sheet.rebuild_award_select()
        await interaction.response.edit_message(
            embed=self.sheet.embed(),
            view=self.sheet,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _AwardSelect(discord.ui.Select[Any]):
    """Award claims for the currently-selected player.

    Rebuilt (not just re-rendered) every time the selected player changes,
    so its `default=True` options always reflect that player's own claimed
    awards -- see `GameScoreSheet.rebuild_award_select`.
    """

    def __init__(self, sheet: GameScoreSheet) -> None:
        self.sheet = sheet
        selected = sheet.claimed_awards(sheet.selected_player_id)
        options = [
            discord.SelectOption(
                label=source.label, value=source.key, default=source.key in selected
            )
            for source in sheet.award_sources
        ]
        super().__init__(
            placeholder="Claim awards for the selected player",
            min_values=0,
            max_values=len(options),
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.sheet.ensure_editable(interaction):
            return
        await self.sheet.set_awards(
            interaction, player_id=self.sheet.selected_player_id, awards=self.values
        )


class _EditButton(discord.ui.Button[Any]):
    def __init__(self, sheet: GameScoreSheet) -> None:
        self.sheet = sheet
        super().__init__(label="Edit points", style=discord.ButtonStyle.primary, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.sheet.ensure_editable(interaction):
            return
        await interaction.response.send_modal(
            ScoreNumericModal(
                self.sheet,
                player_id=self.sheet.selected_player_id,
                revision=self.sheet.revision,
            )
        )


class _ClearPlayerButton(discord.ui.Button[Any]):
    def __init__(self, sheet: GameScoreSheet) -> None:
        self.sheet = sheet
        super().__init__(label="Clear selected player", style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.sheet.ensure_editable(interaction):
            return
        await self.sheet.clear_player(interaction, player_id=self.sheet.selected_player_id)


class _ClearAllButton(discord.ui.Button[Any]):
    """Escape hatch for replacing every player's stored score with SQL NULLs."""

    def __init__(self, sheet: GameScoreSheet) -> None:
        self.sheet = sheet
        super().__init__(label="Clear all points", style=discord.ButtonStyle.danger, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.sheet.clear_all_points(interaction)


class _SubmitButton(discord.ui.Button[Any]):
    def __init__(self, sheet: GameScoreSheet) -> None:
        self.sheet = sheet
        super().__init__(label="Save update", style=discord.ButtonStyle.success, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.sheet.submit(interaction)


class _CancelButton(discord.ui.Button[Any]):
    def __init__(self, sheet: GameScoreSheet) -> None:
        self.sheet = sheet
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.sheet.cancel(interaction)


class GameScoreSheet(discord.ui.View):
    """An ephemeral admin score-correction draft, owned by the editor for 30 minutes."""

    def __init__(
        self,
        *,
        pool: object,
        guild_id: int,
        actor: Actor,
        prepared: object,
        channel: discord.abc.Messageable,
        bot: object | None = None,
    ) -> None:
        super().__init__(timeout=_TIMEOUT_SECONDS)
        self.pool = pool
        self.guild_id = guild_id
        self.actor = actor
        self.prepared = prepared
        self.channel = channel
        self.bot = bot
        self.participant_ids = (prepared.winner_id, *prepared.loser_ids)
        self.rules = prepared.rules
        self.numeric_sources, self.award_sources = entry_fields(self.rules)
        self.all_sources = score_sources(self.rules)
        self.values: dict[int, dict[str, int | None]] = {
            user_id: {source.key: None for source in self.all_sources}
            for user_id in self.participant_ids
        }
        self._prefill_from_initial_scores()
        self.selected_player_id = self.participant_ids[0]
        self.revision = 0
        self.updated_game: object | None = None
        self.cancelled = False
        self.completed = False
        self.expired = False
        self.private_message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._deadline = monotonic() + _TIMEOUT_SECONDS
        self._player_select = _PlayerSelect(self)
        self.add_item(self._player_select)
        self._award_select = _AwardSelect(self)
        self.add_item(self._award_select)
        self.add_item(_EditButton(self))
        self.add_item(_ClearPlayerButton(self))
        self.add_item(_ClearAllButton(self))
        self.add_item(_SubmitButton(self))
        self.add_item(_CancelButton(self))

    def _prefill_from_initial_scores(self) -> None:
        """Copy only compatible stored cells; absent/legacy scores stay genuinely blank."""
        for score in getattr(self.prepared, "initial_scores", ()):
            if score.user_id not in self.values:
                continue
            source_values = {entry.key: entry.points for entry in score.breakdown}
            for source in self.all_sources:
                if source.key in source_values:
                    self.values[score.user_id][source.key] = source_values[source.key]

    def claimed_awards(self, player_id: int) -> frozenset[str]:
        return frozenset(
            source.key for source in self.award_sources if self.values[player_id].get(source.key)
        )

    def rebuild_award_select(self) -> None:
        """Swap in a fresh `_AwardSelect` reflecting the now-selected player's awards."""
        self.remove_item(self._award_select)
        self._award_select = _AwardSelect(self)
        self.add_item(self._award_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if monotonic() >= self._deadline:
            self.expired = True
        if self.expired:
            await _ephemeral_notice(interaction, "This score sheet has expired.")
            return False
        if interaction.guild_id == self.guild_id and interaction.user.id == self.actor.user_id:
            if self._lock.locked():
                await _ephemeral_notice(interaction, "Submission in progress. Please wait.")
                return False
            return True
        await _ephemeral_notice(
            interaction, "Only the administrator who started this update can edit this sheet."
        )
        return False

    def embed(self) -> discord.Embed:
        header = f"{'Point source':<28}" + "".join(
            f"{'P' + str(index + 1):>4}" for index in range(len(self.participant_ids))
        )
        rows = [header]
        for source in self.all_sources:
            values = []
            for user_id in self.participant_ids:
                value = self.values[user_id][source.key]
                values.append(_BLANK if value is None else str(value))
            cells = "".join(f"{value:>4}" for value in values)
            rows.append(f"{_table_label(source.label):<28}" + cells)
        totals = []
        for user_id in self.participant_ids:
            row = self.values[user_id]
            if any(row[source.key] is None for source in self.all_sources):
                totals.append(_BLANK)
            else:
                totals.append(str(sum(row[source.key] for source in self.all_sources)))  # type: ignore[misc]
        rows.append(f"{'TOTAL':<28}" + "".join(f"{value:>4}" for value in totals))
        legend = " · ".join(
            [
                f"P{index + 1} = {_mention(user_id)}"
                for index, user_id in enumerate(self.participant_ids)
            ]
        )
        original = self.prepared.original
        current_roster = ", ".join(
            _mention(user_id)
            for user_id in (
                getattr(original, "winner_id", self.prepared.winner_id),
                *getattr(original, "loser_ids", self.prepared.loser_ids),
            )
        )
        proposed_roster = ", ".join(_mention(user_id) for user_id in self.participant_ids)
        old_scenario = discord.utils.escape_mentions(
            str(getattr(original.game, "scenario", None) or "none")
        )
        new_scenario = discord.utils.escape_mentions(str(self.rules.scenario or "none"))
        old = (
            f"Current: #{self.prepared.game_id} · {original.game.game_type}"
            f" · {original.game.played_on} · scenario: {old_scenario}"
        )
        proposed = (
            f"Proposed: {self.rules.game_type} · {self.prepared.played_on}"
            f" · target {self.rules.target_points} · scenario: {new_scenario}"
        )
        context = (
            old[:350]
            + "\n"
            + proposed[:350]
            + "\n"
            + f"Current roster: {current_roster}\nProposed roster: {proposed_roster}\n"
            + f"Editing revision {self.prepared.expected_revision}. "
            + f"Selected player: {_mention(self.selected_player_id)}.\n"
        )
        embed = discord.Embed(
            title="Update game score sheet",
            description=context + "```\n" + "\n".join(rows) + "\n```\n" + legend,
        )
        embed.set_footer(
            text=(
                "Only you can edit. A player's row must be entirely filled or entirely "
                "blank -- Clear selected player resets just one row."
            )
        )
        return embed

    def _disable_controls(self) -> None:
        for child in self.children:
            child.disabled = True

    def _freeze_after_persist(self) -> None:
        self.revision += 1
        for child in self.children:
            if getattr(child, "label", None) != "Save update":
                child.disabled = True

    async def ensure_editable(self, interaction: discord.Interaction) -> bool:
        if self._lock.locked():
            await _ephemeral_notice(interaction, "Submission in progress. Please wait.")
            return False
        if monotonic() >= self._deadline:
            self.expired = True
        editable = not (
            self.expired or self.cancelled or self.completed or self.updated_game is not None
        )
        if not editable:
            await _ephemeral_notice(interaction, "This score sheet is no longer editable.")
        return editable

    def _collect_scores(self) -> tuple[PlayerScore, ...]:
        """Every player whose row is entirely filled; entirely-blank rows stay absent.

        A row with *some* fields set and others blank is a half-finished
        edit, not real partial-collection data, so it raises rather than
        silently dropping (or silently zero-filling) the missing cells.
        """
        scores: list[PlayerScore] = []
        for user_id in self.participant_ids:
            row = self.values[user_id]
            values = [row[source.key] for source in self.all_sources]
            if all(value is None for value in values):
                continue
            if any(value is None for value in values):
                raise ValueError(
                    f"{_mention(user_id)}'s row is partially filled. Finish every field, "
                    "or use Clear selected player to reset it."
                )
            breakdown = tuple(
                ScoreEntry(source.key, row[source.key])  # type: ignore[arg-type]
                for source in self.all_sources
            )
            total = sum(entry.points for entry in breakdown)
            scores.append(PlayerScore(user_id=user_id, total_points=total, breakdown=breakdown))
        return tuple(scores)

    async def clear_player(self, interaction: discord.Interaction, *, player_id: int) -> None:
        await interaction.response.defer()
        async with self._lock:
            if not await self._guard_still_editable(interaction):
                return
            for source in self.all_sources:
                self.values[player_id][source.key] = None
            self.revision += 1
        if self.selected_player_id == player_id:
            self.rebuild_award_select()
        await self._refresh_private()

    async def clear_all_points(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        async with self._lock:
            if not await self._guard_still_editable(interaction):
                return
            for player_values in self.values.values():
                for key in player_values:
                    player_values[key] = None
            self.revision += 1
        self.rebuild_award_select()
        await _ephemeral_notice(
            interaction, "All points cleared. Saving will leave score fields unrecorded."
        )
        await self._refresh_private()

    async def _guard_still_editable(self, interaction: discord.Interaction) -> bool:
        """Same terminal-state guard as `ensure_editable`, for use *inside* the lock."""
        if (
            self.expired
            or monotonic() >= self._deadline
            or self.cancelled
            or self.completed
            or self.updated_game is not None
        ):
            self.expired = self.expired or monotonic() >= self._deadline
            await _ephemeral_notice(interaction, "This score sheet is no longer editable.")
            return False
        return True

    async def set_awards(
        self, interaction: discord.Interaction, *, player_id: int, awards: list[str]
    ) -> None:
        await interaction.response.defer()
        async with self._lock:
            if not await self._guard_still_editable(interaction):
                return
            claimed = set(awards)
            for source in self.award_sources:
                self.values[player_id][source.key] = (
                    source.fixed_points if source.key in claimed else 0
                )
            self.revision += 1
        await self._refresh_private()

    async def save_numeric(
        self,
        interaction: discord.Interaction,
        *,
        player_id: int,
        revision: int,
        values: tuple[int | None, ...],
    ) -> None:
        async with self._lock:
            if self.expired or monotonic() >= self._deadline:
                self.expired = True
                await _ephemeral_notice(interaction, "This score sheet has expired.")
                return
            if self.cancelled or self.completed or self.updated_game is not None:
                await _ephemeral_notice(interaction, "This score sheet is no longer editable.")
                return
            if revision != self.revision:
                await _ephemeral_notice(
                    interaction, "That score form is stale. Open it again and retry."
                )
                return
            fields = tuple(source.key for source in self.numeric_sources)
            if len(fields) != len(values):
                raise ValueError("The score form has an unexpected number of fields.")
            self.values[player_id].update(zip(fields, values, strict=True))
            self.revision += 1
        await _ephemeral_notice(interaction, "Points saved.")
        await self._refresh_private()

    async def submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        async with self._lock:
            if self.expired or monotonic() >= self._deadline:
                self.expired = True
                await _ephemeral_notice(interaction, "This score sheet has expired.")
                return
            if self.cancelled:
                await _ephemeral_notice(interaction, "This score sheet was cancelled.")
                return
            if self.completed:
                await _ephemeral_notice(interaction, "This update has already been saved.")
                return
            try:
                scores = None if self.updated_game is not None else self._collect_scores() or None
            except ValueError as exc:
                await _ephemeral_notice(interaction, str(exc))
                return
            try:
                persisted_before_submit = self.updated_game is not None
                if not persisted_before_submit:
                    # Rebuild privileges at the final mutation, not only when opening the draft.
                    from catan_bot.permissions import actor_from_interaction

                    self.updated_game = await game_service.submit_game_update(
                        self.pool,
                        self.guild_id,
                        actor_from_interaction(interaction),
                        self.prepared,
                        scores=scores,
                        now=discord.utils.utcnow(),
                    )
                    self._freeze_after_persist()
                    await interaction.edit_original_response(
                        embed=self.embed(),
                        view=self,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    # A retry is only a message refresh. Load the authoritative current
                    # row first so this draft cannot re-render stale metadata.
                    self.updated_game = await game_service.get_game(
                        self.pool, self.guild_id, self.prepared.game_id
                    )
                await self._refresh_updated_message()
            except (DomainValidationError, ServiceError) as exc:
                await handle_interaction_error(interaction, exc, command_name="game:score-sheet")
                return
            except Exception as exc:
                if self.updated_game is None:
                    await handle_interaction_error(
                        interaction, exc, command_name="game:score-sheet"
                    )
                else:
                    await _ephemeral_notice(
                        interaction,
                        "The update was saved, but its announcement could not be refreshed. "
                        "Press Save update to retry; /game show has the current report.",
                    )
                return
            self.completed = True
            self._disable_controls()
        await interaction.edit_original_response(
            content=f"Game #{self.prepared.game_id} updated.",
            embed=formatting.build_game_status_embed(self.updated_game),
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        async with self._lock:
            if self.expired or monotonic() >= self._deadline:
                self.expired = True
                await _ephemeral_notice(interaction, "This score sheet has expired.")
                return
            if self.completed:
                await _ephemeral_notice(interaction, "This update has already been saved.")
                return
            if self.updated_game is not None:
                await _ephemeral_notice(
                    interaction,
                    "This update was already saved and cannot be cancelled from the score sheet.",
                )
                return
            self.cancelled = True
            self._disable_controls()
        await interaction.edit_original_response(
            content="Game update cancelled. No changes were saved.",
            embed=None,
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _refresh_private(self) -> None:
        if self.private_message is not None:
            with suppress(discord.HTTPException):
                await self.private_message.edit(
                    embed=self.embed(), view=self, allowed_mentions=discord.AllowedMentions.none()
                )

    async def _refresh_updated_message(self) -> None:
        """Publish a stable current row, never repeating the already-saved update.

        A separate administrator can change or void the game between our update
        transaction and Discord's message edit.  Resolve the old message first,
        then use a bounded read/edit/read reconciliation loop so the displayed
        embed follows the latest revision (including a void) rather than this
        draft's now-stale result.
        """
        original_game = self.prepared.original.game
        channel_id, message_id = original_game.channel_id, original_game.message_id
        if channel_id is None or message_id is None:
            return
        destination: object | None = None
        if getattr(self.channel, "id", None) == channel_id:
            destination = self.channel
        elif self.bot is not None:
            getter = getattr(self.bot, "get_channel", None)
            if getter is not None:
                destination = getter(channel_id)
            if destination is None:
                fetcher = getattr(self.bot, "fetch_channel", None)
                if fetcher is not None:
                    destination = await fetcher(channel_id)
        if destination is None:
            raise RuntimeError("The original game channel is unavailable.")
        guild = getattr(destination, "guild", None)
        if guild is not None and getattr(guild, "id", self.guild_id) != self.guild_id:
            raise RuntimeError("The original game message is not in this server.")
        fetch_message = getattr(destination, "fetch_message", None)
        if fetch_message is None:
            raise RuntimeError("The original game message is unavailable.")
        message = await fetch_message(message_id)
        for _ in range(_MAX_UPDATE_REFRESH_ATTEMPTS):
            # This read intentionally happens immediately before *every* edit,
            # including the first one after submit.
            current = await game_service.get_game(self.pool, self.guild_id, self.prepared.game_id)
            self.updated_game = current
            await message.edit(
                embed=formatting.build_game_status_embed(current),
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            after_edit = await game_service.get_game(
                self.pool, self.guild_id, self.prepared.game_id
            )
            self.updated_game = after_edit
            if _same_game_announcement_state(current, after_edit):
                return
        raise RuntimeError("The game changed while its announcement was being refreshed.")

    async def on_timeout(self) -> None:
        async with self._lock:
            if self.completed or self.cancelled:
                return
            self.expired = True
            created = self.updated_game is not None
            self._disable_controls()
        if self.private_message is not None:
            with suppress(discord.HTTPException):
                await self.private_message.edit(
                    content=(
                        "Score sheet expired after an update was saved; "
                        "its announcement may need refreshing."
                        if created
                        else "Score sheet expired. No changes were saved."
                    ),
                    embed=None,
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )


class ScoreNumericModal(discord.ui.Modal, title="Enter Catan points"):
    def __init__(self, sheet: GameScoreSheet, *, player_id: int, revision: int) -> None:
        super().__init__(timeout=900)
        self.sheet = sheet
        self.player_id = player_id
        self.revision = revision
        self.inputs: list[discord.ui.TextInput[Any]] = []
        for source in sheet.numeric_sources:
            existing = sheet.values[player_id][source.key]
            item = discord.ui.TextInput(
                label=source.label[:45],
                default="" if existing is None else str(existing),
                required=False,
                max_length=2,
            )
            self.add_item(item)
            self.inputs.append(item)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.sheet._lock.locked():
            await _ephemeral_notice(interaction, "Submission in progress. Please wait.")
            return False
        if self.sheet.expired or monotonic() >= self.sheet._deadline:
            self.sheet.expired = True
            await _ephemeral_notice(interaction, "This score sheet has expired.")
            return False
        if (
            interaction.guild_id == self.sheet.guild_id
            and interaction.user.id == self.sheet.actor.user_id
        ):
            return True
        await _ephemeral_notice(
            interaction, "Only the administrator who started this update can edit this sheet."
        )
        return False

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if (
            self.sheet.expired
            or self.sheet.cancelled
            or self.sheet.completed
            or self.sheet.updated_game is not None
        ):
            await _ephemeral_notice(interaction, "This score sheet is no longer editable.")
            return
        try:
            values = tuple(parse_score_cell(item.value) for item in self.inputs)
        except ValueError as exc:
            await _ephemeral_notice(interaction, str(exc))
            return
        await self.sheet.save_numeric(
            interaction, player_id=self.player_id, revision=self.revision, values=values
        )


async def _ephemeral_notice(interaction: discord.Interaction, content: str) -> None:
    """Reply once for a component or modal interaction in testable, safe form."""
    if interaction.response.is_done():
        await interaction.followup.send(
            content, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )
    else:
        await interaction.response.send_message(
            content, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )


def _same_game_announcement_state(left: object, right: object) -> bool:
    """The fields that make a Discord game-status announcement authoritative."""
    left_game = left.game
    right_game = right.game
    return (
        left_game.game_id,
        getattr(left_game, "revision", 0),
        left_game.status,
        left_game.channel_id,
        left_game.message_id,
    ) == (
        right_game.game_id,
        getattr(right_game, "revision", 0),
        right_game.status,
        right_game.channel_id,
        right_game.message_id,
    )
