from dataclasses import dataclass
from uuid import UUID


@dataclass
class Track:
    """A strip of `capacity` units identified by a unique `code`."""

    id: UUID
    code: str
    capacity: int
    status: str = "open"


class TrackNotFoundError(Exception):
    """No track has the requested id."""


class TrackCodeTakenError(Exception):
    """Another track already has the requested code."""
