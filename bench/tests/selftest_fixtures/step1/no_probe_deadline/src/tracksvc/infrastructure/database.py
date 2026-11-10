import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

STEP_TIMEOUT = 2.0


async def is_ready(engine: AsyncEngine) -> bool:
    """Tell whether a `SELECT 1` round-trip succeeds (broken on purpose: connecting and
    the query are bounded, releasing the connection is not)."""
    try:
        connection = await asyncio.wait_for(engine.connect(), STEP_TIMEOUT)
        try:
            await asyncio.wait_for(connection.execute(text("SELECT 1")), STEP_TIMEOUT)
        finally:
            await connection.close()
    except Exception:
        return False
    return True
