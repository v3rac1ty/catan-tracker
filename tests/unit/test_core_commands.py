from __future__ import annotations

from datetime import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from catan_bot.cogs import config_cog, game_cog, season_cog, stats_cog
from catan_bot.domain.errors import DomainValidationError
from catan_bot.domain.scoring import GameRules
from catan_bot.permissions import actor_from_interaction, guild_id_from_interaction
from catan_bot.services.context import Actor
from catan_bot.services.errors import PermissionDeniedError


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
        "scores",
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


def _reported_game(*, winner_id: int = 456, loser_ids: tuple[int, ...] = (789,)) -> SimpleNamespace:
    game = SimpleNamespace(
        game_id=42,
        game_type="normal",
        extension_5_6=False,
        scenario=None,
        target_points=10,
        status="pending",
        channel_id=None,
        message_id=None,
        reported_by=winner_id,
    )
    return SimpleNamespace(game=game, winner_id=winner_id, loser_ids=loser_ids, scores=())


@pytest.mark.asyncio
async def test_game_report_creates_the_game_immediately_and_dms_every_participant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 2: `/game report` no longer waits for a reporter Submit click --
    the pending game and its public message exist before any DM is sent."""
    interaction = _interaction()
    interaction.channel.send = AsyncMock(
        return_value=SimpleNamespace(
            channel=SimpleNamespace(id=700),
            id=800,
            jump_url="https://example.test/800",
            edit=AsyncMock(),
        )
    )
    pool = object()
    cog = game_cog.GameCog(SimpleNamespace(pool=pool))
    actor = Actor(user_id=456, has_manage_guild=False, role_ids=frozenset())
    prepared = SimpleNamespace(
        winner_id=456,
        loser_ids=(789,),
        rules=GameRules(game_type="normal", target_points=10),
    )
    created = _reported_game()
    winner = SimpleNamespace(
        id=456,
        bot=False,
        send=AsyncMock(return_value=SimpleNamespace(channel=SimpleNamespace(id=1), id=2)),
    )
    loser = SimpleNamespace(
        id=789,
        bot=False,
        send=AsyncMock(return_value=SimpleNamespace(channel=SimpleNamespace(id=3), id=4)),
    )

    monkeypatch.setattr(game_cog, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(
        game_cog.game_service, "prepare_game_report", AsyncMock(return_value=prepared)
    )
    submit = AsyncMock(return_value=created)
    monkeypatch.setattr(game_cog.game_service, "submit_game_report", submit)
    record_message = AsyncMock()
    monkeypatch.setattr(game_cog.game_service, "record_game_message", record_message)
    open_collection = AsyncMock()
    monkeypatch.setattr(game_cog.game_service, "open_score_collection", open_collection)
    deliveries: list[object] = []

    async def _record_delivery(*_args: object, **kwargs: object) -> None:
        deliveries.append(kwargs)

    monkeypatch.setattr(game_cog.game_service, "record_score_request_delivery", _record_delivery)
    monkeypatch.setattr(
        game_cog.game_service, "score_collection_status", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(
        game_cog.formatting, "build_game_report_embed", lambda *_a, **_k: discord.Embed()
    )
    monkeypatch.setattr(game_cog, "build_game_action_view", lambda _game_id: object())
    monkeypatch.setattr(
        game_cog.score_entry, "build_score_entry_embed", lambda *_a: discord.Embed()
    )
    monkeypatch.setattr(game_cog.score_entry, "build_score_entry_view", lambda *_a, **_k: object())
    command = game_cog.GameCog.game_group.get_command("report")
    assert command is not None

    await command.callback(cog, interaction, winner, loser, None, None, None, None, None)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    submit.assert_awaited_once()
    assert submit.await_args.kwargs["scores"] is None
    record_message.assert_awaited_once_with(pool, 123, 42, 700, 800)
    open_collection.assert_awaited_once()
    assert open_collection.await_args.args[2] == 42
    assert set(open_collection.await_args.args[3]) == {456, 789}
    winner.send.assert_awaited_once()
    loser.send.assert_awaited_once()
    assert len(deliveries) == 2
    assert all(kwargs["delivered"] is True for kwargs in deliveries)
    edited = interaction.edit_original_response.await_args
    assert "Game #42 reported" in edited.kwargs["content"]
    assert "https://example.test/800" in edited.kwargs["content"]


@pytest.mark.asyncio
async def test_game_report_marks_closed_dms_blocked_and_keeps_going(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    interaction.channel.send = AsyncMock(
        return_value=SimpleNamespace(
            channel=SimpleNamespace(id=700),
            id=800,
            jump_url="https://example.test/800",
            edit=AsyncMock(),
        )
    )
    cog = game_cog.GameCog(SimpleNamespace(pool=object()))
    actor = Actor(user_id=456, has_manage_guild=False, role_ids=frozenset())
    prepared = SimpleNamespace(
        winner_id=456, loser_ids=(789,), rules=GameRules(game_type="normal", target_points=10)
    )
    created = _reported_game()
    winner = SimpleNamespace(
        id=456,
        bot=False,
        send=AsyncMock(return_value=SimpleNamespace(channel=SimpleNamespace(id=1), id=2)),
    )
    forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "closed")
    loser = SimpleNamespace(id=789, bot=False, send=AsyncMock(side_effect=forbidden))

    monkeypatch.setattr(game_cog, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(
        game_cog.game_service, "prepare_game_report", AsyncMock(return_value=prepared)
    )
    monkeypatch.setattr(
        game_cog.game_service, "submit_game_report", AsyncMock(return_value=created)
    )
    monkeypatch.setattr(game_cog.game_service, "record_game_message", AsyncMock())
    monkeypatch.setattr(game_cog.game_service, "open_score_collection", AsyncMock())
    deliveries: list[dict[str, object]] = []

    async def _record_delivery(*_args: object, **kwargs: object) -> None:
        deliveries.append(kwargs)

    monkeypatch.setattr(game_cog.game_service, "record_score_request_delivery", _record_delivery)
    monkeypatch.setattr(
        game_cog.game_service, "score_collection_status", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(
        game_cog.formatting, "build_game_report_embed", lambda *_a, **_k: discord.Embed()
    )
    monkeypatch.setattr(game_cog, "build_game_action_view", lambda _game_id: object())
    monkeypatch.setattr(
        game_cog.score_entry, "build_score_entry_embed", lambda *_a: discord.Embed()
    )
    monkeypatch.setattr(game_cog.score_entry, "build_score_entry_view", lambda *_a, **_k: object())
    command = game_cog.GameCog.game_group.get_command("report")
    assert command is not None

    await command.callback(cog, interaction, winner, loser, None, None, None, None, None)

    assert len(deliveries) == 2
    blocked = next(kwargs for kwargs in deliveries if kwargs["delivered"] is False)
    assert blocked["channel_id"] is None and blocked["message_id"] is None
    edited = interaction.edit_original_response.await_args
    assert "DMs closed" in edited.kwargs["content"]
    assert "<@789>" in edited.kwargs["content"]


@pytest.mark.asyncio
async def test_game_report_dm_http_exception_does_not_abort_remaining_participants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient, non-`Forbidden` HTTP error on one participant's DM (a
    5xx, a network blip, ...) must not prevent every participant *after*
    them in the loop from ever being messaged -- the bug being fixed here
    caught only `discord.Forbidden`, so any other `HTTPException` escaped
    the `try` and aborted the whole `for` loop before reaching the rest."""
    interaction = _interaction()
    interaction.channel.send = AsyncMock(
        return_value=SimpleNamespace(
            channel=SimpleNamespace(id=700),
            id=800,
            jump_url="https://example.test/800",
            edit=AsyncMock(),
        )
    )
    cog = game_cog.GameCog(SimpleNamespace(pool=object()))
    actor = Actor(user_id=456, has_manage_guild=False, role_ids=frozenset())
    prepared = SimpleNamespace(
        winner_id=456,
        loser_ids=(789, 999),
        rules=GameRules(game_type="normal", target_points=10),
    )
    created = _reported_game(loser_ids=(789, 999))
    winner = SimpleNamespace(
        id=456,
        bot=False,
        send=AsyncMock(return_value=SimpleNamespace(channel=SimpleNamespace(id=1), id=2)),
    )
    server_error = discord.HTTPException(SimpleNamespace(status=502, reason="Bad Gateway"), "boom")
    failing_loser = SimpleNamespace(id=789, bot=False, send=AsyncMock(side_effect=server_error))
    healthy_loser = SimpleNamespace(
        id=999,
        bot=False,
        send=AsyncMock(return_value=SimpleNamespace(channel=SimpleNamespace(id=5), id=6)),
    )

    monkeypatch.setattr(game_cog, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(
        game_cog.game_service, "prepare_game_report", AsyncMock(return_value=prepared)
    )
    monkeypatch.setattr(
        game_cog.game_service, "submit_game_report", AsyncMock(return_value=created)
    )
    monkeypatch.setattr(game_cog.game_service, "record_game_message", AsyncMock())
    monkeypatch.setattr(game_cog.game_service, "open_score_collection", AsyncMock())
    deliveries: list[dict[str, object]] = []

    async def _record_delivery(*_args: object, **kwargs: object) -> None:
        deliveries.append(kwargs)

    monkeypatch.setattr(game_cog.game_service, "record_score_request_delivery", _record_delivery)
    monkeypatch.setattr(
        game_cog.game_service, "score_collection_status", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(
        game_cog.formatting, "build_game_report_embed", lambda *_a, **_k: discord.Embed()
    )
    monkeypatch.setattr(game_cog, "build_game_action_view", lambda _game_id: object())
    monkeypatch.setattr(
        game_cog.score_entry, "build_score_entry_embed", lambda *_a: discord.Embed()
    )
    monkeypatch.setattr(game_cog.score_entry, "build_score_entry_view", lambda *_a, **_k: object())
    command = game_cog.GameCog.game_group.get_command("report")
    assert command is not None

    await command.callback(
        cog, interaction, winner, failing_loser, healthy_loser, None, None, None, None
    )

    # The participant *after* the one whose DM raised must still have been
    # messaged -- proves the loop wasn't aborted by the wider except clause.
    healthy_loser.send.assert_awaited_once()
    assert len(deliveries) == 3
    blocked = next(kwargs for kwargs in deliveries if kwargs["delivered"] is False)
    assert blocked["channel_id"] is None and blocked["message_id"] is None
    assert sum(1 for kwargs in deliveries if kwargs["delivered"] is True) == 2


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
    assert edited.kwargs["view"].timeout == 1800
    _assert_no_mentions(edited)


@pytest.mark.asyncio
async def test_game_scores_with_explicit_id_opens_a_participants_sheet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    cog = game_cog.GameCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=False, role_ids=frozenset())
    game = SimpleNamespace(game=SimpleNamespace())
    monkeypatch.setattr(game_cog, "actor_from_interaction", lambda _: actor)
    get_for_player = AsyncMock(return_value=game)
    monkeypatch.setattr(game_cog.game_service, "get_game_for_player", get_for_player)
    find_open = AsyncMock()
    monkeypatch.setattr(game_cog.game_service, "find_open_score_request_game_id", find_open)
    monkeypatch.setattr(game_cog.score_entry, "rules_for_game", lambda _g: object())
    monkeypatch.setattr(
        game_cog.score_entry, "build_score_entry_embed", lambda *_a: discord.Embed()
    )
    monkeypatch.setattr(game_cog.score_entry, "selected_awards_for", lambda *_a: frozenset())
    monkeypatch.setattr(game_cog.score_entry, "build_score_entry_view", lambda *_a, **_k: object())
    command = game_cog.GameCog.game_group.get_command("scores")
    assert command is not None

    await command.callback(cog, interaction, 42)

    find_open.assert_not_awaited()
    get_for_player.assert_awaited_once_with("pool", 123, 42, 456)
    edited = interaction.edit_original_response.await_args
    assert isinstance(edited.kwargs["embed"], discord.Embed)


@pytest.mark.asyncio
async def test_game_scores_without_an_id_falls_back_to_the_open_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    cog = game_cog.GameCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=False, role_ids=frozenset())
    monkeypatch.setattr(game_cog, "actor_from_interaction", lambda _: actor)
    find_open = AsyncMock(return_value=77)
    monkeypatch.setattr(game_cog.game_service, "find_open_score_request_game_id", find_open)
    get_for_player = AsyncMock(return_value=SimpleNamespace(game=SimpleNamespace()))
    monkeypatch.setattr(game_cog.game_service, "get_game_for_player", get_for_player)
    monkeypatch.setattr(game_cog.score_entry, "rules_for_game", lambda _g: object())
    monkeypatch.setattr(
        game_cog.score_entry, "build_score_entry_embed", lambda *_a: discord.Embed()
    )
    monkeypatch.setattr(game_cog.score_entry, "selected_awards_for", lambda *_a: frozenset())
    monkeypatch.setattr(game_cog.score_entry, "build_score_entry_view", lambda *_a, **_k: object())
    command = game_cog.GameCog.game_group.get_command("scores")
    assert command is not None

    await command.callback(cog, interaction, None)

    find_open.assert_awaited_once_with("pool", 123, 456)
    get_for_player.assert_awaited_once_with("pool", 123, 77, 456)


