"""Unit coverage for the per-player DM score-entry sheet (`views/score_entry.py`).

Every component here is a `discord.ui.DynamicItem`, so these tests exercise
the same two things `tests/unit/test_game_buttons.py` does for
`GameActionButton`: the persistent `custom_id` round-trip, and the
callback's own behavior given a bare `SimpleNamespace` interaction double.
The sheets live in DMs, where `interaction.guild_id` is `None` -- every
interaction double below reflects that, unlike `tests/unit/test_game_scores.py`'s
guild-scoped admin sheet.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.scoring import GameRules, PlayerScore, ScoreEntry
from catan_bot.views import score_entry


class _Response:
    def __init__(self) -> None:
        self.done = False
        self.send_modal = AsyncMock(side_effect=self._mark_done)
        self.send_message = AsyncMock(side_effect=self._mark_done)
        self.edit_message = AsyncMock(side_effect=self._mark_done)

    async def _mark_done(self, *_args: object, **_kwargs: object) -> None:
        self.done = True

    def is_done(self) -> bool:
        return self.done


def _interaction(*, user_id: int = 1, pool: object = "pool") -> SimpleNamespace:
    # These sheets live in DMs: `guild_id` is always None on the real
    # interaction, and ownership is checked purely off `interaction.user.id`
    # against the id baked into the custom_id -- never against a guild.
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=None,
        client=SimpleNamespace(pool=pool),
        response=_Response(),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _game(
    *,
    status: str = "pending",
    winner_id: int = 1,
    loser_ids: tuple[int, ...] = (2,),
    scores: tuple[PlayerScore, ...] = (),
    channel_id: int | None = 700,
    message_id: int | None = 800,
) -> SimpleNamespace:
    game = SimpleNamespace(
        game_id=42,
        game_type="normal",
        extension_5_6=False,
        scenario=None,
        target_points=10,
        status=status,
        channel_id=channel_id,
        message_id=message_id,
    )
    return SimpleNamespace(game=game, winner_id=winner_id, loser_ids=loser_ids, scores=scores)


def _score(user_id: int, *, settlements: int = 8, longest_road: int = 0) -> PlayerScore:
    return PlayerScore(
        user_id=user_id,
        total_points=settlements + longest_road,
        breakdown=(
            ScoreEntry("settlements", settlements),
            ScoreEntry("cities", 0),
            ScoreEntry("longest_road", longest_road),
            ScoreEntry("largest_army", 0),
            ScoreEntry("vp_cards", 0),
        ),
    )


def _award_select(
    *, guild_id: int = 10, game_id: int = 42, user_id: int = 1, selected: list[str] | None = None
) -> score_entry.ScoreAwardSelect:
    raw = discord.ui.Select(
        custom_id=f"score:award:{guild_id}:{game_id}:{user_id}",
        options=[
            discord.SelectOption(label="Longest Road", value="longest_road"),
            discord.SelectOption(label="Largest Army", value="largest_army"),
        ],
    )
    if selected is not None:
        raw._values = selected
    return score_entry.ScoreAwardSelect(raw, guild_id=guild_id, game_id=game_id, user_id=user_id)


# ---------------------------------------------------------------------------
# custom_id round trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_button_custom_id_round_trip() -> None:
    match = score_entry.ScoreEntryButton.__discord_ui_compiled_template__.fullmatch(
        "score:edit:10:42:1"
    )
    assert match is not None
    raw_item = discord.ui.Button(custom_id="score:edit:10:42:1")

    rebuilt = await score_entry.ScoreEntryButton.from_custom_id(SimpleNamespace(), raw_item, match)

    assert (rebuilt.guild_id, rebuilt.game_id, rebuilt.user_id) == (10, 42, 1)
    assert rebuilt.item.custom_id == "score:edit:10:42:1"


@pytest.mark.asyncio
async def test_clear_button_custom_id_round_trip() -> None:
    match = score_entry.ScoreClearButton.__discord_ui_compiled_template__.fullmatch(
        "score:clear:10:42:1"
    )
    assert match is not None
    raw_item = discord.ui.Button(custom_id="score:clear:10:42:1")

    rebuilt = await score_entry.ScoreClearButton.from_custom_id(SimpleNamespace(), raw_item, match)

    assert (rebuilt.guild_id, rebuilt.game_id, rebuilt.user_id) == (10, 42, 1)


@pytest.mark.asyncio
async def test_award_select_custom_id_round_trip_reuses_the_live_select_item() -> None:
    match = score_entry.ScoreAwardSelect.__discord_ui_compiled_template__.fullmatch(
        "score:award:10:42:1"
    )
    assert match is not None
    raw_item = discord.ui.Select(
        custom_id="score:award:10:42:1",
        options=[discord.SelectOption(label="Longest Road", value="longest_road")],
    )

    rebuilt = await score_entry.ScoreAwardSelect.from_custom_id(SimpleNamespace(), raw_item, match)

    assert (rebuilt.guild_id, rebuilt.game_id, rebuilt.user_id) == (10, 42, 1)
    assert rebuilt.item is raw_item


def test_build_score_entry_view_wires_up_all_three_ids() -> None:
    rules = GameRules("normal", target_points=10)
    view = score_entry.build_score_entry_view(10, 42, 1, rules)

    custom_ids = {item.item.custom_id for item in view.children}
    assert custom_ids == {"score:edit:10:42:1", "score:award:10:42:1", "score:clear:10:42:1"}


@pytest.mark.parametrize("bad_id", [0, -1, 2**63])
def test_build_score_entry_view_rejects_invalid_ids(bad_id: int) -> None:
    rules = GameRules("normal", target_points=10)
    with pytest.raises(ValueError, match="positive BIGINT"):
        score_entry.build_score_entry_view(bad_id, 42, 1, rules)


# ---------------------------------------------------------------------------
# Ownership enforcement (DM context: interaction.guild_id is None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_button_rejects_wrong_owner_without_touching_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction(user_id=2)
    assert interaction.guild_id is None
    get_game = AsyncMock()
    monkeypatch.setattr(score_entry.game_service, "get_game", get_game)
    button = score_entry.ScoreEntryButton(10, 42, 1)

    await button.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.args[0] == score_entry._WRONG_OWNER_TEXT
    get_game.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_button_rejects_wrong_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    interaction = _interaction(user_id=2)
    clear = AsyncMock()
    monkeypatch.setattr(score_entry.game_service, "clear_player_score", clear)
    button = score_entry.ScoreClearButton(10, 42, 1)

    await button.callback(interaction)

    assert interaction.response.send_message.await_args.args[0] == score_entry._WRONG_OWNER_TEXT
    clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_award_select_rejects_wrong_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    interaction = _interaction(user_id=2)
    record = AsyncMock()
    monkeypatch.setattr(score_entry.game_service, "record_player_score", record)
    select = _award_select(selected=["longest_road"])

    await select.callback(interaction)

    assert interaction.response.send_message.await_args.args[0] == score_entry._WRONG_OWNER_TEXT
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_numeric_modal_rejects_wrong_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    interaction = _interaction(user_id=2)
    rules = GameRules("normal", target_points=10)
    modal = score_entry.ScoreNumericModal(
        guild_id=10, game_id=42, user_id=1, rules=rules, current={}
    )
    record = AsyncMock()
    monkeypatch.setattr(score_entry.game_service, "record_player_score", record)

    await modal.on_submit(interaction)

    assert interaction.response.send_message.await_args.args[0] == score_entry._WRONG_OWNER_TEXT
    record.assert_not_awaited()


# ---------------------------------------------------------------------------
# ScoreEntryButton
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_button_opens_modal_prefilled_from_stored_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction(user_id=1)
    monkeypatch.setattr(
        score_entry.game_service,
        "get_game",
        AsyncMock(return_value=_game(scores=(_score(1, settlements=6),))),
    )
    button = score_entry.ScoreEntryButton(10, 42, 1)

    await button.callback(interaction)

    interaction.response.send_modal.assert_awaited_once()
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, score_entry.ScoreNumericModal)
    # entry_fields' numeric order for "normal": settlements, cities, vp_cards.
    assert [item.default for item in modal.inputs] == ["6", "0", "0"]


@pytest.mark.asyncio
async def test_edit_button_shows_blank_defaults_for_an_unrecorded_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction(user_id=1)
    monkeypatch.setattr(
        score_entry.game_service, "get_game", AsyncMock(return_value=_game(scores=()))
    )
    button = score_entry.ScoreEntryButton(10, 42, 1)

    await button.callback(interaction)

    modal = interaction.response.send_modal.await_args.args[0]
    assert all(item.default == "" for item in modal.inputs)


@pytest.mark.asyncio
async def test_edit_button_blocks_when_the_game_is_no_longer_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction(user_id=1)
    monkeypatch.setattr(
        score_entry.game_service, "get_game", AsyncMock(return_value=_game(status="voided"))
    )
    button = score_entry.ScoreEntryButton(10, 42, 1)

    await button.callback(interaction)

    interaction.response.send_modal.assert_not_awaited()
    assert "no longer accepting scores" in interaction.response.send_message.await_args.args[0]


# ---------------------------------------------------------------------------
# ScoreNumericModal.on_submit
# ---------------------------------------------------------------------------


def _modal(*, current: dict[str, int | None] | None = None) -> score_entry.ScoreNumericModal:
    rules = GameRules("normal", target_points=10)
    return score_entry.ScoreNumericModal(
        guild_id=10, game_id=42, user_id=1, rules=rules, current=current or {}
    )


@pytest.mark.asyncio
async def test_numeric_modal_rejects_a_partially_filled_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction(user_id=1)
    modal = _modal()
    modal.inputs[0]._value = "8"
    modal.inputs[1]._value = ""  # left blank -- not 0, genuinely unset
    modal.inputs[2]._value = "0"
    record = AsyncMock()
    monkeypatch.setattr(score_entry.game_service, "record_player_score", record)

    await modal.on_submit(interaction)

    assert (
        interaction.response.send_message.await_args.args[0] == score_entry._FILL_EVERY_FIELD_TEXT
    )
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_numeric_modal_rejects_an_out_of_range_value(monkeypatch: pytest.MonkeyPatch) -> None:
    interaction = _interaction(user_id=1)
    modal = _modal()
    modal.inputs[0]._value = "100"
    modal.inputs[1]._value = "0"
    modal.inputs[2]._value = "0"
    record = AsyncMock()
    monkeypatch.setattr(score_entry.game_service, "record_player_score", record)

    await modal.on_submit(interaction)

    assert "between 0 and 99" in interaction.response.send_message.await_args.args[0]
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_numeric_modal_submit_preserves_existing_awards_and_refreshes_both_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A numeric-only edit must not silently clear a previously claimed award."""
    interaction = _interaction(user_id=1)
    modal = _modal()
    modal.inputs[0]._value = "8"
    modal.inputs[1]._value = "0"
    modal.inputs[2]._value = "0"
    existing = _score(1, settlements=4, longest_road=2)
    monkeypatch.setattr(
        score_entry.game_service, "get_game", AsyncMock(return_value=_game(scores=(existing,)))
    )
    updated_game = _game(scores=(_score(1, settlements=8, longest_road=2),))
    record = AsyncMock(return_value=updated_game)
    monkeypatch.setattr(score_entry.game_service, "record_player_score", record)
    refresh = AsyncMock()
    monkeypatch.setattr(score_entry, "_refresh_public_message", refresh)

    await modal.on_submit(interaction)

    assert record.await_args.kwargs["numeric"] == {"settlements": 8, "cities": 0, "vp_cards": 0}
    assert record.await_args.kwargs["awards"] == ["longest_road"]
    interaction.response.edit_message.assert_awaited_once()
    refresh.assert_awaited_once_with(interaction.client, 10, updated_game)


