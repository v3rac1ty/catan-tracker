"""`/season` -- start, adjust, end, cancel, and read back the active season.

Every mutating subcommand calls exactly one `season_service` function with
`now=datetime.now(UTC)` (cogs read the clock; services never do) and
renders the result with `formatting`. `info`/`history` are open to anyone;
every mutation requires admin, enforced server-side inside the service
(`context.require_admin`), never assumed here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from catan_bot import formatting
from catan_bot.bot import CatanBot
from catan_bot.permissions import actor_from_interaction, guild_id_from_interaction
from catan_bot.services import season_service

_DEFAULT_HISTORY_LIMIT = 10
_NO_ACTIVE_SEASON_TEXT = "There's no active season."

_DATE_CHOICES: tuple[tuple[str, str], ...] = (("Today", "today"), ("Yesterday", "yesterday"))


def filter_date_choices(query: str) -> list[tuple[str, str]]:
    """The `("Today", "today")`/`("Yesterday", "yesterday")` autocomplete
    choices whose label case-insensitively contains `query`."""
    lowered = query.strip().lower()
    if not lowered:
        return list(_DATE_CHOICES)
    return [(label, value) for label, value in _DATE_CHOICES if lowered in label.lower()]


class SeasonCog(commands.Cog):
    season_group = app_commands.Group(
        name="season",
        description="Start, adjust, end, or look up this server's Catan season.",
        guild_only=True,
    )

    def __init__(self, bot: CatanBot) -> None:
        self.bot = bot

    # -- start ---------------------------------------------------------

    @season_group.command(name="start", description="Start a new season.")
    @app_commands.describe(
        name="The season's name.",
        end_date="When the season ends (YYYY-MM-DD, MM/DD/YYYY, today, or yesterday).",
        start_date="When the season starts. Defaults to today.",
        min_games="Minimum confirmed games to be eligible for the bet (1-100).",
    )
    async def start_command(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 100],
        end_date: app_commands.Range[str, 1, 32],
        start_date: app_commands.Range[str, 1, 32] | None = None,
        min_games: app_commands.Range[int, 1, 100] | None = None,
    ) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        await interaction.response.defer(thinking=True)
        season = await season_service.start_season(
            self.bot.pool,
            guild_id,
            actor,
            name=name,
            end_date_text=end_date,
            start_date_text=start_date,
            min_games=min_games,
            now=datetime.now(UTC),
        )
        embed = formatting.build_season_summary_embed(season, heading="Season Started")
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @start_command.autocomplete("end_date")
    @start_command.autocomplete("start_date")
    async def start_date_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=label, value=value)
            for label, value in filter_date_choices(current)
        ]

    # -- min-games -------------------------------------------------------

    @season_group.command(name="min-games", description="Change the minimum games to be eligible.")
    @app_commands.describe(count="1-100. Updates the active season and this server's default.")
    async def min_games_command(
        self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 100]
    ) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        await interaction.response.defer(thinking=True)
        season, _config = await season_service.set_min_games(self.bot.pool, guild_id, actor, count)
        if season is not None:
            embed = formatting.build_season_summary_embed(season, heading="Minimum Games Updated")
        else:
            embed = discord.Embed(
                title="Minimum Games Updated",
                description=f"This server's default is now {count}. No season is active right now.",
                color=discord.Color.blurple(),
            )
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    # -- end-date --------------------------------------------------------

    @season_group.command(name="end-date", description="Change the active season's end date.")
    @app_commands.describe(date="The new end date (YYYY-MM-DD, MM/DD/YYYY, today, or yesterday).")
    async def end_date_command(
        self, interaction: discord.Interaction, date: app_commands.Range[str, 1, 32]
    ) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        await interaction.response.defer(thinking=True)
        season = await season_service.set_end_date(
            self.bot.pool, guild_id, actor, date, now=datetime.now(UTC)
        )
        embed = formatting.build_season_summary_embed(season, heading="Season End Date Updated")
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @end_date_command.autocomplete("date")
    async def end_date_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=label, value=value)
            for label, value in filter_date_choices(current)
        ]

    # -- end / cancel ------------------------------------------------------

    @season_group.command(name="end", description="Resolve the active season right now.")
    async def end_command(self, interaction: discord.Interaction) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        await interaction.response.defer(thinking=True)
        resolution = await season_service.end_season_now(
            self.bot.pool, guild_id, actor, datetime.now(UTC)
        )
        embed = formatting.build_season_announcement_embed(resolution)
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @season_group.command(
        name="cancel", description="Cancel the active season without resolving it."
    )
    async def cancel_command(self, interaction: discord.Interaction) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        await interaction.response.defer(thinking=True)
        season = await season_service.cancel_season(self.bot.pool, guild_id, actor)
        embed = formatting.build_season_summary_embed(season, heading="Season Cancelled")
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    # -- info / history (anyone) --------------------------------------

    @season_group.command(name="info", description="Show the active season and its standings.")
    async def info_command(self, interaction: discord.Interaction) -> None:
        guild_id = guild_id_from_interaction(interaction)
        await interaction.response.defer(thinking=True)
        info = await season_service.season_info(self.bot.pool, guild_id)
        if info is None:
            await interaction.edit_original_response(
                content=_NO_ACTIVE_SEASON_TEXT, allowed_mentions=discord.AllowedMentions.none()
            )
            return
        embed = formatting.build_season_info_embed(info)
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @season_group.command(name="history", description="Show this server's past seasons.")
    @app_commands.describe(limit="How many seasons to show (1-25).")
    async def history_command(
        self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 25] | None = None
    ) -> None:
        guild_id = guild_id_from_interaction(interaction)
        await interaction.response.defer(thinking=True)
        seasons = await season_service.season_history(
            self.bot.pool, guild_id, limit or _DEFAULT_HISTORY_LIMIT
        )
        embed = formatting.build_season_history_embed(seasons)
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot: CatanBot) -> None:
    await bot.add_cog(SeasonCog(bot))
