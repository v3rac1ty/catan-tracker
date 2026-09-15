from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from pydantic import SecretStr

from catan_bot import bot as bot_module
from catan_bot.bot import CatanBot
from catan_bot.cogs.config_cog import ConfigCog
from catan_bot.cogs.game_cog import GameCog
from catan_bot.cogs.help_cog import HelpCog
from catan_bot.cogs.season_cog import SeasonCog
from catan_bot.cogs.stats_cog import StatsCog
from catan_bot.config import BotSettings
from catan_bot.views.game_confirm import GameActionButton


def _settings(*, sync: bool, guild_id: int | None = None) -> BotSettings:
    return BotSettings(
        discord_token=SecretStr("token-placeholder"),
        database_url=SecretStr("postgresql://user:password@localhost/db"),
        sync_commands=sync,
        dev_guild_id=guild_id,
        _env_file=None,
    )


@pytest.mark.asyncio
async def test_all_m4_commands_register_in_an_offline_tree() -> None:
    bot = CatanBot(_settings(sync=False))
    try:
        for cog_type in (ConfigCog, SeasonCog, GameCog, StatsCog, HelpCog):
            await bot.add_cog(cog_type(bot))

        top_level = {command.name: command for command in bot.tree.get_commands()}
        assert set(top_level) == {"config", "season", "game", "leaderboard", "stats", "help"}
        assert {command.name for command in top_level["config"].commands} == {
            "channel",
            "timezone",
            "admin-role",
            "show",
        }
        assert {command.name for command in top_level["season"].commands} == {
            "start",
            "min-games",
            "end-date",
            "end",
            "cancel",
            "info",
            "history",
        }
        assert {command.name for command in top_level["game"].commands} == {
            "report",
            "void",
            "history",
        }
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_setup_hook_loads_cogs_and_dynamic_button_without_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = CatanBot(_settings(sync=False))
    pool = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(bot_module, "create_pool", AsyncMock(return_value=pool))
    load = AsyncMock()
    monkeypatch.setattr(bot, "load_extension", load)
    add_dynamic = Mock()
    monkeypatch.setattr(bot, "add_dynamic_items", add_dynamic)
    sync = AsyncMock()
    monkeypatch.setattr(bot.tree, "sync", sync)

    await bot.setup_hook()

    add_dynamic.assert_called_once_with(GameActionButton)
    assert [call.args[0] for call in load.await_args_list] == list(bot_module.INITIAL_COGS)
    sync.assert_not_awaited()
    await bot.close()


@pytest.mark.asyncio
async def test_setup_hook_syncs_only_to_configured_dev_guild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = CatanBot(_settings(sync=True, guild_id=987))
    pool = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(bot_module, "create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(bot, "load_extension", AsyncMock())
    monkeypatch.setattr(bot, "add_dynamic_items", Mock())
    copy = Mock()
    monkeypatch.setattr(bot.tree, "copy_global_to", copy)
    sync = AsyncMock(return_value=[])
    monkeypatch.setattr(bot.tree, "sync", sync)

    await bot.setup_hook()

    copy.assert_called_once()
    guild = copy.call_args.kwargs["guild"]
    assert isinstance(guild, discord.Object)
    assert guild.id == 987
    sync.assert_awaited_once()
    assert sync.await_args.kwargs["guild"].id == 987
    await bot.close()


def test_sync_commands_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "token-placeholder")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@localhost/db")
    monkeypatch.delenv("SYNC_COMMANDS", raising=False)

    assert BotSettings(_env_file=None).sync_commands is False
