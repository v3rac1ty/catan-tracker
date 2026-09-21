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
from catan_bot.domain.scoring import ScoreSource, entry_fields
from catan_bot.services import config_service, game_service, season_service
from catan_bot.services.context import Actor
from catan_bot.services.errors import PermissionDeniedError
from catan_bot.views import game_confirm, score_entry

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
        self.response = SimpleNamespace(
            defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()
        )
        self.followup = SimpleNamespace(send=AsyncMock())
        self.edit_original_response = AsyncMock(
            return_value=SimpleNamespace(channel=SimpleNamespace(id=700), id=800)
        )
        # Only exercised by the "confirm anyway" dialog flow
        # (`views/game_confirm.py`'s `_prompt_confirm_anyway`/`_ConfirmAnywayView`):
        # it awaits `interaction.original_response()` right after showing the
        # ephemeral dialog, and reads `interaction.message` -- the *public*
        # message the persistent Confirm button lives on -- to know what to
        # edit once "Confirm anyway" is clicked. A test sets `.message`
        # explicitly when it needs that path.
        self.original_response = AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))
        self.message: SimpleNamespace | None = None


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


def _numeric_values(numeric_sources: tuple[ScoreSource, ...], total: int) -> dict[str, int]:
    """Distribute `total` points across `numeric_sources`, honoring each
    source's even-parity requirement.

    Built from `entry_fields` rather than a hardcoded set of keys (e.g.
    "settlements"/"cities"/"vp_cards") because those only exist for a
    `normal` game -- a Cities & Knights game has a different numeric
    catalog entirely (`metropolis_bonus` instead of `vp_cards`, ...), and
    this helper needs to keep working for whichever rules a test builds.
    """
    values = {source.key: 0 for source in numeric_sources}
    remaining = total
    for source in numeric_sources:
        if remaining <= 0:
            break
        take = remaining - (remaining % 2) if source.requires_even else remaining
        values[source.key] = take
        remaining -= take
    assert remaining == 0, f"couldn't distribute {total} across {numeric_sources!r}"
    return values


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

    # `record_player_score` has no integration coverage anywhere else, so
    # this is the natural place to exercise it for real, including the
    # deadlock the `enforce_winner_target=False` fix protects against: a
    # winner's numeric and award halves arrive as two separate saves
    # (`views/score_entry.py`'s modal, then its award select), so the
    # winner's *first* save -- below target -- must succeed on its own, or
    # they could never reach the second save that claims the award.
    game_before_scores = await game_service.get_game(pool, guild_id, game_id)
    rules = score_entry.rules_for_game(game_before_scores.game)
    numeric_sources, award_sources = entry_fields(rules)
    target = rules.target_points
    assert target is not None
    assert award_sources, "this test needs at least one fixed-value award to claim"
    winning_award = award_sources[0]
    below_target_total = target - winning_award.fixed_points
    assert 0 <= below_target_total < target
    below_target_numeric = _numeric_values(numeric_sources, below_target_total)

    await game_service.record_player_score(
        pool,
        guild_id,
        game_id,
        reporter.user_id,
        numeric=below_target_numeric,
        awards=[],
        now=datetime.now(UTC),
    )
    mid_status = await game_service.score_collection_status(pool, guild_id, game_id)
    mid_game = await game_service.get_game(pool, guild_id, game_id)
    winner_row = next(score for score in mid_game.scores if score.user_id == reporter.user_id)
    # The below-target save landed -- it was never rejected -- and the
    # shortfall it leaves is visible via the same property the confirm
    # dialog reads from, against a real recorded row rather than a stub.
    assert winner_row.total_points == below_target_total
    assert mid_status.winner_shortfall == target - below_target_total

    # Claiming the award now is what pushes the winner's total to (at
    # least) target -- exactly the save that used to deadlock.
    await game_service.record_player_score(
        pool,
        guild_id,
        game_id,
        reporter.user_id,
        numeric=below_target_numeric,
        awards=[winning_award.key],
        now=datetime.now(UTC),
    )

    other_numeric = _numeric_values(numeric_sources, max(target - 4, 1))
    await game_service.record_player_score(
        pool,
        guild_id,
        game_id,
        other_player.user_id,
        numeric=other_numeric,
        awards=[],
        now=datetime.now(UTC),
    )

    status = await game_service.score_collection_status(pool, guild_id, game_id)
    assert status.complete
    assert status.winner_shortfall is None

    # Same refresh report.callback itself does once collection finishes --
    # the public message's progress field must reflect the now-complete
    # collection.
    updated_game = await game_service.get_game(pool, guild_id, game_id)
    await public_message.edit(
        embed=formatting.build_game_report_embed(updated_game, collection=status),
        view=game_confirm.build_game_action_view(game_id),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    assert public_message.edit.await_count == 2
    complete_embed = public_message.edit.await_args.kwargs["embed"]
    complete_progress = next(
        field.value for field in complete_embed.fields if field.name == "Score entry"
    )
    assert complete_progress == "All scores received (2/2)."

    error_handler = AsyncMock()
    monkeypatch.setattr(game_confirm, "actor_from_interaction", lambda _: reporter)
    monkeypatch.setattr(game_confirm, "handle_interaction_error", error_handler)
    reporter_click = InteractionStub(guild_id, reporter.user_id, pool=pool)
    await game_confirm.GameActionButton(game_id, "confirm").callback(reporter_click)

    error_handler.assert_awaited_once()
    denied = error_handler.await_args.args[1]
    assert isinstance(denied, PermissionDeniedError)
    assert "another player" in denied.user_message

    # Collection is complete and the winner is recorded at target, so this
    # confirms directly in one click -- no "confirm anyway" dialog, and
    # `edit_original_response` (not `response.send_message`) carries the
    # result.
    monkeypatch.setattr(game_confirm, "actor_from_interaction", lambda _: other_player)
    confirmer_click = InteractionStub(guild_id, other_player.user_id, pool=pool)
    await game_confirm.GameActionButton(game_id, "confirm").callback(confirmer_click)
    confirmer_click.response.send_message.assert_not_awaited()
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


async def test_confirm_button_dialog_for_partial_collection_then_confirm_anyway(
    pool: asyncpg.Pool, guild_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The branch that broke the happy-path test above, pinned down on its
    own: clicking Confirm while `game_score_requests` still has open rows
    must show the ephemeral "confirm anyway" dialog rather than confirming
    outright (`views.game_confirm.GameActionButton.callback`'s
    confirm-preflight guard), and only actually confirms -- editing the
    public message with partial scores -- once that dialog's own
    "Confirm anyway" button is pressed.
    """
    reporter = _actor(11)
    other_player = _actor(21)
    report_interaction = InteractionStub(guild_id, reporter.user_id)
    game_cog_instance = game_cog.GameCog(SimpleNamespace(pool=pool))
    monkeypatch.setattr(game_cog, "actor_from_interaction", lambda _: reporter)
    report = game_cog.GameCog.game_group.get_command("report")
    assert report is not None

    reporter_member = _member_stub(reporter.user_id, dm_channel_id=930, dm_message_id=931)
    other_member = _member_stub(other_player.user_id, dm_channel_id=940, dm_message_id=941)

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

    game_id = await game_service.find_open_score_request_game_id(pool, guild_id, reporter.user_id)
    assert game_id is not None
    public_message = report_interaction.channel.send.return_value
    # Only report.callback's own progress refresh has touched it so far.
    public_message.edit.assert_awaited_once()

    # Neither participant has submitted a score -- both `game_score_requests`
    # rows are still open, so Confirm must show the dialog, never confirm
    # directly.
    monkeypatch.setattr(game_confirm, "actor_from_interaction", lambda _: other_player)
    confirm_click = InteractionStub(guild_id, other_player.user_id, pool=pool)
    # A real component interaction's `.message` is the message its button
    # lives on -- the public game message, here.
    confirm_click.message = public_message

    await game_confirm.GameActionButton(game_id, "confirm").callback(confirm_click)

    confirm_click.response.defer.assert_not_awaited()
    confirm_click.edit_original_response.assert_not_awaited()
    confirm_click.response.send_message.assert_awaited_once()
    content = confirm_click.response.send_message.await_args.args[0]
    assert formatting.mention(reporter.user_id) in content
    assert formatting.mention(other_player.user_id) in content
    assert "haven't entered their points" in content
    assert "short of the" not in content  # no recorded winner row yet
    dialog_kwargs = confirm_click.response.send_message.await_args.kwargs
    assert dialog_kwargs["ephemeral"] is True
    dialog = dialog_kwargs["view"]
    assert isinstance(dialog, game_confirm._ConfirmAnywayView)
    assert dialog.public_message is public_message
    confirm_click.original_response.assert_awaited_once()

    # The game is still pending, and the public message untouched by this
    # click -- only "Confirm anyway" may change either.
    still_pending = await game_service.get_game(pool, guild_id, game_id)
    assert still_pending.game.status == "pending"
    public_message.edit.assert_awaited_once()

    # Pressing "Confirm anyway" on the dialog now must confirm for real.
    dialog_click = InteractionStub(guild_id, other_player.user_id, pool=pool)
    await dialog.confirm_anyway(dialog_click)

    dialog_click.response.edit_message.assert_awaited_once()
    assert public_message.edit.await_count == 2
    confirmed_embed = public_message.edit.await_args.kwargs["embed"]
    assert (
        next(field.value for field in confirmed_embed.fields if field.name == "Status")
        == "Confirmed"
    )
    assert public_message.edit.await_args.kwargs["view"] is None
    dialog_click.edit_original_response.assert_awaited_once()
    final_kwargs = dialog_click.edit_original_response.await_args.kwargs
    assert final_kwargs["content"] == "Confirmed -- game saved with partial scores."
    assert final_kwargs["view"] is None

    confirmed_game = await game_service.get_game(pool, guild_id, game_id)
    assert confirmed_game.game.status == "confirmed"


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
