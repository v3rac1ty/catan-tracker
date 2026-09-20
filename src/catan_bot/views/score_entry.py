"""Per-player DM score-entry sheet (Phase 2 of `/game report`).

Each participant gets their own one-page sheet, DM'd separately, and fills
in only their own row -- the public message (see `views/game_confirm.py`)
is the one shared, live surface; these sheets are private and per-user.

Every interactive piece here is a `discord.ui.DynamicItem`, exactly like
`GameActionButton`/`EventRsvpButton`: the game/guild/user ids live in the
`custom_id`, nothing is held in Python state between interactions, and a
fresh instance is reconstructed from the `custom_id` alone every time a
component fires. That is not a style preference -- a collection window is
24 hours and must survive a bot restart, so a stateful `discord.ui.View`
instance (which lives only in memory and is gone the moment the process
restarts) is not an option here the way it is for the short-lived,
in-memory `GameScoreSheet` reporter draft in `views/game_scores.py`.

A sheet is only ever built from what's already durable: `numeric` and
`awards` are read back from the stored `PlayerScore` row (or treated as
unset) on every interaction, never cached on a component instance. Awards
and numeric fields are submitted through two different components (a modal
for numeric fields, a select for awards), but `domain.scoring.build_player_score`
needs both at once to build one complete row -- so each write combines the
just-submitted half with whatever the *other* half currently holds in the
database, never inventing a value neither interaction actually supplied.
"""

from __future__ import annotations

import re
from typing import Any

import discord

from catan_bot import formatting
from catan_bot.db.models import Game, GameWithParticipants
from catan_bot.domain.scoring import GameRules, PlayerScore, entry_fields
from catan_bot.errors import handle_interaction_error
from catan_bot.services import game_service
from catan_bot.views.game_confirm import build_game_action_view
from catan_bot.views.game_scores import parse_score_cell

_BIGINT = r"[0-9]{1,19}"
_EDIT_TEMPLATE = re.compile(
    rf"score:edit:(?P<guild>{_BIGINT}):(?P<game>{_BIGINT}):(?P<user>{_BIGINT})"
)
_AWARD_TEMPLATE = re.compile(
    rf"score:award:(?P<guild>{_BIGINT}):(?P<game>{_BIGINT}):(?P<user>{_BIGINT})"
)
_CLEAR_TEMPLATE = re.compile(
    rf"score:clear:(?P<guild>{_BIGINT}):(?P<game>{_BIGINT}):(?P<user>{_BIGINT})"
)
_BIGINT_MAX = 2**63 - 1
_WRONG_OWNER_TEXT = "This score sheet belongs to another player."
_FILL_EVERY_FIELD_TEXT = (
    "Fill in every point field (0 counts) or use Clear my points instead of leaving one blank."
)
_ENTER_POINTS_FIRST_TEXT = (
    "Enter your point totals first (use the button below), then choose your awards."
)

_NO_MENTIONS = discord.AllowedMentions.none()


def _validate_ids(guild_id: int, game_id: int, user_id: int) -> None:
    for value, name in ((guild_id, "guild_id"), (game_id, "game_id"), (user_id, "user_id")):
        if type(value) is not int or not (1 <= value <= _BIGINT_MAX):
            raise ValueError(f"{name} must be a positive BIGINT")


def rules_for_game(game: Game) -> GameRules:
    return GameRules(
        game_type=game.game_type,
        extension_5_6=game.extension_5_6,
        scenario=game.scenario,
        target_points=game.target_points,
    )


def _stored_score(game: GameWithParticipants, user_id: int) -> PlayerScore | None:
    return next((score for score in game.scores if score.user_id == user_id), None)


async def _ephemeral(interaction: discord.Interaction, content: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True, allowed_mentions=_NO_MENTIONS)
    else:
        await interaction.response.send_message(
            content, ephemeral=True, allowed_mentions=_NO_MENTIONS
        )


