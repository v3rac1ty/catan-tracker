"""`/game` commands, including the private score-sheet report workflow."""

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
from catan_bot.views.game_scores import GameScoreSheet

_DEFAULT_HISTORY_LIMIT = 10
_REPORT_COOLDOWN_SECONDS = 30.0
_DISCORD_INTEGER_MAX = 2**53 - 1
_GAME_TYPE_CHOICES = [
    app_commands.Choice(name="Normal", value="normal"),
    app_commands.Choice(name="Seafarers", value="seafarers"),
    app_commands.Choice(name="Cities & Knights", value="cities_knights"),
    app_commands.Choice(name="Seafarers + Cities & Knights", value="seafarers_cities_knights"),
]


def _participants(
    winner: discord.Member, losers: list[discord.Member]
) -> tuple[ParticipantRef, list[ParticipantRef]]:
    winner_ref = ParticipantRef(user_id=winner.id, is_bot=winner.bot)
    loser_refs = [ParticipantRef(user_id=m.id, is_bot=m.bot) for m in losers]
    return winner_ref, loser_refs


class GameCog(commands.Cog):
    game_group = app_commands.Group(
        name="game",
        description="Report, update, void, or look up Catan games.",
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
        time="Optional local time of play.",
        game_type="Rules used for this game.",
        extension_5_6="Whether the 5–6 Player Extension was used.",
        scenario="Optional official scenario name.",
        target_points="Winning score for this game.",
    )
    @app_commands.choices(game_type=_GAME_TYPE_CHOICES)
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
        time: app_commands.Range[str, 1, 32] | None = None,
        game_type: app_commands.Choice[str] | None = None,
        extension_5_6: bool | None = None,
        scenario: app_commands.Range[str, 1, 100] | None = None,
        target_points: app_commands.Range[int, 1, 99] | None = None,
    ) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        losers = [m for m in (loser1, loser2, loser3, loser4, loser5) if m is not None]
        winner_ref, loser_refs = _participants(winner, losers)

        await interaction.response.defer(ephemeral=True, thinking=True)
        prepared = await game_service.prepare_game_report(
            self.bot.pool,
            guild_id,
            actor,
            winner=winner_ref,
            losers=loser_refs,
            date_text=date,
            time_text=time,
            now=datetime.now(UTC),
            game_type=game_type.value if game_type is not None else "normal",
            extension_5_6=extension_5_6,
            scenario=scenario,
            target_points=target_points,
        )
        view = GameScoreSheet(
            pool=self.bot.pool,
            guild_id=guild_id,
            actor=actor,
            prepared=prepared,
            channel=interaction.channel,
        )
        private_message = await interaction.edit_original_response(
            embed=view.embed(),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        view.private_message = private_message

    @game_group.command(name="update", description="Correct a confirmed game (admin).")
    @app_commands.describe(
        game_id="The confirmed game to correct.",
        winner="Replacement winner; an external winner needs a full loser list.",
        loser1="First replacement loser. Supplying any loser replaces the entire loser list.",
        loser2="Replacement loser.",
        loser3="Replacement loser.",
        loser4="Replacement loser.",
        loser5="Replacement loser.",
        date="Optional replacement date.",
        time="Optional replacement local time.",
        game_type="Optional replacement rules.",
        extension_5_6="Optional replacement extension setting.",
        scenario="Optional replacement scenario; use clear_scenario to remove it.",
        target_points="Optional replacement winning score.",
        reason="Why this confirmed game is being corrected.",
        clear_time="Remove the recorded time.",
        clear_scenario="Remove the recorded scenario.",
    )
    @app_commands.choices(game_type=_GAME_TYPE_CHOICES)
    async def update_command(
        self,
        interaction: discord.Interaction,
        game_id: app_commands.Range[int, 1, _DISCORD_INTEGER_MAX],
        winner: discord.Member | None = None,
        loser1: discord.Member | None = None,
        loser2: discord.Member | None = None,
        loser3: discord.Member | None = None,
        loser4: discord.Member | None = None,
        loser5: discord.Member | None = None,
        date: app_commands.Range[str, 1, 32] | None = None,
        time: app_commands.Range[str, 1, 32] | None = None,
        game_type: app_commands.Choice[str] | None = None,
        extension_5_6: bool | None = None,
        scenario: app_commands.Range[str, 1, 100] | None = None,
        target_points: app_commands.Range[int, 1, 99] | None = None,
        reason: app_commands.Range[str, 1, 200] | None = None,
        clear_time: bool = False,
        clear_scenario: bool = False,
    ) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        loser_members = (loser1, loser2, loser3, loser4, loser5)
        # `None` means preserve; an option (including just loser1) replaces all losers.
        loser_refs = (
            None
            if all(member is None for member in loser_members)
            else [
                ParticipantRef(user_id=member.id, is_bot=member.bot)
                for member in loser_members
                if member is not None
            ]
        )
        winner_ref = (
            None if winner is None else ParticipantRef(user_id=winner.id, is_bot=winner.bot)
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        prepared = await game_service.prepare_game_update(
            self.bot.pool,
            guild_id,
            actor,
            game_id=game_id,
            winner=winner_ref,
            losers=loser_refs,
            date_text=date,
            time_text=time,
            game_type=game_type.value if game_type is not None else None,
            extension_5_6=extension_5_6,
            scenario=scenario,
            target_points=target_points,
            reason=reason,
            clear_time=clear_time,
            clear_scenario=clear_scenario,
            now=datetime.now(UTC),
        )
        view = GameScoreSheet(
            pool=self.bot.pool,
            guild_id=guild_id,
            actor=actor,
            prepared=prepared,
            channel=interaction.channel,
            mode="update",
            bot=self.bot,
        )
        private_message = await interaction.edit_original_response(
            embed=view.embed(), view=view, allowed_mentions=discord.AllowedMentions.none()
        )
        view.private_message = private_message

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

    @game_group.command(name="show", description="Show a complete game report.")
    @app_commands.describe(game_id="The game's id.")
    async def show_command(
        self,
        interaction: discord.Interaction,
        game_id: app_commands.Range[int, 1, _DISCORD_INTEGER_MAX],
    ) -> None:
        guild_id = guild_id_from_interaction(interaction)
        await interaction.response.defer(thinking=True)
        game = await game_service.get_game(self.bot.pool, guild_id, game_id)
        await interaction.edit_original_response(
            embed=formatting.build_game_status_embed(game),
            allowed_mentions=discord.AllowedMentions.none(),
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
