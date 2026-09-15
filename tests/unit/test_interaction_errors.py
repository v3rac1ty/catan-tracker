"""Interaction error mapping, privacy, and log-redaction regressions."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from catan_bot.domain.errors import DomainValidationError
from catan_bot.errors import handle_interaction_error
from catan_bot.services.errors import ServiceError


class _FakePostgresError(Exception):
    sqlstate = "23505"


_FakePostgresError.__module__ = "asyncpg.exceptions"


def _interaction(
    *,
    done: bool = False,
    response_type: discord.InteractionResponseType | None = None,
    loading: bool = False,
) -> SimpleNamespace:
    response = SimpleNamespace(
        is_done=Mock(return_value=done),
        type=response_type,
        send_message=AsyncMock(),
    )
    return SimpleNamespace(
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
        original_response=AsyncMock(
            return_value=SimpleNamespace(flags=SimpleNamespace(loading=loading))
        ),
        command=SimpleNamespace(qualified_name="game report"),
        guild_id=123,
    )


@pytest.mark.asyncio
async def test_expected_error_is_escaped_truncated_and_sent_ephemerally() -> None:
    interaction = _interaction()
    separator = chr(0x200B)
    message = "**bad** @everyone " + separator + "x" * 3000

    await handle_interaction_error(interaction, DomainValidationError(message))

    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.await_args
    assert len(args[0]) == 2000
    assert args[0].startswith(r"\*\*bad\*\* @" + separator + "everyone x")
    assert kwargs["ephemeral"] is True
    assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()


@pytest.mark.asyncio
async def test_public_defer_is_completed_before_private_error_followup() -> None:
    interaction = _interaction(
        done=True,
        response_type=discord.InteractionResponseType.deferred_channel_message,
        loading=True,
    )

    await handle_interaction_error(interaction, ServiceError("Try a different date."))

    interaction.edit_original_response.assert_awaited_once()
    edit_kwargs = interaction.edit_original_response.await_args.kwargs
    assert edit_kwargs["content"] == "That command could not be completed."
    assert edit_kwargs["embed"] is None
    assert edit_kwargs["view"] is None
    assert edit_kwargs["allowed_mentions"].to_dict() == (discord.AllowedMentions.none().to_dict())
    interaction.followup.send.assert_awaited_once()
    followup_args, followup_kwargs = interaction.followup.send.await_args
    assert followup_args == ("Try a different date.",)
    assert followup_kwargs["ephemeral"] is True
    assert followup_kwargs["allowed_mentions"].to_dict() == (
        discord.AllowedMentions.none().to_dict()
    )


@pytest.mark.asyncio
async def test_completed_non_deferred_interaction_uses_private_followup_only() -> None:
    interaction = _interaction(
        done=True, response_type=discord.InteractionResponseType.channel_message
    )

    await handle_interaction_error(interaction, DomainValidationError("Invalid date."))

    interaction.edit_original_response.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_completed_deferred_success_is_not_overwritten_by_later_error() -> None:
    interaction = _interaction(
        done=True,
        response_type=discord.InteractionResponseType.deferred_channel_message,
        loading=False,
    )

    await handle_interaction_error(interaction, RuntimeError("late bookkeeping failed"))

    interaction.original_response.assert_awaited_once()
    interaction.edit_original_response.assert_not_awaited()
    interaction.followup.send.assert_awaited_once()
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_unexpected_error_never_logs_or_replies_with_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    interaction = _interaction()
    secret = "postgres://user:password@example.invalid/db DETAIL: private-row"

    with caplog.at_level(logging.ERROR, logger="catan_bot.errors"):
        await handle_interaction_error(interaction, RuntimeError(secret))

    assert secret not in caplog.text
    assert "password" not in caplog.text
    assert "DETAIL" not in caplog.text
    assert "RuntimeError" in caplog.text
    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.args[0] == (
        "Something went wrong. Please try again."
    )


@pytest.mark.asyncio
async def test_postgres_error_anywhere_in_wrapper_graph_is_generic_and_sanitized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    interaction = _interaction()
    database_error = _FakePostgresError("DETAIL: api-key=top-secret")
    middle = RuntimeError("outer-secret")
    middle.__context__ = database_error
    wrapper = RuntimeError("wrapper-secret")
    wrapper.original = middle  # type: ignore[attr-defined]

    with caplog.at_level(logging.ERROR, logger="catan_bot.errors"):
        await handle_interaction_error(interaction, wrapper)

    assert "23505" in caplog.text
    assert "top-secret" not in caplog.text
    assert "DETAIL" not in caplog.text
    assert interaction.response.send_message.await_args.args[0] == (
        "Something went wrong. Please try again."
    )


@pytest.mark.asyncio
async def test_mapped_service_error_keeps_safe_message_despite_postgres_cause() -> None:
    interaction = _interaction()
    error = ServiceError("A season is already active.")
    error.__cause__ = _FakePostgresError("DETAIL: private-row")

    await handle_interaction_error(interaction, error)

    assert interaction.response.send_message.await_args.args[0] == "A season is already active."
