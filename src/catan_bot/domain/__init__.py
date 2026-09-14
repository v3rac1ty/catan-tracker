"""Pure domain logic: no `discord`, no `asyncpg`, no I/O, no clock reads.

Every function that needs "now" or "today" takes it as an explicit
parameter, which is what keeps everything here trivially unit-testable and
restart-safe. See `tests/static/test_domain_purity.py` for the enforcement.
"""

from __future__ import annotations
