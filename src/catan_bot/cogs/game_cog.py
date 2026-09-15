"""`/game` -- report a game, void a bad report, and read back history.

`report` is the one command that legitimately calls two service functions:
`report_game` (the business operation) and `record_game_message` (an
internal-only bookkeeping call DESIGN.md forbids exposing as its own
command) so the Confirm/Reject buttons know which message to edit later.
Every other subcommand calls exactly one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from catan_bot import formatting
from catan_bot.bot import CatanBot
from catan_bot.cogs.season_cog import filter_date_choices
from catan_bot.domain.validation import ParticipantRef
from catan_bot.permissions import actor_from_interaction, guild_id_from_interaction
from catan_bot.services import game_service
from catan_bot.views.game_confirm import build_game_action_view

_DEFAULT_HISTORY_LIMIT = 10
_REPORT_COOLDOWN_SECONDS = 30.0
_DISCORD_INTEGER_MAX = 2**53 - 1


def _participants(
    winner: discord.Member, losers: list[discord.Member]
) -> tuple[ParticipantRef, list[ParticipantRef]]:
    winner_ref = ParticipantRef(user_id=winner.id, is_bot=winner.bot)
    loser_refs = [ParticipantRef(user_id=m.id, is_bot=m.bot) for m in losers]
    return winner_ref, loser_refs


class GameCog(commands.Cog):
    game_group = app_commands.Group(
        name="game",
        description="Report, void, or look up Catan games.",
        guild_only=True,
    )

    def __init__(self, bot: CatanBot) -> None:
        self.bot = bot

    @game_group.command(name="report", description="Report a game's winner and losers.")
    @app_commands.describe(
        winner="Who won.",
        loser1="A player who lost.",
        loser2="Another player who lost.",
        loser3="Another player who lost.",
        loser4="Another player who lost.",
        loser5="Another player who lost.",
        date="When it was played (YYYY-MM-DD, MM/DD/YYYY, today, or yesterday). Defaults to today.",
    )
    @app_commands.checks.cooldown(1, _REPORT_COOLDOWN_SECONDS)
    async def report_command(
        self,
        interaction: discord.Interaction,
        winner: discord.Member,
        loser1: discord.Member,
        loser2: discord.Member | None = None,
        loser3: discord.Member | None = None,
        loser4: discord.Member | None = None,
        loser5: discord.Member | None = None,
        date: app_commands.Range[str, 1, 32] | None = None,
    ) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        losers = [m for m in (loser1, loser2, loser3, loser4, loser5) if m is not None]
        winner_ref, loser_refs = _participants(winner, losers)

        await interaction.response.defer(thinking=True)
        created = await game_service.report_game(
            self.bot.pool,
            guild_id,
            actor,
            winner=winner_ref,
            losers=loser_refs,
            date_text=date,
            now=datetime.now(UTC),
        )

        embed = formatting.build_game_report_embed(created)
        view = build_game_action_view(created.game.game_id)
        sent = await interaction.edit_original_response(
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await game_service.record_game_message(
            self.bot.pool, guild_id, created.game.game_id, sent.channel.id, sent.id
        )

    @report_command.autocomplete("date")
    async def report_date_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=label, value=value)
            for label, value in filter_date_choices(current)
        ]

    @game_group.command(name="void", description="Void a reported game (admin).")
    @app_commands.describe(game_id="The game's id.", reason="Why it's being voided.")
    async def void_command(
        self,
        interaction: discord.Interaction,
        game_id: app_commands.Range[int, 1, _DISCORD_INTEGER_MAX],
        reason: app_commands.Range[str, 1, 200] | None = None,
    ) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        updated = await game_service.void_game(self.bot.pool, guild_id, game_id, actor, reason)
        embed = formatting.build_game_status_embed(updated)
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @game_group.command(name="history", description="Show recent games.")
    @app_commands.describe(
        member="Only show this player's games.", limit="How many games to show (1-25)."
    )
    async def history_command(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        limit: app_commands.Range[int, 1, 25] | None = None,
    ) -> None:
        guild_id = guild_id_from_interaction(interaction)
        await interaction.response.defer(thinking=True)
        games = await game_service.game_history(
            self.bot.pool,
            guild_id,
            user_id=member.id if member is not None else None,
            limit=limit or _DEFAULT_HISTORY_LIMIT,
        )
        embed = formatting.build_game_history_embed(
            games, member_id=member.id if member is not None else None
        )
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot: CatanBot) -> None:
    await bot.add_cog(GameCog(bot))