async def _refresh_public_message(
    client: object, guild_id: int, game: GameWithParticipants
) -> None:
    """Best-effort refresh of the public game message after a DM write.

    The player's own row is already durably saved by the time this runs --
    a failure here (message deleted, channel gone, a transient Discord
    outage) must never be reported back to the player as their submission
    having failed, so every exception is swallowed. This mirrors the
    tolerance `GameScoreSheet._refresh_updated_message` shows toward a
    since-changed announcement, just without its read/edit/read
    reconciliation loop -- nothing here races a second writer to the same
    field the way an admin's confirmed-game edit can.
    """
    channel_id, message_id = game.game.channel_id, game.game.message_id
    if channel_id is None or message_id is None:
        return
    try:
        pool = client.pool  # type: ignore[attr-defined]
        status = await game_service.score_collection_status(pool, guild_id, game.game.game_id)
        channel = client.get_channel(channel_id)  # type: ignore[attr-defined]
        if channel is None:
            channel = await client.fetch_channel(channel_id)  # type: ignore[attr-defined]
        message = await channel.fetch_message(message_id)
        if game.game.status == "pending":
            embed = formatting.build_game_report_embed(game, collection=status)
            view = build_game_action_view(game.game.game_id)
        else:
            embed = formatting.build_game_status_embed(game, collection=status)
            view = None
        await message.edit(embed=embed, view=view, allowed_mentions=_NO_MENTIONS)
    except Exception:  # noqa: S110 -- best-effort refresh, see docstring above.
        return


def build_score_entry_embed(game: GameWithParticipants, user_id: int) -> discord.Embed:
    """One player's private score-entry sheet for `game`."""
    rules = rules_for_game(game.game)
    numeric_sources, award_sources = entry_fields(rules)
    stored = _stored_score(game, user_id)
    entries = {entry.key: entry.points for entry in stored.breakdown} if stored is not None else {}

    embed = discord.Embed(
        title=f"Enter your score -- Game #{game.game.game_id}",
        description=(
            f"Winner: {formatting.mention(game.winner_id)}\n"
            f"Losers: {', '.join(formatting.mention(uid) for uid in game.loser_ids)}"
        ),
        color=discord.Color.blurple(),
    )
    numeric_lines = [
        f"{source.label}: {entries.get(source.key, '—')}" for source in numeric_sources
    ]
    award_lines = [
        f"{source.label}: {'Claimed' if entries.get(source.key) else 'Not claimed'}"
        for source in award_sources
    ]
    embed.add_field(name="Your points", value="\n".join(numeric_lines), inline=True)
    embed.add_field(name="Your awards", value="\n".join(award_lines), inline=True)
    total_text = str(stored.total_points) if stored is not None else "Not recorded yet"
    embed.add_field(name="Your total", value=total_text, inline=False)
    embed.set_footer(
        text="Only you can edit this sheet. It stays open until this game is confirmed."
    )
    return embed


def selected_awards_for(
    game: GameWithParticipants, user_id: int, rules: GameRules
) -> frozenset[str]:
    """The award keys `user_id` currently has claimed in `game`'s stored row, if any."""
    stored = _stored_score(game, user_id)
    if stored is None:
        return frozenset()
    _, award_sources = entry_fields(rules)
    award_keys = {source.key for source in award_sources}
    by_key = {entry.key: entry.points for entry in stored.breakdown}
    return frozenset(key for key in award_keys if by_key.get(key))


def build_score_entry_view(
    guild_id: int,
    game_id: int,
    user_id: int,
    rules: GameRules,
    *,
    selected_awards: frozenset[str] = frozenset(),
) -> discord.ui.View:
    """A player's persistent score-entry controls: edit button, award select, clear."""
    _validate_ids(guild_id, game_id, user_id)
    _, award_sources = entry_fields(rules)
    view = discord.ui.View(timeout=None)
    view.add_item(ScoreEntryButton(guild_id, game_id, user_id))
    options = [
        discord.SelectOption(
            label=source.label[:100], value=source.key, default=source.key in selected_awards
        )
        for source in award_sources
    ]
    select = discord.ui.Select(
        placeholder="Claim your awards (optional)",
        min_values=0,
        max_values=len(options),
        options=options,
        custom_id=f"score:award:{guild_id}:{game_id}:{user_id}",
    )
    view.add_item(ScoreAwardSelect(select, guild_id=guild_id, game_id=game_id, user_id=user_id))
    view.add_item(ScoreClearButton(guild_id, game_id, user_id))
    return view


