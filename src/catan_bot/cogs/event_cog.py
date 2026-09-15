"""`/event` commands for game-night scheduling."""

from __future__ import annotations

from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from catan_bot import formatting
from catan_bot.bot import CatanBot
from catan_bot.cogs.season_cog import filter_date_choices
from catan_bot.permissions import actor_from_interaction, guild_id_from_interaction
from catan_bot.services import event_service
from catan_bot.views.event_rsvp import build_event_rsvp_view

_DEFAULT_LIST_LIMIT = 10
_DISCORD_INTEGER_MAX = 2**53 - 1
_BIGINT_MAX = 2**63 - 1


def _channel_id(interaction: discord.Interaction) -> int:
    channel_id = interaction.channel_id
    if type(channel_id) is not int or not (1 <= channel_id <= _BIGINT_MAX):
        raise ValueError("interaction requires a valid channel id")
    return channel_id


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
    )
    async def create_command(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, 100],
        time: app_commands.Range[str, 1, 16],
        date: app_commands.Range[str, 1, 32] | None = None,
        location: app_commands.Range[str, 1, 200] | None = None,
        description: app_commands.Range[str, 1, 1000] | None = None,
    ) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        channel_id = _channel_id(interaction)
        await interaction.response.defer(thinking=True)
        event = await event_service.create_event(
            self.bot.pool,
            guild_id,
            actor,
            title=title,
            date_text=date,
            time_text=time,
            location=location,
            description=description,
            channel_id=channel_id,
            now=datetime.now(UTC),
        )
        sent = await interaction.edit_original_response(
            embed=formatting.build_event_embed(event),
            view=build_event_rsvp_view(event.event_id),
            allowed_mentions=discord.AllowedMentions.none(),
        )
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
        await interaction.edit_original_response(
            embed=formatting.build_event_embed(event),
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: CatanBot) -> None:
    await bot.add_cog(EventCog(bot))
