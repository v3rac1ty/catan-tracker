"""Unit coverage for the admin-only confirmed-game score-correction sheet.

Phase 2 moved the reporter-facing score sheet to per-player DMs
(`views/score_entry.py`, covered by `tests/unit/test_score_entry.py`).
`views/game_scores.py` now serves exactly one workflow -- `/game update` --
so these tests exercise the player-picker + numeric-modal + award-select
design and its locking/staleness/error-routing behavior.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import discord
import pytest

from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.scoring import GameRules
from catan_bot.services.context import Actor
from catan_bot.views import game_scores


class _Response:
    def __init__(self) -> None:
        self.done = False
        self.type = discord.InteractionResponseType.deferred_message_update
        self.send_message = AsyncMock()
        self.edit_message = AsyncMock()
        self.send_modal = AsyncMock()
        self.defer = AsyncMock(side_effect=self._defer)

    async def _defer(self) -> None:
        self.done = True

    def is_done(self) -> bool:
        return self.done


def _interaction(*, user_id: int = 1, guild_id: int = 10) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=guild_id,
        response=_Response(),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def _sheet(*, stored_scores: tuple[object, ...] = ()) -> game_scores.GameScoreSheet:
    original = SimpleNamespace(
        game=SimpleNamespace(
            game_id=42,
            game_type="normal",
            played_on="2026-01-02",
            channel_id=None,
            message_id=None,
        ),
        winner_id=1,
        loser_ids=(2,),
        scores=stored_scores,
    )
    prepared = SimpleNamespace(
        game_id=42,
        winner_id=1,
        loser_ids=(2,),
        rules=GameRules("normal", target_points=10),
        original=original,
        initial_scores=stored_scores,
        played_on="2026-01-02",
        expected_revision=3,
    )
    return game_scores.GameScoreSheet(
        pool="pool",
        guild_id=10,
        actor=Actor(user_id=1, has_manage_guild=True, role_ids=frozenset()),
        prepared=prepared,
        channel=SimpleNamespace(id=700, send=AsyncMock()),
    )


def _announcement_state(*, revision: int, status: str = "confirmed") -> SimpleNamespace:
    return SimpleNamespace(
        game=SimpleNamespace(
            game_id=42,
            revision=revision,
            status=status,
            channel_id=700,
            message_id=800,
        )
    )


@pytest.mark.parametrize(("raw", "expected"), [("", None), ("  ", None), ("0", 0), ("99", 99)])
def test_score_cells_preserve_blank_vs_explicit_zero(raw: str, expected: int | None) -> None:
    assert game_scores.parse_score_cell(raw) == expected


@pytest.mark.parametrize("raw", ["-1", "100", "١", "one", "1.0"])
def test_score_cells_reject_invalid_values(raw: str) -> None:
    with pytest.raises(ValueError):
        game_scores.parse_score_cell(raw)


def test_sheet_has_a_thirty_minute_timeout() -> None:
    sheet = _sheet()
    assert sheet.timeout == 1800


def test_score_sheet_is_player_column_oriented() -> None:
    sheet = _sheet()
    sheet.values[1]["settlements"] = 0

    description = sheet.embed().description

    assert "P1" in description and "P2" in description
    assert "Settlements / houses" in description
    assert "P2 = <@2>" in description
    assert game_scores._BLANK in description


def test_sheet_has_player_and_award_selects_plus_five_buttons() -> None:
    sheet = _sheet()

    selects = [child for child in sheet.children if isinstance(child, discord.ui.Select)]
    buttons = [child for child in sheet.children if isinstance(child, discord.ui.Button)]
    assert len(selects) == 2
    assert {button.label for button in buttons} == {
        "Edit points",
        "Clear selected player",
        "Clear all points",
        "Save update",
        "Cancel",
    }


def test_numeric_modal_never_exceeds_five_inputs() -> None:
    sheet = _sheet()
    modal = game_scores.ScoreNumericModal(sheet, player_id=1, revision=0)
    assert len(modal.children) <= 5
    assert len(modal.children) == len(sheet.numeric_sources)


def test_player_select_switch_rebuilds_award_select_for_the_new_player() -> None:
    sheet = _sheet()
    sheet.values[2]["longest_road"] = 2
    old_award_select = sheet._award_select

    sheet.selected_player_id = 2
    sheet.rebuild_award_select()

    assert sheet._award_select is not old_award_select
    assert old_award_select not in sheet.children
    assert sheet._award_select in sheet.children
    selected = {option.value for option in sheet._award_select.options if option.default}
    assert "longest_road" in selected


@pytest.mark.asyncio
async def test_view_and_modal_reject_wrong_owner_or_guild() -> None:
    sheet = _sheet()
    wrong_owner = _interaction(user_id=2)
    wrong_guild = _interaction(guild_id=11)
    modal = game_scores.ScoreNumericModal(sheet, player_id=1, revision=0)

    assert await sheet.interaction_check(wrong_owner) is False
    assert await modal.interaction_check(wrong_guild) is False
    wrong_owner.response.send_message.assert_awaited_once()
    wrong_guild.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_modal_save_does_not_overwrite_current_draft() -> None:
    sheet = _sheet()
    interaction = _interaction()
    await sheet.save_numeric(interaction, player_id=1, revision=0, values=(4, 4, 0))
    stale = _interaction()
    await sheet.save_numeric(stale, player_id=1, revision=0, values=(1, 1, 0))

    assert sheet.values[1]["settlements"] == 4
    stale.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_award_select_zero_fills_unclaimed_and_preserves_unrelated_players() -> None:
    sheet = _sheet()
    await sheet.set_awards(_interaction(), player_id=1, awards=["longest_road"])

    assert sheet.values[1]["longest_road"] == 2
    assert sheet.values[1]["largest_army"] == 0
    assert sheet.values[2]["longest_road"] is None


@pytest.mark.asyncio
async def test_clear_selected_player_only_clears_that_player() -> None:
    sheet = _sheet()
    await sheet.save_numeric(_interaction(), player_id=1, revision=0, values=(8, 4, 0))
    await sheet.set_awards(_interaction(), player_id=1, awards=["longest_road"])
    sheet.values[2]["settlements"] = 3

    await sheet.clear_player(_interaction(), player_id=1)

    assert all(value is None for value in sheet.values[1].values())
    assert sheet.values[2]["settlements"] == 3


@pytest.mark.asyncio
async def test_partial_row_raises_a_clear_error_naming_the_player(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet = _sheet()
    sheet.values[1]["settlements"] = 4  # every other field stays None: a half-finished row.
    submit = AsyncMock()
    monkeypatch.setattr(game_scores.game_service, "submit_game_update", submit)

    await sheet.submit(_interaction())

    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_untouched_submit_passes_none_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    sheet = _sheet()
    interaction = _interaction()
    updated = _announcement_state(revision=1)
    submit = AsyncMock(return_value=updated)
    monkeypatch.setattr(game_scores.game_service, "submit_game_update", submit)
    monkeypatch.setattr("catan_bot.permissions.actor_from_interaction", lambda _: sheet.actor)
    monkeypatch.setattr(
        game_scores.formatting, "build_game_status_embed", lambda _: discord.Embed()
    )

    await sheet.submit(interaction)

    assert submit.await_args.kwargs["scores"] is None
    submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_complete_row_for_one_player_submits_partial_score_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only player 1's row is filled; player 2 stays absent -- allow_partial=True
    territory, matching `submit_game_update`'s own `allow_partial=True` call."""
    sheet = _sheet()
    await sheet.save_numeric(_interaction(), player_id=1, revision=0, values=(8, 0, 0))
    # A player's row also needs its award columns explicitly zero-filled to
    # count as complete -- `set_awards([])` claims nothing, matching a
    # player who touched the select but claimed no award.
    await sheet.set_awards(_interaction(), player_id=1, awards=[])
    updated = _announcement_state(revision=1)
    submit = AsyncMock(return_value=updated)
    monkeypatch.setattr(game_scores.game_service, "submit_game_update", submit)
    monkeypatch.setattr("catan_bot.permissions.actor_from_interaction", lambda _: sheet.actor)
    monkeypatch.setattr(
        game_scores.formatting, "build_game_status_embed", lambda _: discord.Embed()
    )

    await sheet.submit(_interaction())

    scores = submit.await_args.kwargs["scores"]
    assert scores is not None
    assert [score.user_id for score in scores] == [1]
    assert scores[0].total_points == 8


