"""`/game` commands: reporting, per-player DM score collection, and admin edits."""

from __future__ import annotations

import logging
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
from catan_bot.views import score_entry
from catan_bot.views.game_confirm import build_game_action_view
from catan_bot.views.game_scores import GameScoreSheet

logger = logging.getLogger(__name__)

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
        members_by_id = {winner.id: winner, **{loser.id: loser for loser in losers}}

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
        # The pending game is created immediately -- Phase 2 no longer waits
        # for a reporter-facing Submit click. Its public message carries the
        # existing Confirm/Reject/Nudge buttons and doubles as the live
        # score sheet; each participant separately gets their own DM to
        # fill in only their own row (`views/score_entry.py`).
        created = await game_service.submit_game_report(
            self.bot.pool, guild_id, actor, prepared, scores=None, now=datetime.now(UTC)
        )
        game_id = created.game.game_id
        public_message = await interaction.channel.send(
            embed=formatting.build_game_report_embed(created),
            view=build_game_action_view(game_id),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await game_service.record_game_message(
            self.bot.pool, guild_id, game_id, public_message.channel.id, public_message.id
        )
        participant_ids = (created.winner_id, *created.loser_ids)
        await game_service.open_score_collection(
            self.bot.pool, guild_id, game_id, participant_ids, datetime.now(UTC)
        )

        rules = score_entry.rules_for_game(created.game)
        blocked: list[discord.Member] = []
        for user_id in participant_ids:
            member = members_by_id[user_id]
            sheet_embed = score_entry.build_score_entry_embed(created, user_id)
            sheet_view = score_entry.build_score_entry_view(guild_id, game_id, user_id, rules)
            try:
                dm_message = await member.send(
                    embed=sheet_embed,
                    view=sheet_view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException as exc:
                # One player's failed DM -- closed DMs (`discord.Forbidden`,
                # a subclass of `HTTPException`) or any other transient HTTP
                # error -- must not break the report for everyone else --
                # record it as blocked and keep going; `/game scores` is
                # their fallback, and the public message's progress field
                # marks them distinctly. Catching the wider `HTTPException`
                # (not just `Forbidden`) means a one-off 5xx/network blip
                # hitting a single participant's DM no longer aborts the
                # whole loop before the remaining participants are ever
                # messaged. Logged with the exception TYPE only, never
                # `str(exc)` or a traceback -- matching `scheduler.py`'s
                # `_log_failure` discipline -- since a Discord HTTP error's
                # message can echo back request content.
                logger.warning(
                    "Score sheet DM failed user_id=%s game_id=%s: %s",
                    user_id,
                    game_id,
                    type(exc).__name__,
                )
                blocked.append(member)
                await game_service.record_score_request_delivery(
                    self.bot.pool,
                    guild_id,
                    game_id,
                    user_id,
                    channel_id=None,
                    message_id=None,
                    delivered=False,
                )
                continue
            await game_service.record_score_request_delivery(
                self.bot.pool,
                guild_id,
                game_id,
                user_id,
                channel_id=dm_message.channel.id,
                message_id=dm_message.id,
                delivered=True,
            )

        status = await game_service.score_collection_status(self.bot.pool, guild_id, game_id)
        await public_message.edit(
            embed=formatting.build_game_report_embed(created, collection=status),
            view=build_game_action_view(game_id),
            allowed_mentions=discord.AllowedMentions.none(),
        )

        summary = f"Game #{game_id} reported: {public_message.jump_url}"
        if blocked:
            names = ", ".join(formatting.mention(member.id) for member in blocked)
            summary += f"\nCouldn't DM {names} (DMs closed) -- they can run `/game scores` instead."
        await interaction.edit_original_response(
            content=summary, embed=None, allowed_mentions=discord.AllowedMentions.none()
        )

    @game_group.command(name="scores", description="Open your own score-entry sheet for a game.")
    @app_commands.describe(
        game_id="The game to enter scores for. Defaults to your most recent game "
        "still awaiting your score."
    )
    async def scores_command(
        self,
        interaction: discord.Interaction,
        game_id: app_commands.Range[int, 1, _DISCORD_INTEGER_MAX] | None = None,
    ) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        resolved_id = game_id
        if resolved_id is None:
            resolved_id = await game_service.find_open_score_request_game_id(
                self.bot.pool, guild_id, actor.user_id
            )
            if resolved_id is None:
                await interaction.edit_original_response(
                    content="You have no games waiting on your score."
                )
                return
        game = await game_service.get_game_for_player(
            self.bot.pool, guild_id, resolved_id, actor.user_id
        )
        rules = score_entry.rules_for_game(game.game)
        embed = score_entry.build_score_entry_embed(game, actor.user_id)
        selected = score_entry.selected_awards_for(game, actor.user_id, rules)
        view = score_entry.build_score_entry_view(
            guild_id, resolved_id, actor.user_id, rules, selected_awards=selected
        )
        await interaction.edit_original_response(
            embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
        )

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
        # A game whose public message failed to send (or whose message/view
        # was otherwise lost) has no Confirm/Reject buttons anywhere and can
        # never be confirmed -- re-attaching the view here whenever the game
        # is still pending gives it a way back, without needing a re-report.
        view = build_game_action_view(game_id) if game.game.status == "pending" else None
        await interaction.edit_original_response(
            embed=formatting.build_game_status_embed(game),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @game_group.command(name="history", description="Show recent games.")
    @app_commands.describe(
        member="Only show this player's games.",
        limit="How many games to show (1-25).",
        include_voided="Include voided games. Defaults to hiding them.",
    )
    async def history_command(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        limit: app_commands.Range[int, 1, 25] | None = None,
        include_voided: bool = False,
    ) -> None:
        guild_id = guild_id_from_interaction(interaction)
        await interaction.response.defer(thinking=True)
        games = await game_service.game_history(
            self.bot.pool,
            guild_id,
            user_id=member.id if member is not None else None,
            limit=limit or _DEFAULT_HISTORY_LIMIT,
            include_voided=include_voided,
        )
        embed = formatting.build_game_history_embed(
            games, member_id=member.id if member is not None else None
        )
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot: CatanBot) -> None:
    await bot.add_cog(GameCog(bot))
