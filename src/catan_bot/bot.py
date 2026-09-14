"""Bot skeleton: pool lifecycle, cog loading, command sync."""

from __future__ import annotations

import logging

import asyncpg
import discord
from discord.ext import commands

from catan_bot.config import BotSettings
from catan_bot.db.pool import create_pool

logger = logging.getLogger(__name__)

# Cogs to load at startup, in order. Extended in later milestones.
INITIAL_COGS: tuple[str, ...] = ("catan_bot.cogs.help_cog",)


class CatanBot(commands.Bot):
    """The Catan Tracker bot.

    Uses only default (non-privileged) intents: slash command interactions
    carry resolved member/user data already, so no privileged gateway intent
    is needed. Mentions are opt-in per-message (`AllowedMentions.none()` by
    default); features that need to ping someone must pass an explicit
    `AllowedMentions` for just that call.
    """

    def __init__(self, settings: BotSettings) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.pool: asyncpg.Pool | None = None

    async def setup_hook(self) -> None:
        self.pool = await create_pool(self.settings.database_url.get_secret_value())
        logger.info("Database pool created")

        for extension in INITIAL_COGS:
            await self.load_extension(extension)
            logger.info("Loaded extension %s", extension)

        if self.settings.dev_guild_id is not None:
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info(
                "Synced %d command(s) to dev guild %s", len(synced), self.settings.dev_guild_id
            )
        else:
            synced = await self.tree.sync()
            logger.info("Synced %d command(s) globally", len(synced))

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            logger.info("Database pool closed")
        await super().close()