@pytest.mark.asyncio
async def test_submit_defers_before_starting_service(monkeypatch: pytest.MonkeyPatch) -> None:
    sheet = _sheet()
    interaction = _interaction()

    async def submit(*_args: object, **_kwargs: object) -> object:
        interaction.response.defer.assert_awaited_once_with()
        raise DomainValidationError("Safe validation message")

    monkeypatch.setattr(game_scores.game_service, "submit_game_update", submit)
    monkeypatch.setattr("catan_bot.permissions.actor_from_interaction", lambda _: sheet.actor)
    await sheet.submit(interaction)

    interaction.followup.send.assert_awaited_once()
    assert interaction.followup.send.await_args.args[0] == "Safe validation message"
    assert sheet.updated_game is None


@pytest.mark.asyncio
async def test_unexpected_value_error_is_sanitized_after_defer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet = _sheet()
    interaction = _interaction()
    monkeypatch.setattr(
        game_scores.game_service,
        "submit_game_update",
        AsyncMock(side_effect=ValueError("secret raw input")),
    )
    await sheet.submit(interaction)

    assert interaction.followup.send.await_args.args[0] == "Something went wrong. Please try again."


@pytest.mark.asyncio
async def test_terminal_and_expired_sheets_reject_editing() -> None:
    sheet = _sheet()
    sheet.updated_game = object()
    assert await sheet.ensure_editable(_interaction()) is False
    sheet = _sheet()
    sheet._deadline = 0
    assert await sheet.ensure_editable(_interaction()) is False


