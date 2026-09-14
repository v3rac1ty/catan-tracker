"""asyncpg connection pool factory."""

from __future__ import annotations

import asyncpg


async def create_pool(dsn: str) -> asyncpg.Pool:
    """Create the shared asyncpg pool.

    Small pool (1-5 connections) sized for a single bot process; a short
    command timeout plus a server-side statement_timeout bound worst-case
    query latency (defense in depth against slow/blind SQLi techniques such
    as pg_sleep-based time attacks).
    """
    return await asyncpg.create_pool(
        dsn,
        min_size=1,
        max_size=5,
        command_timeout=10,
        server_settings={
            "statement_timeout": "5000",
            "application_name": "catan_bot",
        },
    )
