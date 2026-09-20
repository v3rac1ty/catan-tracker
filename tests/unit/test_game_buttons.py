from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from catan_bot.services.context import Actor
from catan_bot.views import game_confirm


def _interaction() -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=123,
        client=SimpleNamespace(pool="pool"),
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def test_game_action_view_is_persistent_and_has_stable_ids() -> None:
    view = game_confirm.build_game_action_view(42)

    assert view.timeout is None
    assert [item.item.custom_id for item in view.children] == [
        "game:confirm:42",
        "game:reject:42",
        "game:nudge:42",
    ]


@pytest.mark.parametrize("game_id", [None, 0, -1, True, 2**63])
def test_game_action_view_rejects_invalid_ids(game_id: object) -> None:
    with pytest.raises(ValueError, match="positive BIGINT"):
        game_confirm.build_game_action_view(game_id)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["confirm", "reject"])
async def test_dynamic_button_reconstructs_after_restart(action: str) -> None:
    match = game_confirm.GameActionButton.__discord_ui_compiled_template__.fullmatch(
        f"game:{action}:987"
    )
    assert match is not None
    raw_item = discord.ui.Button(custom_id=f"game:{action}:987")

    rebuilt = await game_confirm.GameActionButton.from_custom_id(SimpleNamespace(), raw_item, match)

    assert rebuilt.game_id == 987
    assert rebuilt.action == action
    assert rebuilt.item.custom_id == f"game:{action}:987"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "service_name"), [("confirm", "confirm_game"), ("reject", "reject_game")]
)
async def test_button_defers_then_updates_original_message(
    monkeypatch: pytest.MonkeyPatch, action: str, service_name: str
) -> None:
    interaction = _interaction()
    actor = Actor(user_id=456, has_manage_guild=False, role_ids=frozenset())
    updated = object()
    service = AsyncMock()

    async def transition(*args: object) -> object:
        interaction.response.defer.assert_awaited_once_with()
        return updated

    service.side_effect = transition
    monkeypatch.setattr(game_confirm, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(game_confirm.game_service, service_name, service)
    monkeypatch.setattr(
        game_confirm.formatting, "build_game_status_embed", lambda _: discord.Embed(title="Done")
    )
    button = game_confirm.GameActionButton(42, action)  # type: ignore[arg-type]

    await button.callback(interaction)

    service.assert_awaited_once_with("pool", 123, 42, actor)
    interaction.edit_original_response.assert_awaited_once()
    edited = interaction.edit_original_response.await_args
    assert edited.kwargs["view"] is None
    allowed = edited.kwargs["allowed_mentions"]
    assert allowed.everyone is False
    assert allowed.users is False
    assert allowed.roles is False


@pytest.mark.asyncio
async def test_out_of_range_dynamic_id_uses_shared_error_handler_without_database_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    actor = Actor(user_id=456, has_manage_guild=False, role_ids=frozenset())
    handler = AsyncMock()
    confirm = AsyncMock()
    monkeypatch.setattr(game_confirm, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(game_confirm, "handle_interaction_error", handler)
    monkeypatch.setattr(game_confirm.game_service, "confirm_game", confirm)
    button = game_confirm.GameActionButton(2**63, "confirm")

    await button.callback(interaction)

    confirm.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()
    handler.assert_awaited_once()
    assert isinstance(handler.await_args.args[1], ValueError)
    assert handler.await_args.kwargs == {"command_name": "game:confirm"}


@pytest.mark.asyncio
async def test_button_edit_failure_uses_shared_error_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    interaction.edit_original_response.side_effect = discord.HTTPException(
        SimpleNamespace(status=500, reason="Server Error"), "failed"
    )
    actor = Actor(user_id=456, has_manage_guild=False, role_ids=frozenset())
    handler = AsyncMock()
    monkeypatch.setattr(game_confirm, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(game_confirm, "handle_interaction_error", handler)
    monkeypatch.setattr(game_confirm.game_service, "confirm_game", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        game_confirm.formatting, "build_game_status_embed", lambda _: discord.Embed(title="Done")
    )

    await game_confirm.GameActionButton(42, "confirm").callback(interaction)

    handler.assert_awaited_once()
    assert isinstance(handler.await_args.args[1], discord.HTTPException)


# ---------------------------------------------------------------------------
# GameNudgeButton (Phase 2)
# ---------------------------------------------------------------------------


def _nudge_interaction(*, guild_id: int = 123, user_id: int = 456) -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=guild_id,
        user=SimpleNamespace(id=user_id),
        client=SimpleNamespace(pool="pool"),
        response=SimpleNamespace(send_message=AsyncMock()),
    )


def _status(
    *,
    winner_id: int = 1,
    loser_ids: tuple[int, ...] = (2, 3),
    reported_by: int = 1,
    outstanding: tuple[int, ...] = (2, 3),
) -> SimpleNamespace:
    game = SimpleNamespace(reported_by=reported_by)
    inner = SimpleNamespace(winner_id=winner_id, loser_ids=loser_ids, game=game)
    return SimpleNamespace(game=inner, outstanding_ids=outstanding)


@pytest.mark.asyncio
async def test_nudge_button_custom_id_round_trip() -> None:
    match = game_confirm.GameNudgeButton.__discord_ui_compiled_template__.fullmatch(
        "game:nudge:987"
    )
    assert match is not None
    raw_item = discord.ui.Button(custom_id="game:nudge:987")

    rebuilt = await game_confirm.GameNudgeButton.from_custom_id(SimpleNamespace(), raw_item, match)

    assert rebuilt.game_id == 987
    assert rebuilt.item.custom_id == "game:nudge:987"


@pytest.mark.asyncio
async def test_nudge_button_pings_only_outstanding_players_with_scoped_mentions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(game_confirm, "_last_nudge_at", {})
    interaction = _nudge_interaction(user_id=1)  # the reporter, not a loser
    actor = Actor(user_id=1, has_manage_guild=False, role_ids=frozenset())
    monkeypatch.setattr(game_confirm, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(
        game_confirm.game_service, "score_collection_status", AsyncMock(return_value=_status())
    )

    await game_confirm.GameNudgeButton(42).callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    content = interaction.response.send_message.await_args.args[0]
    assert "<@2>" in content
    assert "<@3>" in content
    allowed = interaction.response.send_message.await_args.kwargs["allowed_mentions"]
    assert allowed.everyone is False
    assert allowed.roles is False
    assert {user.id for user in allowed.users} == {2, 3}


@pytest.mark.asyncio
async def test_nudge_button_rejects_a_non_participant_non_reporter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(game_confirm, "_last_nudge_at", {})
    interaction = _nudge_interaction(user_id=999)
    actor = Actor(user_id=999, has_manage_guild=False, role_ids=frozenset())
    monkeypatch.setattr(game_confirm, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(
        game_confirm.game_service, "score_collection_status", AsyncMock(return_value=_status())
    )
    handler = AsyncMock()
    monkeypatch.setattr(game_confirm, "handle_interaction_error", handler)

    await game_confirm.GameNudgeButton(42).callback(interaction)

    interaction.response.send_message.assert_not_awaited()
    handler.assert_awaited_once()
    assert handler.await_args.kwargs == {"command_name": "game:nudge"}


@pytest.mark.asyncio
async def test_nudge_button_reports_when_everyone_already_submitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(game_confirm, "_last_nudge_at", {})
    interaction = _nudge_interaction(user_id=1)
    actor = Actor(user_id=1, has_manage_guild=False, role_ids=frozenset())
    monkeypatch.setattr(game_confirm, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(
        game_confirm.game_service,
        "score_collection_status",
        AsyncMock(return_value=_status(outstanding=())),
    )

    await game_confirm.GameNudgeButton(42).callback(interaction)

    assert "already submitted" in interaction.response.send_message.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "service_name"), [("confirm", "confirm_game"), ("reject", "reject_game")]
)
async def test_leaderboard_post_fires_only_on_confirm_not_reject(
    monkeypatch: pytest.MonkeyPatch, action: str, service_name: str
) -> None:
    interaction = _interaction()
    actor = Actor(user_id=456, has_manage_guild=False, role_ids=frozenset())
    monkeypatch.setattr(game_confirm, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(game_confirm.game_service, service_name, AsyncMock(return_value=object()))
    monkeypatch.setattr(
        game_confirm.formatting, "build_game_status_embed", lambda _: discord.Embed(title="Done")
    )
    post_leaderboard = AsyncMock()
    monkeypatch.setattr(game_confirm, "_post_leaderboard_after_confirm", post_leaderboard)

    await game_confirm.GameActionButton(42, action).callback(interaction)  # type: ignore[arg-type]

    if action == "confirm":
        post_leaderboard.assert_awaited_once_with(interaction.client, 123)
    else:
        post_leaderboard.assert_not_awaited()


# ---------------------------------------------------------------------------
# _post_leaderboard_after_confirm (Phase 3): best-effort, per_game-mode only.
# ---------------------------------------------------------------------------


def _client(*, get_channel_result: object = None, fetch_result: object = None) -> SimpleNamespace:
    return SimpleNamespace(
        pool="pool",
        get_channel=lambda _cid: get_channel_result,
        fetch_channel=AsyncMock(return_value=fetch_result),
    )


def _post(channel_id: int = 700) -> SimpleNamespace:
    return SimpleNamespace(channel_id=channel_id, guild_id=123)


@pytest.mark.asyncio
async def test_post_leaderboard_after_confirm_does_nothing_when_mode_off_or_no_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    monkeypatch.setattr(
        game_confirm.leaderboard_service, "leaderboard_after_game", AsyncMock(return_value=None)
    )
    called = Mock()
    monkeypatch.setattr(game_confirm.formatting, "build_leaderboard_post_embed", called)

    await game_confirm._post_leaderboard_after_confirm(client, 123)

    called.assert_not_called()


@pytest.mark.asyncio
async def test_post_leaderboard_after_confirm_sends_to_cached_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = _post(700)
    channel = SimpleNamespace(send=AsyncMock())
    client = _client(get_channel_result=channel)
    monkeypatch.setattr(
        game_confirm.leaderboard_service, "leaderboard_after_game", AsyncMock(return_value=post)
    )
    embed = discord.Embed(title="Board")
    monkeypatch.setattr(game_confirm.formatting, "build_leaderboard_post_embed", lambda _p: embed)

    await game_confirm._post_leaderboard_after_confirm(client, 123)

    channel.send.assert_awaited_once()
    assert channel.send.await_args.kwargs["embed"] is embed
    assert channel.send.await_args.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    client.fetch_channel.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_leaderboard_after_confirm_falls_back_to_fetch_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = _post(700)
    channel = SimpleNamespace(send=AsyncMock())
    client = _client(get_channel_result=None, fetch_result=channel)
    monkeypatch.setattr(
        game_confirm.leaderboard_service, "leaderboard_after_game", AsyncMock(return_value=post)
    )
    monkeypatch.setattr(
        game_confirm.formatting, "build_leaderboard_post_embed", lambda _p: discord.Embed()
    )

    await game_confirm._post_leaderboard_after_confirm(client, 123)

    client.fetch_channel.assert_awaited_once_with(700)
    channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_leaderboard_after_confirm_swallows_every_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    monkeypatch.setattr(
        game_confirm.leaderboard_service,
        "leaderboard_after_game",
        AsyncMock(side_effect=RuntimeError("boom")),
    )

    await game_confirm._post_leaderboard_after_confirm(client, 123)  # must not raise


@pytest.mark.asyncio
async def test_post_leaderboard_after_confirm_swallows_send_failure_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post = _post(700)
    channel = SimpleNamespace(
        send=AsyncMock(
            side_effect=discord.HTTPException(SimpleNamespace(status=500, reason="err"), "err")
        )
    )
    client = _client(get_channel_result=channel)
    monkeypatch.setattr(
        game_confirm.leaderboard_service, "leaderboard_after_game", AsyncMock(return_value=post)
    )
    monkeypatch.setattr(
        game_confirm.formatting, "build_leaderboard_post_embed", lambda _p: discord.Embed()
    )

    await game_confirm._post_leaderboard_after_confirm(client, 123)  # must not raise


@pytest.mark.asyncio
async def test_nudge_button_is_rate_limited_per_game(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(game_confirm, "_last_nudge_at", {})
    actor = Actor(user_id=1, has_manage_guild=False, role_ids=frozenset())
    monkeypatch.setattr(game_confirm, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(
        game_confirm.game_service, "score_collection_status", AsyncMock(return_value=_status())
    )

    await game_confirm.GameNudgeButton(42).callback(_nudge_interaction(user_id=1))
    second = _nudge_interaction(user_id=1)
    await game_confirm.GameNudgeButton(42).callback(second)

    assert "try again in" in second.response.send_message.await_args.args[0]
