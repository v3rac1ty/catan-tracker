"""Unit tests for `catan_bot.services.context`. Pure logic: no Docker, no clock."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from catan_bot.db.models import GuildConfig
from catan_bot.services.context import (
    Actor,
    is_admin,
    require_admin,
    require_manage_guild,
    require_valid_timezone,
)
from catan_bot.services.errors import PermissionDeniedError, ServiceError

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _config(admin_role_id: int | None, *, timezone: str = "UTC") -> GuildConfig:
    return GuildConfig(
        guild_id=1,
        timezone=timezone,
        announce_channel_id=None,
        admin_role_id=admin_role_id,
        default_min_games=2,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_manage_guild_alone_is_admin() -> None:
    actor = Actor(user_id=1, has_manage_guild=True, role_ids=frozenset())
    assert is_admin(actor, _config(admin_role_id=None)) is True


def test_admin_role_alone_is_admin() -> None:
    actor = Actor(user_id=1, has_manage_guild=False, role_ids=frozenset({42}))
    assert is_admin(actor, _config(admin_role_id=42)) is True


def test_neither_manage_guild_nor_admin_role_is_not_admin() -> None:
    actor = Actor(user_id=1, has_manage_guild=False, role_ids=frozenset({7}))
    assert is_admin(actor, _config(admin_role_id=42)) is False


def test_no_admin_role_configured_means_only_manage_guild_qualifies() -> None:
    actor = Actor(user_id=1, has_manage_guild=False, role_ids=frozenset({1, 2, 3}))
    assert is_admin(actor, _config(admin_role_id=None)) is False


def test_role_ids_not_matching_configured_admin_role_is_not_admin() -> None:
    actor = Actor(user_id=1, has_manage_guild=False, role_ids=frozenset({1, 2, 3}))
    assert is_admin(actor, _config(admin_role_id=999)) is False


def test_require_manage_guild_passes_with_manage_guild() -> None:
    actor = Actor(user_id=1, has_manage_guild=True, role_ids=frozenset())
    require_manage_guild(actor)  # must not raise


def test_require_manage_guild_rejects_admin_role_holder_without_manage_guild() -> None:
    """The admin role alone is not enough for config commands -- an admin-role
    holder without Manage Server can't reassign the admin role to someone else."""
    actor = Actor(user_id=1, has_manage_guild=False, role_ids=frozenset({42}))
    with pytest.raises(PermissionDeniedError) as exc_info:
        require_manage_guild(actor)
    assert exc_info.value.user_message == exc_info.value.args[0]
    assert "Manage Server" in exc_info.value.user_message


def test_require_admin_passes_for_admin_role_holder() -> None:
    actor = Actor(user_id=1, has_manage_guild=False, role_ids=frozenset({42}))
    require_admin(actor, _config(admin_role_id=42))  # must not raise


def test_require_admin_rejects_non_admin() -> None:
    actor = Actor(user_id=1, has_manage_guild=False, role_ids=frozenset())
    with pytest.raises(PermissionDeniedError) as exc_info:
        require_admin(actor, _config(admin_role_id=None))
    assert exc_info.value.user_message


def test_actor_is_frozen_and_hashable() -> None:
    actor = Actor(user_id=1, has_manage_guild=True, role_ids=frozenset({1}))
    with pytest.raises(AttributeError):
        actor.user_id = 2  # type: ignore[misc]
    assert hash(actor) is not None


# ---------------------------------------------------------------------------
# I4: `Actor.__post_init__` type checks -- these are caller bugs, so they
# raise a plain `ValueError`, matching `db/repositories/_params.py`.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_user_id", [0, -1, "1", True, 1.5, None], ids=lambda v: f"{v!r}")
def test_actor_rejects_bad_user_id(bad_user_id: object) -> None:
    # `True` is deliberately included: `bool` is an `int` subclass, so
    # `isinstance(True, int)` would silently accept it as `user_id=1`.
    with pytest.raises(ValueError, match="user_id"):
        Actor(user_id=bad_user_id, has_manage_guild=True, role_ids=frozenset())  # type: ignore[arg-type]


def test_actor_accepts_minimum_valid_user_id() -> None:
    actor = Actor(user_id=1, has_manage_guild=True, role_ids=frozenset())
    assert actor.user_id == 1


@pytest.mark.parametrize("bad_flag", ["false", 0, 1, None, "true"], ids=lambda v: f"{v!r}")
def test_actor_rejects_non_bool_has_manage_guild(bad_flag: object) -> None:
    with pytest.raises(ValueError, match="has_manage_guild"):
        Actor(user_id=1, has_manage_guild=bad_flag, role_ids=frozenset())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad_role_ids",
    [frozenset({"42"}), frozenset({1.5}), {1, 2}, [1, 2], (1, 2), frozenset({True})],
    ids=lambda v: f"{v!r}",
)
def test_actor_rejects_bad_role_ids(bad_role_ids: object) -> None:
    # A plain `set`/`list`/`tuple` of ints is rejected too: `role_ids` must
    # specifically be a `frozenset` (immutable, matching the frozen
    # dataclass it lives on), and `True` inside the set is rejected the
    # same way a `True` `user_id` is.
    with pytest.raises(ValueError, match="role_ids"):
        Actor(user_id=1, has_manage_guild=True, role_ids=bad_role_ids)  # type: ignore[arg-type]


def test_actor_accepts_empty_and_populated_frozenset_role_ids() -> None:
    assert Actor(user_id=1, has_manage_guild=True, role_ids=frozenset()).role_ids == frozenset()
    populated = frozenset({1, 2, 3})
    assert Actor(user_id=1, has_manage_guild=True, role_ids=populated).role_ids == populated


# ---------------------------------------------------------------------------
# I1: `require_valid_timezone` -- a friendly, fixed `ServiceError` instead
# of `validate_timezone`'s generic message.
# ---------------------------------------------------------------------------


def test_require_valid_timezone_returns_stripped_valid_zone() -> None:
    assert require_valid_timezone(_config(None, timezone="America/Chicago")) == "America/Chicago"


def test_require_valid_timezone_raises_friendly_service_error_for_invalid_zone() -> None:
    with pytest.raises(ServiceError) as exc_info:
        require_valid_timezone(_config(None, timezone="Mars/Base"))
    assert exc_info.value.user_message == (
        "This server's timezone setting is invalid. Ask someone with Manage Server "
        "to fix it with /config timezone."
    )
    # Never the generic domain message.
    assert "recognized" not in exc_info.value.user_message
