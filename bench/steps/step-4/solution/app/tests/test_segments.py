from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from tests.conftest import assert_error, assert_validation_error

type Json = dict[str, Any]


def create_track(client: TestClient, code: str = "TRK-A1", capacity: int = 100) -> Json:
    response = client.post("/tracks", json={"code": code, "capacity": capacity})
    assert response.status_code == 201
    track: Json = response.json()
    return track


def segment(label: str, start: int, end: int) -> Json:
    return {"label": label, "span": {"start": start, "end": end}}


def add(client: TestClient, track: Json, label: str, start: int, end: int) -> Response:
    return client.post(f"/tracks/{track['id']}/segments", json=segment(label, start, end))


def read(client: TestClient, track: Json) -> Json:
    response = client.get(f"/tracks/{track['id']}")
    assert response.status_code == 200
    body: Json = response.json()
    return body


@pytest.fixture
def track(client: TestClient) -> Json:
    return create_track(client)


@pytest.fixture
def sealed_track(client: TestClient, track: Json) -> Json:
    assert add(client, track, "alpha", 0, 10).status_code == 201
    response = client.post(f"/tracks/{track['id']}/seal")
    assert response.status_code == 200
    sealed: Json = response.json()
    return sealed


def test_add_returns_the_track_with_the_segment(client: TestClient, track: Json) -> None:
    response = add(client, track, "alpha", 0, 10)

    assert response.status_code == 201
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {**track, "segments": [segment("alpha", 0, 10)]}
    assert read(client, track) == response.json()


def test_segments_are_listed_by_span_start_whatever_the_insertion_order(
    client: TestClient, track: Json
) -> None:
    add(client, track, "gamma", 50, 60)
    add(client, track, "beta", 10, 25)

    response = add(client, track, "alpha", 0, 10)

    expected = [segment("alpha", 0, 10), segment("beta", 10, 25), segment("gamma", 50, 60)]
    assert response.json()["segments"] == expected
    assert read(client, track)["segments"] == expected


def test_spans_that_touch_are_accepted(client: TestClient, track: Json) -> None:
    add(client, track, "beta", 10, 20)

    assert add(client, track, "alpha", 0, 10).status_code == 201
    assert add(client, track, "gamma", 20, 30).status_code == 201


@pytest.mark.parametrize(("start", "end"), [(0, 100), (99, 100), (0, 1)])
def test_a_span_within_the_capacity_is_accepted(
    client: TestClient, track: Json, start: int, end: int
) -> None:
    assert add(client, track, "alpha", start, end).status_code == 201


@pytest.mark.parametrize(
    ("label", "start", "end", "code"),
    [
        ("gamma", 9, 9, "invalid_span"),
        ("gamma", 60, 50, "invalid_span"),
        ("gamma", 0, 0, "invalid_span"),
        ("gamma", 90, 101, "span_out_of_bounds"),
        ("gamma", 100, 101, "span_out_of_bounds"),
        ("gamma", 50, 10000, "span_out_of_bounds"),
        ("alpha", 50, 60, "segment_label_taken"),
        ("gamma", 5, 12, "segment_overlap"),
        ("gamma", 20, 30, "segment_overlap"),
        ("gamma", 12, 20, "segment_overlap"),
        ("gamma", 0, 40, "segment_overlap"),
        ("gamma", 10, 25, "segment_overlap"),
    ],
)
def test_a_segment_that_breaks_an_invariant_is_rejected_without_effect(
    client: TestClient, track: Json, label: str, start: int, end: int, code: str
) -> None:
    add(client, track, "alpha", 0, 10)
    add(client, track, "beta", 10, 25)
    before = read(client, track)

    response = add(client, track, label, start, end)

    assert_error(response, 409, code)
    assert read(client, track) == before


def test_the_same_label_is_accepted_in_another_track(client: TestClient, track: Json) -> None:
    other = create_track(client, code="TRK-B2")
    add(client, track, "alpha", 0, 10)

    assert add(client, other, "alpha", 0, 10).status_code == 201


