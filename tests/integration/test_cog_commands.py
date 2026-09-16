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
from catan_bot.domain.errors import DomainValidationError
from catan_bot.services import config_service, season_service
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
            )
        )
        self.response = SimpleNamespace(defer=AsyncMock())
        self.followup = SimpleNamespace(send=AsyncMock())
        self.edit_original_response = AsyncMock(
            return_value=SimpleNamespace(channel=SimpleNamespace(id=700), id=800)
        )


def _actor(user_id: int, *, manager: bool = False, roles: frozenset[int] = frozenset()) -> Actor:
    return Actor(user_id=user_id, has_manage_guild=manager, role_ids=roles)


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

    await report.callback(
        game_cog_instance,
        report_interaction,
        SimpleNamespace(id=reporter.user_id, bot=False),
        SimpleNamespace(id=other_player.user_id, bot=False),
        None,
        None,
        None,
        None,
        None,
    )

    report_interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    report_embed = _embed_from(report_interaction)
    assert report_embed.title == "Game score sheet"
    score_sheet = report_interaction.edit_original_response.await_args.kwargs["view"]
    submit_button = next(item for item in score_sheet.children if item.label == "Submit report")
    submit_interaction = InteractionStub(guild_id, reporter.user_id, pool=pool)
    await submit_button.callback(submit_interaction)
    submit_interaction.response.defer.assert_awaited_once_with()
    assert score_sheet.created_report is not None
    assert score_sheet.completed
    assert score_sheet.message_recorded
    report_interaction.channel.send.assert_awaited_once()
    game_id = score_sheet.created_report.game.game_id

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