@pytest.mark.asyncio
async def test_game_scores_without_an_open_request_says_so_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    cog = game_cog.GameCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=False, role_ids=frozenset())
    monkeypatch.setattr(game_cog, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(
        game_cog.game_service, "find_open_score_request_game_id", AsyncMock(return_value=None)
    )
    get_for_player = AsyncMock()
    monkeypatch.setattr(game_cog.game_service, "get_game_for_player", get_for_player)
    command = game_cog.GameCog.game_group.get_command("scores")
    assert command is not None

    await command.callback(cog, interaction, None)

    get_for_player.assert_not_awaited()
    edited = interaction.edit_original_response.await_args
    assert "no games waiting" in edited.kwargs["content"]


@pytest.mark.asyncio
async def test_game_scores_access_control_is_delegated_to_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-participant must be rejected -- proven here by asserting the
    cog calls `get_game_for_player` (whose own tests cover the rejection)
    rather than the unguarded `get_game`."""
    interaction = _interaction()
    cog = game_cog.GameCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=False, role_ids=frozenset())
    monkeypatch.setattr(game_cog, "actor_from_interaction", lambda _: actor)
    get_game = AsyncMock()
    monkeypatch.setattr(game_cog.game_service, "get_game", get_game)
    get_for_player = AsyncMock(side_effect=PermissionDeniedError("Only a participant..."))
    monkeypatch.setattr(game_cog.game_service, "get_game_for_player", get_for_player)
    command = game_cog.GameCog.game_group.get_command("scores")
    assert command is not None

    with pytest.raises(PermissionDeniedError):
        await command.callback(cog, interaction, 42)

    get_game.assert_not_awaited()
    get_for_player.assert_awaited_once()


@pytest.mark.asyncio
async def test_game_show_reads_one_guild_scoped_complete_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    cog = game_cog.GameCog(SimpleNamespace(pool="pool"))
    loaded = SimpleNamespace(game=SimpleNamespace(status="confirmed"))
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
    assert interaction.edit_original_response.await_args.kwargs["view"] is None


@pytest.mark.asyncio
async def test_game_show_attaches_confirm_reject_view_when_still_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A game whose public message failed to send has no buttons anywhere
    else -- `/game show` must re-attach them whenever the game is pending,
    so it isn't permanently stuck unconfirmable."""
    interaction = _interaction()
    cog = game_cog.GameCog(SimpleNamespace(pool="pool"))
    loaded = SimpleNamespace(game=SimpleNamespace(status="pending"))
    monkeypatch.setattr(game_cog.game_service, "get_game", AsyncMock(return_value=loaded))
    monkeypatch.setattr(
        game_cog.formatting, "build_game_status_embed", lambda _: discord.Embed(title="Game")
    )
    sentinel_view = object()
    build_view = Mock(return_value=sentinel_view)
    monkeypatch.setattr(game_cog, "build_game_action_view", build_view)
    command = game_cog.GameCog.game_group.get_command("show")
    assert command is not None

    await command.callback(cog, interaction, 42)

    build_view.assert_called_once_with(42)
    assert interaction.edit_original_response.await_args.kwargs["view"] is sentinel_view


@pytest.mark.asyncio
async def test_game_history_defaults_include_voided_to_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    cog = game_cog.GameCog(SimpleNamespace(pool="pool"))
    history = AsyncMock(return_value=[])
    monkeypatch.setattr(game_cog.game_service, "game_history", history)
    monkeypatch.setattr(
        game_cog.formatting, "build_game_history_embed", lambda *_a, **_k: discord.Embed()
    )
    command = game_cog.GameCog.game_group.get_command("history")
    assert command is not None

    await command.callback(cog, interaction, None, None)

    history.assert_awaited_once_with(
        "pool", 123, user_id=None, limit=game_cog._DEFAULT_HISTORY_LIMIT, include_voided=False
    )


@pytest.mark.asyncio
async def test_game_history_threads_include_voided_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    cog = game_cog.GameCog(SimpleNamespace(pool="pool"))
    history = AsyncMock(return_value=[])
    monkeypatch.setattr(game_cog.game_service, "game_history", history)
    monkeypatch.setattr(
        game_cog.formatting, "build_game_history_embed", lambda *_a, **_k: discord.Embed()
    )
    command = game_cog.GameCog.game_group.get_command("history")
    assert command is not None

    await command.callback(cog, interaction, None, 5, True)

    history.assert_awaited_once_with("pool", 123, user_id=None, limit=5, include_voided=True)


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


# ---------------------------------------------------------------------------
# /config leaderboard (Phase 3 wired it; Phase 4 made every option but
# `mode` genuinely partial -- an omitted option must leave that field's
# stored value alone, never silently replace it with a default).
# ---------------------------------------------------------------------------


def _mode_choice(value: str) -> discord.app_commands.Choice[str]:
    return discord.app_commands.Choice(name=value, value=value)


def _leaderboard_command():
    command = config_cog.ConfigCog.config_group.get_command("leaderboard")
    assert command is not None
    return command


def _config_with_channels(
    *,
    announce_channel_id: int | None,
    leaderboard_channel_id: int | None = None,
    leaderboard_mode: str = "off",
) -> SimpleNamespace:
    return SimpleNamespace(
        announce_channel_id=announce_channel_id,
        leaderboard_channel_id=leaderboard_channel_id,
        leaderboard_mode=leaderboard_mode,
    )


def test_config_leaderboard_command_choices_are_off_per_game_daily_and_scopes() -> None:
    command = _leaderboard_command()
    options = {parameter.name: parameter for parameter in command.parameters}
    assert [choice.value for choice in options["mode"].choices] == ["off", "per_game", "daily"]
    assert [choice.value for choice in options["scope"].choices] == ["season", "all_time"]
    assert "clear_channel" in options


@pytest.mark.asyncio
async def test_config_leaderboard_requires_manage_guild(monkeypatch: pytest.MonkeyPatch) -> None:
    interaction = _interaction()
    cog = config_cog.ConfigCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=False, role_ids=frozenset())
    monkeypatch.setattr(config_cog, "actor_from_interaction", lambda _: actor)
    get_config = AsyncMock()
    monkeypatch.setattr(config_cog.config_service, "get_config", get_config)
    set_settings = AsyncMock()
    monkeypatch.setattr(config_cog.config_service, "set_leaderboard_settings", set_settings)

    with pytest.raises(PermissionDeniedError):
        await _leaderboard_command().callback(cog, interaction, _mode_choice("daily"))

    get_config.assert_not_awaited()
    set_settings.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()


