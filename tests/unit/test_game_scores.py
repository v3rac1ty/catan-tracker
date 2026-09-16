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


def _prepared(*, players: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        winner_id=1,
        loser_ids=tuple(range(2, players + 1)),
        rules=GameRules(game_type="normal", target_points=10),
    )


def _sheet(*, players: int = 2) -> game_scores.GameScoreSheet:
    return game_scores.GameScoreSheet(
        pool="pool",
        guild_id=10,
        actor=Actor(user_id=1, has_manage_guild=False, role_ids=frozenset()),
        prepared=_prepared(players=players),
        channel=SimpleNamespace(send=AsyncMock()),
    )


def _update_sheet(*, stored_scores: tuple[object, ...] = ()) -> game_scores.GameScoreSheet:
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
        mode="update",
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


def test_score_sheet_is_player_column_oriented_with_six_player_legend() -> None:
    sheet = _sheet(players=6)
    sheet.values[1]["settlements"] = 0

    description = sheet.embed().description

    assert "P1" in description and "P6" in description
    assert "Settlements / houses" in description
    assert "P6 = <@6>" in description
    assert game_scores._BLANK in description


def test_game_type_catalog_controls_score_rows() -> None:
    normal_pages = game_scores.score_pages(GameRules("normal", target_points=10))
    normal = [source.key for source in normal_pages[0]]
    seafarers_pages = game_scores.score_pages(GameRules("seafarers", target_points=10))
    seafarers = [source.key for source in seafarers_pages[0]]

    assert "longest_road" in normal
    assert "longest_trade_route" in seafarers


def test_first_and_later_score_modals_never_exceed_five_inputs() -> None:
    sheet = _sheet()
    for page_index in range(len(sheet.pages)):
        modal = game_scores.ScorePageModal(sheet, player_id=1, page_index=page_index, revision=0)
        assert len(modal.children) <= 5
    first = game_scores.ScorePageModal(sheet, player_id=1, page_index=0, revision=0)
    assert len(first.children) == 5


@pytest.mark.asyncio
async def test_view_and_modal_reject_wrong_owner_or_guild() -> None:
    sheet = _sheet()
    wrong_owner = _interaction(user_id=2)
    wrong_guild = _interaction(guild_id=11)
    modal = game_scores.ScorePageModal(sheet, player_id=1, page_index=0, revision=0)

    assert await sheet.interaction_check(wrong_owner) is False
    assert await modal.interaction_check(wrong_guild) is False
    wrong_owner.response.send_message.assert_awaited_once()
    wrong_guild.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_modal_save_does_not_overwrite_current_draft() -> None:
    sheet = _sheet()
    interaction = _interaction()
    await sheet.save_page(
        interaction, player_id=1, page_index=0, revision=0, values=(10, 4, 4, 2, 0)
    )
    stale = _interaction()
    await sheet.save_page(stale, player_id=1, page_index=0, revision=0, values=(1, 1, 0, 0, 0))

    assert sheet.values[1]["settlements"] == 4
    stale.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_untouched_submit_passes_none_and_publishes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet = _sheet()
    interaction = _interaction()
    created = SimpleNamespace(game=SimpleNamespace(game_id=42))
    public = SimpleNamespace(
        channel=SimpleNamespace(id=700), id=800, jump_url="https://example.test/42"
    )
    sheet.channel.send.return_value = public
    submit = AsyncMock(return_value=created)
    record = AsyncMock()
    monkeypatch.setattr(game_scores.game_service, "submit_game_report", submit)
    monkeypatch.setattr(game_scores.game_service, "record_game_message", record)
    monkeypatch.setattr(
        game_scores.formatting, "build_game_report_embed", lambda _: discord.Embed()
    )

    await sheet.submit(interaction)
    await sheet.submit(_interaction())

    assert submit.await_args.kwargs["scores"] is None
    submit.assert_awaited_once()
    sheet.channel.send.assert_awaited_once()
    record.assert_awaited_once_with("pool", 10, 42, 700, 800)


