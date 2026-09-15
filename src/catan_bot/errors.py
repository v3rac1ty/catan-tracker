"""Shared interaction error handling.

One function, `handle_interaction_error`, is used both as the
`app_commands.CommandTree.on_error` handler (wired up in `bot.py`) and,
directly, inside every `discord.ui.DynamicItem` button callback in
`views/game_confirm.py` -- a persistent `DynamicItem` is dispatched through
a plain `discord.ui.View` reconstructed on the fly (per discord.py's docs,
"custom view subclasses cannot be accessed from this item"), so there is no
custom `View.on_error` to override for it. Routing both paths through the
same function keeps the mapping from exception to user-facing message (and
the log-sanitization rule) in exactly one place.

Never logs `str(exc)`, a DETAIL/HINT message, or raw user input: see the
M3b audit lesson (`season_service._log_resolution_failure`) and DESIGN.md's
"Required before M5 N1". `asyncpg` is never imported here (the boundary
rule for `errors.py` forbids it), so a `PostgresError` anywhere in the
`__cause__`/`__context__` chain is detected by duck-typing: its module name
starts with `asyncpg` and it carries a `sqlstate` attribute.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from catan_bot.domain.errors import DomainValidationError
from catan_bot.formatting import escape_user_text, truncate
from catan_bot.services.errors import ServiceError

logger = logging.getLogger(__name__)

_GENERIC_ERROR_MESSAGE = "Something went wrong. Please try again."
_DEFERRED_ERROR_MESSAGE = "That command could not be completed."
_NO_PRIVATE_MESSAGE_TEXT = "This command can only be used in a server."
_MESSAGE_MAX = 2000

_NO_MENTIONS = discord.AllowedMentions.none()


def _unwrap(error: BaseException) -> BaseException:
    """The exception a command/button callback actually raised.

    `app_commands` wraps anything a command callback raises (other than a
    check failure like `CommandOnCooldown`/`NoPrivateMessage`, which it
    raises itself) in a `CommandInvokeError`, with the real exception on
    `.original`. Button callbacks in this codebase never wrap their own
    exceptions, so `error` is already unwrapped for them; `getattr` simply
    finds nothing to unwrap in that case.
    """
    seen: set[int] = set()
    current = error
    while id(current) not in seen:
        seen.add(id(current))
        original = getattr(current, "original", None)
        if not isinstance(original, BaseException):
            break
        current = original
    return current


def _is_postgres_error(exc: BaseException) -> bool:
    """Duck-typed `isinstance(exc, asyncpg.PostgresError)`, without importing asyncpg."""
    return type(exc).__module__.startswith("asyncpg") and getattr(exc, "sqlstate", None) is not None


def _chain(exc: BaseException) -> list[BaseException]:
    """Walk wrapper, cause, and context links, including cycles only once."""
    seen: set[int] = set()
    out: list[BaseException] = []
    pending = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        out.append(current)
        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        original = getattr(current, "original", None)
        if isinstance(original, BaseException):
            pending.append(original)
    return out


def _postgres_error_in_chain(exc: BaseException) -> BaseException | None:
    for candidate in _chain(exc):
        if _is_postgres_error(candidate):
            return candidate
    return None


def _log_unexpected(command_name: str, guild_id: int | None, exc: BaseException) -> None:
    postgres_error = _postgres_error_in_chain(exc)
    if postgres_error is not None:
        # Never `exc_info` here: a traceback ends by rendering the deepest
        # frame's exception via `str()`, and `PostgresError.__str__`
        # appends the server's DETAIL/HINT text (which can carry row
        # contents). `sqlstate` is a fixed 5-character error class code,
        # never server-supplied free text, so it's always safe to log.
        logger.error(
            "Unhandled error in command=%s guild_id=%s: %s (sqlstate=%s)",
            command_name,
            guild_id,
            type(exc).__name__,
            _safe_sqlstate(postgres_error),
        )
    else:
        # Tracebacks and exception messages can contain command input,
        # credentials, file paths, or other secrets. The exception class is
        # enough to correlate this event with metrics without copying any of
        # those values into a long-lived log.
        logger.error(
            "Unhandled error in command=%s guild_id=%s: %s",
            command_name,
            guild_id,
            type(exc).__name__,
        )


def _safe_sqlstate(exc: BaseException) -> str:
    sqlstate = getattr(exc, "sqlstate", None)
    if not isinstance(sqlstate, str) or len(sqlstate) != 5 or not sqlstate.isascii():
        return "unknown"
    if not all(ch.isdigit() or "A" <= ch <= "Z" for ch in sqlstate):
        return "unknown"
    return sqlstate


async def _respond_ephemeral(interaction: discord.Interaction, message: str) -> None:
    message = truncate(escape_user_text(message), _MESSAGE_MAX)
    if interaction.response.is_done():
        if interaction.response.type is discord.InteractionResponseType.deferred_channel_message:
            original = await interaction.original_response()
            if original.flags.loading:
                # Discord treats the first followup after an unresolved
                # channel-message defer as the original response and ignores
                # its ephemeral flag. Complete that placeholder first; the
                # subsequent followup is then genuinely private. A response
                # that no longer has the loading flag is already a real
                # success message and must be left intact.
                await interaction.edit_original_response(
                    content=_DEFERRED_ERROR_MESSAGE,
                    embed=None,
                    view=None,
                    allowed_mentions=_NO_MENTIONS,
                )
        await interaction.followup.send(message, ephemeral=True, allowed_mentions=_NO_MENTIONS)
    else:
        await interaction.response.send_message(
            message, ephemeral=True, allowed_mentions=_NO_MENTIONS
        )


def _command_name(interaction: discord.Interaction, command_name: str | None) -> str:
    if command_name is not None:
        return command_name
    command = interaction.command
    return command.qualified_name if command is not None else "unknown"


async def handle_interaction_error(
    interaction: discord.Interaction, error: BaseException, *, command_name: str | None = None
) -> None:
    """Map `error` to a fixed, ephemeral user message, and log the rest safely.

    `command_name` overrides `interaction.command` (which is `None` for a
    component/button interaction) -- callers in `views/game_confirm.py`
    pass an explicit name like `"game:confirm"`.
    """
    exc = _unwrap(error)
    name = _command_name(interaction, command_name)

    if isinstance(exc, DomainValidationError | ServiceError):
        await _respond_ephemeral(interaction, exc.user_message)
        return
    if isinstance(exc, app_commands.CommandOnCooldown):
        seconds = round(exc.retry_after)
        await _respond_ephemeral(interaction, f"Slow down -- try again in {seconds} seconds.")
        return
    if isinstance(exc, app_commands.NoPrivateMessage):
        await _respond_ephemeral(interaction, _NO_PRIVATE_MESSAGE_TEXT)
        return

    _log_unexpected(name, interaction.guild_id, exc)
    await _respond_ephemeral(interaction, _GENERIC_ERROR_MESSAGE)


async def on_tree_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    """`CommandTree.on_error`'s exact signature, wired up in `bot.py`."""
    await handle_interaction_error(interaction, error)
