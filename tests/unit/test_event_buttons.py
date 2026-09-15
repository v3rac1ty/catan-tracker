from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from catan_bot.services.context import Actor
from catan_bot.views import event_rsvp


def _interaction() -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=123,
        client=SimpleNamespace(pool="pool"),
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def test_event_rsvp_view_is_persistent_with_stable_ids() -> None:
    view = event_rsvp.build_event_rsvp_view(42)

    assert view.timeout is None
    assert [item.item.custom_id for item in view.children] == [
        "rsvp:going:42",
        "rsvp:maybe:42",
        "rsvp:no:42",
    ]


@pytest.mark.parametrize("event_id", [None, 0, -1, True, 2**63])
def test_event_rsvp_view_rejects_invalid_ids(event_id: object) -> None:
    with pytest.raises(ValueError, match="positive BIGINT"):
        event_rsvp.build_event_rsvp_view(event_id)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["going", "maybe", "no"])
async def test_dynamic_rsvp_reconstructs_after_restart(response: str) -> None:
    match = event_rsvp.EventRsvpButton.__discord_ui_compiled_template__.fullmatch(
        f"rsvp:{response}:987"
    )
    assert match is not None

    rebuilt = await event_rsvp.EventRsvpButton.from_custom_id(
        SimpleNamespace(), discord.ui.Button(custom_id=f"rsvp:{response}:987"), match
    )

    assert rebuilt.event_id == 987
    assert rebuilt.response == response
    assert rebuilt.item.custom_id == f"rsvp:{response}:987"


@pytest.mark.asyncio
async def test_not_going_maps_to_service_value_and_keeps_buttons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    actor = Actor(user_id=456, has_manage_guild=False, role_ids=frozenset())
    counts = object()
    event = SimpleNamespace(status="scheduled")
    rsvp = AsyncMock(return_value=counts)
    get_event = AsyncMock(return_value=event)
    monkeypatch.setattr(event_rsvp, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(event_rsvp.event_service, "rsvp", rsvp)
    monkeypatch.setattr(event_rsvp.event_service, "get_event", get_event, raising=False)
    monkeypatch.setattr(event_rsvp.formatting, "build_event_embed", lambda *_: discord.Embed())

    await event_rsvp.EventRsvpButton(42, "no").callback(interaction)

    interaction.response.defer.assert_awaited_once_with()
    rsvp.assert_awaited_once_with("pool", 123, 42, actor, "not_going")
    get_event.assert_awaited_once_with("pool", 123, 42)
    edited = interaction.edit_original_response.await_args
    assert [item.item.custom_id for item in edited.kwargs["view"].children] == [
        "rsvp:going:42",
        "rsvp:maybe:42",
        "rsvp:no:42",
    ]
    assert edited.kwargs["allowed_mentions"].everyone is False


@pytest.mark.asyncio
async def test_cancelled_event_removes_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    interaction = _interaction()
    actor = Actor(user_id=456, has_manage_guild=False, role_ids=frozenset())
    monkeypatch.setattr(event_rsvp, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(event_rsvp.event_service, "rsvp", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        event_rsvp.event_service,
        "get_event",
        AsyncMock(return_value=SimpleNamespace(status="cancelled")),
        raising=False,
    )
    monkeypatch.setattr(event_rsvp.formatting, "build_event_embed", lambda *_: discord.Embed())

    await event_rsvp.EventRsvpButton(42, "going").callback(interaction)

    assert interaction.edit_original_response.await_args.kwargs["view"] is None


@pytest.mark.asyncio
async def test_out_of_range_event_id_uses_shared_error_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    actor = Actor(user_id=456, has_manage_guild=False, role_ids=frozenset())
    handler = AsyncMock()
    rsvp = AsyncMock()
    monkeypatch.setattr(event_rsvp, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(event_rsvp, "handle_interaction_error", handler)
    monkeypatch.setattr(event_rsvp.event_service, "rsvp", rsvp)

    await event_rsvp.EventRsvpButton(2**63, "going").callback(interaction)

    rsvp.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()
    handler.assert_awaited_once()
    assert isinstance(handler.await_args.args[1], ValueError)


@pytest.mark.asyncio
async def test_edit_failure_uses_shared_error_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    interaction = _interaction()
    interaction.edit_original_response.side_effect = RuntimeError("Discord edit failed")
    actor = Actor(user_id=456, has_manage_guild=False, role_ids=frozenset())
    handler = AsyncMock()
    monkeypatch.setattr(event_rsvp, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(event_rsvp, "handle_interaction_error", handler)
    monkeypatch.setattr(event_rsvp.event_service, "rsvp", AsyncMock(return_value=object()))
    monkeypatch.setattr(
        event_rsvp.event_service,
        "get_event",
        AsyncMock(return_value=SimpleNamespace(status="scheduled")),
        raising=False,
    )
    monkeypatch.setattr(event_rsvp.formatting, "build_event_embed", lambda *_: discord.Embed())

    await event_rsvp.EventRsvpButton(42, "going").callback(interaction)

    handler.assert_awaited_once()
    assert isinstance(handler.await_args.args[1], RuntimeError)
