"""`/help` -- lists the real commands. Ephemeral; no persisted state."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

# (command, description) pairs shown in the help embed, matching what's
# actually registered.
_COMMANDS: tuple[tuple[str, str], ...] = (
    (
        "/config channel|timezone|admin-role|player-role|leaderboard|show",
        "View or change this server's settings, including the recurring leaderboard post "
        "(mode, channel, scope, and daily time -- each option but mode is optional, and an "
        "omitted one leaves its current value alone).",
    ),
    ("/season start|min-games|end-date|end|cancel|info|history", "Manage this server's season."),
    (
        "/game report",
        "Report a game's winner, losers, type, and optional extension/scenario/time. The "
        "game is created immediately: its public message is the live score sheet (showing a "
        "receipt checkmark per player as scores arrive), and each participant is separately "
        "DM'd their own one-page sheet to fill in -- scoring is expected to finish gradually, "
        "not all at once. Anyone who still hasn't submitted gets automatically re-prompted "
        "with a fresh sheet roughly once a day, up to three times total, alongside a channel "
        "notice naming who's still missing; after that it goes quiet and the game keeps "
        "whatever scores it has.",
    ),
    (
        "/game scores [game_id]",
        "Reopen your own score-entry sheet -- the fallback when a DM never arrived (closed "
        "DMs) or you closed it. Defaults to your most recent game still awaiting your score.",
    ),
    ("/game void", "Void a reported game (admin)."),
    (
        "/game history",
        "Show recent games, numbered, in chronological date/time order, including their "
        "ruleset. Voided games are hidden by default; include_voided shows them too.",
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
    (
        "Confirm / Reject / Nudge buttons",
        "On a reported game's public message: confirm or reject the report (another "
        "participant, or the reporter retracting it), or nudge whichever participants still "
        "owe a score for that game (rate-limited to once every ten minutes per game). "
        "Confirming while a score is still missing shows an ephemeral confirm-anyway step "
        "naming who hasn't submitted, so saving a partial game is always a deliberate choice.",
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
                "Tracks Catan wins/losses and ranks players by win rate. Score sheets are "
                "ruleset-aware: blank cells are unrecorded (NULL), while 0 is an explicitly "
                "recorded zero. Times are shown in 12-hour clock, e.g. 7:30 PM."
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
