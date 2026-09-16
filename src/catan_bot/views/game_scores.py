"""Private, reporter-owned score-sheet UI for a pending Catan report.

The sheet deliberately has no database identity.  It is a short-lived
Discord draft; only a successful Submit creates a pending game.  Blank cells
therefore remain distinct from an explicit zero all the way to the service.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from time import monotonic
from typing import TYPE_CHECKING, Any

import discord

from catan_bot import formatting
from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.scoring import PlayerScore, ScoreEntry, ScoreSource, score_sources
from catan_bot.errors import handle_interaction_error
from catan_bot.services import game_service
from catan_bot.services.errors import ServiceError
from catan_bot.views.game_confirm import build_game_action_view

if TYPE_CHECKING:
    from catan_bot.services.context import Actor

_BLANK = "—"
_TOTAL_KEY = "__total__"
_MAX_VALUE = 99
_MAX_UPDATE_REFRESH_ATTEMPTS = 2


def score_pages(rules: object) -> tuple[tuple[ScoreSource, ...], ...]:
    """Split source rows so every edit modal stays within Discord's five inputs."""
    sources = score_sources(rules)  # type: ignore[arg-type]
    first = tuple(sources[:4])
    remaining = tuple(sources[4:])
    pages: list[tuple[ScoreSource, ...]] = [first]
    pages.extend(tuple(remaining[index : index + 5]) for index in range(0, len(remaining), 5))
    return tuple(pages)


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


