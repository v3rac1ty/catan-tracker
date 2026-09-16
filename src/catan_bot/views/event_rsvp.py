"""Persistent RSVP buttons for scheduled events."""

from __future__ import annotations

import re
from typing import Literal

import discord

from catan_bot import formatting
from catan_bot.errors import handle_interaction_error
from catan_bot.permissions import actor_from_interaction, guild_id_from_interaction
from catan_bot.services import event_service

_CUSTOM_ID_TEMPLATE = re.compile(r"rsvp:(?P<resp>going|maybe|no):(?P<id>[0-9]{1,19})")
_BIGINT_MAX = 2**63 - 1

RsvpChoice = Literal["going", "maybe", "no"]

_STYLES: dict[RsvpChoice, discord.ButtonStyle] = {
    "going": discord.ButtonStyle.success,
    "maybe": discord.ButtonStyle.primary,
    "no": discord.ButtonStyle.secondary,
}
_LABELS: dict[RsvpChoice, str] = {
    "going": "Going",
    "maybe": "Maybe",
    "no": "Not going",
}
_SERVICE_RESPONSES = {
    "going": "going",
    "maybe": "maybe",
    "no": "not_going",
}


class EventRsvpButton(discord.ui.DynamicItem[discord.ui.Button], template=_CUSTOM_ID_TEMPLATE):
    """One restart-safe RSVP choice bound to a scheduled event."""

    def __init__(self, event_id: int, response: RsvpChoice) -> None:
        self.event_id = event_id
        self.response = response
        super().__init__(
            discord.ui.Button(
                style=_STYLES[response],
                label=_LABELS[response],
                custom_id=f"rsvp:{response}:{event_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item, match: re.Match[str], /
    ) -> EventRsvpButton:
        response: RsvpChoice = match["resp"]  # type: ignore[assignment]
        return cls(int(match["id"]), response)

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            actor = actor_from_interaction(interaction)
            guild_id = guild_id_from_interaction(interaction)
            if not (1 <= self.event_id <= _BIGINT_MAX):
                raise ValueError("RSVP button has an invalid event id")
            pool = interaction.client.pool  # type: ignore[attr-defined]
            await interaction.response.defer()
            await event_service.rsvp(
                pool,
                guild_id,
                self.event_id,
                actor,
                _SERVICE_RESPONSES[self.response],
            )
            event = await event_service.get_event(pool, guild_id, self.event_id)
            if event is None:
                raise RuntimeError("event vanished after a successful RSVP")
            view = build_event_rsvp_view(self.event_id) if event.status == "scheduled" else None
            roster = await event_service.rsvp_roster(pool, guild_id, self.event_id)
            await interaction.edit_original_response(
                embed=formatting.build_event_embed(event, roster),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            await handle_interaction_error(interaction, exc, command_name=f"rsvp:{self.response}")


def build_event_rsvp_view(event_id: int) -> discord.ui.View:
    """The persistent Going/Maybe/Not going controls for one event."""
    if type(event_id) is not int or not (1 <= event_id <= _BIGINT_MAX):
        raise ValueError("event_id must be a positive BIGINT")
    view = discord.ui.View(timeout=None)
    view.add_item(EventRsvpButton(event_id, "going"))
    view.add_item(EventRsvpButton(event_id, "maybe"))
    view.add_item(EventRsvpButton(event_id, "no"))
    return view