@pytest.mark.asyncio
async def test_partial_sheet_never_calls_submit_service(monkeypatch: pytest.MonkeyPatch) -> None:
    sheet = _sheet()
    sheet.values[1]["settlements"] = 0
    submit = AsyncMock()
    monkeypatch.setattr(game_scores.game_service, "submit_game_report", submit)

    await sheet.submit(_interaction())

    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_defers_before_starting_service(monkeypatch: pytest.MonkeyPatch) -> None:
    sheet = _sheet()
    interaction = _interaction()

    async def submit(*_args: object, **_kwargs: object) -> object:
        interaction.response.defer.assert_awaited_once_with()
        raise DomainValidationError("Safe validation message")

    monkeypatch.setattr(game_scores.game_service, "submit_game_report", submit)
    await sheet.submit(interaction)

    interaction.followup.send.assert_awaited_once()
    assert interaction.followup.send.await_args.args[0] == "Safe validation message"
    assert sheet.created_report is None


@pytest.mark.asyncio
async def test_unexpected_value_error_is_sanitized_after_defer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet = _sheet()
    interaction = _interaction()
    monkeypatch.setattr(
        game_scores.game_service,
        "submit_game_report",
        AsyncMock(side_effect=ValueError("secret raw input")),
    )
    await sheet.submit(interaction)

    assert interaction.followup.send.await_args.args[0] == "Something went wrong. Please try again."


@pytest.mark.asyncio
async def test_complete_explicit_zero_sheet_submits_player_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet = _sheet()
    for player_values in sheet.values.values():
        for key in player_values:
            player_values[key] = 0
    created = SimpleNamespace(game=SimpleNamespace(game_id=42))
    sheet.channel.send.return_value = SimpleNamespace(
        channel=SimpleNamespace(id=700), id=800, jump_url="https://example.test/42"
    )
    submit = AsyncMock(return_value=created)
    monkeypatch.setattr(game_scores.game_service, "submit_game_report", submit)
    monkeypatch.setattr(game_scores.game_service, "record_game_message", AsyncMock())
    monkeypatch.setattr(
        game_scores.formatting, "build_game_report_embed", lambda _: discord.Embed()
    )

    await sheet.submit(_interaction())

    scores = submit.await_args.kwargs["scores"]
    assert scores is not None
    assert [score.total_points for score in scores] == [0, 0]
    assert all(entry.points == 0 for score in scores for entry in score.breakdown)


@pytest.mark.asyncio
async def test_failed_publication_retries_without_duplicate_database_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet = _sheet()
    created = SimpleNamespace(game=SimpleNamespace(game_id=42))
    public = SimpleNamespace(
        channel=SimpleNamespace(id=700), id=800, jump_url="https://example.test/42"
    )
    sheet.channel.send.side_effect = [RuntimeError("offline"), public]
    submit = AsyncMock(return_value=created)
    monkeypatch.setattr(game_scores.game_service, "submit_game_report", submit)
    monkeypatch.setattr(game_scores.game_service, "record_game_message", AsyncMock())
    monkeypatch.setattr(
        game_scores.formatting, "build_game_report_embed", lambda _: discord.Embed()
    )

    await sheet.submit(_interaction())
    await sheet.submit(_interaction())

    submit.assert_awaited_once()
    assert sheet.channel.send.await_count == 2


@pytest.mark.asyncio
async def test_persisted_retry_ignores_later_partial_state(monkeypatch: pytest.MonkeyPatch) -> None:
    sheet = _sheet()
    created = SimpleNamespace(game=SimpleNamespace(game_id=42))
    public = SimpleNamespace(
        channel=SimpleNamespace(id=700), id=800, jump_url="https://example.test/42"
    )
    sheet.channel.send.side_effect = [RuntimeError("offline"), public]
    submit = AsyncMock(return_value=created)
    monkeypatch.setattr(game_scores.game_service, "submit_game_report", submit)
    monkeypatch.setattr(game_scores.game_service, "record_game_message", AsyncMock())
    monkeypatch.setattr(
        game_scores.formatting, "build_game_report_embed", lambda _: discord.Embed()
    )

    await sheet.submit(_interaction())
    sheet.values[1]["settlements"] = 1
    await sheet.submit(_interaction())

    submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_and_expired_sheets_reject_editing() -> None:
    sheet = _sheet()
    sheet.created_report = object()
    assert await sheet.ensure_editable(_interaction()) is False
    sheet = _sheet()
    sheet._deadline = 0
    assert await sheet.ensure_editable(_interaction()) is False


