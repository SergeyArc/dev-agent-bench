"""Composition root of the worker process."""

import asyncio
import logging
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracksvc.application.ports import UnitOfWork
from tracksvc.config import read_consumer_max_retries, read_database_url, read_rabbitmq_url
from tracksvc.infrastructure.database import create_engine, create_session_factory
from tracksvc.infrastructure.unit_of_work import SqlAlchemyUnitOfWork
from tracksvc.worker.consumer import Consumer
from tracksvc.worker.handler import MessageHandler, UnitOfWorkProvider


def unit_of_work_provider(session_factory: async_sessionmaker[AsyncSession]) -> UnitOfWorkProvider:
    """Return the provider of the unit of work of one consumed message.

    This is the only place where the worker creates sessions: each call of the provider
    opens a session for one message and closes it on every path.
    """

    @asynccontextmanager
    async def unit_of_work() -> AsyncIterator[UnitOfWork]:
        async with session_factory() as session:
            yield SqlAlchemyUnitOfWork(session)

    return unit_of_work


async def run(stop: asyncio.Event) -> None:
    """Read the configuration, wire the worker and consume until ``stop`` is set.

    Owns the database engine: creates it here and disposes of it before returning.
    """
    database_url = read_database_url()
    rabbitmq_url = read_rabbitmq_url()
    max_retries = read_consumer_max_retries()
    engine = create_engine(database_url)
    try:
        handler = MessageHandler(unit_of_work_provider(create_session_factory(engine)), max_retries)
        await Consumer(rabbitmq_url, handler).run(stop)
    finally:
        await engine.dispose()


async def _run_until_signalled() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signal_number, stop.set)
    await run(stop)


def main() -> None:
    """Run the worker until it receives SIGTERM or SIGINT."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(_run_until_signalled())
