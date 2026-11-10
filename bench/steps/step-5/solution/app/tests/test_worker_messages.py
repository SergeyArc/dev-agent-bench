from uuid import UUID

import pytest

from tracksvc.worker.messages import is_valid_message_id, parse_body, parse_retry_count

TRACK_ID = "0b9f6c1e-5f0a-4e0e-9d57-3c8f5a1f2b11"


@pytest.mark.parametrize("message_id", ["m", "m-1", " ", "~", "a b/c:d", "x" * 255])
def test_printable_ascii_message_ids_are_valid(message_id: str) -> None:
    assert is_valid_message_id(message_id)


@pytest.mark.parametrize(
    "message_id", [None, "", "x" * 256, "tab\t", "line\n", "é", "\x7f", "id\x00"]
)
def test_other_message_ids_are_invalid(message_id: str | None) -> None:
    assert not is_valid_message_id(message_id)


@pytest.mark.parametrize(
    ("headers", "retry_count"),
    [({}, 0), ({"other": "x"}, 0), ({"x-retry-count": 0}, 0), ({"x-retry-count": 7}, 7)],
)
def test_the_retry_count_is_read_from_the_header(
    headers: dict[str, object], retry_count: int
) -> None:
    assert parse_retry_count(headers) == retry_count


@pytest.mark.parametrize("value", ["1", -1, True, False, 1.0, None, b"1", [1]])
def test_a_retry_count_that_is_not_a_non_negative_integer_is_invalid(value: object) -> None:
    assert parse_retry_count({"x-retry-count": value}) is None


@pytest.mark.parametrize("delta", [1, 5, 1000])
def test_a_valid_body_is_parsed(delta: int) -> None:
    body = parse_body(f'{{"track_id": "{TRACK_ID}", "delta": {delta}}}'.encode())

    assert body is not None
    assert body.track_id == UUID(TRACK_ID)
    assert body.delta == delta


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json",
        b"\xff\xfe",
        b"[]",
        b"null",
        b'"text"',
        b"{}",
        f'{{"track_id": "{TRACK_ID}"}}'.encode(),
        b'{"delta": 5}',
        b'{"track_id": "not-a-uuid", "delta": 5}',
        b'{"track_id": 5, "delta": 5}',
        b'{"track_id": null, "delta": 5}',
        f'{{"track_id": "{TRACK_ID}", "delta": "5"}}'.encode(),
        f'{{"track_id": "{TRACK_ID}", "delta": 5.5}}'.encode(),
        f'{{"track_id": "{TRACK_ID}", "delta": true}}'.encode(),
        f'{{"track_id": "{TRACK_ID}", "delta": null}}'.encode(),
        f'{{"track_id": "{TRACK_ID}", "delta": 0}}'.encode(),
        f'{{"track_id": "{TRACK_ID}", "delta": -5}}'.encode(),
        f'{{"track_id": "{TRACK_ID}", "delta": 1001}}'.encode(),
    ],
)
def test_an_invalid_body_is_not_parsed(body: bytes) -> None:
    assert parse_body(body) is None
