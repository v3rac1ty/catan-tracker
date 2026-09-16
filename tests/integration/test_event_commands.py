"""Event command adapters, with focused real-database flow coverage."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import asyncpg
import discord
import pytest

from catan_bot import formatting
from catan_bot.cogs import event_cog
from catan_bot.domain.errors import DomainValidationError
from catan_bot.services import config_service, event_service
from catan_bot.services.context import Actor
from catan_bot.services.errors import PermissionDeniedError
from catan_bot.views import event_rsvp

requires_database = pytest.mark.skipif(
    not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
    reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
)

NOW = datetime(2026, 9, 14, 4, 30, tzinfo=UTC)


class InteractionStub:
    def __init__(self, guild_id: int, user_id: int, *, pool: asyncpg.Pool | None = None) -> None:
        self.guild_id = guild_id
        self.channel_id = 700
        self.user = SimpleNamespace(id=user_id)
        permissions = SimpleNamespace(
            view_channel=True,
            send_messages=True,
            send_messages_in_threads=True,
            embed_links=True,
        )
        self.guild = SimpleNamespace(id=guild_id, me=SimpleNamespace(id=999))
        self.channel = SimpleNamespace(
            id=self.channel_id,
            guild=self.guild,
            permissions_for=lambda _member: permissions,
        )
        self.client = SimpleNamespace(pool=pool)
        self.response = SimpleNamespace(defer=AsyncMock())
        self.edit_original_response = AsyncMock(
            return_value=SimpleNamespace(channel=SimpleNamespace(id=700), id=800)
        )


def _actor(user_id: int, *, manager: bool = False) -> Actor:
    return Actor(user_id=user_id, has_manage_guild=manager, role_ids=frozenset())


def _embed_from(interaction: InteractionStub) -> discord.Embed:
    return interaction.edit_original_response.await_args.kwargs["embed"]


def test_event_group_and_option_bounds() -> None:
    group = event_cog.EventCog.__cog_app_commands__[0]
    assert group.name == "event"
    assert {command.name for command in group.commands} == {"create", "list", "cancel"}

    create = event_cog.EventCog.event_group.get_command("create")
    listing = event_cog.EventCog.event_group.get_command("list")
    cancel = event_cog.EventCog.event_group.get_command("cancel")
    assert create is not None and listing is not None and cancel is not None
    create_options = {parameter.name: parameter for parameter in create.parameters}
    assert (create_options["title"].min_value, create_options["title"].max_value) == (1, 100)
    assert (create_options["time"].min_value, create_options["time"].max_value) == (1, 16)
    assert (create_options["date"].min_value, create_options["date"].max_value) == (1, 32)
    assert (create_options["location"].min_value, create_options["location"].max_value) == (
        1,
        200,
    )
    assert (
        create_options["description"].min_value,
        create_options["description"].max_value,
    ) == (1, 1000)
    list_option = listing.parameters[0]
    assert (list_option.min_value, list_option.max_value) == (1, 10)
    cancel_option = cancel.parameters[0]
    assert (cancel_option.min_value, cancel_option.max_value) == (1, 2**53 - 1)


@pytest.mark.asyncio
async def test_create_defers_before_service_and_records_returned_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = InteractionStub(123, 456)
    pool = object()
    cog = event_cog.EventCog(SimpleNamespace(pool=pool))
    actor = _actor(456)
    event = SimpleNamespace(event_id=42)
    created = SimpleNamespace(event=event, player_role_id=None)
    create_event = AsyncMock()

    async def create(*args: object, **kwargs: object) -> object:
        interaction.response.defer.assert_awaited_once_with(thinking=True)
        return created

    create_event.side_effect = create
    record = AsyncMock()
    monkeypatch.setattr(event_cog, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(
        event_cog.event_service, "create_event_with_notification_role", create_event
    )
    monkeypatch.setattr(event_cog.event_service, "record_event_message", record, raising=False)
    monkeypatch.setattr(event_cog.formatting, "build_event_embed", lambda _: discord.Embed())
    command = event_cog.EventCog.event_group.get_command("create")
    assert command is not None

    await command.callback(cog, interaction, "Game Night", "19:00", None, None, None)

    assert create_event.await_args.args[:3] == (pool, 123, actor)
    assert create_event.await_args.kwargs["channel_id"] == 700
    record.assert_awaited_once_with(pool, 123, 42, 700, 800)
    edited = interaction.edit_original_response.await_args
    assert edited.kwargs["view"].timeout is None
    assert edited.kwargs["allowed_mentions"].everyone is False


@pytest.mark.asyncio
async def test_create_to_selected_channel_pings_only_snapshot_role_and_confirms_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = InteractionStub(123, 456)
    pool = object()
    cog = event_cog.EventCog(SimpleNamespace(pool=pool))
    actor = _actor(456)
    event = SimpleNamespace(event_id=42)
    create = AsyncMock(return_value=SimpleNamespace(event=event, player_role_id=44))
    target = SimpleNamespace(
        id=701,
        guild=interaction.guild,
        permissions_for=interaction.channel.permissions_for,
        send=AsyncMock(
            return_value=SimpleNamespace(
                channel=SimpleNamespace(id=701), id=801, jump_url="https://discord.test/jump"
            )
        ),
    )
    record = AsyncMock()
    monkeypatch.setattr(event_cog, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(event_cog.event_service, "create_event_with_notification_role", create)
    monkeypatch.setattr(event_cog.event_service, "record_event_message", record)
    monkeypatch.setattr(event_cog.formatting, "build_event_embed", lambda _: discord.Embed())
    command = event_cog.EventCog.event_group.get_command("create")
    assert command is not None

    await command.callback(cog, interaction, "Game Night", "19:00", None, None, None, target)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert create.await_args.kwargs["channel_id"] == 701
    posted = target.send.await_args
    assert posted.kwargs["content"] == "<@&44>"
    allowed = posted.kwargs["allowed_mentions"]
    assert allowed.everyone is False and allowed.users is False
    assert [role.id for role in allowed.roles] == [44]
    record.assert_awaited_once_with(pool, 123, 42, 701, 801)
    confirmation = interaction.edit_original_response.await_args.kwargs["content"]
    assert "<#701>" in confirmation and "https://discord.test/jump" in confirmation


@pytest.mark.asyncio
async def test_selected_channel_records_message_before_acknowledgement_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = InteractionStub(123, 456)
    pool = object()
    cog = event_cog.EventCog(SimpleNamespace(pool=pool))
    event = SimpleNamespace(event_id=42)
    target = SimpleNamespace(
        id=701,
        guild=interaction.guild,
        permissions_for=interaction.channel.permissions_for,
        send=AsyncMock(
            return_value=SimpleNamespace(
                channel=SimpleNamespace(id=701), id=801, jump_url="https://discord.test/jump"
            )
        ),
    )
    order: list[str] = []
    record = AsyncMock(side_effect=lambda *_args: order.append("record"))

    async def fail_ack(**_kwargs: object) -> None:
        order.append("ack")
        raise RuntimeError("ack failed")

    interaction.edit_original_response.side_effect = fail_ack
    monkeypatch.setattr(event_cog, "actor_from_interaction", lambda _: _actor(456))
    monkeypatch.setattr(
        event_cog.event_service,
        "create_event_with_notification_role",
        AsyncMock(return_value=SimpleNamespace(event=event, player_role_id=None)),
    )
    monkeypatch.setattr(event_cog.event_service, "record_event_message", record)
    monkeypatch.setattr(event_cog.formatting, "build_event_embed", lambda _: discord.Embed())
    command = event_cog.EventCog.event_group.get_command("create")
    assert command is not None

    with pytest.raises(RuntimeError, match="ack failed"):
        await command.callback(cog, interaction, "Game Night", "19:00", None, None, None, target)

    assert order == ["record", "ack"]
    record.assert_awaited_once_with(pool, 123, 42, 701, 801)


@pytest.mark.asyncio
async def test_create_rejects_unusable_target_before_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = InteractionStub(123, 456)
    interaction.channel.permissions_for = lambda _member: SimpleNamespace(
        view_channel=True,
        send_messages=False,
        send_messages_in_threads=False,
        embed_links=True,
    )
    cog = event_cog.EventCog(SimpleNamespace(pool=object()))
    create = AsyncMock()
    monkeypatch.setattr(event_cog, "actor_from_interaction", lambda _: _actor(456))
    monkeypatch.setattr(event_cog.event_service, "create_event_with_notification_role", create)
    command = event_cog.EventCog.event_group.get_command("create")
    assert command is not None

    with pytest.raises(DomainValidationError, match="Send Messages"):
        await command.callback(cog, interaction, "Game Night", "19:00", None, None, None)

    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_rejects_missing_configured_notification_role_before_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = InteractionStub(123, 456)
    interaction.guild.get_role = lambda _role_id: None
    cog = event_cog.EventCog(SimpleNamespace(pool=object()))
    create = AsyncMock()
    monkeypatch.setattr(event_cog, "actor_from_interaction", lambda _: _actor(456))
    monkeypatch.setattr(
        event_cog.config_service,
        "get_config",
        AsyncMock(return_value=SimpleNamespace(player_role_id=44)),
    )
    monkeypatch.setattr(event_cog.event_service, "create_event_with_notification_role", create)
    command = event_cog.EventCog.event_group.get_command("create")
    assert command is not None

    with pytest.raises(DomainValidationError, match="no longer exists"):
        await command.callback(cog, interaction, "Game Night", "19:00", None, None, None)

    interaction.response.defer.assert_awaited_once_with(thinking=True)
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_race_publishes_unpinged_and_records_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = InteractionStub(123, 456)
    role = SimpleNamespace(id=44, mentionable=True, is_default=lambda: False)
    role_lookups = 0

    def get_role(_role_id: int) -> object | None:
        nonlocal role_lookups
        role_lookups += 1
        return role if role_lookups == 1 else None

    interaction.guild.get_role = get_role
    pool = object()
    cog = event_cog.EventCog(SimpleNamespace(pool=pool))
    event = SimpleNamespace(event_id=42)
    target = SimpleNamespace(
        id=701,
        guild=interaction.guild,
        permissions_for=interaction.channel.permissions_for,
        send=AsyncMock(
            return_value=SimpleNamespace(
                channel=SimpleNamespace(id=701), id=801, jump_url="https://discord.test/jump"
            )
        ),
    )
    record = AsyncMock()
    monkeypatch.setattr(event_cog, "actor_from_interaction", lambda _: _actor(456))
    monkeypatch.setattr(
        event_cog.config_service,
        "get_config",
        AsyncMock(return_value=SimpleNamespace(player_role_id=44)),
    )
    monkeypatch.setattr(
        event_cog.event_service,
        "create_event_with_notification_role",
        AsyncMock(return_value=SimpleNamespace(event=event, player_role_id=44)),
    )
    monkeypatch.setattr(event_cog.event_service, "record_event_message", record)
    monkeypatch.setattr(event_cog.formatting, "build_event_embed", lambda _: discord.Embed())
    command = event_cog.EventCog.event_group.get_command("create")
    assert command is not None

    await command.callback(cog, interaction, "Game Night", "19:00", None, None, None, target)

    assert target.send.await_args.kwargs["content"] is None
    assert "ping was skipped" in interaction.edit_original_response.await_args.kwargs["content"]
    record.assert_awaited_once_with(pool, 123, 42, 701, 801)


@pytest.mark.asyncio
@requires_database
async def test_event_command_rsvp_cancel_and_guild_isolation_flow(
    pool: asyncpg.Pool,
    guild_id: int,
    other_guild_id: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return NOW if tz is not None else NOW.replace(tzinfo=None)

    creator = _actor(10, manager=True)
    attendee = _actor(20)
    await config_service.set_timezone(pool, guild_id, creator, "America/Chicago")
    monkeypatch.setattr(event_cog, "datetime", FixedDatetime)
    monkeypatch.setattr(event_cog, "actor_from_interaction", lambda _: creator)
    interaction = InteractionStub(guild_id, creator.user_id, pool=pool)
    cog = event_cog.EventCog(SimpleNamespace(pool=pool))
    create = event_cog.EventCog.event_group.get_command("create")
    assert create is not None
    title = "'; DROP TABLE events;-- @everyone **Night**"
    location = "<#12345678901234567> -- home"
    description = "Bring snacks\n@everyone"

    await create.callback(
        cog,
        interaction,
        title,
        "23:59",
        None,
        location,
        description,
    )

    created_embed = _embed_from(interaction)
    event_id = int((created_embed.footer.text or "").removeprefix("Event #"))
    stored = await event_service.get_event(pool, guild_id, event_id)
    assert stored is not None
    assert stored.title == title
    assert stored.location == location
    assert stored.description == description
    assert stored.starts_at.astimezone(ZoneInfo("America/Chicago")).date().isoformat() == (
        "2026-09-13"
    )
    assert stored.channel_id == 700
    assert stored.message_id == 800
    assert title not in str(created_embed.to_dict())
    assert formatting.escape_user_text(title) in (created_embed.title or "")

    monkeypatch.setattr(event_rsvp, "actor_from_interaction", lambda _: attendee)
    going_click = InteractionStub(guild_id, attendee.user_id, pool=pool)
    await event_rsvp.EventRsvpButton(event_id, "going").callback(going_click)
    going_counts = next(
        field.value for field in _embed_from(going_click).fields if field.name == "Going (1)"
    )
    assert going_counts == "<@20>"

    no_click = InteractionStub(guild_id, attendee.user_id, pool=pool)
    await event_rsvp.EventRsvpButton(event_id, "no").callback(no_click)
    changed_counts = next(
        field.value for field in _embed_from(no_click).fields if field.name == "Not Going (1)"
    )
    assert changed_counts == "<@20>"

    other_interaction = InteractionStub(other_guild_id, 99, pool=pool)
    listing = event_cog.EventCog.event_group.get_command("list")
    assert listing is not None
    await listing.callback(cog, other_interaction, 10)
    assert _embed_from(other_interaction).description == "No game nights are scheduled."

    cancel = event_cog.EventCog.event_group.get_command("cancel")
    assert cancel is not None
    monkeypatch.setattr(event_cog, "actor_from_interaction", lambda _: _actor(30))
    denied_interaction = InteractionStub(guild_id, 30, pool=pool)
    with pytest.raises(PermissionDeniedError):
        await cancel.callback(cog, denied_interaction, event_id)

    monkeypatch.setattr(event_cog, "actor_from_interaction", lambda _: creator)
    cancel_interaction = InteractionStub(guild_id, creator.user_id, pool=pool)
    await cancel.callback(cog, cancel_interaction, event_id)
    status = next(
        field.value for field in _embed_from(cancel_interaction).fields if field.name == "Status"
    )
    assert status == "Cancelled"
