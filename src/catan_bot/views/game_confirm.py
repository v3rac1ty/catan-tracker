"""Persistent Confirm/Reject buttons on a reported game's public message.

One `discord.ui.DynamicItem` handles both actions via an action group in its
`custom_id` template, per DESIGN.md: `r"game:(?P<action>confirm|reject):
(?P<id>[0-9]{1,19})"`. Registered once via `bot.add_dynamic_items
(GameActionButton)` (see `bot.py`), so the buttons keep working after a
restart -- discord.py reconstructs a fresh `GameActionButton` from the
`custom_id` alone (via `from_custom_id`) every time one is pressed, with no
persisted Python state in between.
"""

from __future__ import annotations

import re
from typing import Literal

import discord

from catan_bot import formatting
from catan_bot.errors import handle_interaction_error
from catan_bot.permissions import actor_from_interaction, guild_id_from_interaction
from catan_bot.services import game_service

_CUSTOM_ID_TEMPLATE = re.compile(r"game:(?P<action>confirm|reject):(?P<id>[0-9]{1,19})")
_BIGINT_MAX = 2**63 - 1

_STYLES: dict[str, discord.ButtonStyle] = {
    "confirm": discord.ButtonStyle.success,
    "reject": discord.ButtonStyle.danger,
}
_LABELS: dict[str, str] = {"confirm": "Confirm", "reject": "Reject"}


GameAction = Literal["confirm", "reject"]


def _custom_id(action: GameAction, game_id: int) -> str:
    return f"game:{action}:{game_id}"


class GameActionButton(discord.ui.DynamicItem[discord.ui.Button], template=_CUSTOM_ID_TEMPLATE):
    """A Confirm or Reject button bound to one pending game report."""

    def __init__(self, game_id: int, action: GameAction) -> None:
        self.game_id = game_id
        self.action = action
        super().__init__(
            discord.ui.Button(
                style=_STYLES[action],
                label=_LABELS[action],
                custom_id=_custom_id(action, game_id),
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item, match: re.Match[str], /
    ) -> GameActionButton:
        action: GameAction = match["action"]  # type: ignore[assignment]
        return cls(int(match["id"]), action)

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            actor = actor_from_interaction(interaction)
            guild_id = guild_id_from_interaction(interaction)
            if not (1 <= self.game_id <= _BIGINT_MAX):
                raise ValueError("game action button has an invalid game id")
            pool = interaction.client.pool  # type: ignore[attr-defined]
            await interaction.response.defer()
            if self.action == "confirm":
                updated = await game_service.confirm_game(pool, guild_id, self.game_id, actor)
            else:
                updated = await game_service.reject_game(pool, guild_id, self.game_id, actor)

            embed = formatting.build_game_status_embed(updated)
            await interaction.edit_original_response(
                embed=embed, view=None, allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception as exc:  # routed through the shared handler below
            await handle_interaction_error(interaction, exc, command_name=f"game:{self.action}")


def build_game_action_view(game_id: int) -> discord.ui.View:
    """The Confirm + Reject view attached to a freshly reported game's public message."""
    if type(game_id) is not int or not (1 <= game_id <= _BIGINT_MAX):
        raise ValueError("game_id must be a positive BIGINT")
    view = discord.ui.View(timeout=None)
    view.add_item(GameActionButton(game_id, "confirm"))
    view.add_item(GameActionButton(game_id, "reject"))
    return view