class _SheetSelect(discord.ui.Select[Any]):
    def __init__(self, sheet: GameScoreSheet, *, kind: str) -> None:
        self.sheet = sheet
        self.kind = kind
        if kind == "player":
            options = [
                discord.SelectOption(label=f"P{index + 1}: {user_id}", value=str(user_id))
                for index, user_id in enumerate(sheet.participant_ids)
            ]
            placeholder = "Choose player"
        else:
            options = [
                discord.SelectOption(label=f"Score page {index + 1}", value=str(index))
                for index in range(len(sheet.pages))
            ]
            placeholder = "Choose score page"
        super().__init__(placeholder=placeholder, options=options, row=0 if kind == "player" else 1)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.sheet.ensure_editable(interaction):
            return
        if self.kind == "player":
            self.sheet.selected_player_id = int(self.values[0])
        else:
            self.sheet.selected_page = int(self.values[0])
        await interaction.response.edit_message(
            embed=self.sheet.embed(),
            view=self.sheet,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _EditButton(discord.ui.Button[Any]):
    def __init__(self, sheet: GameScoreSheet) -> None:
        self.sheet = sheet
        super().__init__(label="Edit selected player", style=discord.ButtonStyle.primary, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.sheet.ensure_editable(interaction):
            return
        await interaction.response.send_modal(
            ScorePageModal(
                self.sheet,
                player_id=self.sheet.selected_player_id,
                page_index=self.sheet.selected_page,
                revision=self.sheet.revision,
            )
        )


class _SubmitButton(discord.ui.Button[Any]):
    def __init__(self, sheet: GameScoreSheet) -> None:
        self.sheet = sheet
        super().__init__(label=sheet.submit_label, style=discord.ButtonStyle.success, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.sheet.submit(interaction)


class _CancelButton(discord.ui.Button[Any]):
    def __init__(self, sheet: GameScoreSheet) -> None:
        self.sheet = sheet
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.sheet.cancel(interaction)


class _ClearAllButton(discord.ui.Button[Any]):
    """Update-only escape hatch for replacing historic scores with SQL NULLs."""

    def __init__(self, sheet: GameScoreSheet) -> None:
        self.sheet = sheet
        super().__init__(label="Clear all points", style=discord.ButtonStyle.danger, row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.sheet.clear_all_points(interaction)


class GameScoreSheet(discord.ui.View):
    """An ephemeral score draft, owned by the reporter for fifteen minutes."""

    def __init__(
        self,
        *,
        pool: object,
        guild_id: int,
        actor: Actor,
        prepared: object,
        channel: discord.abc.Messageable,
        mode: str = "report",
        bot: object | None = None,
    ) -> None:
        super().__init__(timeout=900)
        self.pool = pool
        self.guild_id = guild_id
        self.actor = actor
        self.prepared = prepared
        self.channel = channel
        self.mode = mode
        self.bot = bot
        if self.mode not in {"report", "update"}:
            raise ValueError("mode must be 'report' or 'update'")
        self.participant_ids = (prepared.winner_id, *prepared.loser_ids)
        self.pages = score_pages(prepared.rules)
        self.sources = tuple(source for page in self.pages for source in page)
        self.values: dict[int, dict[str, int | None]] = {
            user_id: {_TOTAL_KEY: None, **{source.key: None for source in self.sources}}
            for user_id in self.participant_ids
        }
        if self.is_update:
            self._prefill_update_scores()
        self.selected_player_id = self.participant_ids[0]
        self.selected_page = 0
        self.revision = 0
        self.created_report: object | None = None
        self.updated_game: object | None = None
        self.public_message: discord.Message | None = None
        self.message_recorded = False
        self.cancelled = False
        self.completed = False
        self.expired = False
        self.private_message: discord.Message | None = None
        self._lock = asyncio.Lock()
        self._deadline = monotonic() + 900
        self.add_item(_SheetSelect(self, kind="player"))
        self.add_item(_SheetSelect(self, kind="page"))
        self.add_item(_EditButton(self))
        self.add_item(_SubmitButton(self))
        self.add_item(_CancelButton(self))
        if self.is_update:
            self.add_item(_ClearAllButton(self))

    @property
    def is_update(self) -> bool:
        return self.mode == "update"

    @property
    def submit_label(self) -> str:
        return "Save update" if self.is_update else "Submit report"

    def _prefill_update_scores(self) -> None:
        """Copy only compatible stored cells; absent/legacy scores stay genuinely blank."""
        for score in getattr(self.prepared, "initial_scores", ()):
            if score.user_id not in self.values:
                continue
            self.values[score.user_id][_TOTAL_KEY] = score.total_points
            source_values = {entry.key: entry.points for entry in score.breakdown}
            for source in self.sources:
                if source.key in source_values:
                    self.values[score.user_id][source.key] = source_values[source.key]

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
            interaction,
            "Only the administrator who started this update can edit this sheet."
            if self.is_update
            else "Only the reporter in this server can edit this sheet.",
        )
        return False

    def embed(self) -> discord.Embed:
        header = f"{'Point source':<28}" + "".join(
            f"{'P' + str(index + 1):>4}" for index in range(len(self.participant_ids))
        )
        rows = [header]
        for source in self.sources:
            values = []
            for user_id in self.participant_ids:
                value = self.values[user_id][source.key]
                values.append(_BLANK if value is None else str(value))
            cells = "".join(f"{value:>4}" for value in values)
            rows.append(f"{_table_label(source.label):<28}" + cells)
        totals = []
        for user_id in self.participant_ids:
            value = self.values[user_id][_TOTAL_KEY]
            totals.append(_BLANK if value is None else str(value))
        rows.append(f"{'TOTAL':<28}" + "".join(f"{value:>4}" for value in totals))
        legend = " · ".join(
            [
                f"P{index + 1} = {_mention(user_id)}"
                for index, user_id in enumerate(self.participant_ids)
            ]
        )
        title = "Update game score sheet" if self.is_update else "Game score sheet"
        context = ""
        if self.is_update:
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
            new_scenario = discord.utils.escape_mentions(
                str(getattr(self.prepared.rules, "scenario", None) or "none")
            )
            old = (
                f"Current: #{self.prepared.game_id} · {original.game.game_type}"
                f" · {original.game.played_on} · scenario: {old_scenario}"
            )
            proposed = (
                f"Proposed: {self.prepared.rules.game_type} · {self.prepared.played_on}"
                f" · target {self.prepared.rules.target_points} · scenario: {new_scenario}"
            )
            context = (
                old[:350]
                + "\n"
                + proposed[:350]
                + "\n"
                + f"Current roster: {current_roster}\nProposed roster: {proposed_roster}\n"
                + f"Editing revision {self.prepared.expected_revision}.\n"
            )
        embed = discord.Embed(
            title=title, description=context + "```\n" + "\n".join(rows) + "\n```\n" + legend
        )
        embed.set_footer(
            text=(
                "Only you can edit. Blank cells remain unrecorded; explicit 0 is preserved."
                if not self.is_update
                else "Only you can edit. Clear all points stores no score data; 0 is preserved."
            )
        )
        return embed

    def _disable_controls(self) -> None:
        for child in self.children:
            child.disabled = True

    def _freeze_after_persist(self) -> None:
        self.revision += 1
        for child in self.children:
            if getattr(child, "label", None) != self.submit_label:
                child.disabled = True

    async def ensure_editable(self, interaction: discord.Interaction) -> bool:
        if self._lock.locked():
            await _ephemeral_notice(interaction, "Submission in progress. Please wait.")
            return False
        if monotonic() >= self._deadline:
            self.expired = True
        editable = not (
            self.expired or self.cancelled or self.completed or self._persisted_result() is not None
        )
        if not editable:
            await _ephemeral_notice(interaction, "This score sheet is no longer editable.")
        return editable

    def _scores_or_none(self) -> tuple[PlayerScore, ...] | None:
        every_value = [value for player in self.values.values() for value in player.values()]
        if all(value is None for value in every_value):
            return None
        if any(value is None for value in every_value):
            raise ValueError(
                "This score sheet is partial. Fill every total and point source, or clear every "
                "cell."
            )
        return tuple(
            PlayerScore(
                user_id=user_id,
                total_points=self.values[user_id][_TOTAL_KEY],  # type: ignore[arg-type]
                breakdown=tuple(
                    ScoreEntry(key=source.key, points=self.values[user_id][source.key])  # type: ignore[arg-type]
                    for source in self.sources
                ),
            )
            for user_id in self.participant_ids
        )

    def _persisted_result(self) -> object | None:
        return self.updated_game if self.is_update else self.created_report

    async def clear_all_points(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        async with self._lock:
            if (
                self.expired
                or monotonic() >= self._deadline
                or self.cancelled
                or self.completed
                or self._persisted_result() is not None
            ):
                self.expired = self.expired or monotonic() >= self._deadline
                await _ephemeral_notice(interaction, "This score sheet is no longer editable.")
                return
            for player_values in self.values.values():
                for key in player_values:
                    player_values[key] = None
            self.revision += 1
        await _ephemeral_notice(
            interaction, "All points cleared. Saving will leave score fields unrecorded."
        )
        await self._refresh_private()

    async def save_page(
        self,
        interaction: discord.Interaction,
        *,
        player_id: int,
        page_index: int,
        revision: int,
        values: Sequence[int | None],
    ) -> None:
        async with self._lock:
            if self.expired or monotonic() >= self._deadline:
                self.expired = True
                await _ephemeral_notice(interaction, "This score sheet has expired.")
                return
            if self.cancelled or self.completed or self._persisted_result() is not None:
                await _ephemeral_notice(interaction, "This score sheet is no longer editable.")
                return
            if revision != self.revision:
                await _ephemeral_notice(
                    interaction, "That score form is stale. Open it again and retry."
                )
                return
            fields = ((_TOTAL_KEY,) if page_index == 0 else ()) + tuple(
                source.key for source in self.pages[page_index]
            )
            if len(fields) != len(values):
                raise ValueError("The score form has an unexpected number of fields.")
            self.values[player_id].update(zip(fields, values, strict=True))
            self.revision += 1
        await _ephemeral_notice(interaction, "Score page saved.")
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
                await _ephemeral_notice(
                    interaction,
                    "This update has already been saved."
                    if self.is_update
                    else "This game report has already been submitted.",
                )
                return
            try:
                scores = None if self._persisted_result() is not None else self._scores_or_none()
            except ValueError as exc:
                await _ephemeral_notice(interaction, str(exc))
                return
            try:
                persisted_before_submit = self._persisted_result() is not None
                if not persisted_before_submit:
                    if self.is_update:
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
                    else:
                        self.created_report = await game_service.submit_game_report(
                            self.pool,
                            self.guild_id,
                            self.actor,
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
                if self.is_update:
                    # A retry is only a message refresh. Load the authoritative current
                    # row first so this draft cannot re-render stale metadata.
                    if persisted_before_submit:
                        self.updated_game = await game_service.get_game(
                            self.pool, self.guild_id, self.prepared.game_id
                        )
                    await self._refresh_updated_message()
                elif self.public_message is None:
                    self.public_message = await self.channel.send(
                        embed=formatting.build_game_report_embed(self.created_report),
                        view=build_game_action_view(self.created_report.game.game_id),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                if not self.is_update and not self.message_recorded:
                    await game_service.record_game_message(
                        self.pool,
                        self.guild_id,
                        self.created_report.game.game_id,
                        self.public_message.channel.id,
                        self.public_message.id,
                    )
                    self.message_recorded = True
            except (DomainValidationError, ServiceError) as exc:
                await handle_interaction_error(interaction, exc, command_name="game:score-sheet")
                return
            except Exception as exc:
                if self._persisted_result() is None:
                    await handle_interaction_error(
                        interaction, exc, command_name="game:score-sheet"
                    )
                else:
                    message = (
                        "The update was saved, but its announcement could not be refreshed. "
                        "Press Save update to retry; /game show has the current report."
                        if self.is_update
                        else (
                            "The report was saved but could not be fully published. "
                            "Press Submit to retry."
                        )
                    )
                    await _ephemeral_notice(interaction, message)
                return
            self.completed = True
            self._disable_controls()
        if self.is_update:
            await interaction.edit_original_response(
                content=f"Game #{self.prepared.game_id} updated.",
                embed=formatting.build_game_status_embed(self.updated_game),
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.edit_original_response(
                content=f"Report submitted: {self.public_message.jump_url}",
                embed=None,
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
                await _ephemeral_notice(
                    interaction,
                    "This update has already been saved."
                    if self.is_update
                    else "This game report has already been submitted.",
                )
                return
            if self._persisted_result() is not None:
                await _ephemeral_notice(
                    interaction,
                    "This update was already saved and cannot be cancelled from the score sheet."
                    if self.is_update
                    else (
                        "This report was already saved and cannot be cancelled "
                        "from the score sheet."
                    ),
                )
                return
            self.cancelled = True
            self._disable_controls()
        await interaction.edit_original_response(
            content="Game update cancelled. No changes were saved."
            if self.is_update
            else "Game report cancelled. No game was created.",
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
            created = self._persisted_result() is not None
            self._disable_controls()
        if self.private_message is not None:
            with suppress(discord.HTTPException):
                await self.private_message.edit(
                    content=(
                        (
                            "Score sheet expired after an update was saved; "
                            "its announcement may need refreshing."
                            if self.is_update
                            else (
                                "Score sheet expired after a report was saved; "
                                "its publication may need retrying."
                            )
                        )
                        if created
                        else (
                            "Score sheet expired. No changes were saved."
                            if self.is_update
                            else "Score sheet expired. No game was created."
                        )
                    ),
                    embed=None,
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )


class ScorePageModal(discord.ui.Modal, title="Enter Catan points"):
    def __init__(
        self, sheet: GameScoreSheet, *, player_id: int, page_index: int, revision: int
    ) -> None:
        super().__init__(timeout=900)
        self.sheet = sheet
        self.player_id = player_id
        self.page_index = page_index
        self.revision = revision
        fields = ((_TOTAL_KEY, "Total points"),) if page_index == 0 else ()
        fields += tuple((source.key, source.label) for source in sheet.pages[page_index])
        self.inputs: list[discord.ui.TextInput[Any]] = []
        for key, label in fields:
            existing = sheet.values[player_id][key]
            item = discord.ui.TextInput(
                label=label[:45],
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
            interaction,
            "Only the administrator who started this update can edit this sheet."
            if self.sheet.is_update
            else "Only the reporter in this server can edit this sheet.",
        )
        return False

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if (
            self.sheet.expired
            or self.sheet.cancelled
            or self.sheet.completed
            or self.sheet._persisted_result()
        ):
            await _ephemeral_notice(interaction, "This score sheet is no longer editable.")
            return
        try:
            values = tuple(parse_score_cell(item.value) for item in self.inputs)
        except ValueError as exc:
            await _ephemeral_notice(interaction, str(exc))
            return
        await self.sheet.save_page(
            interaction,
            player_id=self.player_id,
            page_index=self.page_index,
            revision=self.revision,
            values=values,
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
