"""Repository-layer exceptions.

Distinct from `catan_bot.domain.errors.DomainValidationError`: repositories
accept already-validated typed values (the M3b services layer validates
against `domain/`, and the DB CHECKs are the backstop), so an exception
raised here signals a *concurrency*/*invariant* condition surfaced by
Postgres, not a bad user input.
"""

from __future__ import annotations


class RepositoryError(Exception):
    """Base class for every exception this package raises."""


class ActiveSeasonExistsError(RepositoryError):
    """Raised when creating a season would violate `seasons_one_active_per_guild`.

    Maps `asyncpg.UniqueViolationError` on that partial unique index so
    callers get a typed, guild-scoped error instead of a raw Postgres
    exception.
    """

    def __init__(self, guild_id: int) -> None:
        super().__init__(f"Guild {guild_id} already has an active season.")
        self.guild_id = guild_id