@pytest.mark.asyncio
async def test_numeric_modal_submit_with_no_prior_row_claims_no_awards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction(user_id=1)
    modal = _modal()
    modal.inputs[0]._value = "8"
    modal.inputs[1]._value = "0"
    modal.inputs[2]._value = "0"
    monkeypatch.setattr(
        score_entry.game_service, "get_game", AsyncMock(return_value=_game(scores=()))
    )
    updated_game = _game(scores=(_score(1, settlements=8),))
    record = AsyncMock(return_value=updated_game)
    monkeypatch.setattr(score_entry.game_service, "record_player_score", record)
    monkeypatch.setattr(score_entry, "_refresh_public_message", AsyncMock())

    await modal.on_submit(interaction)

    assert record.await_args.kwargs["awards"] == []


@pytest.mark.asyncio
async def test_numeric_modal_exclusive_award_conflict_is_reported_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction(user_id=1)
    modal = _modal()
    modal.inputs[0]._value = "8"
    modal.inputs[1]._value = "0"
    modal.inputs[2]._value = "0"
    existing = _score(1, longest_road=2)
    monkeypatch.setattr(
        score_entry.game_service, "get_game", AsyncMock(return_value=_game(scores=(existing,)))
    )
    monkeypatch.setattr(
        score_entry.game_service,
        "record_player_score",
        AsyncMock(side_effect=DomainValidationError("Only one player can claim Longest Road.")),
    )

    await modal.on_submit(interaction)

    interaction.response.edit_message.assert_not_awaited()
    assert "Longest Road" in interaction.response.send_message.await_args.args[0]


