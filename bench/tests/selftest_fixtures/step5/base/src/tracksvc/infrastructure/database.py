import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

PROBE_TIMEOUT = 5.0
# Shorter than PROBE_TIMEOUT: once the query is abandoned, the probe deadline is still
# ahead and cuts off the release of a connection that no longer answers.
QUERY_TIMEOUT = 2.0


async def is_ready(engine: AsyncEngine) -> bool:
    """Tell whether a `SELECT 1` round-trip to the database succeeds."""
    try:
        async with asyncio.timeout(PROBE_TIMEOUT), engine.connect() as connection:
            await asyncio.wait_for(connection.execute(text("SELECT 1")), QUERY_TIMEOUT)
    except Exception:
        return False
    return True