def test_the_eighth_segment_is_accepted_and_the_ninth_rejected(
    client: TestClient, track: Json
) -> None:
    statuses = [add(client, track, f"s{i}", i * 10, i * 10 + 10).status_code for i in range(8)]
    before = read(client, track)

    response = add(client, track, "s8", 80, 90)

    assert statuses == [201] * 8
    assert_error(response, 409, "segment_limit_reached")
    assert len(before["segments"]) == 8
    assert read(client, track) == before


def test_remove_returns_the_track_without_the_segment(client: TestClient, track: Json) -> None:
    add(client, track, "alpha", 0, 10)
    add(client, track, "beta", 10, 25)

    response = client.delete(f"/tracks/{track['id']}/segments/beta")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {**track, "segments": [segment("alpha", 0, 10)]}
    assert read(client, track) == response.json()


def test_a_removed_span_can_be_reserved_again(client: TestClient, track: Json) -> None:
    add(client, track, "alpha", 0, 10)
    client.delete(f"/tracks/{track['id']}/segments/alpha")

    response = add(client, track, "beta", 0, 10)

    assert response.status_code == 201
    assert response.json()["segments"] == [segment("beta", 0, 10)]


def test_removing_an_absent_label_is_not_found(client: TestClient, track: Json) -> None:
    add(client, track, "alpha", 0, 10)
    before = read(client, track)

    response = client.delete(f"/tracks/{track['id']}/segments/beta")

    assert_error(response, 404, "segment_not_found")
    assert read(client, track) == before


def test_remove_and_seal_do_not_read_a_request_body(client: TestClient, track: Json) -> None:
    add(client, track, "alpha", 0, 10)
    add(client, track, "beta", 10, 20)
    path = f"/tracks/{track['id']}"

    removed = client.request("DELETE", f"{path}/segments/beta", content=b"{not json")
    sealed = client.post(f"{path}/seal", content=b"{not json")

    assert removed.status_code == 200
    assert sealed.status_code == 200


def test_seal_returns_the_sealed_track(client: TestClient, track: Json) -> None:
    add(client, track, "alpha", 0, 10)

    response = client.post(f"/tracks/{track['id']}/seal")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {**track, "status": "sealed", "segments": [segment("alpha", 0, 10)]}
    assert read(client, track) == response.json()


def test_an_empty_track_cannot_be_sealed(client: TestClient, track: Json) -> None:
    response = client.post(f"/tracks/{track['id']}/seal")

    assert_error(response, 409, "track_empty")
    assert read(client, track) == track


def test_a_sealed_track_rejects_every_change(client: TestClient, sealed_track: Json) -> None:
    path = f"/tracks/{sealed_track['id']}"

    responses = [
        add(client, sealed_track, "beta", 10, 20),
        client.delete(f"{path}/segments/alpha"),
        client.post(f"{path}/seal"),
    ]

    for response in responses:
        assert_error(response, 409, "track_sealed")
    assert read(client, sealed_track) == sealed_track


def test_an_unknown_track_is_not_found_on_every_route(client: TestClient) -> None:
    path = f"/tracks/{uuid4()}"

    assert_error(client.post(f"{path}/segments", json=segment("a", 0, 10)), 404, "track_not_found")
    assert_error(client.delete(f"{path}/segments/a"), 404, "track_not_found")
    assert_error(client.post(f"{path}/seal"), 404, "track_not_found")


