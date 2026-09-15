"""Service-layer exceptions safe to surface to Discord users.

Distinct from `catan_bot.domain.errors.DomainValidationError` (a bad user
input caught by domain validation) and from repository exceptions (a
concurrency/invariant condition surfaced by Postgres): a `ServiceError` is
what a service raises when a repository call *succeeds* at the SQL level
but the outcome (a `TransitionResult`, a `None`, an `ActiveSeasonExistsError`)
means the requested operation can't go through for a reason a user should
be told about -- wrong permissions, a missing/wrong-state resource, or a
conflict with the resource's current state.

`user_message` on every one of these (and on `DomainValidationError`, which
services let propagate unchanged) is always a fixed, hardcoded string that a
cog can show back to the user verbatim -- never raw asyncpg exception text,
a DETAIL message, or unescaped user input. See CLAUDE.md and the message
catalogue test in `tests/integration/test_service_hygiene.py`.
"""

from __future__ import annotations


class ServiceError(Exception):
    """Base class for every user-facing failure the services layer raises."""

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class PermissionDeniedError(ServiceError):
    """The actor isn't allowed to perform this operation."""


class NotFoundError(ServiceError):
    """The requested resource doesn't exist (or isn't visible in this guild)."""


class ConflictError(ServiceError):
    """The operation conflicts with the resource's current state."""
