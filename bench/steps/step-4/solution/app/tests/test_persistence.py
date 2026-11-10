from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from tests.conftest import RunSql, alembic, assert_error
from tracksvc.api.app import create_app

type Json = dict[str, Any]

TRACK_ROWS = "SELECT id::text, code, capacity, status FROM tracks ORDER BY code"
SEGMENT_ROWS = (
    "SELECT track_id::text, label, span_start, span_end FROM track_segments "
    "ORDER BY track_id, span_start"
)


def create_track(client: TestClient, code: str = "TRK-A1", capacity: int = 100) -> Json:
    response = client.post("/tracks", json={"code": code, "capacity": capacity})
    assert response.status_code == 201
    track: Json = response.json()
    return track


def add(client: TestClient, track: Json, label: str, start: int, end: int) -> Response:
    body = {"label": label, "span": {"start": start, "end": end}}
    return client.post(f"/tracks/{track['id']}/segments", json=body)


def rows_of(track: Json) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """Rows the database must hold for a track the API returned."""
    track_rows = [(track["id"], track["code"], track["capacity"], track["status"])]
    segment_rows = [
        (track["id"], segment["label"], segment["span"]["start"], segment["span"]["end"])
        for segment in track["segments"]
    ]
    return track_rows, segment_rows


def stored_rows(run_sql: RunSql) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    track_rows = [tuple(row) for row in run_sql(TRACK_ROWS)]
    segment_rows = [tuple(row) for row in run_sql(SEGMENT_ROWS)]
    return track_rows, segment_rows


def test_a_created_track_is_one_row_with_the_values_of_the_response(
    client: TestClient, run_sql: RunSql
) -> None:
    track = create_track(client)

    assert stored_rows(run_sql) == rows_of(track)


def test_the_rows_follow_every_change_of_the_segments(client: TestClient, run_sql: RunSql) -> None:
    track = create_track(client)

    added = [add(client, track, "beta", 10, 25).json(), add(client, track, "alpha", 0, 10).json()]
    assert stored_rows(run_sql) == rows_of(added[-1])

    removed = client.delete(f"/tracks/{track['id']}/segments/beta").json()
    assert stored_rows(run_sql) == rows_of(removed)

    reused = add(client, track, "gamma", 10, 25).json()
    assert stored_rows(run_sql) == rows_of(reused)

    sealed = client.post(f"/tracks/{track['id']}/seal").json()
    assert sealed["status"] == "sealed"
    assert stored_rows(run_sql) == rows_of(sealed)


def test_a_rejected_request_leaves_the_rows_as_they_were(
    client: TestClient, run_sql: RunSql
) -> None:
    track = create_track(client)
    add(client, track, "alpha", 0, 10)
    path = f"/tracks/{track['id']}"
    before = stored_rows(run_sql)

    responses = [
        client.post("/tracks", json={"code": "TRK-A1", "capacity": 5}),
        client.post("/tracks", json={"code": "TRK-B2", "capacity": 0}),
        add(client, track, "beta", 5, 15),
        add(client, track, "alpha", 20, 30),
        add(client, track, "beta", 20, 20),
        add(client, track, "beta", 90, 101),
        add(client, track, "Beta", 20, 30),
        client.delete(f"{path}/segments/beta"),
        client.post(f"/tracks/{uuid4()}/seal"),
    ]

    assert [response.status_code for response in responses] == [
        409,
        422,
        409,
        409,
        409,
        409,
        422,
        404,
        404,
    ]
    assert stored_rows(run_sql) == before


@pytest.mark.usefixtures("database")
def test_a_track_is_served_by_every_application_on_the_database() -> None:
    with TestClient(create_app()) as first, TestClient(create_app()) as second:
        track = create_track(first)

        added = add(second, track, "alpha", 0, 10)

        assert added.status_code == 201
        assert first.get(f"/tracks/{track['id']}").json() == added.json()
        assert_error(
            second.post("/tracks", json={"code": "TRK-A1", "capacity": 5}), 409, "track_code_taken"
        )


