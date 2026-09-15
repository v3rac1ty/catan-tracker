from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from catan_bot.cogs import config_cog, game_cog, season_cog, stats_cog
from catan_bot.permissions import actor_from_interaction, guild_id_from_interaction
from catan_bot.services.context import Actor


def _interaction(*, guild_id: int = 123, user_id: int = 456) -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=guild_id,
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def _assert_no_mentions(call: object) -> None:
    allowed = call.kwargs["allowed_mentions"]  # type: ignore[attr-defined]
    assert allowed.everyone is False
    assert allowed.users is False
    assert allowed.roles is False
    assert allowed.replied_user is False


def test_group_cogs_expose_every_m4_subcommand() -> None:
    season = season_cog.SeasonCog.__cog_app_commands__[0]
    game = game_cog.GameCog.__cog_app_commands__[0]

    assert season.name == "season"
    assert {command.name for command in season.commands} == {
        "start",
        "min-games",
        "end-date",
        "end",
        "cancel",
        "info",
        "history",
    }
    assert game.name == "game"
    assert {command.name for command in game.commands} == {"report", "void", "history"}


def test_command_options_publish_required_bounds() -> None:
    start = season_cog.SeasonCog.season_group.get_command("start")
    void = game_cog.GameCog.game_group.get_command("void")
    assert start is not None
    assert void is not None

    start_options = {parameter.name: parameter for parameter in start.parameters}
    void_options = {parameter.name: parameter for parameter in void.parameters}
    assert (start_options["name"].min_value, start_options["name"].max_value) == (1, 100)
    assert (start_options["end_date"].min_value, start_options["end_date"].max_value) == (
        1,
        32,
    )
    assert (start_options["min_games"].min_value, start_options["min_games"].max_value) == (
        1,
        100,
    )
    assert (void_options["game_id"].min_value, void_options["game_id"].max_value) == (
        1,
        2**53 - 1,
    )
    assert (void_options["reason"].min_value, void_options["reason"].max_value) == (1, 200)


def test_bundled_timezone_choices_exclude_host_only_names() -> None:
    zones = config_cog.bundled_timezones()

    assert "America/Chicago" in zones
    assert "UTC" in zones
    assert "Factory" not in zones
    assert config_cog.filter_timezones(zones, "chicago") == ["America/Chicago"]
    assert config_cog.validate_bundled_timezone(" America/Chicago ") == "America/Chicago"


def test_actor_and_guild_translation_use_concrete_types(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeMember:
        id = 456
        roles = [SimpleNamespace(id=10), SimpleNamespace(id=20)]

    monkeypatch.setattr("catan_bot.permissions.discord.Member", FakeMember)
    interaction = SimpleNamespace(
        guild_id=123,
        user=FakeMember(),
        permissions=SimpleNamespace(manage_guild=1),
    )

    actor = actor_from_interaction(interaction)

    assert actor == Actor(user_id=456, has_manage_guild=True, role_ids=frozenset({10, 20}))
    assert type(actor.has_manage_guild) is bool
    assert type(actor.role_ids) is frozenset
    assert guild_id_from_interaction(interaction) == 123


@pytest.mark.parametrize("guild_id", [None, 0, -1, True, 2**63])
def test_guild_translation_rejects_invalid_ids(guild_id: object) -> None:
    with pytest.raises(ValueError, match="valid guild id"):
        guild_id_from_interaction(SimpleNamespace(guild_id=guild_id))


@pytest.mark.asyncio
async def test_season_start_defers_before_service_and_edits_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    pool = object()
    cog = season_cog.SeasonCog(SimpleNamespace(pool=pool))
    actor = Actor(user_id=456, has_manage_guild=True, role_ids=frozenset())
    season = object()
    embed = discord.Embed(title="Season Started")

    monkeypatch.setattr(season_cog, "actor_from_interaction", lambda _: actor)
    service = AsyncMock()

    async def start(*args: object, **kwargs: object) -> object:
        interaction.response.defer.assert_awaited_once_with(thinking=True)
        return season

    service.side_effect = start
    monkeypatch.setattr(season_cog.season_service, "start_season", service)
    monkeypatch.setattr(
        season_cog.formatting, "build_season_summary_embed", lambda *_a, **_k: embed
    )
    command = season_cog.SeasonCog.season_group.get_command("start")
    assert command is not None

    await command.callback(cog, interaction, "Fall", "2026-10-01", None, 2)

    service.assert_awaited_once()
    assert service.await_args.args[:3] == (pool, 123, actor)
    interaction.edit_original_response.assert_awaited_once()
    _assert_no_mentions(interaction.edit_original_response.await_args)


@pytest.mark.asyncio
async def test_game_report_persists_the_message_returned_by_original_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    sent = SimpleNamespace(channel=SimpleNamespace(id=700), id=800)
    interaction.edit_original_response.return_value = sent
    pool = object()
    cog = game_cog.GameCog(SimpleNamespace(pool=pool))
    actor = Actor(user_id=456, has_manage_guild=False, role_ids=frozenset())
    created = SimpleNamespace(game=SimpleNamespace(game_id=42))
    winner = SimpleNamespace(id=456, bot=False)
    loser = SimpleNamespace(id=789, bot=False)

    monkeypatch.setattr(game_cog, "actor_from_interaction", lambda _: actor)
    report = AsyncMock(return_value=created)
    record = AsyncMock()
    monkeypatch.setattr(game_cog.game_service, "report_game", report)
    monkeypatch.setattr(game_cog.game_service, "record_game_message", record)
    monkeypatch.setattr(game_cog.formatting, "build_game_report_embed", lambda _: discord.Embed())
    command = game_cog.GameCog.game_group.get_command("report")
    assert command is not None

    await command.callback(cog, interaction, winner, loser, None, None, None, None, None)

    interaction.response.defer.assert_awaited_once_with(thinking=True)
    report.assert_awaited_once()
    record.assert_awaited_once_with(pool, 123, 42, 700, 800)
    edited = interaction.edit_original_response.await_args
    assert edited.kwargs["view"].timeout is None
    _assert_no_mentions(edited)


@pytest.mark.asyncio
async def test_leaderboard_maps_choice_and_defers_before_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    cog = stats_cog.StatsCog(SimpleNamespace(pool="pool"))
    board = object()
    leaderboard = AsyncMock(return_value=board)
    monkeypatch.setattr(stats_cog.stats_service, "leaderboard", leaderboard)
    monkeypatch.setattr(stats_cog.formatting, "build_leaderboard_embed", lambda _: discord.Embed())

    await stats_cog.StatsCog.leaderboard_command.callback(
        cog, interaction, discord.app_commands.Choice(name="all-time", value="all_time")
    )

    interaction.response.defer.assert_awaited_once_with(thinking=True)
    leaderboard.assert_awaited_once_with("pool", 123, "all_time")
    _assert_no_mentions(interaction.edit_original_response.await_args)
