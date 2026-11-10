"""HTTP helper. Every response is recorded so that C6 (no HTTP 500 during the
step's checks) can be evaluated at the end of the run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from . import config


@dataclass(frozen=True)
class Observed:
    method: str
    url: str
    status: int


OBSERVED: list[Observed] = []


def _record(response: httpx.Response) -> None:
    OBSERVED.append(Observed(response.request.method, str(response.request.url), response.status_code))


def client(base_url: str = config.HTTP_BASE, timeout: float = 15.0) -> httpx.Client:
    """Synchronous client; use as a context manager."""
    return httpx.Client(base_url=base_url, timeout=timeout, event_hooks={"response": [_record]})


def async_client(base_url: str = config.HTTP_BASE, timeout: float = 15.0) -> httpx.AsyncClient:
    """Asynchronous client for concurrency checks (criteria 4.9, 4.10)."""

    async def record(response: httpx.Response) -> None:
        _record(response)

    return httpx.AsyncClient(base_url=base_url, timeout=timeout, event_hooks={"response": [record]})


def error_code(response: httpx.Response) -> str | None:
    """`error.code` of the error body of contract §5, or None if malformed."""
    try:
        body: Any = response.json()
    except ValueError:
        return None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        code = body["error"].get("code")
        return code if isinstance(code, str) else None
    return None


def error_body_problem(response: httpx.Response) -> str | None:
    """Why the body violates the error schema of contract §5; None if it conforms.

    Checked: `error` has exactly the keys `code` and `message` (a non-empty
    string), plus `details` for `validation_error` only (a non-empty array of
    items with string `field` and `message`).
    """
    try:
        body: Any = response.json()
    except ValueError:
        return "the body is not JSON"
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return "the body has no `error` object"
    if not isinstance(error.get("code"), str):
        return "`error.code` is not a string"
    message = error.get("message")
    if not isinstance(message, str) or not message:
        return "`error.message` is not a non-empty string"
    allowed = {"code", "message"} | ({"details"} if error["code"] == "validation_error" else set())
    extra = sorted(set(error) - allowed)
    if extra:
        return f"`error` has key(s) {extra} not allowed for code {error['code']!r}"
    if error["code"] != "validation_error":
        return None
    details = error.get("details")
    if not isinstance(details, list) or not details:
        return "`error.details` is not a non-empty array"
    for item in details:
        if not (
            isinstance(item, dict)
            and isinstance(item.get("field"), str)
            and isinstance(item.get("message"), str)
        ):
            return "an `error.details` item lacks a string `field` or `message`"
    return None
