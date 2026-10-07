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
async def test_chart_uses_thread_and_attaches_chart_file(monkeypatch: pytest.MonkeyPatch) -> None:
    interaction = _interaction()
    cog = insights_cog.InsightsCog(SimpleNamespace(pool="pool"))
    view = object()
    rendered = insights_cog.charts.RenderedChart(b"png-bytes", "Chart title", (), None)
    service = AsyncMock(return_value=view)
    render = Mock(return_value=rendered)
    to_thread = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    builder = Mock(return_value=discord.Embed(title="chart"))
    monkeypatch.setattr(insights_cog.insights_service, "chart_insights", service)
    monkeypatch.setattr(insights_cog.charts, "render_chart", render)
    monkeypatch.setattr(insights_cog.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(insights_cog.formatting, "build_chart_embed", builder)
    command = insights_cog.InsightsCog.insights_group.get_command("chart")
    assert command is not None

    await command.callback(cog, interaction, _choice("winning-formula"), None, None)

    service.assert_awaited_once_with("pool", 123, scope="all_time", game_type=None)
    to_thread.assert_awaited_once_with(insights_cog.charts.render_chart, "winning-formula", view)
    render.assert_called_once_with("winning-formula", view)
    filename = "insights-winning-formula.png"
    builder.assert_called_once_with(view, rendered, filename)
    interaction.response.defer.assert_awaited_once_with(thinking=True)
    call = interaction.edit_original_response.await_args
    assert call.kwargs["embed"] is builder.return_value
    assert len(call.kwargs["attachments"]) == 1
    file = call.kwargs["attachments"][0]
    assert file.filename == filename
    assert file.fp.read() == b"png-bytes"
    allowed = call.kwargs["allowed_mentions"]
    assert (allowed.everyone, allowed.users, allowed.roles, allowed.replied_user) == (
        False,
        False,
        False,
        False,
    )


@pytest.mark.asyncio
async def test_chart_passes_explicit_filters_and_uses_unavailable_embed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    cog = insights_cog.InsightsCog(SimpleNamespace(pool="pool"))
    view = object()
    service = AsyncMock(return_value=view)
    render = Mock(return_value=None)
    to_thread = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    unavailable = Mock(return_value=discord.Embed(title="unavailable"))
    monkeypatch.setattr(insights_cog.insights_service, "chart_insights", service)
    monkeypatch.setattr(insights_cog.charts, "render_chart", render)
    monkeypatch.setattr(insights_cog.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(insights_cog.formatting, "build_chart_unavailable_embed", unavailable)
    command = insights_cog.InsightsCog.insights_group.get_command("chart")
    assert command is not None

    await command.callback(
        cog, interaction, _choice("head-to-head"), _choice("season"), _choice("seafarers")
    )

    service.assert_awaited_once_with("pool", 123, scope="season", game_type="seafarers")
    to_thread.assert_awaited_once_with(insights_cog.charts.render_chart, "head-to-head", view)
    unavailable.assert_called_once_with(view, "Head-to-head")
    call = interaction.edit_original_response.await_args
    assert call.kwargs["embed"] is unavailable.return_value
    assert "attachments" not in call.kwargs
    allowed = call.kwargs["allowed_mentions"]
    assert (allowed.everyone, allowed.users, allowed.roles, allowed.replied_user) == (
        False,
        False,
        False,
        False,
    )


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
