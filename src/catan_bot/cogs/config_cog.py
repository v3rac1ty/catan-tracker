"""`/config` -- announcement channel, timezone, admin role, and a read-back.

Most subcommands are thin: build an `Actor`, call one `config_service`
function with the guild id from the interaction, render the result with
`formatting`, respond. `/config show` additionally requires Manage Server
itself before reading the config back (`context.require_manage_guild`,
called directly here since `config_service.get_config` has no actor check
of its own -- this is the M4 requirement the M3b audit added).

`/config leaderboard` is the exception: every option is optional, and an
omitted one must leave that field's stored value alone rather than
resetting it to a default -- see `leaderboard_command`'s own comments for
how that "only pass what was actually supplied" behavior is built, and
`config_service.set_leaderboard_settings`'s docstring for how it is
threaded down to the repository layer. At least one option must still be
supplied, or there's nothing for the command to do.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import cache
from importlib.resources import files

import discord
from discord import app_commands
from discord.ext import commands

from catan_bot import formatting
from catan_bot.bot import CatanBot
from catan_bot.cogs.channel_publish import validate_publish_channel
from catan_bot.db.models import GuildConfig
from catan_bot.domain.dates import parse_time
from catan_bot.domain.errors import DomainValidationError
from catan_bot.permissions import actor_from_interaction, guild_id_from_interaction
from catan_bot.services import config_service
from catan_bot.services.context import require_manage_guild

_TZ_NAME_RE = re.compile(r"^[A-Z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$")
_EXCLUDED_TZ_PREFIXES = ("posix/", "right/")
_EXCLUDED_TZ_NAMES = frozenset({"Factory", "localtime", "posixrules"})
_AUTOCOMPLETE_LIMIT = 25
_TIMEZONE_HINT = "That isn't a recognized timezone name. Pick one from the list."

_LEADERBOARD_MODE_CHOICES = [
    app_commands.Choice(name="off", value="off"),
    app_commands.Choice(name="per-game", value="per_game"),
    app_commands.Choice(name="daily", value="daily"),
]
_LEADERBOARD_SCOPE_CHOICES = [
    app_commands.Choice(name="season", value="season"),
    app_commands.Choice(name="all-time", value="all_time"),
]
_NO_LEADERBOARD_CHANNEL = (
    "Choose a channel, or configure an announcement channel first with /config channel."
)
_NO_LEADERBOARD_OPTIONS = "Set at least one of mode, channel, scope, time, or clear_channel."


def validate_player_role(role: discord.Role, guild_id: int) -> int:
    """Reject broad or cross-guild roles before persisting a notification target."""
    if role.guild.id != guild_id:
        raise DomainValidationError("Choose a role from this server.")
    if role.is_default() or role.id == guild_id:
        raise DomainValidationError("The @everyone role cannot be used for event notifications.")
    return role.id


def is_canonical_timezone_name(name: str) -> bool:
    """Whether `name` is a real, human-facing IANA zone name.

    Excludes the POSIX-compatibility trees, the `Factory`/`localtime`/
    `posixrules` special entries, and anything that doesn't look like an
    ordinary `Area/Location` zone name (see DESIGN.md's M4 requirements).
    """
    if name in _EXCLUDED_TZ_NAMES:
        return False
    if any(name.startswith(prefix) for prefix in _EXCLUDED_TZ_PREFIXES):
        return False
    return bool(_TZ_NAME_RE.match(name))


def filter_timezones(
    names: Iterable[str], query: str, *, limit: int = _AUTOCOMPLETE_LIMIT
) -> list[str]:
    """Up to `limit` canonical zone names that case-insensitively contain `query`."""
    canonical = sorted(name for name in names if is_canonical_timezone_name(name))
    lowered = query.strip().lower()
    if not lowered:
        return canonical[:limit]
    return [name for name in canonical if lowered in name.lower()][:limit]


@cache
def bundled_timezones() -> frozenset[str]:
    """Canonical zones shipped by the installed `tzdata` package.

    Reading `zone1970.tab` avoids host-only aliases such as `Factory` that
    might validate locally but be absent from the slim production image.
    """
    table = files("tzdata.zoneinfo").joinpath("zone1970.tab").read_text(encoding="utf-8")
    names = {
        line.split("\t", 3)[2] for line in table.splitlines() if line and not line.startswith("#")
    }
    names.add("UTC")
    return frozenset(names)


def validate_bundled_timezone(name: str) -> str:
    """Validate manual input against the same bundled list as autocomplete."""
    stripped = name.strip()
    if stripped not in bundled_timezones():
        raise DomainValidationError(_TIMEZONE_HINT)
    return stripped


class ConfigCog(commands.Cog):
    # A class attribute (not a module-level variable) so discord.py's Cog
    # machinery picks it up: `CogMeta.__new__` only collects `app_commands
    # .Group`/`Command` values with `parent is None` that it finds in the
    # class namespace, and adds this Group (with every subcommand below,
    # each already parented to it) to the tree when the cog is loaded.
    config_group = app_commands.Group(
        name="config",
        description="View or change this server's Catan Tracker settings.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: CatanBot) -> None:
        self.bot = bot

    @config_group.command(name="channel", description="Set the channel for season announcements.")
    async def channel_command(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        config = await config_service.set_announce_channel(
            self.bot.pool, guild_id, actor, channel.id
        )
        embed = formatting.build_config_show_embed(config)
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @config_group.command(name="timezone", description="Set this server's timezone.")
    @app_commands.describe(tz="An IANA timezone name, e.g. America/Chicago")
    async def timezone_command(
        self, interaction: discord.Interaction, tz: app_commands.Range[str, 1, 64]
    ) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        timezone = validate_bundled_timezone(tz)
        await interaction.response.defer(ephemeral=True, thinking=True)
        config = await config_service.set_timezone(self.bot.pool, guild_id, actor, timezone)
        embed = formatting.build_config_show_embed(config)
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @timezone_command.autocomplete("tz")
    async def timezone_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        names = filter_timezones(bundled_timezones(), current)
        return [app_commands.Choice(name=name, value=name) for name in names]

    @config_group.command(name="admin-role", description="Set (or clear) the admin role.")
    @app_commands.describe(role="Omit to clear the configured admin role.")
    async def admin_role_command(
        self, interaction: discord.Interaction, role: discord.Role | None = None
    ) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        config = await config_service.set_admin_role(
            self.bot.pool, guild_id, actor, role.id if role is not None else None
        )
        embed = formatting.build_config_show_embed(config)
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @config_group.command(
        name="player-role", description="Set (or clear) the event notification role."
    )
    @app_commands.describe(role="Role to ping for event announcements. Omit to disable pings.")
    async def player_role_command(
        self, interaction: discord.Interaction, role: discord.Role | None = None
    ) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        role_id = validate_player_role(role, guild_id) if role is not None else None
        await interaction.response.defer(ephemeral=True, thinking=True)
        config = await config_service.set_player_role(self.bot.pool, guild_id, actor, role_id)
        embed = formatting.build_config_show_embed(config)
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @config_group.command(
        name="leaderboard", description="Configure the recurring leaderboard post."
    )
    @app_commands.describe(
        mode="off: never post. per-game: after every confirmed game. daily: once a day. "
        "Omitted leaves the current mode.",
        channel="Where to post it. Omitted leaves the current channel (or, if none has "
        "ever been set, falls back to the announcement channel).",
        scope="season or all-time standings. Omitted leaves the current scope.",
        time="Local time for the daily post (h:MMam/pm, e.g. 7:30pm, or 24-hour HH:MM). "
        "Omitted leaves the current time -- 10:00 PM the first time this is configured.",
        clear_channel="Clear the configured leaderboard channel instead of setting one.",
    )
    @app_commands.choices(mode=_LEADERBOARD_MODE_CHOICES, scope=_LEADERBOARD_SCOPE_CHOICES)
    async def leaderboard_command(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str] | None = None,
        channel: discord.TextChannel | None = None,
        scope: app_commands.Choice[str] | None = None,
        time: app_commands.Range[str, 1, 16] | None = None,
        clear_channel: bool = False,
    ) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        # Checked up front (matching /config show's own explicit check):
        # this command both may read the current config below and always
        # writes, so gating it before either happens avoids doing any work
        # -- including a Discord permission probe -- for someone who isn't
        # allowed to change this in the first place.
        require_manage_guild(actor)

        # Every option, `mode` included, is genuinely optional, matching
        # `/game update`'s "omitted means unchanged" convention -- but a
        # call that omits all five is never useful (it would just re-read
        # and re-write the row for nothing), so that's rejected up front
        # rather than falling through to a pointless write.
        if (
            mode is None
            and channel is None
            and scope is None
            and time is None
            and not clear_channel
        ):
            raise DomainValidationError(_NO_LEADERBOARD_OPTIONS)

        if channel is not None and clear_channel:
            raise DomainValidationError("Choose either a channel or clear_channel, not both.")

        # A kwarg this cog never adds to `settings_kwargs` is never passed
        # to `config_service.set_leaderboard_settings` at all, so that
        # field's already-stored value survives untouched (see that
        # function's docstring for how the omission is threaded down to the
        # repository layer's own partial-update sentinel).
        settings_kwargs: dict[str, object] = {}
        if mode is not None:
            settings_kwargs["mode"] = mode.value
        if scope is not None:
            settings_kwargs["scope"] = scope.value
        if time is not None:
            settings_kwargs["daily_time"] = parse_time(time)
        if channel is not None:
            validate_publish_channel(interaction, channel)
            settings_kwargs["channel_id"] = channel.id
        elif clear_channel:
            settings_kwargs["channel_id"] = None

        # The two guards below both care about "the mode this write will
        # actually leave in place" -- the newly supplied one if given,
        # otherwise whatever is already stored -- not just the `mode`
        # argument, which may now be absent entirely. Resolving that means
        # a read, but only when one of the guards can actually fire:
        # `clear_channel` was passed, or the channel is being left as-is
        # (in which case the same read also supplies the current channel
        # for the fallback below), matching this command's existing
        # "off never touches the database" and "an explicit channel never
        # reads the current config" behavior.
        if clear_channel or "channel_id" not in settings_kwargs:
            current: GuildConfig | None = None
            if mode is not None:
                effective_mode = mode.value
            else:
                current = await config_service.get_config(self.bot.pool, guild_id)
                effective_mode = current.leaderboard_mode

            if clear_channel and effective_mode != "off":
                # Mirrors the "no destination" check below: a mode that
                # posts somewhere can't be saved with no channel at all.
                raise DomainValidationError(_NO_LEADERBOARD_CHANNEL)

            if "channel_id" not in settings_kwargs and effective_mode != "off":
                # The channel is being left as-is. That's fine as long as
                # one is already on file; if a leaderboard channel has
                # never been configured, fall back to the announcement
                # channel (the same first-time convenience this command has
                # always offered) so this mode has somewhere to post.
                if current is None:
                    current = await config_service.get_config(self.bot.pool, guild_id)
                if current.leaderboard_channel_id is None:
                    fallback = None
                    if current.announce_channel_id is not None and interaction.guild is not None:
                        fallback = interaction.guild.get_channel(current.announce_channel_id)
                    if fallback is None:
                        raise DomainValidationError(_NO_LEADERBOARD_CHANNEL)
                    validate_publish_channel(interaction, fallback)
                    settings_kwargs["channel_id"] = fallback.id

        await interaction.response.defer(ephemeral=True, thinking=True)
        config = await config_service.set_leaderboard_settings(
            self.bot.pool, guild_id, actor, **settings_kwargs
        )
        embed = formatting.build_config_show_embed(config)
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )

    @config_group.command(name="show", description="Show this server's current settings.")
    async def show_command(self, interaction: discord.Interaction) -> None:
        actor = actor_from_interaction(interaction)
        guild_id = guild_id_from_interaction(interaction)
        require_manage_guild(actor)
        await interaction.response.defer(ephemeral=True, thinking=True)
        config = await config_service.get_config(self.bot.pool, guild_id)
        embed = formatting.build_config_show_embed(config)
        await interaction.edit_original_response(
            embed=embed, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot: CatanBot) -> None:
    await bot.add_cog(ConfigCog(bot))
