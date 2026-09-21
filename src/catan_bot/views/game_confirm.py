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

Phase 5: since scores now arrive gradually via per-player DMs, a Confirm
click can land before every participant has submitted. `GameActionButton`
confirms immediately, with no extra step, once collection is complete --
but for a partial roster it shows `_ConfirmAnywayView`, an ephemeral,
owner-locked second step naming who hasn't submitted yet. That dialog is
deliberately a plain `discord.ui.View`, *not* a `DynamicItem`: it lives only
as long as the interaction token that created it (about 15 minutes, and
this one times out itself well before that), so nothing is gained -- and
restart-durability would be misleadingly implied -- by making it
`custom_id`-addressed like the persistent buttons above.

The same dialog now also covers a second, unrelated reason to pause: a
recorded winner whose total is below the game's target score. That check is
deliberately no longer made at per-row save time
(`domain.scoring.validate_game_scores`'s `enforce_winner_target`, and
`services.game_service.record_player_score`'s docstring) since a winner who
only reaches target via a later-claimed award would otherwise be
permanently deadlocked -- so this confirm path is where it's enforced
instead, via `services.results.ScoreCollectionStatus.winner_shortfall`.
Reusing the existing dialog (rather than adding a second one) means both
"someone hasn't submitted" and "the winner is short" can be named in the
same prompt when they both apply, and an admin/participant sees exactly one
extra click either way.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from contextlib import suppress
from time import monotonic
from typing import Any, Literal

import discord

from catan_bot import formatting
from catan_bot.db.models import GameWithParticipants
from catan_bot.errors import handle_interaction_error
from catan_bot.permissions import actor_from_interaction, guild_id_from_interaction
from catan_bot.services import game_service, leaderboard_service
from catan_bot.services.context import Actor
from catan_bot.services.errors import PermissionDeniedError
from catan_bot.services.results import ScoreCollectionStatus

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

_CONFIRM_ANYWAY_TIMEOUT_SECONDS = 120.0
_DIALOG_WRONG_OWNER_TEXT = "Only the person who clicked Confirm can respond to this."


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

            if self.action == "confirm":
                # Permission is decided here, before anything is shown, so a
                # reporter or non-participant gets the same permission error
                # `confirm_game` would raise instead of a confirmation
                # prompt for an action they were never allowed to take.
                status = await game_service.confirm_preflight(pool, guild_id, self.game_id, actor)
                if not status.complete or status.winner_shortfall is not None:
                    await _prompt_confirm_anyway(
                        interaction,
                        guild_id=guild_id,
                        game_id=self.game_id,
                        actor=actor,
                        status=status,
                    )
                    return

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


def _list_mentions(user_ids: Sequence[int]) -> str:
    """A natural-language mention list: "@A", "@A and @B", or "@A, @B, and @C"."""
    names = [formatting.mention(uid) for uid in user_ids]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def _confirm_anyway_content(status: ScoreCollectionStatus) -> str:
    """Build the dialog's warning text from whichever reason(s) triggered it.

    Either or both of "someone hasn't submitted" and "the winner is short of
    target" can apply at once (e.g. two outstanding players *and* an
    already-recorded winner below target) -- when both do, this names both
    rather than picking one, so nothing about the game's actual state is
    hidden from whoever is about to confirm anyway.
    """
    lines: list[str] = []
    outstanding = status.outstanding_ids
    if outstanding:
        verb = "hasn't" if len(outstanding) == 1 else "haven't"
        lines.append(f"{_list_mentions(outstanding)} {verb} entered their points yet.")
    shortfall = status.winner_shortfall
    if shortfall is not None:
        target = status.game.game.target_points
        recorded = target - shortfall
        lines.append(
            f"{formatting.mention(status.game.winner_id)} is recorded with {recorded} point(s), "
            f"short of the {target}-point target."
        )
    lines.append("Confirm anyway and save the game with partial scores?")
    return "\n".join(lines)


async def _prompt_confirm_anyway(
    interaction: discord.Interaction,
    *,
    guild_id: int,
    game_id: int,
    actor: Actor,
    status: ScoreCollectionStatus,
) -> None:
    """Show the ephemeral "confirm anyway" second step for a partially-scored
    or below-target-winner game.

    Only ever reached once `game_service.confirm_preflight` has already
    confirmed `actor` is allowed to confirm this game -- this function's
    only job is the extra click, never a permission decision. The dialog
    captures `interaction.message` -- the *public* message the Confirm
    button lives on -- so its own buttons can edit that message directly
    rather than `edit_original_response`, which would only ever reach this
    new ephemeral dialog. See `_ConfirmAnywayView`.
    """
    content = _confirm_anyway_content(status)
    view = _ConfirmAnywayView(
        guild_id=guild_id, game_id=game_id, actor=actor, public_message=interaction.message
    )
    await interaction.response.send_message(
        content, view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
    )
    view.message = await interaction.original_response()


async def _resolve_public_message(
    client: object, public_message: discord.Message | None, game: GameWithParticipants
) -> discord.Message:
    """The public game message to edit after a delayed ("Confirm anyway") confirm.

    Prefers `public_message`, captured from the interaction that first
    showed the dialog -- ordinarily just the message the Confirm button
    lives on. If that reference is unavailable, it's re-resolved the same
    way `views/score_entry.py`'s `_refresh_public_message` does: from the
    game's own stored `channel_id`/`message_id`, never from anything
    client-supplied. Unlike that helper this is *not* best-effort:
    `confirm_game` has already durably committed by the time this runs, so
    a failure here should surface to the user (via the caller's error
    handler) rather than silently leaving a stale Confirm button on an
    already-confirmed game.
    """
    if public_message is not None:
        return public_message
    channel_id, message_id = game.game.channel_id, game.game.message_id
    if channel_id is None or message_id is None:
        raise RuntimeError(f"game {game.game.game_id} has no stored public message to update")
    channel = client.get_channel(channel_id)  # type: ignore[attr-defined]
    if channel is None:
        channel = await client.fetch_channel(channel_id)  # type: ignore[attr-defined]
    return await channel.fetch_message(message_id)


class _ConfirmAnywayButton(discord.ui.Button[Any]):
    def __init__(self, dialog: _ConfirmAnywayView) -> None:
        self.dialog = dialog
        super().__init__(style=discord.ButtonStyle.success, label="Confirm anyway")

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.dialog.confirm_anyway(interaction)


class _CancelConfirmButton(discord.ui.Button[Any]):
    def __init__(self, dialog: _ConfirmAnywayView) -> None:
        self.dialog = dialog
        super().__init__(style=discord.ButtonStyle.secondary, label="Cancel")

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.dialog.cancel(interaction)


class _ConfirmAnywayView(discord.ui.View):
    """The ephemeral, owner-locked second step for confirming a partial game.

    Scoped to the single click that created it: `owner_id` gates every
    button via `interaction_check`, and both buttons disable themselves
    (immediately on use, or via `on_timeout`) so this can't be used twice or
    left clickable indefinitely. `public_message` is the message this
    dialog's own success path must edit -- never `edit_original_response`
    here, which would only reach this ephemeral message, not the public one
    the persistent Confirm button lives on.
    """

    def __init__(
        self,
        *,
        guild_id: int,
        game_id: int,
        actor: Actor,
        public_message: discord.Message | None,
    ) -> None:
        super().__init__(timeout=_CONFIRM_ANYWAY_TIMEOUT_SECONDS)
        self.guild_id = guild_id
        self.game_id = game_id
        self.actor = actor
        self.owner_id = actor.user_id
        self.public_message = public_message
        self.message: discord.Message | None = None
        self.add_item(_ConfirmAnywayButton(self))
        self.add_item(_CancelConfirmButton(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            _DIALOG_WRONG_OWNER_TEXT,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    def _disable_all(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]

    async def on_timeout(self) -> None:
        self._disable_all()
        if self.message is not None:
            with suppress(discord.HTTPException):
                await self.message.edit(view=self)

    async def confirm_anyway(self, interaction: discord.Interaction) -> None:
        try:
            self._disable_all()
            await interaction.response.edit_message(view=self)
            pool = interaction.client.pool  # type: ignore[attr-defined]
            updated = await game_service.confirm_game(pool, self.guild_id, self.game_id, self.actor)
            embed = formatting.build_game_status_embed(updated)
            public_message = await _resolve_public_message(
                interaction.client, self.public_message, updated
            )
            await public_message.edit(
                embed=embed, view=None, allowed_mentions=discord.AllowedMentions.none()
            )
            await interaction.edit_original_response(
                content="Confirmed -- game saved with partial scores.",
                view=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            # The game is already durably confirmed above -- this is purely
            # a best-effort extra post from here on, exactly as it is on the
            # direct (no-dialog) Confirm path.
            await _post_leaderboard_after_confirm(interaction.client, self.guild_id)
        except Exception as exc:
            await handle_interaction_error(interaction, exc, command_name="game:confirm-anyway")
        finally:
            self.stop()

    async def cancel(self, interaction: discord.Interaction) -> None:
        try:
            self._disable_all()
            await interaction.response.edit_message(
                content="Cancelled -- the game stays pending.", view=self
            )
        except Exception as exc:
            await handle_interaction_error(
                interaction, exc, command_name="game:confirm-anyway-cancel"
            )
        finally:
            self.stop()


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
