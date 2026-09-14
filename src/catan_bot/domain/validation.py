"""Pure validation for user-supplied values headed for Postgres parameters.

Nothing here mutates the *content* of a value beyond whitespace trimming in
`clean_text` -- SQL safety comes entirely from parameterized queries in the
repository layer, and Discord markup escaping happens at display time (M4),
so text that merely looks like SQL, or contains Discord markup such as
mentions, links, or spoilers, is returned verbatim rather than altered here.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from catan_bot.domain.dates import MIN_GAME_DATE, ensure_aware, to_utc
from catan_bot.domain.errors import DomainValidationError

SEASON_NAME_MAX = 100
EVENT_TITLE_MAX = 100
EVENT_DESCRIPTION_MAX = 1000
EVENT_LOCATION_MAX = 200
VOID_REASON_MAX = 200

_BIGINT_MAX = 2**63 - 1

MIN_LOSERS = 1
MAX_LOSERS = 5
MIN_PLAYERS = 2
MAX_PLAYERS = 6

MAX_EVENT_LEAD_DAYS = 366
MAX_SEASON_SPAN_DAYS = 730

_MIN_GAMES_LOWER = 1
_MIN_GAMES_UPPER = 100


def validate_game_date(played_on: date, *, today: date) -> date:
    """A reported game's date: not in the future, not before Catan existed."""
    if played_on > today:
        raise DomainValidationError("The game date can't be in the future.")
    if played_on < MIN_GAME_DATE:
        raise DomainValidationError(
            f"The game date can't be earlier than {MIN_GAME_DATE.isoformat()}."
        )
    return played_on


def validate_game_in_season(played_on: date, *, starts_on: date, ends_on: date) -> None:
    """A game assigned to a season must land inside that season's (inclusive) window."""
    if not (starts_on <= played_on <= ends_on):
        raise DomainValidationError(
            "The game date must land inside the active season, which runs "
            f"from {starts_on.isoformat()} through {ends_on.isoformat()}."
        )


@dataclass(frozen=True, slots=True)
class ParticipantRef:
    user_id: int
    is_bot: bool


def _validate_player_id(user_id: int) -> None:
    # Exact-type check (not `isinstance`): a `bool`, an `IntEnum`, or any
    # other `int` subclass is rejected. Discord ids are always plain `int`,
    # and an exotic subclass could carry surprising `__eq__`/`__hash__`
    # behavior we don't want flowing into a set-based duplicate check.
    if type(user_id) is not int:
        raise DomainValidationError("A player id must be a whole number.")
    if user_id <= 0:
        raise DomainValidationError("A player id must be a positive number.")
    if user_id > _BIGINT_MAX:
        raise DomainValidationError("A player id is too large to store.")


def validate_participants(
    winner: ParticipantRef, losers: Sequence[ParticipantRef]
) -> tuple[int, tuple[int, ...]]:
    """Validate a game report's participants, returning `(winner_id, loser_ids)`.

    Rules: 1-5 losers (so 2-6 players total), no bots, and no player -- the
    winner included -- may appear more than once.
    """
    if not (MIN_LOSERS <= len(losers) <= MAX_LOSERS):
        raise DomainValidationError(f"A game needs {MIN_LOSERS} to {MAX_LOSERS} losers.")

    all_refs = (winner, *losers)
    if not (MIN_PLAYERS <= len(all_refs) <= MAX_PLAYERS):
        raise DomainValidationError(f"A game needs {MIN_PLAYERS} to {MAX_PLAYERS} players total.")
    if any(ref.is_bot for ref in all_refs):
        raise DomainValidationError("A bot can't be reported as a player.")

    # Validate every id's *type* before it ever reaches a `set()` -- an
    # unhashable id (e.g. a list slipped in upstream) would otherwise raise
    # a raw TypeError out of the duplicate check below instead of a clean
    # DomainValidationError.
    for ref in all_refs:
        _validate_player_id(ref.user_id)

    ids = [ref.user_id for ref in all_refs]
    if len(set(ids)) != len(ids):
        raise DomainValidationError("The same player can't be listed more than once.")

    return winner.user_id, tuple(ref.user_id for ref in losers)


def validate_event_start(starts_at: datetime, *, now: datetime) -> datetime:
    """An event's start: aware, strictly in the future, not absurdly far out.

    Both operands are normalized to UTC before comparing: two aware
    datetimes that share the same `tzinfo` object compare using their naive
    wall-clock fields only (a documented `datetime` rule), which silently
    ignores `fold` and can misorder an ambiguous local hour.
    """
    ensure_aware(starts_at, name="starts_at")
    ensure_aware(now, name="now")
    starts_at_utc = to_utc(starts_at)
    now_utc = to_utc(now)
    if starts_at_utc <= now_utc:
        raise DomainValidationError("The event start time must be in the future.")
    if starts_at_utc > now_utc + timedelta(days=MAX_EVENT_LEAD_DAYS):
        raise DomainValidationError(
            f"The event start time can't be more than {MAX_EVENT_LEAD_DAYS} days away."
        )
    return starts_at


def validate_season_window(starts_on: date, ends_on: date, *, today: date) -> None:
    """A season's start/end dates: sane order, not already over, not absurdly long."""
    if ends_on < starts_on:
        raise DomainValidationError("A season's end date can't be earlier than its start date.")
    if ends_on < today:
        raise DomainValidationError("A season's end date can't be in the past.")
    if starts_on < MIN_GAME_DATE:
        raise DomainValidationError(
            f"A season's start date can't be earlier than {MIN_GAME_DATE.isoformat()}."
        )
    if (ends_on - starts_on).days > MAX_SEASON_SPAN_DAYS:
        raise DomainValidationError(f"A season can't run longer than {MAX_SEASON_SPAN_DAYS} days.")
    if ends_on > today + timedelta(days=MAX_SEASON_SPAN_DAYS):
        raise DomainValidationError(
            f"A season's end date can't be more than {MAX_SEASON_SPAN_DAYS} days from today."
        )


