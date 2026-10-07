from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from catan_bot.cogs import insights_cog


def _interaction(*, user_id: int = 456) -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=123,
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def _choice(value: str) -> discord.app_commands.Choice[str]:
    return discord.app_commands.Choice(name=value, value=value)


def _assert_response(interaction: SimpleNamespace, embed: discord.Embed) -> None:
    interaction.response.defer.assert_awaited_once_with(thinking=True)
    interaction.edit_original_response.assert_awaited_once()
    call = interaction.edit_original_response.await_args
    assert call.kwargs["embed"] is embed
    allowed_mentions = call.kwargs["allowed_mentions"]
    assert allowed_mentions.everyone is False
    assert allowed_mentions.users is False
    assert allowed_mentions.roles is False
    assert allowed_mentions.replied_user is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command_name", "service_name", "builder_name"),
    [
        ("player", "player_insights", "build_player_insights_embed"),
        ("meta", "meta_insights", "build_meta_insights_embed"),
        ("head-to-head", "head_to_head_insights", "build_head_to_head_embed"),
    ],
)
async def test_insights_defaults_to_all_time_and_caller(
    monkeypatch: pytest.MonkeyPatch,
    command_name: str,
    service_name: str,
    builder_name: str,
) -> None:
    interaction = _interaction()
    cog = insights_cog.InsightsCog(SimpleNamespace(pool="pool"))
    view = object()
    embed = discord.Embed(title="result")
    service = AsyncMock(return_value=view)
    builder = Mock(return_value=embed)
    monkeypatch.setattr(insights_cog.insights_service, service_name, service)
    monkeypatch.setattr(insights_cog.formatting, builder_name, builder)
    command = insights_cog.InsightsCog.insights_group.get_command(command_name)
    assert command is not None

    if command_name == "meta":
        await command.callback(cog, interaction, None, None)
        service.assert_awaited_once_with("pool", 123, scope="all_time", game_type=None)
    else:
        await command.callback(cog, interaction, None, None, None)
        service.assert_awaited_once_with("pool", 123, 456, scope="all_time", game_type=None)
    builder.assert_called_once_with(view)
    _assert_response(interaction, embed)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command_name", "service_name", "builder_name"),
    [
        ("player", "player_insights", "build_player_insights_embed"),
        ("meta", "meta_insights", "build_meta_insights_embed"),
        ("head-to-head", "head_to_head_insights", "build_head_to_head_embed"),
    ],
)
async def test_insights_passes_explicit_filters_and_member(
    monkeypatch: pytest.MonkeyPatch,
    command_name: str,
    service_name: str,
    builder_name: str,
) -> None:
    interaction = _interaction()
    cog = insights_cog.InsightsCog(SimpleNamespace(pool="pool"))
    view = object()
    embed = discord.Embed(title="result")
    service = AsyncMock(return_value=view)
    builder = Mock(return_value=embed)
    monkeypatch.setattr(insights_cog.insights_service, service_name, service)
    monkeypatch.setattr(insights_cog.formatting, builder_name, builder)
    command = insights_cog.InsightsCog.insights_group.get_command(command_name)
    assert command is not None
    scope = _choice("season")
    game_type = _choice("seafarers")
    member = SimpleNamespace(id=789)

    if command_name == "meta":
        await command.callback(cog, interaction, scope, game_type)
        service.assert_awaited_once_with("pool", 123, scope="season", game_type="seafarers")
    else:
        await command.callback(cog, interaction, member, scope, game_type)
        service.assert_awaited_once_with("pool", 123, 789, scope="season", game_type="seafarers")
    builder.assert_called_once_with(view)
    _assert_response(interaction, embed)