# ---------------------------------------------------------------------------
# ScoreAwardSelect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_award_select_requires_a_numeric_row_first(monkeypatch: pytest.MonkeyPatch) -> None:
    interaction = _interaction(user_id=1)
    monkeypatch.setattr(
        score_entry.game_service, "get_game", AsyncMock(return_value=_game(scores=()))
    )
    record = AsyncMock()
    monkeypatch.setattr(score_entry.game_service, "record_player_score", record)
    select = _award_select(selected=["longest_road"])

    await select.callback(interaction)

    assert (
        interaction.response.send_message.await_args.args[0] == score_entry._ENTER_POINTS_FIRST_TEXT
    )
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_award_select_combines_selection_with_stored_numeric_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction(user_id=1)
    stored = _score(1, settlements=6, longest_road=0)
    monkeypatch.setattr(
        score_entry.game_service, "get_game", AsyncMock(return_value=_game(scores=(stored,)))
    )
    updated_game = _game(scores=(_score(1, settlements=6, longest_road=2),))
    record = AsyncMock(return_value=updated_game)
    monkeypatch.setattr(score_entry.game_service, "record_player_score", record)
    refresh = AsyncMock()
    monkeypatch.setattr(score_entry, "_refresh_public_message", refresh)
    select = _award_select(selected=["longest_road"])

    await select.callback(interaction)

    assert record.await_args.kwargs["numeric"] == {"settlements": 6, "cities": 0, "vp_cards": 0}
    assert record.await_args.kwargs["awards"] == ["longest_road"]
    interaction.response.edit_message.assert_awaited_once()
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_award_select_can_clear_a_previously_claimed_award(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction(user_id=1)
    stored = _score(1, settlements=6, longest_road=2)
    monkeypatch.setattr(
        score_entry.game_service, "get_game", AsyncMock(return_value=_game(scores=(stored,)))
    )
    monkeypatch.setattr(
        score_entry.game_service, "record_player_score", AsyncMock(return_value=_game())
    )
    monkeypatch.setattr(score_entry, "_refresh_public_message", AsyncMock())
    select = _award_select(selected=[])  # deselect everything

    await select.callback(interaction)

    record = score_entry.game_service.record_player_score
    assert record.await_args.kwargs["awards"] == []


@pytest.mark.asyncio
async def test_award_select_exclusive_conflict_routes_through_error_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction(user_id=1)
    stored = _score(1, settlements=6)
    monkeypatch.setattr(
        score_entry.game_service, "get_game", AsyncMock(return_value=_game(scores=(stored,)))
    )
    monkeypatch.setattr(
        score_entry.game_service,
        "record_player_score",
        AsyncMock(side_effect=DomainValidationError("Only one player can claim Longest Road.")),
    )
    select = _award_select(selected=["longest_road"])

    await select.callback(interaction)

    interaction.response.edit_message.assert_not_awaited()
    assert "Longest Road" in interaction.response.send_message.await_args.args[0]


# ---------------------------------------------------------------------------
# ScoreClearButton -- clear, then resubmit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_button_resets_the_row_and_refreshes_both_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction(user_id=1)
    cleared_game = _game(scores=())
    clear = AsyncMock(return_value=cleared_game)
    monkeypatch.setattr(score_entry.game_service, "clear_player_score", clear)
    refresh = AsyncMock()
    monkeypatch.setattr(score_entry, "_refresh_public_message", refresh)
    button = score_entry.ScoreClearButton(10, 42, 1)

    await button.callback(interaction)

    clear.assert_awaited_once()
    interaction.response.edit_message.assert_awaited_once()
    embed = interaction.response.edit_message.await_args.kwargs["embed"]
    assert embed.fields[-1].value == "Not recorded yet"
    refresh.assert_awaited_once_with(interaction.client, 10, cleared_game)


@pytest.mark.asyncio
async def test_clear_then_resubmit_round_trips_through_a_blank_modal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After Clear my points, opening the edit button again shows a fresh,
    genuinely-blank modal -- not the pre-clear values."""
    interaction = _interaction(user_id=1)
    monkeypatch.setattr(
        score_entry.game_service,
        "clear_player_score",
        AsyncMock(return_value=_game(scores=())),
    )
    monkeypatch.setattr(score_entry, "_refresh_public_message", AsyncMock())
    await score_entry.ScoreClearButton(10, 42, 1).callback(interaction)

    reopened = _interaction(user_id=1)
    monkeypatch.setattr(
        score_entry.game_service, "get_game", AsyncMock(return_value=_game(scores=()))
    )
    await score_entry.ScoreEntryButton(10, 42, 1).callback(reopened)

    modal = reopened.response.send_modal.await_args.args[0]
    assert all(item.default == "" for item in modal.inputs)


# ---------------------------------------------------------------------------
# _refresh_public_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_message_refresh_is_a_noop_without_a_recorded_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(pool="pool")
    status_call = AsyncMock()
    monkeypatch.setattr(score_entry.game_service, "score_collection_status", status_call)

    await score_entry._refresh_public_message(client, 10, _game(channel_id=None, message_id=None))

    status_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_message_refresh_edits_a_pending_game_with_the_action_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    client = SimpleNamespace(pool="pool", get_channel=Mock(return_value=channel))
    game = _game(status="pending")
    status = object()
    monkeypatch.setattr(
        score_entry.game_service, "score_collection_status", AsyncMock(return_value=status)
    )
    embed = discord.Embed()
    build_report = Mock(return_value=embed)
    monkeypatch.setattr(score_entry.formatting, "build_game_report_embed", build_report)
    monkeypatch.setattr(score_entry, "build_game_action_view", lambda _game_id: "view-marker")

    await score_entry._refresh_public_message(client, 10, game)

    message.edit.assert_awaited_once()
    assert message.edit.await_args.kwargs["embed"] is embed
    assert message.edit.await_args.kwargs["view"] == "view-marker"
    build_report.assert_called_once_with(game, collection=status)


@pytest.mark.asyncio
async def test_public_message_refresh_of_a_confirmed_game_drops_the_action_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
    client = SimpleNamespace(
        pool="pool",
        get_channel=Mock(return_value=None),
        fetch_channel=AsyncMock(return_value=channel),
    )
    game = _game(status="confirmed")
    monkeypatch.setattr(
        score_entry.game_service, "score_collection_status", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(
        score_entry.formatting, "build_game_status_embed", lambda *_a, **_k: discord.Embed()
    )

    await score_entry._refresh_public_message(client, 10, game)

    client.fetch_channel.assert_awaited_once_with(700)
    message.edit.assert_awaited_once()
    assert message.edit.await_args.kwargs["view"] is None


@pytest.mark.asyncio
async def test_public_message_refresh_swallows_any_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(pool="pool")
    monkeypatch.setattr(
        score_entry.game_service,
        "score_collection_status",
        AsyncMock(side_effect=RuntimeError("db offline")),
    )

    await score_entry._refresh_public_message(client, 10, _game())  # must not raise


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_selected_awards_for_reads_only_claimed_award_keys() -> None:
    rules = GameRules("normal", target_points=10)
    game = _game(scores=(_score(1, longest_road=2),))

    assert score_entry.selected_awards_for(game, 1, rules) == frozenset({"longest_road"})
    assert score_entry.selected_awards_for(game, 2, rules) == frozenset()


def test_build_score_entry_embed_distinguishes_unrecorded_from_recorded() -> None:
    unrecorded = score_entry.build_score_entry_embed(_game(scores=()), 1)
    assert unrecorded.fields[-1].value == "Not recorded yet"

    recorded = score_entry.build_score_entry_embed(_game(scores=(_score(1, settlements=8),)), 1)
    assert recorded.fields[-1].value == "8"