@pytest.mark.asyncio
async def test_config_leaderboard_off_mode_never_requires_a_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    cog = config_cog.ConfigCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=True, role_ids=frozenset())
    monkeypatch.setattr(config_cog, "actor_from_interaction", lambda _: actor)
    get_config = AsyncMock()
    monkeypatch.setattr(config_cog.config_service, "get_config", get_config)
    updated = SimpleNamespace()
    set_settings = AsyncMock(return_value=updated)
    monkeypatch.setattr(config_cog.config_service, "set_leaderboard_settings", set_settings)
    monkeypatch.setattr(config_cog.formatting, "build_config_show_embed", lambda _: discord.Embed())

    await _leaderboard_command().callback(cog, interaction, _mode_choice("off"))

    # "off" never needs a destination, so this must not even read the
    # current config -- and with every other option omitted, only `mode`
    # reaches the service call.
    get_config.assert_not_awaited()
    set_settings.assert_awaited_once()
    assert set_settings.await_args.kwargs == {"mode": "off"}


@pytest.mark.asyncio
async def test_config_leaderboard_mode_only_leaves_channel_scope_and_time_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Phase 4 fix, proven at the cog boundary: passing only `mode` for
    a guild that already has a leaderboard channel configured must reach
    `config_service.set_leaderboard_settings` with nothing but `mode` --
    the previously configured channel, scope, and time are never
    re-submitted (see that function's own docstring for how the omission
    survives down to the repository layer)."""
    interaction = _interaction()
    cog = config_cog.ConfigCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=True, role_ids=frozenset())
    monkeypatch.setattr(config_cog, "actor_from_interaction", lambda _: actor)
    get_config = AsyncMock(
        return_value=_config_with_channels(announce_channel_id=None, leaderboard_channel_id=555)
    )
    monkeypatch.setattr(config_cog.config_service, "get_config", get_config)
    set_settings = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(config_cog.config_service, "set_leaderboard_settings", set_settings)
    monkeypatch.setattr(config_cog.formatting, "build_config_show_embed", lambda _: discord.Embed())

    await _leaderboard_command().callback(cog, interaction, _mode_choice("per_game"))

    # A channel is already on file, so the read above only confirms a
    # destination exists -- it must never turn into a write of that
    # channel (or the untouched scope/time) back through the service call.
    set_settings.assert_awaited_once()
    assert set_settings.await_args.kwargs == {"mode": "per_game"}


@pytest.mark.asyncio
async def test_config_leaderboard_daily_mode_without_any_channel_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    cog = config_cog.ConfigCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=True, role_ids=frozenset())
    monkeypatch.setattr(config_cog, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(
        config_cog.config_service,
        "get_config",
        AsyncMock(return_value=_config_with_channels(announce_channel_id=None)),
    )
    set_settings = AsyncMock()
    monkeypatch.setattr(config_cog.config_service, "set_leaderboard_settings", set_settings)

    with pytest.raises(DomainValidationError, match="announcement channel"):
        await _leaderboard_command().callback(cog, interaction, _mode_choice("daily"))

    set_settings.assert_not_awaited()


@pytest.mark.asyncio
async def test_config_leaderboard_falls_back_to_announce_channel_only_the_first_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    announce_channel = interaction.channel  # already permission-valid in _interaction()
    interaction.guild.get_channel = lambda channel_id: (
        announce_channel if channel_id == 900 else None
    )
    cog = config_cog.ConfigCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=True, role_ids=frozenset())
    monkeypatch.setattr(config_cog, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(
        config_cog.config_service,
        "get_config",
        AsyncMock(
            return_value=_config_with_channels(announce_channel_id=900, leaderboard_channel_id=None)
        ),
    )
    set_settings = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(config_cog.config_service, "set_leaderboard_settings", set_settings)
    monkeypatch.setattr(config_cog.formatting, "build_config_show_embed", lambda _: discord.Embed())

    await _leaderboard_command().callback(cog, interaction, _mode_choice("daily"))

    assert set_settings.await_args.kwargs["channel_id"] == announce_channel.id


@pytest.mark.asyncio
async def test_config_leaderboard_explicit_channel_is_validated_and_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    permissions = SimpleNamespace(
        view_channel=True, send_messages=True, send_messages_in_threads=True, embed_links=True
    )
    explicit_channel = SimpleNamespace(
        id=701, guild=interaction.guild, permissions_for=lambda _member: permissions
    )
    cog = config_cog.ConfigCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=True, role_ids=frozenset())
    monkeypatch.setattr(config_cog, "actor_from_interaction", lambda _: actor)
    get_config = AsyncMock()
    monkeypatch.setattr(config_cog.config_service, "get_config", get_config)
    set_settings = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(config_cog.config_service, "set_leaderboard_settings", set_settings)
    monkeypatch.setattr(config_cog.formatting, "build_config_show_embed", lambda _: discord.Embed())

    await _leaderboard_command().callback(
        cog, interaction, _mode_choice("per_game"), explicit_channel
    )

    # An explicit channel is used as-is; the current-config read never
    # happens because there's nothing to fall back for or leave untouched.
    get_config.assert_not_awaited()
    assert set_settings.await_args.kwargs["channel_id"] == 701


@pytest.mark.asyncio
async def test_config_leaderboard_rejects_channel_bot_cannot_post_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    bad_permissions = SimpleNamespace(
        view_channel=True, send_messages=False, send_messages_in_threads=False, embed_links=True
    )
    explicit_channel = SimpleNamespace(
        id=701, guild=interaction.guild, permissions_for=lambda _member: bad_permissions
    )
    cog = config_cog.ConfigCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=True, role_ids=frozenset())
    monkeypatch.setattr(config_cog, "actor_from_interaction", lambda _: actor)
    set_settings = AsyncMock()
    monkeypatch.setattr(config_cog.config_service, "set_leaderboard_settings", set_settings)

    with pytest.raises(DomainValidationError, match="View Channel"):
        await _leaderboard_command().callback(
            cog, interaction, _mode_choice("per_game"), explicit_channel
        )

    set_settings.assert_not_awaited()


@pytest.mark.asyncio
async def test_config_leaderboard_clear_channel_explicitly_clears_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    cog = config_cog.ConfigCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=True, role_ids=frozenset())
    monkeypatch.setattr(config_cog, "actor_from_interaction", lambda _: actor)
    set_settings = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(config_cog.config_service, "set_leaderboard_settings", set_settings)
    monkeypatch.setattr(config_cog.formatting, "build_config_show_embed", lambda _: discord.Embed())

    await _leaderboard_command().callback(
        cog, interaction, _mode_choice("off"), None, None, None, True
    )

    assert set_settings.await_args.kwargs == {"mode": "off", "channel_id": None}


@pytest.mark.asyncio
async def test_config_leaderboard_clear_channel_conflicts_with_an_explicit_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    permissions = SimpleNamespace(
        view_channel=True, send_messages=True, send_messages_in_threads=True, embed_links=True
    )
    explicit_channel = SimpleNamespace(
        id=701, guild=interaction.guild, permissions_for=lambda _member: permissions
    )
    cog = config_cog.ConfigCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=True, role_ids=frozenset())
    monkeypatch.setattr(config_cog, "actor_from_interaction", lambda _: actor)
    set_settings = AsyncMock()
    monkeypatch.setattr(config_cog.config_service, "set_leaderboard_settings", set_settings)

    with pytest.raises(DomainValidationError, match="clear_channel"):
        await _leaderboard_command().callback(
            cog, interaction, _mode_choice("off"), explicit_channel, None, None, True
        )

    set_settings.assert_not_awaited()


@pytest.mark.asyncio
async def test_config_leaderboard_clear_channel_with_a_posting_mode_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    cog = config_cog.ConfigCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=True, role_ids=frozenset())
    monkeypatch.setattr(config_cog, "actor_from_interaction", lambda _: actor)
    set_settings = AsyncMock()
    monkeypatch.setattr(config_cog.config_service, "set_leaderboard_settings", set_settings)

    with pytest.raises(DomainValidationError, match="announcement channel"):
        await _leaderboard_command().callback(
            cog, interaction, _mode_choice("daily"), None, None, None, True
        )

    set_settings.assert_not_awaited()


@pytest.mark.asyncio
async def test_config_leaderboard_omitted_time_is_not_passed_and_explicit_value_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    permissions = SimpleNamespace(
        view_channel=True, send_messages=True, send_messages_in_threads=True, embed_links=True
    )
    explicit_channel = SimpleNamespace(
        id=701, guild=interaction.guild, permissions_for=lambda _member: permissions
    )
    cog = config_cog.ConfigCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=True, role_ids=frozenset())
    monkeypatch.setattr(config_cog, "actor_from_interaction", lambda _: actor)
    set_settings = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(config_cog.config_service, "set_leaderboard_settings", set_settings)
    monkeypatch.setattr(config_cog.formatting, "build_config_show_embed", lambda _: discord.Embed())

    await _leaderboard_command().callback(cog, interaction, _mode_choice("daily"), explicit_channel)
    assert "daily_time" not in set_settings.await_args.kwargs

    await _leaderboard_command().callback(
        cog, interaction, _mode_choice("daily"), explicit_channel, None, "7:30am"
    )
    assert set_settings.await_args.kwargs["daily_time"] == time(7, 30)


@pytest.mark.asyncio
async def test_config_leaderboard_omitted_scope_is_not_passed_and_explicit_value_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    permissions = SimpleNamespace(
        view_channel=True, send_messages=True, send_messages_in_threads=True, embed_links=True
    )
    explicit_channel = SimpleNamespace(
        id=701, guild=interaction.guild, permissions_for=lambda _member: permissions
    )
    cog = config_cog.ConfigCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=True, role_ids=frozenset())
    monkeypatch.setattr(config_cog, "actor_from_interaction", lambda _: actor)
    set_settings = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(config_cog.config_service, "set_leaderboard_settings", set_settings)
    monkeypatch.setattr(config_cog.formatting, "build_config_show_embed", lambda _: discord.Embed())

    await _leaderboard_command().callback(cog, interaction, _mode_choice("daily"), explicit_channel)
    assert "scope" not in set_settings.await_args.kwargs

    await _leaderboard_command().callback(
        cog, interaction, _mode_choice("daily"), explicit_channel, _mode_choice("all_time")
    )
    assert set_settings.await_args.kwargs["scope"] == "all_time"


# ---------------------------------------------------------------------------
# `mode` becoming optional too (Phase 5): now every option, `mode` included,
# follows the same "omitted means unchanged" rule, so a call must supply at
# least one of them, and the guards that used to just read `mode.value`
# must resolve the *effective* mode -- the newly supplied one if given,
# otherwise whatever is already stored.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_leaderboard_no_options_at_all_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = _interaction()
    cog = config_cog.ConfigCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=True, role_ids=frozenset())
    monkeypatch.setattr(config_cog, "actor_from_interaction", lambda _: actor)
    get_config = AsyncMock()
    monkeypatch.setattr(config_cog.config_service, "get_config", get_config)
    set_settings = AsyncMock()
    monkeypatch.setattr(config_cog.config_service, "set_leaderboard_settings", set_settings)

    with pytest.raises(DomainValidationError, match="at least one"):
        await _leaderboard_command().callback(cog, interaction, None)

    get_config.assert_not_awaited()
    set_settings.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()


@pytest.mark.asyncio
async def test_config_leaderboard_channel_only_leaves_mode_scope_and_time_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing only `channel` (no `mode`) must reach the service call with
    nothing but `channel_id` -- the mirror of the existing "mode only"
    coverage above, now that `mode` follows the same rule as every other
    option."""
    interaction = _interaction()
    permissions = SimpleNamespace(
        view_channel=True, send_messages=True, send_messages_in_threads=True, embed_links=True
    )
    explicit_channel = SimpleNamespace(
        id=701, guild=interaction.guild, permissions_for=lambda _member: permissions
    )
    cog = config_cog.ConfigCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=True, role_ids=frozenset())
    monkeypatch.setattr(config_cog, "actor_from_interaction", lambda _: actor)
    get_config = AsyncMock()
    monkeypatch.setattr(config_cog.config_service, "get_config", get_config)
    set_settings = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(config_cog.config_service, "set_leaderboard_settings", set_settings)
    monkeypatch.setattr(config_cog.formatting, "build_config_show_embed", lambda _: discord.Embed())

    await _leaderboard_command().callback(cog, interaction, None, explicit_channel)

    # Nothing to fall back for or leave untouched -- the current-config read
    # never happens, same as when an explicit channel accompanies an
    # explicit mode.
    get_config.assert_not_awaited()
    assert set_settings.await_args.kwargs == {"channel_id": 701}


