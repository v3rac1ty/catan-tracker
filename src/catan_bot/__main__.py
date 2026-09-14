"""Entry point: `python -m catan_bot`."""

from __future__ import annotations

import asyncio
import logging

from catan_bot.bot import CatanBot
from catan_bot.config import BotSettings

logger = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def _main() -> None:
    settings = BotSettings()
    _configure_logging(settings.log_level)

    bot = CatanBot(settings)
    async with bot:
        # Never log the token; get_secret_value() is only used to pass it to
        # discord.py's connection code.
        await bot.start(settings.discord_token.get_secret_value())


if __name__ == "__main__":
    asyncio.run(_main())
