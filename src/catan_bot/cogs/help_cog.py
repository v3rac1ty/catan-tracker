"""`/help` -- lists the real commands. Ephemeral; no persisted state."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

# (command, description) pairs shown in the help embed, matching what's
# actually registered through M5.
_COMMANDS: tuple[tuple[str, str], ...] = (
    (
        "/config channel|timezone|admin-role|player-role|show",
        "View or change this server's settings.",
    ),
    ("/season start|min-games|end-date|end|cancel|info|history", "Manage this server's season."),
    (
        "/game report",
        "Report a game's winner, losers, type, optional extension/scenario/time, "
        "and score sheet for confirmation.",
    ),
    ("/game void", "Void a reported game (admin)."),
    (
        "/game history",
        "Show recent games in chronological date/time order, including their ruleset.",
    ),
    (
        "/game update",
        "Admin-only correction for a confirmed game. Omitted fields are preserved; any "
        "loser supplied replaces the full loser roster, while winner-only swaps the "
        "existing roster. Use clear_time or clear_scenario to remove optional values. "
        "Completed-season winner/roster changes are blocked; each edit creates a "
        "revision, and /game show is the current source of truth.",
    ),
    (
        "/game show",
        "Show one game's current ruleset, participants, point breakdown, and audit revision; "
        "it is the current source of truth.",
    ),
    ("/leaderboard [channel]", "Show rankings here or post them to a chosen channel."),
    ("/stats", "Show a player's win/loss record."),
    (
        "/event create [channel]|list|cancel",
        "Schedule and manage game nights with RSVPs; event posts show Going/Maybe/Not Going.",
    ),
)


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="List Catan Tracker's commands.")
    @app_commands.guild_only()
    async def help_command(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Catan Tracker",
            description=(
                "Tracks Catan wins/losses and ranks players by win rate. "
                "Game reports can include a ruleset-aware point table: blank cells "
                "are unrecorded (NULL), while 0 is an explicitly recorded zero."
            ),
            color=discord.Color.blurple(),
        )
        for name, description in _COMMANDS:
            embed.add_field(name=name, value=description, inline=False)
        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
