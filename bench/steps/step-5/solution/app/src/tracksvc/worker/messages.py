"""The message the worker consumes: its parts and their validation."""

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

RETRY_COUNT_HEADER = "x-retry-count"
MAX_MESSAGE_ID_LENGTH = 255
_FIRST_PRINTABLE, _LAST_PRINTABLE = " ", "~"


@dataclass(frozen=True, slots=True)
class Delivery:
    """What the worker reads of a delivered message."""

    body: bytes
    message_id: str | None
    headers: Mapping[str, object]


class ExtendTrackBody(BaseModel):
    """Body of a message that extends a track; JSON types are exact, nothing is coerced."""

    model_config = ConfigDict(strict=True)

    track_id: UUID
    delta: int = Field(ge=1, le=1000)


def is_valid_message_id(message_id: str | None) -> bool:
    """Tell whether ``message_id`` is 1 to 255 printable ASCII characters."""
    return (
        message_id is not None
        and 1 <= len(message_id) <= MAX_MESSAGE_ID_LENGTH
        and all(_FIRST_PRINTABLE <= character <= _LAST_PRINTABLE for character in message_id)
    )


def parse_retry_count(headers: Mapping[str, object]) -> int | None:
    """Return the number of failed attempts so far: 0 when the header is absent.

    Returns ``None`` if the header is present but is not a non-negative integer; a
    boolean is not an integer.
    """
    if RETRY_COUNT_HEADER not in headers:
        return 0
    value = headers[RETRY_COUNT_HEADER]
    if type(value) is not int or value < 0:
        return None
    return value


def parse_body(body: bytes) -> ExtendTrackBody | None:
    """Return the parsed body, or ``None`` if it is not the JSON object a message carries."""
    try:
        return ExtendTrackBody.model_validate_json(body)
    except ValidationError:
        return None
