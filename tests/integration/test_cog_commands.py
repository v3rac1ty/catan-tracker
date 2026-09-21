"""M4 interaction adapters exercised against the real test database."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import discord
import pytest

from catan_bot import formatting
from catan_bot.cogs import config_cog, game_cog, season_cog, stats_cog
from catan_bot.db.repositories import score_requests
from catan_bot.domain.errors import DomainValidationError
from catan_bot.services import config_service, game_service, season_service
from catan_bot.services.context import Actor
from catan_bot.services.errors import PermissionDeniedError
from catan_bot.views import game_confirm

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (os.environ.get("TEST_DATABASE_URL") and os.environ.get("TEST_MIGRATOR_DATABASE_URL")),
        reason="TEST_DATABASE_URL / TEST_MIGRATOR_DATABASE_URL not set",
    ),
]


class InteractionStub:
    def __init__(self, guild_id: int, user_id: int, *, pool: asyncpg.Pool | None = None) -> None:
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=user_id)
        self.client = SimpleNamespace(pool=pool)
        self.channel = SimpleNamespace(id=700)
        self.channel.send = AsyncMock(
            return_value=SimpleNamespace(
                channel=self.channel,
                id=801,
                jump_url="https://discord.com/channels/900001/700/801",
                # `/game report` refreshes this message with the live
                # score-collection progress field once its DM fan-out
                # finishes (`GameCog.report_command`'s `public_message.edit`
                # call) -- without this the flow below blows up on a plain
                # `SimpleNamespace` the same way it does on a member stub
                # with no `send`.
                edit=AsyncMock(),
            )
        )
        self.response = SimpleNamespace(defer=AsyncMock())
        self.followup = SimpleNamespace(send=AsyncMock())
        self.edit_original_response = AsyncMock(
            return_value=SimpleNamespace(channel=SimpleNamespace(id=700), id=800)
        )


def _actor(user_id: int, *, manager: bool = False, roles: frozenset[int] = frozenset()) -> Actor:
    return Actor(user_id=user_id, has_manage_guild=manager, role_ids=roles)


def _member_stub(user_id: int, *, dm_channel_id: int, dm_message_id: int) -> SimpleNamespace:
    """A `discord.Member` stand-in that can receive `/game report`'s per-player DM.

    `dm_channel_id`/`dm_message_id` are distinct per member (and from the
    public message's own `700`/`801`) so a delivery-recording bug that
    mixes up whose ids are whose can't accidentally pass.
    """
    return SimpleNamespace(
        id=user_id,
        bot=False,
        send=AsyncMock(
            return_value=SimpleNamespace(
                channel=SimpleNamespace(id=dm_channel_id), id=dm_message_id
            )
        ),
    )


def _embed_from(interaction: InteractionStub) -> discord.Embed:
    return interaction.edit_original_response.await_args.kwargs["embed"]


async def test_season_command_escapes_payload_and_config_show_requires_manage_guild(
    pool: asyncpg.Pool, guild_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = "'; DROP TABLE seasons;-- @everyone **Fall**"
    manager = _actor(1, manager=True)
    monkeypatch.setattr(season_cog, "actor_from_interaction", lambda _: manager)
    interaction = InteractionStub(guild_id, manager.user_id)
    cog = season_cog.SeasonCog(SimpleNamespace(pool=pool))
    command = season_cog.SeasonCog.season_group.get_command("start")
    assert command is not None
    end_date = (datetime.now(UTC).date() + timedelta(days=30)).isoformat()

    await command.callback(cog, interaction, payload, end_date, None, 2)

    embed = _embed_from(interaction)
    assert formatting.escape_user_text(payload) in (embed.title or "")
    assert payload not in (embed.title or "")
    interaction.response.defer.assert_awaited_once_with(thinking=True)

    await config_service.set_admin_role(pool, guild_id, manager, role_id=42)
    role_admin = _actor(2, roles=frozenset({42}))
    monkeypatch.setattr(config_cog, "actor_from_interaction", lambda _: role_admin)
    show_interaction = InteractionStub(guild_id, role_admin.user_id)
    show_cog = config_cog.ConfigCog(SimpleNamespace(pool=pool))

    with pytest.raises(PermissionDeniedError, match="Manage Server"):
        await config_cog.ConfigCog.config_group.get_command("show").callback(
            show_cog, show_interaction
        )
    show_interaction.response.defer.assert_not_awaited()


async def test_game_commands_buttons_and_stats_complete_the_interaction_flow(
    pool: asyncpg.Pool, guild_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    reporter = _actor(10)
    other_player = _actor(20)
    report_interaction = InteractionStub(guild_id, reporter.user_id)
    game_cog_instance = game_cog.GameCog(SimpleNamespace(pool=pool))
    monkeypatch.setattr(game_cog, "actor_from_interaction", lambda _: reporter)
    report = game_cog.GameCog.game_group.get_command("report")
    assert report is not None

    reporter_member = _member_stub(reporter.user_id, dm_channel_id=910, dm_message_id=911)
    other_member = _member_stub(other_player.user_id, dm_channel_id=920, dm_message_id=921)

    await report.callback(
        game_cog_instance,
        report_interaction,
        reporter_member,
        other_member,
        None,
        None,
        None,
        None,
        None,
    )

    # The pending game is created immediately -- there's no reporter-facing
    # Submit click to wait on any more. Its public message carries the
    # Confirm/Reject/Nudge buttons, and every participant (winner included)
    # separately got their own DM score sheet.
    report_interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    report_interaction.channel.send.assert_awaited_once()
    reporter_member.send.assert_awaited_once()
    other_member.send.assert_awaited_once()

    game_id = await game_service.find_open_score_request_game_id(pool, guild_id, reporter.user_id)
    assert game_id is not None

    # Delivery was actually recorded -- the real DM channel/message ids,
    # not the `pending` placeholder `open_score_collection` seeds -- for
    # both participants, each against their own DM's ids.
    async with pool.acquire() as conn:
        requests = await score_requests.list_score_requests(conn, guild_id, game_id)
    delivered = {request.user_id: request for request in requests}
    assert delivered.keys() == {reporter.user_id, other_player.user_id}
    assert delivered[reporter.user_id].delivery_status == "delivered"
    assert delivered[reporter.user_id].dm_channel_id == 910
    assert delivered[reporter.user_id].dm_message_id == 911
    assert delivered[other_player.user_id].delivery_status == "delivered"
    assert delivered[other_player.user_id].dm_channel_id == 920
    assert delivered[other_player.user_id].dm_message_id == 921

    # The public message is refreshed with the live score-collection
    # progress field once the DM fan-out finishes.
    public_message = report_interaction.channel.send.return_value
    public_message.edit.assert_awaited_once()
    refreshed_embed = public_message.edit.await_args.kwargs["embed"]
    progress = next(field.value for field in refreshed_embed.fields if field.name == "Score entry")
    assert progress.startswith("0 of 2 received")

    error_handler = AsyncMock()
    monkeypatch.setattr(game_confirm, "actor_from_interaction", lambda _: reporter)
    monkeypatch.setattr(game_confirm, "handle_interaction_error", error_handler)
    reporter_click = InteractionStub(guild_id, reporter.user_id, pool=pool)
    await game_confirm.GameActionButton(game_id, "confirm").callback(reporter_click)

    error_handler.assert_awaited_once()
    denied = error_handler.await_args.args[1]
    assert isinstance(denied, PermissionDeniedError)
    assert "another player" in denied.user_message

    monkeypatch.setattr(game_confirm, "actor_from_interaction", lambda _: other_player)
    confirmer_click = InteractionStub(guild_id, other_player.user_id, pool=pool)
    await game_confirm.GameActionButton(game_id, "confirm").callback(confirmer_click)
    confirmed_embed = _embed_from(confirmer_click)
    assert next(field.value for field in confirmed_embed.fields if field.name == "Status") == (
        "Confirmed"
    )

    stats_interaction = InteractionStub(guild_id, reporter.user_id)
    stats = stats_cog.StatsCog(SimpleNamespace(pool=pool))
    await stats_cog.StatsCog.stats_command.callback(stats, stats_interaction, None)
    stats_embed = _embed_from(stats_interaction)
    all_time = next(field.value for field in stats_embed.fields if field.name == "All-Time")
    assert all_time.startswith("1-0")

    reason = "'; DROP TABLE games;-- @everyone **wrong**"
    monkeypatch.setattr(game_cog, "actor_from_interaction", lambda _: _actor(30, manager=True))
    void_interaction = InteractionStub(guild_id, 30)
    void = game_cog.GameCog.game_group.get_command("void")
    assert void is not None
    await void.callback(game_cog_instance, void_interaction, game_id, reason)
    void_embed = _embed_from(void_interaction)
    displayed_reason = next(field.value for field in void_embed.fields if field.name == "Reason")
    assert displayed_reason == formatting.escape_user_text(reason)
    assert reason not in displayed_reason


async def test_oversized_season_name_is_rejected_before_write(
    pool: asyncpg.Pool, guild_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _actor(1, manager=True)
    monkeypatch.setattr(season_cog, "actor_from_interaction", lambda _: manager)
    interaction = InteractionStub(guild_id, manager.user_id)
    cog = season_cog.SeasonCog(SimpleNamespace(pool=pool))
    command = season_cog.SeasonCog.season_group.get_command("start")
    assert command is not None
    end_date = (datetime.now(UTC).date() + timedelta(days=30)).isoformat()

    with pytest.raises(DomainValidationError):
        await command.callback(cog, interaction, "x" * 10_000, end_date, None, 2)

    assert await season_service.season_info(pool, guild_id) is None
