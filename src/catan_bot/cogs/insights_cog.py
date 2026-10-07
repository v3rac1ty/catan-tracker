"""`/insights` commands for player, group-meta, and head-to-head trends."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from catan_bot import formatting
from catan_bot.bot import CatanBot
from catan_bot.permissions import guild_id_from_interaction
from catan_bot.services import insights_service

_SCOPE_CHOICES = [
    app_commands.Choice(name="all-time", value="all_time"),
    app_commands.Choice(name="season", value="season"),
]
_GAME_TYPE_CHOICES = [
    app_commands.Choice(name="Normal", value="normal"),
    app_commands.Choice(name="Seafarers", value="seafarers"),
    app_commands.Choice(name="Cities & Knights", value="cities_knights"),
    app_commands.Choice(name="Seafarers + Cities & Knights", value="seafarers_cities_knights"),
]


class InsightsCog(commands.Cog):
    insights_group = app_commands.Group(
        name="insights",
        description="Explore player and group trends from recorded games.",
        guild_only=True,
    )

    def __init__(self, bot: CatanBot) -> None:
        self.bot = bot

    @insights_group.command(name="player", description="Show one player's Catan trends.")
    @app_commands.describe(
        member="Whose trends to show. Defaults to you.",
        scope="Which games to include. Defaults to all-time.",
        game_type="Only include games with this ruleset.",
    )
    @app_commands.choices(scope=_SCOPE_CHOICES, game_type=_GAME_TYPE_CHOICES)
    async def player(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        scope: app_commands.Choice[str] | None = None,
        game_type: app_commands.Choice[str] | None = None,
    ) -> None:
        guild_id = guild_id_from_interaction(interaction)
        target = member if member is not None else interaction.user
        scope_value = scope.value if scope is not None else "all_time"
        game_type_value = game_type.value if game_type is not None else None
        await interaction.response.defer(thinking=True)
        view = await insights_service.player_insights(
            self.bot.pool,
            guild_id,
            target.id,
            scope=scope_value,
            game_type=game_type_value,
        )
        embed = formatting.build_player_insights_embed(view)
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @insights_group.command(name="meta", description="Show how the group wins games.")
    @app_commands.describe(
        scope="Which games to include. Defaults to all-time.",
        game_type="Only include games with this ruleset.",
    )
    @app_commands.choices(scope=_SCOPE_CHOICES, game_type=_GAME_TYPE_CHOICES)
    async def meta(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str] | None = None,
        game_type: app_commands.Choice[str] | None = None,
    ) -> None:
        guild_id = guild_id_from_interaction(interaction)
        scope_value = scope.value if scope is not None else "all_time"
        game_type_value = game_type.value if game_type is not None else None
        await interaction.response.defer(thinking=True)
        view = await insights_service.meta_insights(
            self.bot.pool, guild_id, scope=scope_value, game_type=game_type_value
        )
        embed = formatting.build_meta_insights_embed(view)
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @insights_group.command(
        name="head-to-head", description="Compare one player's record against each opponent."
    )
    @app_commands.describe(
        member="Whose head-to-head record to show. Defaults to you.",
        scope="Which games to include. Defaults to all-time.",
        game_type="Only include games with this ruleset.",
    )
    @app_commands.choices(scope=_SCOPE_CHOICES, game_type=_GAME_TYPE_CHOICES)
    async def head_to_head(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        scope: app_commands.Choice[str] | None = None,
        game_type: app_commands.Choice[str] | None = None,
    ) -> None:
        guild_id = guild_id_from_interaction(interaction)
        target = member if member is not None else interaction.user
        scope_value = scope.value if scope is not None else "all_time"
        game_type_value = game_type.value if game_type is not None else None
        await interaction.response.defer(thinking=True)
        view = await insights_service.head_to_head_insights(
            self.bot.pool,
            guild_id,
            target.id,
            scope=scope_value,
            game_type=game_type_value,
        )
        embed = formatting.build_head_to_head_embed(view)
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot: CatanBot) -> None:
    await bot.add_cog(InsightsCog(bot))
