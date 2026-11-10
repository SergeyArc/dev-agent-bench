"""Shared fixtures.

The tests own the database ``DATABASE_URL`` points to: it must be reachable, and every
test that changes it restores what it changed.
"""

import asyncio
import os
import socket
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tracksvc.api.app import create_app
from tracksvc.domain.track import Track

type RunSql = Callable[[str], Sequence[Row[Any]]]


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Client of a freshly started application that is shut down after the test."""
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def run_sql() -> RunSql:
    """Function that runs one SQL statement on the test database and returns its rows."""

    async def execute(statement: str) -> Sequence[Row[Any]]:
        engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                result = await connection.execute(text(statement))
                return result.all() if result.returns_rows else []
        finally:
            await engine.dispose()

    return lambda statement: asyncio.run(execute(statement))


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
