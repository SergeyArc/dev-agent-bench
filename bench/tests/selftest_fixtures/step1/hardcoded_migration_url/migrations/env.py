import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

# Broken on purpose: DATABASE_URL is ignored.
HARD_CODED_URL = "postgresql+asyncpg://tracksvc:tracksvc@127.0.0.1:5432/tracksvc"


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


async def _main(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


asyncio.run(_main(HARD_CODED_URL))
