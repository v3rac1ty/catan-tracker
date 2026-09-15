"""Unit tests for `catan_bot.db.repositories._params` and its wiring.

No Docker/Postgres needed: `_params` is pure (no SQL, touches no
connection), and the "wiring" tests below prove every repository function
validates its id/limit/datetime/sequence parameters *before* touching
`conn` at all, using a connection stand-in that raises on any attribute
access.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from enum import IntEnum
from typing import Any

import pytest

from catan_bot.db.models import SeasonResultRow
from catan_bot.db.repositories import _params, events, games, guilds, players, seasons

# ---------------------------------------------------------------------------
# Representative bad inputs, shared across every id/limit/aware-datetime
# helper (see CLAUDE.md item 7 / the M3a audit's L2/L6 findings).
# ---------------------------------------------------------------------------

_NAIVE_DATETIME = datetime(2026, 1, 1)  # no tzinfo
_AWARE_DATETIME = datetime(2026, 1, 1, tzinfo=UTC)

_BAD_ID_INPUTS: list[Any] = [
    None,
    True,
    False,
    1.5,
    Decimal("7"),
    0,
    -1,
    2**63,
    "5",
    _NAIVE_DATETIME,
]

_BAD_LIMIT_INPUTS: list[Any] = [
    None,
    True,
    1.5,
    Decimal("7"),
    0,
    -1,
    2**63,
    "5",
    _NAIVE_DATETIME,
]

_BAD_AWARE_INPUTS: list[Any] = [
    None,
    True,
    1.5,
    Decimal("7"),
    0,
    -1,
    2**63,
    "5",
    _NAIVE_DATETIME,
]


# ---------------------------------------------------------------------------
# require_id / require_optional_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", _BAD_ID_INPUTS)
def test_require_id_rejects_bad_inputs(bad: Any) -> None:
    with pytest.raises(ValueError, match="guild_id"):
        _params.require_id(bad, name="guild_id")


@pytest.mark.parametrize("good", [1, 2, 2**63 - 1, 900_001])
def test_require_id_accepts_good_inputs(good: int) -> None:
    assert _params.require_id(good, name="guild_id") == good


@pytest.mark.parametrize("bad", [v for v in _BAD_ID_INPUTS if v is not None])
def test_require_optional_id_rejects_bad_inputs(bad: Any) -> None:
    # `None` is the one input `require_optional_id` accepts (it means "no
    # value provided") -- excluded here since that's the whole point of the
    # "optional" variant, covered separately by `test_..._accepts_none`.
    with pytest.raises(ValueError, match="channel_id"):
        _params.require_optional_id(bad, name="channel_id")


def test_require_optional_id_accepts_none() -> None:
    assert _params.require_optional_id(None, name="channel_id") is None


def test_require_optional_id_accepts_valid_id() -> None:
    assert _params.require_optional_id(42, name="channel_id") == 42


# ---------------------------------------------------------------------------
# require_limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", _BAD_LIMIT_INPUTS)
def test_require_limit_rejects_bad_inputs(bad: Any) -> None:
    with pytest.raises(ValueError, match="limit"):
        _params.require_limit(bad)


@pytest.mark.parametrize("good", [1, 50, 100])
def test_require_limit_accepts_good_inputs(good: int) -> None:
    assert _params.require_limit(good) == good


def test_require_limit_rejects_value_above_custom_max() -> None:
    with pytest.raises(ValueError, match="limit"):
        _params.require_limit(11, max_limit=10)


def test_require_limit_accepts_value_at_custom_max() -> None:
    assert _params.require_limit(10, max_limit=10) == 10


# ---------------------------------------------------------------------------
# require_aware
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", _BAD_AWARE_INPUTS)
def test_require_aware_rejects_bad_inputs(bad: Any) -> None:
    with pytest.raises(ValueError, match="now"):
        _params.require_aware(bad, name="now")


def test_require_aware_accepts_aware_datetime() -> None:
    assert _params.require_aware(_AWARE_DATETIME, name="now") == _AWARE_DATETIME


def test_require_aware_rejects_naive_datetime_specifically() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _params.require_aware(_NAIVE_DATETIME, name="now")


def test_require_aware_rejects_bare_date() -> None:
    """N2: a bare `date` (no `datetime` subclass, no `utcoffset` method at
    all) must be rejected just like a naive `datetime`."""
    with pytest.raises(ValueError, match="now"):
        _params.require_aware(date(2026, 1, 1), name="now")


class _NaiveWithFakeUtcoffset(datetime):
    """A naive `datetime` subclass (`tzinfo` left unset) that overrides
    `utcoffset()` directly to lie about being aware -- the N2 audit finding:
    a check that only inspects `utcoffset()` (and not also `tzinfo`) would
    let this sail through and get stored as host-local time."""

    def utcoffset(self) -> timedelta:
        return timedelta(hours=5)


def test_require_aware_rejects_naive_subclass_with_overridden_utcoffset() -> None:
    value = _NaiveWithFakeUtcoffset(2026, 1, 1)
    assert value.tzinfo is None
    assert value.utcoffset() is not None  # the lie this guards against
    with pytest.raises(ValueError, match="timezone-aware"):
        _params.require_aware(value, name="now")


def test_require_aware_converts_non_utc_aware_datetime_to_utc_same_instant() -> None:
    plus_five = timezone(timedelta(hours=5))
    value = datetime(2026, 1, 1, 5, 0, tzinfo=plus_five)  # same instant as 2026-01-01T00:00Z

    result = _params.require_aware(value, name="now")

    assert result.tzinfo == UTC
    assert result == value
    assert result == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


class _FlipFloppingTZ(tzinfo):
    """A `tzinfo` whose `utcoffset()` returns a different value on each
    call, simulating a buggy/unstable implementation. `require_aware` must
    normalize with a single `.astimezone()` call and return that one
    snapshot, so callers downstream never see -- or depend on -- the
    instability (N2 audit finding)."""

    def __init__(self) -> None:
        self.calls = 0

    def utcoffset(self, dt: datetime | None) -> timedelta:
        self.calls += 1
        return timedelta(hours=self.calls)

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str:
        return "FlipFlop"


def test_require_aware_normalizes_flip_flopping_tzinfo_to_utc_once() -> None:
    value = datetime(2026, 1, 1, tzinfo=_FlipFloppingTZ())

    result = _params.require_aware(value, name="now")

    assert result.tzinfo == UTC
    assert result.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# require_int
# ---------------------------------------------------------------------------


class _RankEnum(IntEnum):
    ONE = 1


@pytest.mark.parametrize(
    "bad",
    [True, False, 2.9, Decimal("7"), "5", None, _RankEnum.ONE],
)
def test_require_int_rejects_bad_inputs(bad: Any) -> None:
    with pytest.raises(ValueError, match="min_games"):
        _params.require_int(bad, name="min_games", min_value=1, max_value=100)


def test_require_int_rejects_below_min() -> None:
    with pytest.raises(ValueError, match="min_games"):
        _params.require_int(0, name="min_games", min_value=1, max_value=100)


def test_require_int_rejects_above_max() -> None:
    with pytest.raises(ValueError, match="min_games"):
        _params.require_int(101, name="min_games", min_value=1, max_value=100)


@pytest.mark.parametrize("good", [1, 50, 100])
def test_require_int_accepts_good_inputs(good: int) -> None:
    assert _params.require_int(good, name="min_games", min_value=1, max_value=100) == good


# ---------------------------------------------------------------------------
# require_str_sequence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [None, True, 1.5, 0, "going", [1, 2], (1, "going"), {"going"}, {"going": 1}],
)
def test_require_str_sequence_rejects_bad_inputs(bad: Any) -> None:
    with pytest.raises(ValueError, match="responses"):
        _params.require_str_sequence(bad, name="responses")


def test_require_str_sequence_accepts_list_of_str() -> None:
    assert _params.require_str_sequence(["going", "maybe"], name="responses") == [
        "going",
        "maybe",
    ]


def test_require_str_sequence_accepts_tuple_of_str() -> None:
    assert _params.require_str_sequence(("going",), name="responses") == ["going"]


def test_require_str_sequence_accepts_empty_list() -> None:
    assert _params.require_str_sequence([], name="responses") == []


# ---------------------------------------------------------------------------
# Wiring: every repository function validates before touching `conn`.
#
# `_ExplodingConnection` stands in for `asyncpg.Connection`: any attribute
# access (`.execute`, `.fetchrow`, `.fetch`, `.transaction`, ...) raises
# immediately, so if a repository function reaches a `conn.<anything>` call
# before its `ValueError` fires, the test fails on the *wrong* exception
# type/message instead of the expected `ValueError`.
# ---------------------------------------------------------------------------


class _ExplodingConnection:
    def __getattr__(self, attr_name: str) -> Any:
        raise AssertionError(
            f"repository function touched conn.{attr_name} before validating its parameters"
        )


@pytest.fixture
def conn() -> _ExplodingConnection:
    return _ExplodingConnection()


PLAYED_ON = date(2026, 3, 1)
STARTS_ON = date(2026, 1, 1)
ENDS_ON = date(2026, 12, 31)


# --- guilds.py ---


async def test_ensure_guild_validates_guild_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="guild_id"):
        await guilds.ensure_guild(conn, 0)


async def test_get_guild_validates_guild_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="guild_id"):
        await guilds.get_guild(conn, "bad")


async def test_set_timezone_validates_guild_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="guild_id"):
        await guilds.set_timezone(conn, -1, "UTC")


async def test_set_announce_channel_validates_channel_id_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match="channel_id"):
        await guilds.set_announce_channel(conn, 1, True)


async def test_set_admin_role_validates_role_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="role_id"):
        await guilds.set_admin_role(conn, 1, 1.5)


async def test_set_default_min_games_validates_guild_id_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match="guild_id"):
        await guilds.set_default_min_games(conn, 2**63, 5)


async def test_set_default_min_games_validates_min_games_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match="min_games"):
        await guilds.set_default_min_games(conn, 1, True)


# --- players.py ---


async def test_ensure_players_validates_guild_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="guild_id"):
        await players.ensure_players(conn, 0, [1, 2])


async def test_ensure_players_validates_each_user_id_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match=r"user_ids\[1\]"):
        await players.ensure_players(conn, 1, [1, "bad"])


# --- games.py ---


async def test_create_game_validates_guild_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="guild_id"):
        await games.create_game(conn, 0, None, PLAYED_ON, 1, 1, [2])


async def test_create_game_validates_season_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="season_id"):
        await games.create_game(conn, 1, 1.5, PLAYED_ON, 1, 1, [2])


async def test_create_game_validates_loser_ids_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match=r"loser_ids\[0\]"):
        await games.create_game(conn, 1, None, PLAYED_ON, 1, 1, [True])


async def test_set_game_message_validates_game_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="game_id"):
        await games.set_game_message(conn, 1, 0, 1, 1)


async def test_get_game_validates_game_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="game_id"):
        await games.get_game(conn, 1, "x")


async def test_confirm_game_validates_user_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="user_id"):
        await games.confirm_game(conn, 1, 1, True)


async def test_reject_game_validates_user_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="user_id"):
        await games.reject_game(conn, 1, 1, -1)


async def test_void_game_validates_admin_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="admin_id"):
        await games.void_game(conn, 1, 1, 0, "reason")


async def test_list_recent_games_validates_limit_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="limit"):
        await games.list_recent_games(conn, 1, None)  # type: ignore[arg-type]


async def test_list_recent_games_for_player_validates_limit_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match="limit"):
        await games.list_recent_games_for_player(conn, 1, 1, -1)


# --- seasons.py ---


async def test_get_active_season_validates_guild_id_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match="guild_id"):
        await seasons.get_active_season(conn, 0)


async def test_create_season_validates_guild_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="guild_id"):
        await seasons.create_season(conn, 0, "S", STARTS_ON, ENDS_ON, _AWARE_DATETIME, 2, 1)


async def test_create_season_validates_ends_at_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="ends_at"):
        await seasons.create_season(conn, 1, "S", STARTS_ON, ENDS_ON, _NAIVE_DATETIME, 2, 1)


async def test_create_season_validates_min_games_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="min_games"):
        await seasons.create_season(conn, 1, "S", STARTS_ON, ENDS_ON, _AWARE_DATETIME, 2.9, 1)


async def test_set_active_min_games_validates_guild_id_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match="guild_id"):
        await seasons.set_active_min_games(conn, 0, 2)


async def test_set_active_min_games_validates_n_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="n"):
        await seasons.set_active_min_games(conn, 1, 0)


async def test_set_active_end_validates_ends_at_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="ends_at"):
        await seasons.set_active_end(conn, 1, ENDS_ON, _NAIVE_DATETIME)


async def test_cancel_active_season_validates_guild_id_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match="guild_id"):
        await seasons.cancel_active_season(conn, "x")


async def test_list_seasons_validates_limit_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="limit"):
        await seasons.list_seasons(conn, 1, True)


async def test_get_season_validates_season_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="season_id"):
        await seasons.get_season(conn, 1, Decimal("7"))


async def test_season_player_stats_validates_season_id_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match="season_id"):
        await seasons.season_player_stats(conn, 1, 0)


async def test_all_time_player_stats_validates_guild_id_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match="guild_id"):
        await seasons.all_time_player_stats(conn, -1)


async def test_player_stats_validates_user_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="user_id"):
        await seasons.player_stats(conn, 1, 0)


async def test_lock_due_seasons_validates_now_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="now"):
        await seasons.lock_due_seasons(conn, _NAIVE_DATETIME)


async def test_lock_active_season_validates_guild_id_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match="guild_id"):
        await seasons.lock_active_season(conn, 0)


async def test_lock_next_due_season_validates_now_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="now"):
        await seasons.lock_next_due_season(conn, _NAIVE_DATETIME, [])


async def test_lock_next_due_season_validates_exclude_ids_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match=r"exclude_season_ids\[1\]"):
        await seasons.lock_next_due_season(conn, _AWARE_DATETIME, [1, "bad"])


async def test_lock_next_due_season_rejects_bool_exclude_id_before_conn(
    conn: _ExplodingConnection,
) -> None:
    # `bool` is an `int` subclass -- a stray `True` must not silently
    # encode as season_id 1.
    with pytest.raises(ValueError, match=r"exclude_season_ids\[0\]"):
        await seasons.lock_next_due_season(conn, _AWARE_DATETIME, [True])


async def test_lock_next_due_season_empty_exclude_list_still_validates_now_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match="now"):
        await seasons.lock_next_due_season(conn, _NAIVE_DATETIME, [])


async def test_complete_season_validates_guild_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="guild_id"):
        await seasons.complete_season(conn, 0, 1, [])


# N3: `complete_season` must validate every result row -- field by field --
# before touching `conn` at all, using the same `_ExplodingConnection` as
# every other wiring test above.


async def test_complete_season_validates_result_user_id_before_conn(
    conn: _ExplodingConnection,
) -> None:
    bad_row = SeasonResultRow(user_id="bad", rank=1, games=1, wins=0, eligible=True, outcome=None)
    with pytest.raises(ValueError, match=r"results\[0\]\.user_id"):
        await seasons.complete_season(conn, 1, 1, [bad_row])


async def test_complete_season_validates_result_rank_before_conn(
    conn: _ExplodingConnection,
) -> None:
    bad_row = SeasonResultRow(user_id=1, rank=0, games=1, wins=0, eligible=True, outcome=None)
    with pytest.raises(ValueError, match=r"results\[0\]\.rank"):
        await seasons.complete_season(conn, 1, 1, [bad_row])


async def test_complete_season_validates_result_games_before_conn(
    conn: _ExplodingConnection,
) -> None:
    bad_row = SeasonResultRow(user_id=1, rank=1, games=-1, wins=0, eligible=True, outcome=None)
    with pytest.raises(ValueError, match=r"results\[0\]\.games"):
        await seasons.complete_season(conn, 1, 1, [bad_row])


async def test_complete_season_validates_result_wins_before_conn(
    conn: _ExplodingConnection,
) -> None:
    bad_row = SeasonResultRow(user_id=1, rank=1, games=2, wins=3, eligible=True, outcome=None)
    with pytest.raises(ValueError, match=r"results\[0\]\.wins"):
        await seasons.complete_season(conn, 1, 1, [bad_row])


async def test_complete_season_validates_result_eligible_before_conn(
    conn: _ExplodingConnection,
) -> None:
    bad_row = SeasonResultRow(user_id=1, rank=1, games=2, wins=1, eligible=1, outcome=None)
    with pytest.raises(ValueError, match=r"results\[0\]\.eligible"):
        await seasons.complete_season(conn, 1, 1, [bad_row])


async def test_complete_season_validates_result_outcome_before_conn(
    conn: _ExplodingConnection,
) -> None:
    bad_row = SeasonResultRow(user_id=1, rank=1, games=2, wins=1, eligible=True, outcome="draw")
    with pytest.raises(ValueError, match=r"results\[0\]\.outcome"):
        await seasons.complete_season(conn, 1, 1, [bad_row])


async def test_list_unannounced_completed_validates_limit_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match="limit"):
        await seasons.list_unannounced_completed(conn, 0)


async def test_mark_announced_validates_season_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="season_id"):
        await seasons.mark_announced(conn, 1, -1)


async def test_get_season_results_validates_season_id_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match="season_id"):
        await seasons.get_season_results(conn, 1, "x")


# --- events.py ---


async def test_create_event_validates_guild_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="guild_id"):
        await events.create_event(conn, 0, "T", None, None, _AWARE_DATETIME, 1, None, [])


async def test_create_event_validates_starts_at_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="starts_at"):
        await events.create_event(conn, 1, "T", None, None, _NAIVE_DATETIME, 1, None, [])


async def test_create_event_validates_reminder_remind_at_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match=r"reminders\[0\]\.remind_at"):
        await events.create_event(
            conn, 1, "T", None, None, _AWARE_DATETIME, 1, None, [(60, _NAIVE_DATETIME)]
        )


async def test_set_event_message_validates_event_id_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match="event_id"):
        await events.set_event_message(conn, 1, 0, 1, 1)


async def test_get_event_validates_event_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="event_id"):
        await events.get_event(conn, 1, "x")


async def test_list_upcoming_events_validates_now_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="now"):
        await events.list_upcoming_events(conn, 1, _NAIVE_DATETIME, 10)


async def test_list_upcoming_events_validates_limit_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match="limit"):
        await events.list_upcoming_events(conn, 1, _AWARE_DATETIME, -1)


async def test_cancel_event_validates_actor_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="actor_id"):
        await events.cancel_event(conn, 1, 1, 0, False)


async def test_upsert_rsvp_validates_user_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="user_id"):
        await events.upsert_rsvp(conn, 1, 1, 0, "going")


async def test_rsvp_counts_validates_event_id_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="event_id"):
        await events.rsvp_counts(conn, 1, "x")


async def test_rsvp_user_ids_validates_responses_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="responses"):
        await events.rsvp_user_ids(conn, 1, 1, "going")  # type: ignore[arg-type]


async def test_rsvp_user_ids_validates_response_elements_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match="responses"):
        await events.rsvp_user_ids(conn, 1, 1, [1, 2])  # type: ignore[arg-type]


async def test_claim_due_reminders_validates_now_before_conn(conn: _ExplodingConnection) -> None:
    with pytest.raises(ValueError, match="now"):
        await events.claim_due_reminders(conn, _NAIVE_DATETIME)


async def test_complete_past_events_validates_cutoff_before_conn(
    conn: _ExplodingConnection,
) -> None:
    with pytest.raises(ValueError, match="cutoff"):
        await events.complete_past_events(conn, _NAIVE_DATETIME)
