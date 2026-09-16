"""Permission and mentionability checks for channel-targeted commands."""

from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from catan_bot.cogs.channel_publish import (
    validate_notification_role,
    validate_publish_channel,
)
from catan_bot.domain.errors import DomainValidationError


class _RealThread(discord.Thread):
    """A real discord.py Thread type with deterministic test permissions."""

    def __new__(cls) -> _RealThread:
        return object.__new__(cls)

    def __init__(self) -> None:
        pass

    def permissions_for(self, _member: object) -> discord.Permissions:
        return self._permissions


def _interaction(guild: object, channel: object) -> SimpleNamespace:
    return SimpleNamespace(guild=guild, user=object())


def test_thread_publish_uses_send_messages_in_threads_without_parent_send() -> None:
    guild = SimpleNamespace(id=123, me=object())
    thread = _RealThread()
    thread.guild = guild
    thread._permissions = discord.Permissions(
        view_channel=True,
        send_messages=False,
        send_messages_in_threads=True,
        embed_links=True,
    )

    validate_publish_channel(_interaction(guild, thread), thread)


def test_non_mentionable_role_requires_mention_everyone_in_target() -> None:
    role = SimpleNamespace(id=44, mentionable=False, is_default=lambda: False)
    guild = SimpleNamespace(
        id=123,
        me=object(),
        get_role=lambda role_id: role if role_id == 44 else None,
    )
    channel = SimpleNamespace(
        permissions_for=lambda _member: discord.Permissions(mention_everyone=False)
    )

    with pytest.raises(DomainValidationError, match="Mention @everyone"):
        validate_notification_role(_interaction(guild, channel), channel, 44)


def test_non_mentionable_role_is_allowed_with_mention_everyone_in_target() -> None:
    role = SimpleNamespace(id=44, mentionable=False, is_default=lambda: False)
    guild = SimpleNamespace(
        id=123,
        me=object(),
        get_role=lambda role_id: role if role_id == 44 else None,
    )
    channel = SimpleNamespace(
        permissions_for=lambda _member: discord.Permissions(mention_everyone=True)
    )

    validate_notification_role(_interaction(guild, channel), channel, 44)
