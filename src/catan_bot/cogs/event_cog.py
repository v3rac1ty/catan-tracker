"""`/event` commands for game-night scheduling."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from catan_bot import formatting
from catan_bot.bot import CatanBot
from catan_bot.cogs.channel_publish import (
    validate_notification_role,
    validate_publish_channel,
)
from catan_bot.cogs.season_cog import filter_date_choices
from catan_bot.permissions import actor_from_interaction, guild_id_from_interaction
from catan_bot.services import config_service, event_service
from catan_bot.views.event_rsvp import build_event_rsvp_view

_DEFAULT_LIST_LIMIT = 10
_DISCORD_INTEGER_MAX = 2**53 - 1
_BIGINT_MAX = 2**63 - 1

logger = logging.getLogger(__name__)


def _channel_id(interaction: discord.Interaction) -> int:
    channel_id = interaction.channel_id
    if type(channel_id) is not int or not (1 <= channel_id <= _BIGINT_MAX):
        raise ValueError("interaction requires a valid channel id")
    return channel_id


def _announcement_mentions(
    player_role_id: int | None,
) -> tuple[str | None, discord.AllowedMentions]:
    """Return the only intentional mention an event announcement may send."""
    if player_role_id is None:
        return None, discord.AllowedMentions.none()
    return (
        formatting.role_mention(player_role_id),
        discord.AllowedMentions(
            everyone=False,
            users=False,
            roles=[discord.Object(id=player_role_id)],
            replied_user=False,
        ),
    )


class EventCog(commands.Cog):
    event_group = app_commands.Group(
        name="event",
        description="Create, list, or cancel Catan game-night events.",
        guild_only=True,
    )

    def __init__(self, bot: CatanBot) -> None:
        self.bot = bot

    @event_group.command(name="create", description="Schedule a Catan game night.")
    @app_commands.describe(
        title="The event title.",
        time="Start time (HH:MM or h:MMam/pm).",
        date="Start date. Defaults to today in this server's timezone.",
        location="Where the event will happen.",
        description="Extra details for attendees.",
        channel="Where to post it. Defaults to this channel.",
    )
    async def create_command(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, 100],
        time: app_commands.Range[str, 1, 16],
        date: app_commands.Range[str, 1, 32] | None = None,
        location: app_commands.Range[str, 1, 200] | None = None,
        description: app_commands.Range[str, 1, 1000] | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        destination = channel if channel is not None else interaction.channel
        if destination is None:
            raise ValueError("interaction requires a channel")
        validate_publish_channel(interaction, destination)
        destination_id = destination.id if channel is not None else _channel_id(interaction)
        if channel is None:
            await interaction.response.defer(thinking=True)
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
        # A real Discord guild always provides ``get_role``.  Keeping the
        # callable guard also lets lightweight interaction doubles exercise
        # the legacy no-role path without pretending they can resolve roles.
        configured_role_id: int | None = None
        role_preflighted = callable(getattr(interaction.guild, "get_role", None))
        if role_preflighted:
            config = await config_service.get_config(self.bot.pool, guild_id)
            configured_role_id = config.player_role_id
            validate_notification_role(interaction, destination, configured_role_id)
        created = await event_service.create_event_with_notification_role(
            self.bot.pool,
            guild_id,
            actor,
            title=title,
            date_text=date,
            time_text=time,
            location=location,
            description=description,
            channel_id=destination_id,
            now=datetime.now(UTC),
        )
        event = created.event
        # Re-check the snapshot after the transaction in case configuration
        # changed while the command was being prepared.  The transaction is
        # already committed, so a race must never abort publication and leave
        # an orphaned event.  Publish safely without a ping and explain how to
        # repair the configuration instead.
        effective_role_id = created.player_role_id
        role_warning = False
        if role_preflighted and created.player_role_id != configured_role_id:
            logger.warning(
                "Event player-role changed before publication guild_id=%s event_id=%s",
                guild_id,
                event.event_id,
            )
            effective_role_id = None
            role_warning = True
        elif role_preflighted:
            try:
                validate_notification_role(interaction, destination, effective_role_id)
            except Exception:
                logger.warning(
                    "Event player-role changed before publication guild_id=%s event_id=%s",
                    guild_id,
                    event.event_id,
                )
                effective_role_id = None
                role_warning = True
        content, allowed_mentions = _announcement_mentions(effective_role_id)
        if role_warning and channel is None:
            content = (
                "The player-role ping was skipped because the configured role is unavailable "
                "or not mentionable; review /config player-role."
            )
        embed = formatting.build_event_embed(event)
        view = build_event_rsvp_view(event.event_id)
        if channel is None:
            sent = await interaction.edit_original_response(
                content=content,
                embed=embed,
                view=view,
                allowed_mentions=allowed_mentions,
            )
        else:
            sent = await destination.send(
                content=content,
                embed=embed,
                view=view,
                allowed_mentions=allowed_mentions,
            )
            await event_service.record_event_message(
                self.bot.pool, guild_id, event.event_id, sent.channel.id, sent.id
            )
            await interaction.edit_original_response(
                content=(
                    f"Event posted in {formatting.channel_mention(destination.id)}: {sent.jump_url}"
                    + (
                        " The player-role ping was skipped because the configured role "
                        "is unavailable or not mentionable; review /config player-role."
                        if role_warning
                        else ""
                    )
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await event_service.record_event_message(
            self.bot.pool, guild_id, event.event_id, sent.channel.id, sent.id
        )

    @create_command.autocomplete("date")
    async def create_date_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=label, value=value)
            for label, value in filter_date_choices(current)
        ]

    @event_group.command(name="list", description="Show upcoming Catan game nights.")
    @app_commands.describe(limit="How many events to show (1-10).")
    async def list_command(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 10] | None = None,
    ) -> None:
        guild_id = guild_id_from_interaction(interaction)
        await interaction.response.defer(thinking=True)
        events = await event_service.upcoming_events(
            self.bot.pool, guild_id, datetime.now(UTC), limit or _DEFAULT_LIST_LIMIT
        )
        await interaction.edit_original_response(
            embed=formatting.build_event_list_embed(events),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @event_group.command(name="cancel", description="Cancel a scheduled event.")
    @app_commands.describe(event_id="The event's id.")
    async def cancel_command(
        self,
        interaction: discord.Interaction,
        event_id: app_commands.Range[int, 1, _DISCORD_INTEGER_MAX],
    ) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        await interaction.response.defer(thinking=True)
        event = await event_service.cancel_event(self.bot.pool, guild_id, event_id, actor)
        roster = await event_service.rsvp_roster(self.bot.pool, guild_id, event_id)
        await interaction.edit_original_response(
            embed=formatting.build_event_embed(event, roster),
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: CatanBot) -> None:
    await bot.add_cog(EventCog(bot))
