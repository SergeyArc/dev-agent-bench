"""The track."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4


class TrackStatus(StrEnum):
    """Lifecycle state of a track."""

    OPEN = "open"


@dataclass(frozen=True, slots=True)
class Track:
    """A numbered strip of ``capacity`` units, identified by ``id`` and by its unique ``code``.

    A track does not change after it has been created.
    """

    id: UUID
    code: str
    capacity: int
    status: TrackStatus
    segments: tuple[()]

    @classmethod
    def create(cls, code: str, capacity: int) -> Self:
        """Return a new open track without segments under a freshly generated id."""
        return cls(id=uuid4(), code=code, capacity=capacity, status=TrackStatus.OPEN, segments=())