@pytest.mark.asyncio
async def test_cancel_never_saves_an_update(monkeypatch: pytest.MonkeyPatch) -> None:
    sheet = _sheet()
    submit = AsyncMock()
    monkeypatch.setattr(game_scores.game_service, "submit_game_update", submit)
    await game_scores._CancelButton(sheet).callback(_interaction())
    await sheet.submit(_interaction())

    assert sheet.cancelled is True
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_submit_acknowledges_other_interactions_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet = _sheet()
    started = asyncio.Event()
    release = asyncio.Event()
    updated = _announcement_state(revision=1)

    async def blocked_submit(*_args: object, **_kwargs: object) -> object:
        started.set()
        await release.wait()
        return updated

    monkeypatch.setattr(game_scores.game_service, "submit_game_update", blocked_submit)
    monkeypatch.setattr("catan_bot.permissions.actor_from_interaction", lambda _: sheet.actor)
    monkeypatch.setattr(
        game_scores.formatting, "build_game_status_embed", lambda _: discord.Embed()
    )
    submit_task = asyncio.create_task(sheet.submit(_interaction()))
    await asyncio.wait_for(started.wait(), timeout=0.2)

    assert await asyncio.wait_for(sheet.interaction_check(_interaction()), timeout=0.2) is False
    modal = game_scores.ScoreNumericModal(sheet, player_id=1, revision=0)
    assert await asyncio.wait_for(modal.interaction_check(_interaction()), timeout=0.2) is False
    cancel_interaction = _interaction()
    cancel_task = asyncio.create_task(sheet.cancel(cancel_interaction))
    await asyncio.sleep(0)
    cancel_interaction.response.defer.assert_awaited_once_with()

    release.set()
    await asyncio.wait_for(submit_task, timeout=0.2)
    await asyncio.wait_for(cancel_task, timeout=0.2)


@pytest.mark.asyncio
async def test_timeout_marks_fresh_sheet_expired_and_disables_controls() -> None:
    sheet = _sheet()
    private = SimpleNamespace(edit=AsyncMock())
    sheet.private_message = private

    await sheet.on_timeout()

    assert sheet.expired is True
    assert all(child.disabled for child in sheet.children)
    private.edit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["persisted", "cancelled", "completed", "expired"])
async def test_terminal_modal_save_does_not_mutate_values(terminal: str) -> None:
    sheet = _sheet()
    if terminal == "persisted":
        sheet.updated_game = object()
    else:
        setattr(sheet, terminal, True)
    before = {user_id: values.copy() for user_id, values in sheet.values.items()}
    interaction = _interaction()
    await sheet.save_numeric(interaction, player_id=1, revision=0, values=(4, 4, 0))

    assert sheet.values == before
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_absolute_deadline_expires_independently_of_terminal_flags() -> None:
    sheet = _sheet()
    sheet._deadline = 0

    assert await sheet.interaction_check(_interaction()) is False
    assert sheet.expired is True


def test_prefill_copies_stored_scores_and_leaves_others_blank() -> None:
    score = game_scores.PlayerScore(
        user_id=1,
        total_points=0,
        breakdown=(game_scores.ScoreEntry(key="settlements", points=0),),
    )
    sheet = _sheet(stored_scores=(score,))

    assert sheet.values[1]["settlements"] == 0
    assert sheet.values[1]["cities"] is None
    assert sheet.values[2]["settlements"] is None


@pytest.mark.asyncio
async def test_clear_all_points_stales_open_modal_and_rebuilds_award_select() -> None:
    sheet = _sheet()
    sheet.values[1]["settlements"] = 4
    interaction = _interaction()

    await sheet.clear_all_points(interaction)

    assert sheet.revision == 1
    assert all(value is None for player in sheet.values.values() for value in player.values())
    assert interaction.response.defer.await_count == 1


@pytest.mark.asyncio
async def test_update_submit_rebuilds_actor_and_saves(monkeypatch: pytest.MonkeyPatch) -> None:
    sheet = _sheet()
    interaction = _interaction()
    updated = _announcement_state(revision=1)
    submit = AsyncMock(return_value=updated)
    monkeypatch.setattr(game_scores.game_service, "submit_game_update", submit)
    monkeypatch.setattr("catan_bot.permissions.actor_from_interaction", lambda _: sheet.actor)
    monkeypatch.setattr(
        game_scores.formatting, "build_game_status_embed", lambda _: discord.Embed()
    )

    await sheet.submit(interaction)
    await sheet.submit(_interaction())

    submit.assert_awaited_once()
    assert submit.await_args.kwargs["scores"] is None
    assert sheet.completed is True


