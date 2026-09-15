"""Services layer: orchestrates domain validation and repository calls.

Every function here takes an `asyncpg.Pool` (never a bare `conn`) and opens
its own transaction, so a cog never manages transactions directly. Nothing
in this package imports `discord`, runs SQL directly (it calls
`catan_bot.db.repositories.*` only), or reads the system clock -- every
function that needs "now" or "today" takes it as an explicit `now: datetime`
parameter. See `tests/static/test_services_boundary.py` for the enforcement.
"""

from __future__ import annotations
