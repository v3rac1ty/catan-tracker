"""Persistent Confirm/Reject/Nudge buttons on a reported game's public message.

`GameActionButton` handles Confirm and Reject via an action group in its
`custom_id` template, per DESIGN.md: `r"game:(?P<action>confirm|reject):
(?P<id>[0-9]{1,19})"`. `GameNudgeButton` (Phase 2) is a separate class on its
own template -- unlike confirm/reject it never transitions the game's
status, so it has nothing in common with `_confirm_result_error`/
`_reject_result_error`'s status-transition mapping. Both are registered via
`bot.add_dynamic_items(GameActionButton, GameNudgeButton)` (see `bot.py`),
so the buttons keep working after a restart -- discord.py reconstructs a
fresh instance from the `custom_id` alone (via `from_custom_id`) every time
one is pressed, with no persisted Python state in between.
"""

from __future__ import annotations

import re
from time import monotonic
from typing import Literal

import discord

from catan_bot import formatting
from catan_bot.errors import handle_interaction_error
from catan_bot.permissions import actor_from_interaction, guild_id_from_interaction
from catan_bot.services import game_service, leaderboard_service
from catan_bot.services.errors import PermissionDeniedError

_CUSTOM_ID_TEMPLATE = re.compile(r"game:(?P<action>confirm|reject):(?P<id>[0-9]{1,19})")
_NUDGE_CUSTOM_ID_TEMPLATE = re.compile(r"game:nudge:(?P<id>[0-9]{1,19})")
_BIGINT_MAX = 2**63 - 1
_NUDGE_COOLDOWN_SECONDS = 600.0
_NUDGE_NOT_ALLOWED = "Only a participant or the original reporter can nudge for this game."

_STYLES: dict[str, discord.ButtonStyle] = {
    "confirm": discord.ButtonStyle.success,
    "reject": discord.ButtonStyle.danger,
}
_LABELS: dict[str, str] = {"confirm": "Confirm", "reject": "Reject"}


GameAction = Literal["confirm", "reject"]


def _custom_id(action: GameAction, game_id: int) -> str:
    return f"game:{action}:{game_id}"


async def _post_leaderboard_after_confirm(client: object, guild_id: int) -> None:
    """Best-effort recurring leaderboard post, right after a game is confirmed.

    Mirrors `views/score_entry.py`'s `_refresh_public_message`: the
    confirmation this follows is already durably committed by the time this
    runs, so a failure here (`per_game` mode is off, no channel is
    configured, a transient Discord/DB hiccup, ...) must never surface as
    the confirmation itself having failed -- every exception is swallowed.
    `leaderboard_service.leaderboard_after_game` already returns `None`
    (not an error) for the common "nothing to post" cases; this still
    wraps the whole thing in `try/except` for the uncommon ones (a
    connection drop, a deleted/permission-less channel, ...).
    """
    try:
        pool = client.pool  # type: ignore[attr-defined]
        post = await leaderboard_service.leaderboard_after_game(pool, guild_id)
        if post is None:
            return
        channel = client.get_channel(post.channel_id)  # type: ignore[attr-defined]
        if channel is None:
            channel = await client.fetch_channel(post.channel_id)  # type: ignore[attr-defined]
        embed = formatting.build_leaderboard_post_embed(post)
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except Exception:  # noqa: S110 -- best-effort post, see docstring above.
        return


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
            if self.action == "confirm":
                # The game is already durably confirmed above -- this is
                # purely a best-effort extra post from here on.
                await _post_leaderboard_after_confirm(interaction.client, guild_id)
        except Exception as exc:  # routed through the shared handler below
            await handle_interaction_error(interaction, exc, command_name=f"game:{self.action}")


# In-memory, per-game nudge cooldown (Phase 2). `time.monotonic` is exactly
# right for this: it's view-layer rate limiting, not a durable fact, so it
# belongs nowhere near the services layer's clock-injection rule (services
# never read the clock themselves -- see CLAUDE.md -- but a view rate-
# limiting its own button press is a different concern entirely). Resetting
# on a bot restart is an acceptable trade for not needing a database round
# trip -- and a migration -- just to rate-limit a ping.
_last_nudge_at: dict[int, float] = {}


class GameNudgeButton(
    discord.ui.DynamicItem[discord.ui.Button], template=_NUDGE_CUSTOM_ID_TEMPLATE
):
    """Pings whichever participants still owe a score for one pending game.

    Any participant or the original reporter may press this -- nagging your
    own game's roster carries no real risk, so there's no stricter
    permission check than "you're involved in this game." Rate-limited to
    once per ten minutes per game so it can't be used to spam-ping the same
    handful of players.
    """

    def __init__(self, game_id: int) -> None:
        self.game_id = game_id
        super().__init__(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label="Nudge players",
                custom_id=f"game:nudge:{game_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Item, match: re.Match[str], /
    ) -> GameNudgeButton:
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            actor = actor_from_interaction(interaction)
            guild_id = guild_id_from_interaction(interaction)
            if not (1 <= self.game_id <= _BIGINT_MAX):
                raise ValueError("game nudge button has an invalid game id")
            pool = interaction.client.pool  # type: ignore[attr-defined]
            status = await game_service.score_collection_status(pool, guild_id, self.game_id)
            game = status.game
            allowed_ids = (game.winner_id, *game.loser_ids, game.game.reported_by)
            if actor.user_id not in allowed_ids:
                raise PermissionDeniedError(_NUDGE_NOT_ALLOWED)
            outstanding = status.outstanding_ids
            if not outstanding:
                await interaction.response.send_message(
                    "Everyone has already submitted a score for this game.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            now = monotonic()
            last = _last_nudge_at.get(self.game_id)
            if last is not None and now - last < _NUDGE_COOLDOWN_SECONDS:
                remaining = round(_NUDGE_COOLDOWN_SECONDS - (now - last))
                await interaction.response.send_message(
                    f"This game was already nudged recently -- try again in {remaining} seconds.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            _last_nudge_at[self.game_id] = now
            mentions_text = " ".join(formatting.mention(uid) for uid in outstanding)
            await interaction.response.send_message(
                f"{mentions_text} -- you still owe a score for game #{self.game_id}. "
                "Check your DMs, or run `/game scores`.",
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    roles=False,
                    users=[discord.Object(id=user_id) for user_id in outstanding],
                ),
            )
        except Exception as exc:  # routed through the shared handler below
            await handle_interaction_error(interaction, exc, command_name="game:nudge")


def build_game_action_view(game_id: int) -> discord.ui.View:
    """The Confirm + Reject + Nudge view attached to a reported game's public message."""
    if type(game_id) is not int or not (1 <= game_id <= _BIGINT_MAX):
        raise ValueError("game_id must be a positive BIGINT")
    view = discord.ui.View(timeout=None)
    view.add_item(GameActionButton(game_id, "confirm"))
    view.add_item(GameActionButton(game_id, "reject"))
    view.add_item(GameNudgeButton(game_id))
    return view
