"""Typed application settings.

Split into two settings classes so that `python -m catan_bot.db.migrate` never
needs a Discord token, and the bot process never needs the migrator DSN.
Secrets (`SecretStr`) are never logged or printed.
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class MigrateSettings(BaseSettings):
    """Settings for the migration runner (`catan_migrator` role)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    migrator_database_url: SecretStr


class BotSettings(BaseSettings):
    """Settings for the Discord bot process (`catan_app` role)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    discord_token: SecretStr
    database_url: SecretStr
    dev_guild_id: int | None = None
    log_level: str = "INFO"