def validate_min_games(n: int) -> int:
    """The minimum-games-to-be-eligible threshold: a whole number, 1-100."""
    if type(n) is not int:
        raise DomainValidationError("The minimum games setting must be a whole number.")
    if not (_MIN_GAMES_LOWER <= n <= _MIN_GAMES_UPPER):
        raise DomainValidationError(
            f"The minimum games setting must be {_MIN_GAMES_LOWER} to {_MIN_GAMES_UPPER}."
        )
    return n


# ---------------------------------------------------------------------------
# clean_text: strip -> cheap length gate -> character policy -> blank check
# ---------------------------------------------------------------------------

# Zero-width joiner/non-joiner: the only two Unicode "format" (Cf) category
# characters allowed through. Emoji ZWJ sequences (e.g. a family emoji built
# from several base emoji) and Persian/Indic/Arabic shaping both depend on
# them; every *other* Cf character (bidi overrides/isolates, LRM/RLM, the
# Arabic letter mark, zero-width space, word joiner, the BOM, tag
# characters, ...) is invisible-and-dangerous with no legitimate use here.
_ALLOWED_FORMAT_CHARS = frozenset({"\u200c", "\u200d"})

# Invisible "filler" characters: real, assigned characters (not Cf/Mn/Me),
# but purely visual placeholders with no meaning of their own. A field made
# up of nothing but these (plus whitespace/Cf/Mn/Me) is meaningfully blank.
_INVISIBLE_FILLERS = frozenset({"\u3164", "\u115f", "\u1160", "\uffa0", "\u2800"})

_LINE_SEPARATORS = frozenset({"\u2028", "\u2029"})

# More than this many *consecutive* combining marks (Mn/Me) is almost never
# real text (genuine stacked diacritics, e.g. Vietnamese, top out at 2-3) --
# it's a "zalgo" flood meant to break rendering or hide content.
_MAX_CONSECUTIVE_COMBINING_MARKS = 4

_MARK_CATEGORIES = frozenset({"Mn", "Me"})
_BLANK_SKIP_CATEGORIES = frozenset({"Cf", "Mn", "Me"})


def _is_noncharacter(code: int) -> bool:
    # U+FDD0-U+FDEF, plus U+xFFFE/U+xFFFF in every plane.
    return (0xFDD0 <= code <= 0xFDEF) or (code & 0xFFFE) == 0xFFFE


def _char_violates_policy(ch: str, *, allow_newlines: bool) -> bool:
    if allow_newlines and ch == "\n":
        return False
    code = ord(ch)
    if code <= 0x1F or code == 0x7F:  # C0 controls (incl. NUL/\t/\r) + DEL
        return True
    if 0x80 <= code <= 0x9F:  # C1 controls
        return True
    if ch in _LINE_SEPARATORS:
        return True
    if _is_noncharacter(code):
        return True
    category = unicodedata.category(ch)
    if category == "Cs":  # lone surrogate -- asyncpg can't encode this
        return True
    if category == "Co":  # private use -- meaningless outside a closed app
        return True
    return category == "Cf" and ch not in _ALLOWED_FORMAT_CHARS


def _reject_disallowed_characters(text: str, *, field: str, allow_newlines: bool) -> None:
    consecutive_marks = 0
    for ch in text:
        if _char_violates_policy(ch, allow_newlines=allow_newlines):
            raise DomainValidationError(f"{field} contains a character that isn't allowed.")
        if unicodedata.category(ch) in _MARK_CATEGORIES:
            consecutive_marks += 1
            if consecutive_marks > _MAX_CONSECUTIVE_COMBINING_MARKS:
                raise DomainValidationError(f"{field} has too many stacked accent marks in a row.")
        else:
            consecutive_marks = 0


def _is_blank_after_removing_invisibles(text: str) -> bool:
    for ch in text:
        if ch.isspace() or ch in _INVISIBLE_FILLERS:
            continue
        if unicodedata.category(ch) in _BLANK_SKIP_CATEGORIES:
            continue
        return False
    return True


def _blank_text_result(*, field: str, min_len: int) -> str | None:
    if min_len == 0:
        return None
    raise DomainValidationError(f"{field} is required.")


def clean_text(
    value: str | None,
    *,
    field: str,
    max_len: int,
    min_len: int = 0,
    allow_newlines: bool = False,
) -> str | None:
    """Trim and validate free text without altering its content.

    Order: strip -> a cheap length check on the raw stripped text (so an
    oversized input is rejected before any per-character work runs) -> the
    character policy (control/surrogate/private-use/noncharacter/format/
    zalgo checks) -> a "meaningfully blank" check that also treats pure
    whitespace, zero-width/format marks, bare combining marks, and
    invisible filler characters as empty.

    Content is never escaped or rewritten: SQL-looking text and Discord
    markup (mentions, links, spoilers) are returned verbatim. SQL safety
    comes entirely from parameterized queries in the repository layer;
    Discord markup escaping is a display-time concern, handled elsewhere.
    """
    stripped = "" if value is None else value.strip()
    if not stripped:
        return _blank_text_result(field=field, min_len=min_len)

    length = len(stripped)
    if length > max_len:
        raise DomainValidationError(f"{field} must be at most {max_len} characters long.")

    _reject_disallowed_characters(stripped, field=field, allow_newlines=allow_newlines)

    if _is_blank_after_removing_invisibles(stripped):
        return _blank_text_result(field=field, min_len=min_len)

    if length < min_len:
        raise DomainValidationError(f"{field} must be at least {min_len} characters long.")

    return stripped