@pytest.mark.asyncio
async def test_config_leaderboard_clear_channel_with_a_stored_posting_mode_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mode` omitted, `clear_channel` supplied: the "no destination" guard
    must react to the *stored* mode -- it must not assume "off" (or skip
    the check entirely) just because no mode was passed this call."""
    interaction = _interaction()
    cog = config_cog.ConfigCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=True, role_ids=frozenset())
    monkeypatch.setattr(config_cog, "actor_from_interaction", lambda _: actor)
    get_config = AsyncMock(
        return_value=_config_with_channels(announce_channel_id=None, leaderboard_mode="daily")
    )
    monkeypatch.setattr(config_cog.config_service, "get_config", get_config)
    set_settings = AsyncMock()
    monkeypatch.setattr(config_cog.config_service, "set_leaderboard_settings", set_settings)

    with pytest.raises(DomainValidationError, match="announcement channel"):
        await _leaderboard_command().callback(cog, interaction, None, None, None, None, True)

    get_config.assert_awaited_once()
    set_settings.assert_not_awaited()


@pytest.mark.asyncio
async def test_config_leaderboard_clear_channel_with_a_stored_off_mode_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mirror case: a stored mode of `off` makes a bare `clear_channel`
    (no `mode` passed) perfectly fine, and the write reaches the service
    call without a `mode` kwarg -- the stored mode is left untouched."""
    interaction = _interaction()
    cog = config_cog.ConfigCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=True, role_ids=frozenset())
    monkeypatch.setattr(config_cog, "actor_from_interaction", lambda _: actor)
    get_config = AsyncMock(
        return_value=_config_with_channels(announce_channel_id=None, leaderboard_mode="off")
    )
    monkeypatch.setattr(config_cog.config_service, "get_config", get_config)
    set_settings = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(config_cog.config_service, "set_leaderboard_settings", set_settings)
    monkeypatch.setattr(config_cog.formatting, "build_config_show_embed", lambda _: discord.Embed())

    await _leaderboard_command().callback(cog, interaction, None, None, None, None, True)

    get_config.assert_awaited_once()
    assert set_settings.await_args.kwargs == {"channel_id": None}


@pytest.mark.asyncio
async def test_config_leaderboard_omitted_mode_uses_stored_mode_for_channel_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The channel-fallback guard must resolve its effective mode from the
    stored value too, not just the clear_channel guard: changing only
    `scope` on a guild already in `daily` mode, with no leaderboard channel
    configured yet, must still fall back to the announcement channel."""
    interaction = _interaction()
    announce_channel = interaction.channel  # already permission-valid in _interaction()
    interaction.guild.get_channel = lambda channel_id: (
        announce_channel if channel_id == 900 else None
    )
    cog = config_cog.ConfigCog(SimpleNamespace(pool="pool"))
    actor = Actor(user_id=456, has_manage_guild=True, role_ids=frozenset())
    monkeypatch.setattr(config_cog, "actor_from_interaction", lambda _: actor)
    monkeypatch.setattr(
        config_cog.config_service,
        "get_config",
        AsyncMock(
            return_value=_config_with_channels(
                announce_channel_id=900, leaderboard_channel_id=None, leaderboard_mode="daily"
            )
        ),
    )
    set_settings = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(config_cog.config_service, "set_leaderboard_settings", set_settings)
    monkeypatch.setattr(config_cog.formatting, "build_config_show_embed", lambda _: discord.Embed())

    await _leaderboard_command().callback(cog, interaction, None, None, _mode_choice("all_time"))

    assert set_settings.await_args.kwargs["channel_id"] == announce_channel.id
    assert "mode" not in set_settings.await_args.kwargs
