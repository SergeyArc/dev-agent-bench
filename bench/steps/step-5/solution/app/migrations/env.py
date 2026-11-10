"""Alembic environment: runs the revisions against the database of DATABASE_URL."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

from tracksvc.config import read_database_url
from tracksvc.infrastructure.models import Base

config = context.config
if config.config_file_name is not None:
    # Loggers of a process that runs the migrations from inside (the tests) stay enabled.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Revisions are written by hand; the metadata of the ORM models lets `alembic check`
# compare them with the migrated schema.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=read_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(read_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
