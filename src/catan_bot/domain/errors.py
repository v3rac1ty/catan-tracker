"""Domain-level errors that are safe to surface to Discord users.

`domain/` never talks to Discord directly, but every validation failure it
raises eventually gets shown to a user, so the message text on this
exception is the one place in the domain layer that is explicitly a
user-facing contract: short, friendly, and free of internals (no stack
traces, SQL, file paths, or raw exception reprs).
"""

from __future__ import annotations


class DomainValidationError(ValueError):
    """A user-supplied value failed a domain rule.

    `user_message` is the text a cog should show back to the user, verbatim
    (after Discord's own escaping at display time).
    """

    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message
