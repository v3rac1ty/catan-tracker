"""Pure, connection-free parameter validation for the repository layer.

By the time a call reaches `db/repositories/`, the M3b services layer (and
`domain/validation.py`) is expected to have already validated user input --
so an invalid value getting this far is a *caller bug*, not bad user input.
Left unchecked, asyncpg/Postgres handle a few of these surprisingly: `LIMIT
None` means "no limit" (every row), `LIMIT -1` raises a server-side error,
a `float`/`Decimal` id silently truncates (`71.9` reads guild 71), `True`
encodes as the integer `1`, and a naive `datetime` is interpreted in the
host's local timezone rather than being rejected. Every helper here raises a
plain `ValueError` instead, before any SQL is built or any `conn` method is
called, so a mistake fails loudly and cheaply at the call site.

No SQL lives in this module and it calls nothing on a connection -- see
CLAUDE.md and `tests/static/sql_guard.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime

# Postgres BIGINT range is a signed 64-bit integer; every id column in this
# schema is BIGINT.
_BIGINT_MAX = 2**63 - 1


def require_id(value: object, *, name: str) -> int:
    """A single Postgres `bigint` id: exactly `int` (never `bool`, `float`,
    `Decimal`, or `str`), in `[1, 2**63 - 1]`.

    Exact-type check (not `isinstance`): `bool` is an `int` subclass in
    Python, so `isinstance(True, int)` is `True` and `True` would silently
    encode as the integer `1` -- exactly the L2 audit finding this guards
    against. A `float`/`Decimal` would similarly encode via truncation
    (`71.9` reading guild 71) rather than raising.
    """
    if type(value) is not int:
        raise ValueError(f"{name} must be an int, got {value!r} ({type(value).__name__})")
    if not (1 <= value <= _BIGINT_MAX):
        raise ValueError(f"{name} must be between 1 and 2**63 - 1, got {value!r}")
    return value


def require_optional_id(value: object, *, name: str) -> int | None:
    """`None`, or the `require_id` rule."""
    if value is None:
        return None
    return require_id(value, name=name)


def require_limit(value: object, *, max_limit: int = 100) -> int:
    """A `LIMIT` value: exactly `int`, in `[1, max_limit]`.

    `LIMIT None` means "unbounded" to Postgres (every matching row) and
    `LIMIT -1` raises a raw server-side syntax/range error -- both of which
    this rejects up front with a clear caller-facing message instead.
    """
    if type(value) is not int:
        raise ValueError(f"limit must be an int, got {value!r} ({type(value).__name__})")
    if not (1 <= value <= max_limit):
        raise ValueError(f"limit must be between 1 and {max_limit}, got {value!r}")
    return value


def require_int(value: object, *, name: str, min_value: int, max_value: int) -> int:
    """A plain `int` in `[min_value, max_value]` (e.g. `min_games`, a rank, a
    game count).

    Exact-type check (not `isinstance`), for the same reason as `require_id`:
    `bool` is an `int` subclass (`True` would silently encode as `1`), an
    `IntEnum` member is also an `int` subclass, and a `float`/`Decimal`
    would truncate (`2.9` reading as `2`) rather than raising.
    """
    if type(value) is not int:
        raise ValueError(f"{name} must be an int, got {value!r} ({type(value).__name__})")
    if not (min_value <= value <= max_value):
        raise ValueError(f"{name} must be between {min_value} and {max_value}, got {value!r}")
    return value


def require_aware(value: object, *, name: str) -> datetime:
    """A timezone-aware `datetime`, returned normalized to UTC.

    A naive `datetime` sent to Postgres over the wire is interpreted as
    host-local time by the `timestamptz` codec, not UTC or any other fixed
    zone -- silently shifting every comparison against it.

    Checks `value.tzinfo is not None` *and* `value.utcoffset() is not None`
    (not just the latter): a naive `datetime` subclass that overrides
    `utcoffset()` to return a non-`None` value while leaving `tzinfo` unset
    would otherwise sail through and get stored as host-local time exactly
    like a plain naive `datetime` would -- the N2 audit finding this
    guards against. `isinstance(value, datetime)` (not just any object with
    a `utcoffset` method) also rejects a bare `date`, which has no such
    method at all.

    Returns `value.astimezone(UTC)` rather than `value` itself, and
    every call site must use that returned value -- both to normalize the
    stored instant to a single fixed zone, and because calling
    `.astimezone()` here is the *only* time this value's `tzinfo` is
    consulted: a `tzinfo` whose `utcoffset()` returns a different result on
    each call (buggy, but not impossible) would otherwise silently encode a
    different offset at the SQL call site than whatever was checked here.
    """
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime, got {value!r}")
    return value.astimezone(UTC)


def require_str_sequence(value: object, *, name: str) -> list[str]:
    """A `list`/`tuple` of `str` (e.g. RSVP `response` filters).

    Rejects a bare `str` too: a `str` is itself iterable-of-`str`
    (one-character strings), so an unguarded call site passing a single
    response like `"going"` instead of `["going"]` would otherwise silently
    iterate its characters.
    """
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{name} must be a list or tuple of str, got {value!r}")
    return list(value)
