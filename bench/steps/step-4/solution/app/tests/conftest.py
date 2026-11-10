"""Shared fixtures.

The tests own the database ``DATABASE_URL`` points to: it must be reachable. They migrate
it themselves, delete the tracks it holds and, when the run ends, leave it migrated or
unmigrated as they found it.
"""

import asyncio
import os
import socket
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from alembic.config import main
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tracksvc.api.app import create_app
from tracksvc.domain.track import Track
from tracksvc.infrastructure.database import create_engine, create_session_factory

PROJECT_ROOT = Path(__file__).resolve().parent.parent

type RunSql = Callable[[str], Sequence[Row[Any]]]


def alembic(*arguments: str) -> None:
    """Run an Alembic command with the configuration of the project."""
    main(argv=["--config", str(PROJECT_ROOT / "alembic.ini"), *arguments])


def execute_sql(statement: str) -> Sequence[Row[Any]]:
    """Run one SQL statement on the test database, commit it and return its rows."""

    async def execute() -> Sequence[Row[Any]]:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                result = await connection.execute(text(statement))
                return result.all() if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(execute())


def table_exists(name: str) -> bool:
    return execute_sql(f"SELECT to_regclass('public.{name}')")[0][0] is not None


def remove_schema() -> None:
    """Take the database back to the state before the first migration."""
    alembic("downgrade", "base")
    execute_sql("DROP TABLE IF EXISTS alembic_version")


@pytest.fixture(scope="session")
def migrated_schema() -> Iterator[None]:
    """Migrate the database for the test run; afterwards restore what was there before."""
    was_migrated = table_exists("alembic_version")
    alembic("upgrade", "head")
    yield
    if not was_migrated:
        remove_schema()


@pytest.fixture
def database(migrated_schema: None) -> None:
    """Migrated database that holds no tracks."""
    execute_sql("TRUNCATE tracks CASCADE")


@pytest.fixture
def client(database: None) -> Iterator[TestClient]:
    """Client of a freshly started application that is shut down after the test."""
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def run_sql() -> RunSql:
    """Function that runs one SQL statement on the test database and returns its rows."""
    return execute_sql


@pytest_asyncio.fixture
async def session_factory(database: None) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Session factory of the application on an engine that is disposed of after the test."""
    engine = create_engine(os.environ["DATABASE_URL"])
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def closed_port() -> int:
    """Local TCP port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@pytest.fixture
def silent_port() -> Iterator[int]:
    """Local TCP port that accepts connections and never answers."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port: int = listener.getsockname()[1]
        yield port


def assert_error(response: Response, status_code: int, code: str) -> None:
    assert response.status_code == status_code
    assert response.headers["content-type"] == "application/json"
    assert set(response.json()) == {"error"}
    error = response.json()["error"]
    assert set(error) == {"code", "message"}
    assert error["code"] == code
    assert isinstance(error["message"], str)
    assert error["message"]


def assert_validation_error(response: Response, field: str) -> None:
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/json"
    assert set(response.json()) == {"error"}
    error = response.json()["error"]
    assert set(error) == {"code", "message", "details"}
    assert error["code"] == "validation_error"
    assert isinstance(error["message"], str)
    assert error["message"]
    assert error["details"]
    for item in error["details"]:
        assert set(item) == {"field", "message"}
        assert isinstance(item["field"], str)
        assert isinstance(item["message"], str)
    assert field in [item["field"] for item in error["details"]]


def track_state(track: Track) -> tuple[object, ...]:
    """Everything a track consists of, for comparing two tracks."""
    return (track.id, track.code, track.capacity, track.status, track.segments)
