"""The acting Discord user and server-side permission checks.

`Actor` is filled in by M4 from the Discord interaction (the member's id,
whether they hold Manage Server, and their role ids) -- nothing here talks
to Discord or the database. Every admin-only service call computes
permission from an `Actor` plus the guild's stored `GuildConfig`, never from
a client-supplied flag.
"""

from __future__ import annotations

from dataclasses import dataclass

from catan_bot.db.models import GuildConfig
from catan_bot.domain.dates import validate_timezone
from catan_bot.domain.errors import DomainValidationError
from catan_bot.services.errors import PermissionDeniedError, ServiceError

_MANAGE_GUILD_REQUIRED = "Only someone with the Manage Server permission can do that."
_ADMIN_REQUIRED = "Only a server admin can do that."
_TIMEZONE_INVALID = (
    "This server's timezone setting is invalid. Ask someone with Manage Server "
    "to fix it with /config timezone."
)


@dataclass(frozen=True, slots=True)
class Actor:
    """The Discord member behind a service call.

    Every field is checked in `__post_init__`: a caller passing the wrong
    *type* (a `str` id, a truthy-but-not-`bool` flag, a `role_ids` that
    isn't a `frozenset` of `int`) is a caller bug -- never user input that
    reached this far -- so this raises a plain `ValueError`, matching how
    `db/repositories/_params.py` treats the same class of mistake.
    """

    user_id: int
    has_manage_guild: bool
    role_ids: frozenset[int]

    def __post_init__(self) -> None:
        # Exact-type checks (not `isinstance`): `bool` is an `int` subclass,
        # so `isinstance(True, int)` is `True` and a stray `True` would
        # otherwise silently pass as `user_id=1`.
        if type(self.user_id) is not int or self.user_id < 1:
            raise ValueError(f"user_id must be a positive int, got {self.user_id!r}")
        if type(self.has_manage_guild) is not bool:
            raise ValueError(
                f"has_manage_guild must be a bool, got {self.has_manage_guild!r} "
                f"({type(self.has_manage_guild).__name__})"
            )
        if type(self.role_ids) is not frozenset or not all(
            type(role_id) is int for role_id in self.role_ids
        ):
            raise ValueError(f"role_ids must be a frozenset of int, got {self.role_ids!r}")


def is_admin(actor: Actor, config: GuildConfig) -> bool:
    """Whether `actor` may perform an admin-only operation in this guild.

    True for Manage Server, or for holding the guild's configured admin
    role. Reassigning *which* role that is requires Manage Server
    specifically (see `require_manage_guild`), so an admin-role holder who
    lacks Manage Server can never grant themselves -- or anyone else -- a
    new admin role.
    """
    return actor.has_manage_guild or (
        config.admin_role_id is not None and config.admin_role_id in actor.role_ids
    )


def require_manage_guild(actor: Actor) -> None:
    """Raise `PermissionDeniedError` unless `actor` holds Manage Server.

    Config commands (`/config ...`) require Manage Server specifically --
    the configured admin role is deliberately not enough for these.
    """
    if not actor.has_manage_guild:
        raise PermissionDeniedError(_MANAGE_GUILD_REQUIRED)


def require_admin(actor: Actor, config: GuildConfig) -> None:
    """Raise `PermissionDeniedError` unless `is_admin(actor, config)`."""
    if not is_admin(actor, config):
        raise PermissionDeniedError(_ADMIN_REQUIRED)


def require_valid_timezone(config: GuildConfig) -> str:
    """The guild's stored timezone, re-validated against the IANA database.

    `config_service.set_timezone` validates a timezone before it's ever
    stored, so this should always succeed -- but the `tzdata` package can
    change between deploys (a zone name can be renamed/removed), and the
    column can be edited directly outside the app, so every read site
    re-checks rather than trusting the stored value blindly. On failure
    this raises a fixed, friendly `ServiceError` (never
    `validate_timezone`'s generic "not a recognized timezone" text) so a
    user sees actionable guidance instead of a confusing date/time error.
    """
    try:
        return validate_timezone(config.timezone)
    except DomainValidationError as exc:
        raise ServiceError(_TIMEZONE_INVALID) from exc
