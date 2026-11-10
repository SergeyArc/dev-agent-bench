import json
import re
from uuid import UUID

CODE = re.compile(r"[A-Z][A-Z0-9-]{2,31}")
MAX_CAPACITY = 10000


class InvalidRequestError(Exception):
    """The request is malformed; `field` names the offending location."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def parse_track_id(raw: str) -> UUID:
    """Parse the `track_id` path parameter."""
    try:
        return UUID(raw)
    except ValueError:
        raise InvalidRequestError("track_id", "must be a UUID") from None


def parse_new_track(raw: bytes) -> tuple[str, int]:
    """Parse the body of `POST /tracks` into `(code, capacity)`."""
    try:
        body = json.loads(raw)
    except ValueError:
        raise InvalidRequestError("body", "must be valid JSON") from None
    if not isinstance(body, dict):
        raise InvalidRequestError("body", "must be a JSON object")
    return _code(body.get("code")), _capacity(body.get("capacity"))


def _code(value: object) -> str:
    if not isinstance(value, str) or CODE.fullmatch(value) is None:
        raise InvalidRequestError("code", "must match ^[A-Z][A-Z0-9-]{2,31}$")
    return value


def _capacity(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_CAPACITY:
        raise InvalidRequestError("capacity", "must be an integer from 1 to 10000")
    return value
