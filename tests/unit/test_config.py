"""Unit tests for settings: empty-env handling and secret redaction in errors.

No Docker required. `_env_file=None` plus monkeypatched process env keeps
these hermetic (the real, git-ignored `.env` is never read).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from catan_bot.config import BotSettings


def test_empty_dev_guild_id_becomes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # docker compose passes DEV_GUILD_ID="" when the caller left it unset in
    # .env; that must not crash settings parsing (int("") would raise).
    monkeypatch.setenv("DISCORD_TOKEN", "tok-placeholder")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")
    monkeypatch.setenv("DEV_GUILD_ID", "")

    settings = BotSettings(_env_file=None)

    assert settings.dev_guild_id is None


def test_validation_error_does_not_leak_token_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "tok-SECRETTAIL-123")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        BotSettings(_env_file=None)

    assert "SECRETTAIL" not in str(exc_info.value)


def test_validation_error_does_not_leak_dsn_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://catan_app:SECRETTAIL456@localhost/catan")
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    with pytest.raises(ValidationError) as exc_info:
        BotSettings(_env_file=None)

    assert "SECRETTAIL456" not in str(exc_info.value)
