"""`/help` — lists planned commands. Ephemeral; no persisted state."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

# (command, description) pairs shown in the help embed. Most of these land
# in later milestones; this cog only renders the static list.
_PLANNED_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/config", "Set the announcement channel, timezone, and admin role."),
    ("/season", "Start, end, or configure the active season (incl. min-games)."),
    ("/game report", "Report a game's winner and losers for confirmation."),
    ("/game void", "Void a reported game (admin)."),
    ("/leaderboard", "Show season or all-time rankings."),
    ("/stats", "Show a player's win/loss record."),
    ("/event", "Create, list, or cancel game-night events."),
)


class HelpCog(commands.Cog):
    """Ephemeral help embed. The full command set lands in later milestones."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="List Catan Tracker's commands.")
    async def help_command(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Catan Tracker",
            description="Tracks Catan wins/losses, ranks players, and schedules game nights.",
            color=discord.Color.blurple(),
        )
        for name, description in _PLANNED_COMMANDS:
            embed.add_field(name=name, value=description, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