@pytest.mark.usefixtures("database")
def test_a_sealed_track_comes_back_unchanged_after_a_restart() -> None:
    with TestClient(create_app()) as client:
        track = create_track(client)
        add(client, track, "beta", 10, 25)
        add(client, track, "alpha", 0, 10)
        sealed = client.post(f"/tracks/{track['id']}/seal").json()

    with TestClient(create_app()) as restarted:
        response = restarted.get(f"/tracks/{track['id']}")

        assert response.status_code == 200
        assert response.json() == sealed
        assert_error(add(restarted, track, "gamma", 30, 40), 409, "track_sealed")


def test_earlier_ids_are_not_found_after_the_schema_is_recreated(client: TestClient) -> None:
    track = create_track(client)

    alembic("downgrade", "base")
    alembic("upgrade", "head")

    assert_error(client.get(f"/tracks/{track['id']}"), 404, "track_not_found")
    assert create_track(client)["id"] != track["id"]


def test_a_change_made_directly_in_the_database_is_returned_by_the_next_get(
    client: TestClient, run_sql: RunSql
) -> None:
    track = create_track(client)
    assert client.get(f"/tracks/{track['id']}").json()["capacity"] == 100

    run_sql(f"UPDATE tracks SET capacity = 500 WHERE id = '{track['id']}'")

    assert client.get(f"/tracks/{track['id']}").json() == {**track, "capacity": 500}


def test_a_row_deleted_directly_in_the_database_is_not_found(
    client: TestClient, run_sql: RunSql
) -> None:
    track = create_track(client)

    run_sql(f"DELETE FROM tracks WHERE id = '{track['id']}'")

    assert_error(client.get(f"/tracks/{track['id']}"), 404, "track_not_found")
    assert_error(client.post(f"/tracks/{track['id']}/seal"), 404, "track_not_found")


@pytest.mark.parametrize("attempt", range(3))
def test_simultaneous_creations_of_one_code_create_exactly_one_track(
    client: TestClient, run_sql: RunSql, attempt: int
) -> None:
    body = {"code": f"TRK-{attempt}", "capacity": 100}

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(lambda _: client.post("/tracks", json=body), range(10)))

    assert sorted(response.status_code for response in responses) == [201] + [409] * 9
    for response in responses:
        if response.status_code == 409:
            assert_error(response, 409, "track_code_taken")
    assert len(run_sql(TRACK_ROWS)) == 1


@pytest.mark.parametrize("attempt", range(3))
def test_simultaneous_additions_of_one_span_add_exactly_one_segment(
    client: TestClient, run_sql: RunSql, attempt: int
) -> None:
    track = create_track(client, code=f"TRK-{attempt}")

    with ThreadPoolExecutor(max_workers=10) as pool:
        responses = list(pool.map(lambda index: add(client, track, f"s{index}", 0, 10), range(10)))

    assert sorted(response.status_code for response in responses) == [201] + [409] * 9
    for response in responses:
        if response.status_code == 409:
            assert_error(response, 409, "segment_overlap")
    assert len(run_sql(SEGMENT_ROWS)) == 1
    assert len(client.get(f"/tracks/{track['id']}").json()["segments"]) == 1


def test_every_request_releases_its_session(client: TestClient) -> None:
    track = create_track(client)
    add(client, track, "alpha", 0, 10)
    path = f"/tracks/{track['id']}"

    # More requests of every outcome than the pool has connections: a request that kept
    # its session would exhaust the pool.
    for _ in range(20):
        assert client.get(path).status_code == 200
        assert add(client, track, "beta", 5, 15).status_code == 409
        assert client.get(f"/tracks/{uuid4()}").status_code == 404
        assert add(client, track, "Beta", 5, 15).status_code == 422

    assert client.get("/ready").status_code == 200


def test_track_routes_report_an_unreachable_database_without_a_server_error(
    monkeypatch: pytest.MonkeyPatch, closed_port: int
) -> None:
    monkeypatch.setenv("DATABASE_URL", f"postgresql+asyncpg://u:p@127.0.0.1:{closed_port}/none")

    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        assert_error(
            client.post("/tracks", json={"code": "TRK-A1", "capacity": 1}),
            503,
            "database_unavailable",
        )
        assert_error(client.get(f"/tracks/{uuid4()}"), 503, "database_unavailable")
        assert_error(client.post(f"/tracks/{uuid4()}/seal"), 503, "database_unavailable")