def test_an_unknown_track_is_reported_before_a_broken_invariant(client: TestClient) -> None:
    response = client.post(f"/tracks/{uuid4()}/segments", json=segment("a", 5, 5))

    assert_error(response, 404, "track_not_found")


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"span": {"start": 0, "end": 10}}, "label"),
        ({"label": "a"}, "span"),
        ({"label": "a", "span": {"end": 10}}, "span.start"),
        ({"label": "a", "span": {"start": 0}}, "span.end"),
        ({"label": 5, "span": {"start": 0, "end": 10}}, "label"),
        ({"label": None, "span": {"start": 0, "end": 10}}, "label"),
        ({"label": "", "span": {"start": 0, "end": 10}}, "label"),
        ({"label": "Gamma", "span": {"start": 0, "end": 10}}, "label"),
        ({"label": "1a", "span": {"start": 0, "end": 10}}, "label"),
        ({"label": "a-b", "span": {"start": 0, "end": 10}}, "label"),
        ({"label": "a" * 17, "span": {"start": 0, "end": 10}}, "label"),
        ({"label": "a", "span": None}, "span"),
        ({"label": "a", "span": [0, 10]}, "span"),
        ({"label": "a", "span": "0-10"}, "span"),
        ({"label": "a", "span": {"start": "0", "end": 10}}, "span.start"),
        ({"label": "a", "span": {"start": 0.5, "end": 10}}, "span.start"),
        ({"label": "a", "span": {"start": True, "end": 10}}, "span.start"),
        ({"label": "a", "span": {"start": None, "end": 10}}, "span.start"),
        ({"label": "a", "span": {"start": -1, "end": 10}}, "span.start"),
        ({"label": "a", "span": {"start": 10001, "end": 10}}, "span.start"),
        ({"label": "a", "span": {"start": 0, "end": "10"}}, "span.end"),
        ({"label": "a", "span": {"start": 0, "end": 10.5}}, "span.end"),
        ({"label": "a", "span": {"start": 0, "end": False}}, "span.end"),
        ({"label": "a", "span": {"start": 0, "end": None}}, "span.end"),
        ({"label": "a", "span": {"start": 0, "end": -1}}, "span.end"),
        ({"label": "a", "span": {"start": 0, "end": 10001}}, "span.end"),
        ([segment("a", 0, 10)], "body"),
        ("a", "body"),
        (None, "body"),
    ],
)
def test_add_rejects_an_invalid_field_and_names_it(
    client: TestClient, track: Json, body: Any, field: str
) -> None:
    response = client.post(f"/tracks/{track['id']}/segments", json=body)

    assert_validation_error(response, field)
    assert read(client, track) == track


@pytest.mark.parametrize("content", [b"", b"{", b'{"label": "a", "span": '])
def test_add_rejects_a_body_that_is_not_json(
    client: TestClient, track: Json, content: bytes
) -> None:
    response = client.post(
        f"/tracks/{track['id']}/segments",
        content=content,
        headers={"content-type": "application/json"},
    )

    assert_validation_error(response, "body")


@pytest.mark.parametrize(
    ("label", "start", "end"), [("a", 0, 10000), ("a" * 16, 0, 1), ("z_9", 9999, 10000)]
)
def test_add_accepts_the_limits_of_label_and_span(
    client: TestClient, label: str, start: int, end: int
) -> None:
    track = create_track(client, capacity=10000)

    assert add(client, track, label, start, end).status_code == 201


def test_validation_is_reported_before_an_unknown_track(client: TestClient) -> None:
    path = f"/tracks/{uuid4()}"

    assert_validation_error(client.post(f"{path}/segments", json=segment("A", 0, 10)), "label")
    assert_validation_error(client.delete(f"{path}/segments/Gamma"), "label")


@pytest.mark.parametrize(
    "method_and_suffix", [("POST", "/segments"), ("DELETE", "/segments/a"), ("POST", "/seal")]
)
def test_a_track_id_that_is_not_a_uuid_is_rejected(
    client: TestClient, method_and_suffix: tuple[str, str]
) -> None:
    method, suffix = method_and_suffix

    response = client.request(method, f"/tracks/not-a-uuid{suffix}", json=segment("a", 0, 10))

    assert_validation_error(response, "track_id")


@pytest.mark.parametrize("label", ["Gamma", "1a", "a-b", "a" * 17])
def test_remove_rejects_an_invalid_label(client: TestClient, track: Json, label: str) -> None:
    add(client, track, "alpha", 0, 10)

    response = client.delete(f"/tracks/{track['id']}/segments/{label}")

    assert_validation_error(response, "label")
    assert read(client, track)["segments"] == [segment("alpha", 0, 10)]


def test_tracks_do_not_share_segments(client: TestClient, track: Json) -> None:
    other = create_track(client, code="TRK-B2")

    add(client, track, "alpha", 0, 10)

    assert read(client, other) == other
