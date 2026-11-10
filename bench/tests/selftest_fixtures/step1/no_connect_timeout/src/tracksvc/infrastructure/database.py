from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def is_ready(engine: AsyncEngine) -> bool:
    """Tell whether a `SELECT 1` round-trip succeeds (broken on purpose: no timeout)."""
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return False
    return True
