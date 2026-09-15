"""Build an `Actor` from a live Discord interaction.

The only place a `services.context.Actor` gets constructed from something
Discord-shaped: `discord.Member.roles` -> `frozenset[int]`,
`discord.Interaction.permissions.manage_guild` -> `bool`. Every admin check
itself still happens server-side inside the services layer
(`services.context.is_admin`/`require_admin`/`require_manage_guild`) -- this
module only translates the interaction into the typed value services expect.
"""

from __future__ import annotations

import discord

from catan_bot.services.context import Actor

_BIGINT_MAX = 2**63 - 1


def guild_id_from_interaction(interaction: discord.Interaction) -> int:
    """Return a validated guild id for a guild-only command or component."""
    guild_id = interaction.guild_id
    if type(guild_id) is not int or not (1 <= guild_id <= _BIGINT_MAX):
        raise ValueError("interaction requires a valid guild id")
    return guild_id


def actor_from_interaction(interaction: discord.Interaction) -> Actor:
    """The `Actor` behind `interaction`.

    Requires a guild interaction: `interaction.user` must be a
    `discord.Member` (a plain `discord.User`, as seen in a DM, has no
    roles and no guild-scoped permissions, so it can't produce an `Actor`).
    Every command is `@app_commands.guild_only()`, so this should always
    hold in practice; a violation is a caller bug, not a user input
    problem, hence the plain `ValueError`.
    """
    member = interaction.user
    if not isinstance(member, discord.Member):
        raise ValueError(
            "actor_from_interaction() requires a guild interaction (a discord.Member), "
            f"got {type(member).__name__}"
        )
    role_ids = frozenset(role.id for role in member.roles)
    has_manage_guild = bool(interaction.permissions.manage_guild)
    return Actor(user_id=member.id, has_manage_guild=has_manage_guild, role_ids=role_ids)
