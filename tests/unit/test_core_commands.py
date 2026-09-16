from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from catan_bot.cogs import config_cog, game_cog, season_cog, stats_cog
from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.scoring import GameRules
from catan_bot.permissions import actor_from_interaction, guild_id_from_interaction
from catan_bot.services.context import Actor


def _interaction(*, guild_id: int = 123, user_id: int = 456) -> SimpleNamespace:
    permissions = SimpleNamespace(
        view_channel=True,
        send_messages=True,
        send_messages_in_threads=True,
        embed_links=True,
    )
    member = SimpleNamespace(id=user_id)
    guild = SimpleNamespace(id=guild_id, me=SimpleNamespace(id=999))
    channel = SimpleNamespace(
        id=700,
        guild=guild,
        permissions_for=lambda _member: permissions,
    )
    return SimpleNamespace(
        guild_id=guild_id,
        user=member,
        guild=guild,
        channel=channel,
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
    assert {command.name for command in game.commands} == {
        "report",
        "update",
        "void",
        "history",
        "show",
    }


def test_config_exposes_player_role_command() -> None:
    config = config_cog.ConfigCog.config_group
    assert config.get_command("player-role") is not None


def test_player_role_validation_rejects_everyone_and_cross_guild() -> None:
    everyone = SimpleNamespace(
        id=123,
        guild=SimpleNamespace(id=123),
        is_default=lambda: True,
    )
    foreign = SimpleNamespace(
        id=987,
        guild=SimpleNamespace(id=456),
        is_default=lambda: False,
    )

    with pytest.raises(DomainValidationError, match="@everyone"):
        config_cog.validate_player_role(everyone, 123)  # type: ignore[arg-type]
    with pytest.raises(DomainValidationError, match="this server"):
        config_cog.validate_player_role(foreign, 123)  # type: ignore[arg-type]


def test_command_options_publish_required_bounds() -> None:
    start = season_cog.SeasonCog.season_group.get_command("start")
    void = game_cog.GameCog.game_group.get_command("void")
    report = game_cog.GameCog.game_group.get_command("report")
    show = game_cog.GameCog.game_group.get_command("show")
    update = game_cog.GameCog.game_group.get_command("update")
    assert start is not None
    assert void is not None
    assert report is not None
    assert show is not None
    assert update is not None

    start_options = {parameter.name: parameter for parameter in start.parameters}
    void_options = {parameter.name: parameter for parameter in void.parameters}
    report_options = {parameter.name: parameter for parameter in report.parameters}
    show_options = {parameter.name: parameter for parameter in show.parameters}
    update_options = {parameter.name: parameter for parameter in update.parameters}
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
    assert (report_options["scenario"].min_value, report_options["scenario"].max_value) == (1, 100)
    target_bounds = (
        report_options["target_points"].min_value,
        report_options["target_points"].max_value,
    )
    assert target_bounds == (
        1,
        99,
    )
    assert (report_options["time"].min_value, report_options["time"].max_value) == (1, 32)
    assert [choice.value for choice in report_options["game_type"].choices] == [
        "normal",
        "seafarers",
        "cities_knights",
        "seafarers_cities_knights",
    ]
    assert (show_options["game_id"].min_value, show_options["game_id"].max_value) == (
        1,
        2**53 - 1,
    )
    assert (update_options["reason"].min_value, update_options["reason"].max_value) == (1, 200)
    assert (update_options["game_id"].min_value, update_options["game_id"].max_value) == (
        1,
        2**53 - 1,
    )
    assert [choice.value for choice in update_options["game_type"].choices] == [
        "normal",
        "seafarers",
        "cities_knights",
        "seafarers_cities_knights",
    ]


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
async def test_game_report_prepares_a_private_score_sheet_before_creating_a_game(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    sent = SimpleNamespace(channel=SimpleNamespace(id=700), id=800)
    interaction.edit_original_response.return_value = sent
    pool = object()
    cog = game_cog.GameCog(SimpleNamespace(pool=pool))
    actor = Actor(user_id=456, has_manage_guild=False, role_ids=frozenset())
    prepared = SimpleNamespace(
        winner_id=456,
        loser_ids=(789,),
        rules=GameRules(game_type="normal", target_points=10),
    )
    winner = SimpleNamespace(id=456, bot=False)
    loser = SimpleNamespace(id=789, bot=False)

    monkeypatch.setattr(game_cog, "actor_from_interaction", lambda _: actor)
    prepare = AsyncMock(return_value=prepared)
    submit = AsyncMock()
    monkeypatch.setattr(game_cog.game_service, "prepare_game_report", prepare)
    monkeypatch.setattr(game_cog.game_service, "submit_game_report", submit)
    command = game_cog.GameCog.game_group.get_command("report")
    assert command is not None

    await command.callback(cog, interaction, winner, loser, None, None, None, None, None)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    prepare.assert_awaited_once()
    submit.assert_not_awaited()
    edited = interaction.edit_original_response.await_args
    assert edited.kwargs["view"].timeout == 900
    _assert_no_mentions(edited)


@pytest.mark.asyncio
async def test_game_update_prepares_an_ephemeral_sheet_without_submitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    interaction.edit_original_response.return_value = SimpleNamespace()
    cog = game_cog.GameCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=True, role_ids=frozenset())
    prepared = SimpleNamespace(
        game_id=42,
        winner_id=456,
        loser_ids=(789,),
        rules=GameRules(game_type="normal", target_points=10),
        original=SimpleNamespace(game=SimpleNamespace(game_type="normal", played_on="today")),
        initial_scores=(),
        played_on="today",
        expected_revision=1,
    )
    monkeypatch.setattr(game_cog, "actor_from_interaction", lambda _: actor)
    prepare = AsyncMock(return_value=prepared)
    submit = AsyncMock()
    monkeypatch.setattr(game_cog.game_service, "prepare_game_update", prepare)
    monkeypatch.setattr(game_cog.game_service, "submit_game_update", submit)
    command = game_cog.GameCog.game_group.get_command("update")
    assert command is not None

    await command.callback(cog, interaction, 42)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert prepare.await_args.kwargs["losers"] is None
    submit.assert_not_awaited()
    edited = interaction.edit_original_response.await_args
    assert edited.kwargs["view"].mode == "update"
    _assert_no_mentions(edited)


@pytest.mark.asyncio
async def test_game_show_reads_one_guild_scoped_complete_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    cog = game_cog.GameCog(SimpleNamespace(pool="pool"))
    loaded = object()
    get_game = AsyncMock(return_value=loaded)
    monkeypatch.setattr(game_cog.game_service, "get_game", get_game)
    monkeypatch.setattr(
        game_cog.formatting, "build_game_status_embed", lambda _: discord.Embed(title="Game")
    )
    command = game_cog.GameCog.game_group.get_command("show")
    assert command is not None

    await command.callback(cog, interaction, 42)

    interaction.response.defer.assert_awaited_once_with(thinking=True)
    get_game.assert_awaited_once_with("pool", 123, 42)
    _assert_no_mentions(interaction.edit_original_response.await_args)


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
        cog, interaction, discord.app_commands.Choice(name="all-time", value="all_time"), None
    )

    interaction.response.defer.assert_awaited_once_with(thinking=True)
    leaderboard.assert_awaited_once_with("pool", 123, "all_time")
    _assert_no_mentions(interaction.edit_original_response.await_args)


@pytest.mark.asyncio
async def test_leaderboard_posts_to_selected_channel_and_returns_ephemeral_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    target = SimpleNamespace(
        id=701,
        guild=interaction.guild,
        permissions_for=interaction.channel.permissions_for,
        send=AsyncMock(return_value=SimpleNamespace(jump_url="https://discord.test/board")),
    )
    cog = stats_cog.StatsCog(SimpleNamespace(pool="pool"))
    leaderboard = AsyncMock(return_value=object())
    monkeypatch.setattr(stats_cog.stats_service, "leaderboard", leaderboard)
    monkeypatch.setattr(stats_cog.formatting, "build_leaderboard_embed", lambda _: discord.Embed())

    await stats_cog.StatsCog.leaderboard_command.callback(cog, interaction, None, target)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    leaderboard.assert_awaited_once_with("pool", 123, "season")
    target.send.assert_awaited_once()
    confirmation = interaction.edit_original_response.await_args.kwargs["content"]
    assert "<#701>" in confirmation and "https://discord.test/board" in confirmation
    _assert_no_mentions(interaction.edit_original_response.await_args)
