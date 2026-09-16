"""`/leaderboard` and `/stats` -- read-only, open to anyone."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from catan_bot import formatting
from catan_bot.bot import CatanBot
from catan_bot.cogs.channel_publish import validate_publish_channel
from catan_bot.permissions import guild_id_from_interaction
from catan_bot.services import stats_service


class StatsCog(commands.Cog):
    def __init__(self, bot: CatanBot) -> None:
        self.bot = bot

    @app_commands.command(name="leaderboard", description="Show season or all-time rankings.")
    @app_commands.guild_only()
    @app_commands.describe(channel="Where to post it. Defaults to this channel.")
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="season", value="season"),
            app_commands.Choice(name="all-time", value="all_time"),
        ]
    )
    async def leaderboard_command(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str] | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        guild_id = guild_id_from_interaction(interaction)
        scope_value = scope.value if scope is not None else "season"
        destination = channel if channel is not None else interaction.channel
        if destination is None:
            raise ValueError("interaction requires a channel")
        validate_publish_channel(interaction, destination)
        if channel is None:
            await interaction.response.defer(thinking=True)
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
        board = await stats_service.leaderboard(self.bot.pool, guild_id, scope_value)
        embed = formatting.build_leaderboard_embed(board)
        if channel is None:
            await interaction.edit_original_response(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )
        else:
            sent = await destination.send(
                embed=embed, allowed_mentions=discord.AllowedMentions.none()
            )
            await interaction.edit_original_response(
                content=(
                    f"Leaderboard posted in {formatting.channel_mention(destination.id)}: "
                    f"{sent.jump_url}"
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @app_commands.command(name="stats", description="Show a player's win/loss record.")
    @app_commands.guild_only()
    @app_commands.describe(member="Whose stats to show. Defaults to you.")
    async def stats_command(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        guild_id = guild_id_from_interaction(interaction)
        target = member if member is not None else interaction.user
        await interaction.response.defer(thinking=True)
        view = await stats_service.player_stats(self.bot.pool, guild_id, target.id)
        embed = formatting.build_stats_embed(view)
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot: CatanBot) -> None:
    await bot.add_cog(StatsCog(bot))
