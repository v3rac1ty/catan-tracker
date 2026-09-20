"""Behavior tests for `catan_bot.db.repositories.score_requests`.

Runs as the least-privilege `catan_app` role against `catan_test`.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta

import asyncpg
import pytest

from catan_bot.db.repositories import games, players, score_requests

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]

PLAYED_ON = date(2026, 3, 1)
REQUESTED_AT = datetime(2026, 3, 1, 20, tzinfo=UTC)


async def _game(app_conn: asyncpg.Connection, guild_id: int) -> int:
    await players.ensure_players(app_conn, guild_id, [1, 2, 3])
    game = await games.create_game(app_conn, guild_id, None, PLAYED_ON, 1, 1, [2, 3])
    return game.game_id


async def test_create_score_requests_seeds_one_pending_row_per_participant(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    game_id = await _game(app_conn, guild_id)

    await score_requests.create_score_requests(app_conn, guild_id, game_id, [1, 2, 3], REQUESTED_AT)

    requests = await score_requests.list_score_requests(app_conn, guild_id, game_id)
    assert [r.user_id for r in requests] == [1, 2, 3]
    for r in requests:
        assert r.delivery_status == "pending"
        assert r.dm_channel_id is None
        assert r.dm_message_id is None
        assert r.prompts_sent == 0
        assert r.submitted_at is None
        assert r.requested_at == REQUESTED_AT
        assert r.next_prompt_at == REQUESTED_AT + timedelta(hours=24)


async def test_mark_delivered_then_mark_submitted_round_trips(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    game_id = await _game(app_conn, guild_id)
    await score_requests.create_score_requests(app_conn, guild_id, game_id, [1], REQUESTED_AT)

    delivered = await score_requests.mark_delivered(app_conn, guild_id, game_id, 1, 555, 777)
    assert delivered is True

    requests = await score_requests.list_score_requests(app_conn, guild_id, game_id)
    assert requests[0].delivery_status == "delivered"
    assert requests[0].dm_channel_id == 555
    assert requests[0].dm_message_id == 777

    submitted_at = REQUESTED_AT + timedelta(hours=1)
    submitted = await score_requests.mark_submitted(app_conn, guild_id, game_id, 1, submitted_at)
    assert submitted is True

    after = await score_requests.list_score_requests(app_conn, guild_id, game_id)
    assert after[0].submitted_at == submitted_at
    assert after[0].next_prompt_at is None

    # A second submission attempt is a no-op, not an overwrite.
    again = await score_requests.mark_submitted(
        app_conn, guild_id, game_id, 1, submitted_at + timedelta(hours=1)
    )
    assert again is False
    unchanged = await score_requests.list_score_requests(app_conn, guild_id, game_id)
    assert unchanged[0].submitted_at == submitted_at


async def test_mark_blocked_sets_status_without_dm_identity(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    game_id = await _game(app_conn, guild_id)
    await score_requests.create_score_requests(app_conn, guild_id, game_id, [2], REQUESTED_AT)

    blocked = await score_requests.mark_blocked(app_conn, guild_id, game_id, 2)
    assert blocked is True

    requests = await score_requests.list_score_requests(app_conn, guild_id, game_id)
    assert requests[0].delivery_status == "blocked"
    assert requests[0].dm_channel_id is None


async def test_mark_delivered_unknown_request_returns_false(
    app_conn: asyncpg.Connection, guild_id: int, other_guild_id: int
) -> None:
    game_id = await _game(app_conn, guild_id)
    await score_requests.create_score_requests(app_conn, guild_id, game_id, [1], REQUESTED_AT)

    # Wrong guild.
    assert await score_requests.mark_delivered(app_conn, other_guild_id, game_id, 1, 1, 1) is False
    # Unknown user.
    assert await score_requests.mark_delivered(app_conn, guild_id, game_id, 999, 1, 1) is False


async def test_claim_due_prompts_claims_due_rows_and_reschedules_them(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    game_id = await _game(app_conn, guild_id)
    await score_requests.create_score_requests(app_conn, guild_id, game_id, [1, 2, 3], REQUESTED_AT)

    not_yet_due = REQUESTED_AT + timedelta(hours=23)
    assert await score_requests.claim_due_prompts(app_conn, not_yet_due, 10) == []

    due_now = REQUESTED_AT + timedelta(hours=24, minutes=1)
    claimed = await score_requests.claim_due_prompts(app_conn, due_now, 10)
    assert {c.user_id for c in claimed} == {1, 2, 3}
    for c in claimed:
        assert c.prompts_sent == 1
        assert c.next_prompt_at == due_now + timedelta(hours=24)

    # Already claimed and rescheduled: not due again at the same instant.
    assert await score_requests.claim_due_prompts(app_conn, due_now, 10) == []


async def test_claim_due_prompts_respects_limit_and_max_prompts(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    game_id = await _game(app_conn, guild_id)
    await score_requests.create_score_requests(app_conn, guild_id, game_id, [1, 2, 3], REQUESTED_AT)
    due_now = REQUESTED_AT + timedelta(hours=24, minutes=1)

    first_batch = await score_requests.claim_due_prompts(app_conn, due_now, 2)
    assert len(first_batch) == 2

    remaining = await score_requests.claim_due_prompts(app_conn, due_now, 10)
    assert len(remaining) == 1

    # Push every request's `prompts_sent` to the 3-round ceiling directly,
    # then confirm the sweep stops claiming them even though they're due.
    await app_conn.execute(
        "UPDATE game_score_requests SET prompts_sent = 3, next_prompt_at = $1 WHERE game_id = $2",
        due_now,
        game_id,
    )
    assert await score_requests.claim_due_prompts(app_conn, due_now + timedelta(days=1), 10) == []


async def test_claim_due_prompts_never_claims_a_submitted_request(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    game_id = await _game(app_conn, guild_id)
    await score_requests.create_score_requests(app_conn, guild_id, game_id, [1], REQUESTED_AT)
    submitted_at = REQUESTED_AT + timedelta(hours=1)
    await score_requests.mark_submitted(app_conn, guild_id, game_id, 1, submitted_at)

    due_now = REQUESTED_AT + timedelta(hours=48)
    assert await score_requests.claim_due_prompts(app_conn, due_now, 10) == []


async def test_score_requests_cannot_be_deleted_by_the_app_role(
    app_conn: asyncpg.Connection, guild_id: int
) -> None:
    """Default privileges (`db/roles.sql`) give `catan_app` SELECT/INSERT/
    UPDATE on new tables but never DELETE -- proven here directly, matching
    the design note in `0005_score_collection.sql` that nothing should ever
    need deleting from this table."""
    game_id = await _game(app_conn, guild_id)
    await score_requests.create_score_requests(app_conn, guild_id, game_id, [1], REQUESTED_AT)

    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("DELETE FROM game_score_requests")