class ScoreNumericModal(discord.ui.Modal, title="Enter your points"):
    """The numeric half of a score row -- always exactly one modal (`entry_fields`)."""

    def __init__(
        self,
        *,
        guild_id: int,
        game_id: int,
        user_id: int,
        rules: GameRules,
        current: dict[str, int | None],
    ) -> None:
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.game_id = game_id
        self.user_id = user_id
        self.rules = rules
        numeric_sources, _ = entry_fields(rules)
        self.sources = numeric_sources
        self.inputs: list[discord.ui.TextInput[Any]] = []
        for source in numeric_sources:
            existing = current.get(source.key)
            item = discord.ui.TextInput(
                label=source.label[:45],
                default="" if existing is None else str(existing),
                required=False,
                max_length=2,
            )
            self.add_item(item)
            self.inputs.append(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            if interaction.user is None or interaction.user.id != self.user_id:
                await _ephemeral(interaction, _WRONG_OWNER_TEXT)
                return
            try:
                values = {
                    source.key: parse_score_cell(item.value)
                    for source, item in zip(self.sources, self.inputs, strict=True)
                }
            except ValueError as exc:
                await _ephemeral(interaction, str(exc))
                return
            if any(value is None for value in values.values()):
                await _ephemeral(interaction, _FILL_EVERY_FIELD_TEXT)
                return
            pool = interaction.client.pool  # type: ignore[attr-defined]
            current = await game_service.get_game(pool, self.guild_id, self.game_id)
            _, award_sources = entry_fields(self.rules)
            award_keys = {source.key for source in award_sources}
            stored = _stored_score(current, self.user_id)
            claimed = (
                [
                    entry.key
                    for entry in stored.breakdown
                    if entry.key in award_keys and entry.points
                ]
                if stored is not None
                else []
            )
            updated = await game_service.record_player_score(
                pool,
                self.guild_id,
                self.game_id,
                self.user_id,
                numeric=values,
                awards=claimed,
                now=discord.utils.utcnow(),
            )
            embed = build_score_entry_embed(updated, self.user_id)
            view = build_score_entry_view(
                self.guild_id,
                self.game_id,
                self.user_id,
                self.rules,
                selected_awards=frozenset(claimed),
            )
            await interaction.response.edit_message(
                embed=embed, view=view, allowed_mentions=_NO_MENTIONS
            )
            await _refresh_public_message(interaction.client, self.guild_id, updated)
        except Exception as exc:
            await handle_interaction_error(interaction, exc, command_name="game:score-modal")


class ScoreEntryButton(discord.ui.DynamicItem[discord.ui.Button], template=_EDIT_TEMPLATE):
    """Opens the numeric modal, pre-filled from whatever row is already stored."""

    def __init__(self, guild_id: int, game_id: int, user_id: int) -> None:
        self.guild_id = guild_id
        self.game_id = game_id
        self.user_id = user_id
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.primary,
                label="Enter / edit your points",
                custom_id=f"score:edit:{guild_id}:{game_id}:{user_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item, match: re.Match[str], /
    ) -> ScoreEntryButton:
        return cls(int(match["guild"]), int(match["game"]), int(match["user"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            if interaction.user is None or interaction.user.id != self.user_id:
                await _ephemeral(interaction, _WRONG_OWNER_TEXT)
                return
            _validate_ids(self.guild_id, self.game_id, self.user_id)
            pool = interaction.client.pool  # type: ignore[attr-defined]
            current = await game_service.get_game(pool, self.guild_id, self.game_id)
            if current.game.status not in ("pending", "confirmed"):
                await _ephemeral(interaction, "This game is no longer accepting scores.")
                return
            rules = rules_for_game(current.game)
            numeric_sources, _ = entry_fields(rules)
            stored = _stored_score(current, self.user_id)
            by_key = (
                {entry.key: entry.points for entry in stored.breakdown}
                if stored is not None
                else {}
            )
            prefill = {source.key: by_key.get(source.key) for source in numeric_sources}
            modal = ScoreNumericModal(
                guild_id=self.guild_id,
                game_id=self.game_id,
                user_id=self.user_id,
                rules=rules,
                current=prefill,
            )
            await interaction.response.send_modal(modal)
        except Exception as exc:
            await handle_interaction_error(interaction, exc, command_name="game:score-edit")


class ScoreAwardSelect(discord.ui.DynamicItem[discord.ui.Select], template=_AWARD_TEMPLATE):
    """The award half of a score row.

    Reconstructed dispatch instances reuse the real `discord.ui.Select` item
    Discord round-trips from the live message (`item`, below) instead of
    rebuilding one -- its `options`/`default` flags already reflect
    whatever was last sent, and the selected values (`item.values`) are
    refreshed from the interaction payload regardless, so there is nothing
    to gain from re-deriving the award catalog here.
    """

    def __init__(
        self, item: discord.ui.Select[Any], *, guild_id: int, game_id: int, user_id: int
    ) -> None:
        self.guild_id = guild_id
        self.game_id = game_id
        self.user_id = user_id
        super().__init__(item)

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item, match: re.Match[str], /
    ) -> ScoreAwardSelect:
        return cls(
            item,  # type: ignore[arg-type]
            guild_id=int(match["guild"]),
            game_id=int(match["game"]),
            user_id=int(match["user"]),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            if interaction.user is None or interaction.user.id != self.user_id:
                await _ephemeral(interaction, _WRONG_OWNER_TEXT)
                return
            _validate_ids(self.guild_id, self.game_id, self.user_id)
            pool = interaction.client.pool  # type: ignore[attr-defined]
            current = await game_service.get_game(pool, self.guild_id, self.game_id)
            if current.game.status not in ("pending", "confirmed"):
                await _ephemeral(interaction, "This game is no longer accepting scores.")
                return
            stored = _stored_score(current, self.user_id)
            if stored is None:
                await _ephemeral(interaction, _ENTER_POINTS_FIRST_TEXT)
                return
            rules = rules_for_game(current.game)
            numeric_sources, _ = entry_fields(rules)
            by_key = {entry.key: entry.points for entry in stored.breakdown}
            numeric_values = {source.key: by_key[source.key] for source in numeric_sources}
            selected = list(self.item.values)
            updated = await game_service.record_player_score(
                pool,
                self.guild_id,
                self.game_id,
                self.user_id,
                numeric=numeric_values,
                awards=selected,
                now=discord.utils.utcnow(),
            )
            embed = build_score_entry_embed(updated, self.user_id)
            view = build_score_entry_view(
                self.guild_id,
                self.game_id,
                self.user_id,
                rules,
                selected_awards=frozenset(selected),
            )
            await interaction.response.edit_message(
                embed=embed, view=view, allowed_mentions=_NO_MENTIONS
            )
            await _refresh_public_message(interaction.client, self.guild_id, updated)
        except Exception as exc:
            await handle_interaction_error(interaction, exc, command_name="game:score-award")


class ScoreClearButton(discord.ui.DynamicItem[discord.ui.Button], template=_CLEAR_TEMPLATE):
    """Returns a player's row to unrecorded (SQL NULL), not to all-zero."""

    def __init__(self, guild_id: int, game_id: int, user_id: int) -> None:
        self.guild_id = guild_id
        self.game_id = game_id
        self.user_id = user_id
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.danger,
                label="Clear my points",
                custom_id=f"score:clear:{guild_id}:{game_id}:{user_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item, match: re.Match[str], /
    ) -> ScoreClearButton:
        return cls(int(match["guild"]), int(match["game"]), int(match["user"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            if interaction.user is None or interaction.user.id != self.user_id:
                await _ephemeral(interaction, _WRONG_OWNER_TEXT)
                return
            _validate_ids(self.guild_id, self.game_id, self.user_id)
            pool = interaction.client.pool  # type: ignore[attr-defined]
            updated = await game_service.clear_player_score(
                pool, self.guild_id, self.game_id, self.user_id, discord.utils.utcnow()
            )
            rules = rules_for_game(updated.game)
            embed = build_score_entry_embed(updated, self.user_id)
            view = build_score_entry_view(
                self.guild_id, self.game_id, self.user_id, rules, selected_awards=frozenset()
            )
            await interaction.response.edit_message(
                embed=embed, view=view, allowed_mentions=_NO_MENTIONS
            )
            await _refresh_public_message(interaction.client, self.guild_id, updated)
        except Exception as exc:
            await handle_interaction_error(interaction, exc, command_name="game:score-clear")
