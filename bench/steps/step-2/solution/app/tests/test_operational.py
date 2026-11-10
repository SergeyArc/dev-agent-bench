import os
import time

import pytest
from fastapi.testclient import TestClient

from tests.conftest import RunSql
from tracksvc.api.app import create_app


def assert_database_unavailable(client: TestClient) -> None:
    response = client.get("/ready")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/json"
    error = response.json()["error"]
    assert set(response.json()) == {"error"}
    assert set(error) == {"code", "message"}
    assert error["code"] == "database_unavailable"
    assert isinstance(error["message"], str)
    assert error["message"]


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


def test_ready_reports_ready_when_database_answers(client: TestClient) -> None:
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ready"}


def test_ready_releases_its_connection_after_every_call(client: TestClient) -> None:
    # More calls than the pool can hold: a probe that kept its connection would run out.
    statuses = [client.get("/ready").status_code for _ in range(30)]

    assert statuses == [200] * 30


def test_app_starts_and_reports_unavailable_when_database_port_is_closed(
    monkeypatch: pytest.MonkeyPatch, closed_port: int
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"postgresql+asyncpg://u:p@127.0.0.1:{closed_port}/none")

    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        assert_database_unavailable(client)


def test_ready_reports_unavailable_in_bounded_time_when_database_never_answers(
    monkeypatch: pytest.MonkeyPatch, silent_port: int
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"postgresql+asyncpg://u:p@127.0.0.1:{silent_port}/none")

    with TestClient(create_app()) as client:
        started = time.monotonic()
        assert_database_unavailable(client)
        assert time.monotonic() - started < 10
        assert client.get("/health").status_code == 200


def test_ready_reports_unavailable_when_server_rejects_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = os.environ["DATABASE_URL"].rsplit("/", 1)[0]
    monkeypatch.setenv("DATABASE_URL", f"{server}/tracksvc_no_such_database")

    with TestClient(create_app()) as client:
        assert_database_unavailable(client)


@pytest.mark.parametrize("broker_url", [None, "amqp://u:p@127.0.0.1:1/"])
def test_app_does_not_need_the_broker(
    monkeypatch: pytest.MonkeyPatch, broker_url: str | None
) -> None:
    if broker_url is None:
        monkeypatch.delenv("RABBITMQ_URL", raising=False)
    else:
        monkeypatch.setenv("RABBITMQ_URL", broker_url)

    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 200


def test_app_creates_no_database_objects(run_sql: RunSql) -> None:
    list_tables = "SELECT schemaname, tablename FROM pg_tables WHERE schemaname = 'public'"
    before = run_sql(list_tables)

    with TestClient(create_app()) as client:
        assert client.get("/ready").status_code == 200

    assert run_sql(list_tables) == before