@pytest.mark.asyncio
async def test_cancel_and_timeout_never_create_a_game(monkeypatch: pytest.MonkeyPatch) -> None:
    sheet = _sheet()
    submit = AsyncMock()
    monkeypatch.setattr(game_scores.game_service, "submit_game_report", submit)
    await game_scores._CancelButton(sheet).callback(_interaction())
    await sheet.submit(_interaction())
    await sheet.on_timeout()

    assert sheet.cancelled is True
    submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_busy_submit_acknowledges_other_interactions_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet = _sheet()
    started = asyncio.Event()
    release = asyncio.Event()
    created = SimpleNamespace(game=SimpleNamespace(game_id=42))
    sheet.channel.send.return_value = SimpleNamespace(
        channel=SimpleNamespace(id=700), id=800, jump_url="https://example.test/42"
    )

    async def blocked_submit(*_args: object, **_kwargs: object) -> object:
        started.set()
        await release.wait()
        return created

    monkeypatch.setattr(game_scores.game_service, "submit_game_report", blocked_submit)
    monkeypatch.setattr(game_scores.game_service, "record_game_message", AsyncMock())
    monkeypatch.setattr(
        game_scores.formatting, "build_game_report_embed", lambda _: discord.Embed()
    )
    submit_task = asyncio.create_task(sheet.submit(_interaction()))
    await asyncio.wait_for(started.wait(), timeout=0.2)

    assert await asyncio.wait_for(sheet.interaction_check(_interaction()), timeout=0.2) is False
    modal = game_scores.ScorePageModal(sheet, player_id=1, page_index=0, revision=0)
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
        sheet.created_report = object()
    else:
        setattr(sheet, terminal, True)
    before = {user_id: values.copy() for user_id, values in sheet.values.items()}
    interaction = _interaction()
    await sheet.save_page(
        interaction, player_id=1, page_index=0, revision=0, values=(10, 4, 4, 2, 0)
    )

    assert sheet.values == before
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_absolute_deadline_expires_independently_of_terminal_flags() -> None:
    sheet = _sheet()
    sheet._deadline = 0

    assert await sheet.interaction_check(_interaction()) is False
    assert sheet.expired is True


def test_update_prefills_zero_but_keeps_legacy_empty_scores_blank() -> None:
    score = game_scores.PlayerScore(
        user_id=1,
        total_points=0,
        breakdown=(game_scores.ScoreEntry(key="settlements", points=0),),
    )
    sheet = _update_sheet(stored_scores=(score,))

    assert sheet.values[1]["__total__"] == 0
    assert sheet.values[1]["settlements"] == 0
    assert sheet.values[1]["cities"] is None
    assert sheet.values[2]["__total__"] is None


@pytest.mark.asyncio
async def test_update_clear_all_points_stales_open_modal() -> None:
    sheet = _update_sheet()
    sheet.values[1]["settlements"] = 4
    interaction = _interaction()

    await sheet.clear_all_points(interaction)

    assert sheet.revision == 1
    assert all(value is None for player in sheet.values.values() for value in player.values())
    assert interaction.response.defer.await_count == 1


@pytest.mark.asyncio
async def test_update_submit_rebuilds_actor_and_does_not_create_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet = _update_sheet()
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
    assert sheet.created_report is None
    assert sheet.completed is True


@pytest.mark.asyncio
async def test_update_refreshes_original_message_without_confirm_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sheet = _update_sheet()
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
    sheet = _update_sheet()
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
    sheet = _update_sheet()
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
    sheet = _update_sheet()
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
