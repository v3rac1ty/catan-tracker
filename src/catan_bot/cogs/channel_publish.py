"""Validation shared by commands that publish outside an interaction reply."""

from __future__ import annotations

import discord

from catan_bot.domain.errors import DomainValidationError


def validate_notification_role(
    interaction: discord.Interaction,
    channel: discord.abc.GuildChannel | discord.Thread,
    role_id: int | None,
) -> None:
    """Ensure a configured role can actually be mentioned in ``channel``.

    Discord only delivers a role mention when the role exists in the target
    guild and is mentionable, unless the bot has the target channel's
    ``Mention @everyone, @here, and All Roles`` permission.  This check is
    intentionally destination-aware and never broadens the allowed-mentions
    list to users or everyone.
    """
    if role_id is None:
        return
    guild = interaction.guild
    get_role = getattr(guild, "get_role", None) if guild is not None else None
    role = get_role(role_id) if callable(get_role) else None
    if role is None or getattr(role, "is_default", lambda: False)():
        raise DomainValidationError(
            "The configured event player role no longer exists in this server. "
            "Clear it with /config player-role and choose a valid role."
        )
    if getattr(role, "mentionable", False):
        return
    bot_member = getattr(guild, "me", None)
    permissions_for = getattr(channel, "permissions_for", None)
    permissions = permissions_for(bot_member) if callable(permissions_for) else None
    if not getattr(permissions, "mention_everyone", False):
        raise DomainValidationError(
            "The configured event player role is not mentionable, and I lack "
            "Mention @everyone, @here, and All Roles in the target channel."
        )


def validate_publish_channel(
    interaction: discord.Interaction, channel: discord.abc.GuildChannel | discord.Thread
) -> None:
    """Require a same-guild channel both the caller and bot can use.

    Slash-command option conversion normally guarantees a TextChannel belongs
    to the interaction guild.  These checks are deliberately repeated before
    any service mutation so stale/crafted interaction data cannot create an
    event that can never be announced.
    """
    guild = interaction.guild
    if guild is None or channel.guild.id != guild.id:
        raise DomainValidationError("Choose a channel in this server.")
    bot_member = guild.me
    if bot_member is None:
        raise DomainValidationError("I could not verify my channel permissions.")
    for subject in (interaction.user, bot_member):
        permissions = channel.permissions_for(subject)
        # Discord uses the thread-specific Send Messages in Threads
        # permission for threads.  A member may be allowed to post in a
        # thread even when Send Messages is false on the parent channel, so
        # do not require both permissions there.
        if isinstance(channel, discord.Thread):
            can_send = permissions.send_messages_in_threads
        else:
            can_send = permissions.send_messages
        if not (permissions.view_channel and can_send and permissions.embed_links):
            raise DomainValidationError(
                "Both you and I need View Channel, Send Messages, and Embed Links there."
            )