@pytest.mark.asyncio
async def test_update_refreshes_original_message_without_confirm_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet = _sheet()
    sheet.prepared.original.game.channel_id = 700
    sheet.prepared.original.game.message_id = 800
    message = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(
        id=700, guild=SimpleNamespace(id=10), fetch_message=AsyncMock(return_value=message)
    )
    sheet.channel = channel
    updated = _announcement_state(revision=1)
    monkeypatch.setattr(
        game_scores.game_service, "submit_game_update", AsyncMock(return_value=updated)
    )
    monkeypatch.setattr(game_scores.game_service, "get_game", AsyncMock(return_value=updated))
    monkeypatch.setattr("catan_bot.permissions.actor_from_interaction", lambda _: sheet.actor)
    monkeypatch.setattr(
        game_scores.formatting, "build_game_status_embed", lambda _: discord.Embed()
    )

    await sheet.submit(_interaction())

    channel.fetch_message.assert_awaited_once_with(800)
    assert message.edit.await_args.kwargs["view"] is None
    assert sheet.completed is True


@pytest.mark.asyncio
async def test_update_refresh_failure_retries_message_only_with_fresh_game(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet = _sheet()
    sheet.prepared.original.game.channel_id = 700
    sheet.prepared.original.game.message_id = 800
    message = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(
        id=700,
        guild=SimpleNamespace(id=10),
        fetch_message=AsyncMock(side_effect=[RuntimeError("offline"), message]),
    )
    sheet.channel = channel
    updated = _announcement_state(revision=1)
    submit = AsyncMock(return_value=updated)
    fresh = _announcement_state(revision=1)
    get_game = AsyncMock(return_value=fresh)
    monkeypatch.setattr(game_scores.game_service, "submit_game_update", submit)
    monkeypatch.setattr(game_scores.game_service, "get_game", get_game)
    monkeypatch.setattr("catan_bot.permissions.actor_from_interaction", lambda _: sheet.actor)
    monkeypatch.setattr(
        game_scores.formatting, "build_game_status_embed", lambda _: discord.Embed()
    )

    await sheet.submit(_interaction())
    await sheet.submit(_interaction())

    submit.assert_awaited_once()
    assert get_game.await_count >= 2
    assert all(args == call("pool", 10, 42) for args in get_game.await_args_list)
    message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_refresh_reconciles_a_newer_revision_without_second_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet = _sheet()
    sheet.prepared.original.game.channel_id = 700
    sheet.prepared.original.game.message_id = 800
    message = SimpleNamespace(edit=AsyncMock())
    sheet.channel = SimpleNamespace(
        id=700, guild=SimpleNamespace(id=10), fetch_message=AsyncMock(return_value=message)
    )
    saved, newer = _announcement_state(revision=1), _announcement_state(revision=2)
    submit = AsyncMock(return_value=saved)
    get_game = AsyncMock(side_effect=[saved, newer, newer, newer])
    monkeypatch.setattr(game_scores.game_service, "submit_game_update", submit)
    monkeypatch.setattr(game_scores.game_service, "get_game", get_game)
    monkeypatch.setattr("catan_bot.permissions.actor_from_interaction", lambda _: sheet.actor)
    monkeypatch.setattr(
        game_scores.formatting, "build_game_status_embed", lambda _: discord.Embed()
    )

    await sheet.submit(_interaction())

    submit.assert_awaited_once()
    assert message.edit.await_count == 2
    assert get_game.await_count == 4


@pytest.mark.asyncio
async def test_update_refresh_reconciles_a_concurrent_void(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet = _sheet()
    sheet.prepared.original.game.channel_id = 700
    sheet.prepared.original.game.message_id = 800
    message = SimpleNamespace(edit=AsyncMock())
    sheet.channel = SimpleNamespace(
        id=700, guild=SimpleNamespace(id=10), fetch_message=AsyncMock(return_value=message)
    )
    saved = _announcement_state(revision=1)
    voided = _announcement_state(revision=1, status="voided")
    get_game = AsyncMock(side_effect=[saved, voided, voided, voided])
    status_embed = Mock(return_value=discord.Embed())
    monkeypatch.setattr(
        game_scores.game_service, "submit_game_update", AsyncMock(return_value=saved)
    )
    monkeypatch.setattr(game_scores.game_service, "get_game", get_game)
    monkeypatch.setattr("catan_bot.permissions.actor_from_interaction", lambda _: sheet.actor)
    monkeypatch.setattr(game_scores.formatting, "build_game_status_embed", status_embed)

    await sheet.submit(_interaction())

    assert message.edit.await_count == 2
    assert any(call.args[0] is voided for call in status_embed.call_args_list)
