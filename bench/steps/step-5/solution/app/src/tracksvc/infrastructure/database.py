"""Database engine and session construction, and the readiness probe."""

import asyncio

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

CONNECT_TIMEOUT_SECONDS = 2.0
READINESS_QUERY_TIMEOUT_SECONDS = 2.0
# Longer than connecting plus the query. The query timeout abandons a statement the
# database does not answer; this deadline then stops waiting for the driver to close
# that dead connection, which can otherwise take a minute.
READINESS_TIMEOUT_SECONDS = 5.0

# What a failing database surfaces as. SQLAlchemy wraps what the server reports; network
# failures while connecting (refused, unresolved host) and timeouts (TimeoutError) arrive
# as OSError.
DATABASE_FAILURES = (SQLAlchemyError, OSError)


def create_engine(database_url: str) -> AsyncEngine:
    """Build the engine for ``database_url`` without opening a connection.

    Connecting is bounded by ``CONNECT_TIMEOUT_SECONDS``. The caller owns the returned
    engine and must dispose of it.
    """
    return create_async_engine(database_url, connect_args={"timeout": CONNECT_TIMEOUT_SECONDS})


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the factory of sessions that take their connections from ``engine``.

    A session opens no connection until it is used. Whoever creates a session closes it.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


async def is_database_ready(engine: AsyncEngine) -> bool:
    """Tell whether a ``SELECT 1`` round-trip to the database succeeds.

    The call returns within ``READINESS_TIMEOUT_SECONDS`` and releases its connection
    before returning. Connection and query failures are reported as ``False``, never
    raised.
    """
    try:
        async with asyncio.timeout(READINESS_TIMEOUT_SECONDS), engine.connect() as connection:
            await asyncio.wait_for(
                connection.execute(text("SELECT 1")), READINESS_QUERY_TIMEOUT_SECONDS
            )
    except DATABASE_FAILURES:
        return False
    return True
