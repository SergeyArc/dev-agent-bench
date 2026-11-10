from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from tests.conftest import assert_error, assert_validation_error

VALID_BODY = {"code": "TRK-A1", "capacity": 100}


def test_create_returns_the_new_open_track_without_segments(client: TestClient) -> None:
    response = client.post("/tracks", json=VALID_BODY)

    assert response.status_code == 201
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert set(body) == {"id", "code", "capacity", "status", "segments"}
    assert body["code"] == "TRK-A1"
    assert body["capacity"] == 100
    assert body["status"] == "open"
    assert body["segments"] == []


def test_created_tracks_get_distinct_canonical_uuids(client: TestClient) -> None:
    first = client.post("/tracks", json={"code": "TRK-A1", "capacity": 1}).json()["id"]
    second = client.post("/tracks", json={"code": "TRK-B2", "capacity": 1}).json()["id"]

    assert str(UUID(first)) == first
    assert str(UUID(second)) == second
    assert first != second


@pytest.mark.parametrize(
    "body",
    [
        {"code": "ABC", "capacity": 1},
        {"code": "A" + "9" * 31, "capacity": 10000},
        {"code": "Z-0-", "capacity": 5000},
    ],
)
def test_create_accepts_the_limits_of_code_and_capacity(
    client: TestClient, body: dict[str, Any]
) -> None:
    response = client.post("/tracks", json=body)

    assert response.status_code == 201
    assert response.json()["code"] == body["code"]
    assert response.json()["capacity"] == body["capacity"]


def test_get_returns_the_body_of_the_creation_response(client: TestClient) -> None:
    created = client.post("/tracks", json=VALID_BODY).json()
    client.post("/tracks", json={"code": "TRK-B2", "capacity": 7})

    response = client.get(f"/tracks/{created['id']}")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == created


def test_get_reports_an_unknown_id_as_not_found(client: TestClient) -> None:
    client.post("/tracks", json=VALID_BODY)

    assert_error(client.get(f"/tracks/{uuid4()}"), 404, "track_not_found")


def test_get_rejects_an_id_that_is_not_a_uuid(client: TestClient) -> None:
    assert_validation_error(client.get("/tracks/not-a-uuid"), "track_id")


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"capacity": 100}, "code"),
        ({"code": "TRK-A1"}, "capacity"),
        ({}, "code"),
        ({}, "capacity"),
        ({"code": 123, "capacity": 100}, "code"),
        ({"code": None, "capacity": 100}, "code"),
        ({"code": True, "capacity": 100}, "code"),
        ({"code": ["TRK-A1"], "capacity": 100}, "code"),
        ({"code": "", "capacity": 100}, "code"),
        ({"code": "AB", "capacity": 100}, "code"),
        ({"code": "A" * 33, "capacity": 100}, "code"),
        ({"code": "trk-a1", "capacity": 100}, "code"),
        ({"code": "1TRK", "capacity": 100}, "code"),
        ({"code": "TRK_A1", "capacity": 100}, "code"),
        ({"code": "TRK-A1\n", "capacity": 100}, "code"),
        ({"code": "TRK-A1", "capacity": "5"}, "capacity"),
        ({"code": "TRK-A1", "capacity": 5.5}, "capacity"),
        ({"code": "TRK-A1", "capacity": True}, "capacity"),
        ({"code": "TRK-A1", "capacity": None}, "capacity"),
        ({"code": "TRK-A1", "capacity": [5]}, "capacity"),
        ({"code": "TRK-A1", "capacity": 0}, "capacity"),
        ({"code": "TRK-A1", "capacity": -1}, "capacity"),
        ({"code": "TRK-A1", "capacity": 10001}, "capacity"),
    ],
)
def test_create_rejects_an_invalid_field_and_names_it(
    client: TestClient, body: dict[str, Any], field: str
) -> None:
    assert_validation_error(client.post("/tracks", json=body), field)


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{",
        b"not json",
        b'{"code": "TRK-A1", "capacity": 100',
        b"\x80abc",
        b'{"code": "TRK-\xff", "capacity": 100}',
        b"[" * 100000,
    ],
)
def test_create_rejects_a_body_that_is_not_json(client: TestClient, content: bytes) -> None:
    response = client.post("/tracks", content=content, headers={"content-type": "application/json"})

    assert_validation_error(response, "body")


@pytest.mark.parametrize("body", [[1], [VALID_BODY], "TRK-A1", 5, True, None])
def test_create_rejects_a_body_that_is_not_a_json_object(client: TestClient, body: Any) -> None:
    assert_validation_error(client.post("/tracks", json=body), "body")


def test_a_rejected_request_creates_no_track(client: TestClient) -> None:
    client.post("/tracks", json={"code": "TRK-A1", "capacity": 0})

    assert client.post("/tracks", json=VALID_BODY).status_code == 201


def test_create_rejects_a_code_that_is_taken_and_keeps_the_first_track(
    client: TestClient,
) -> None:
    created = client.post("/tracks", json=VALID_BODY).json()

    response = client.post("/tracks", json={"code": "TRK-A1", "capacity": 5})

    assert_error(response, 409, "track_code_taken")
    assert client.get(f"/tracks/{created['id']}").json() == created


def test_validation_is_reported_before_a_taken_code(client: TestClient) -> None:
    client.post("/tracks", json=VALID_BODY)

    response = client.post("/tracks", json={"code": "TRK-A1", "capacity": 0})

    assert_validation_error(response, "capacity")
