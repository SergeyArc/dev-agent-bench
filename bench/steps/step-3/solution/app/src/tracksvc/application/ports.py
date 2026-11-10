"""Abstractions the use cases depend on; implemented in the infrastructure layer."""

from typing import Protocol
from uuid import UUID

from tracksvc.domain.track import Track


class TrackRepository(Protocol):
    """Storage of tracks that keeps their codes unique.

    A track handed out by the storage is detached from it: changes made to the track are
    stored only by ``save``.
    """

    async def add(self, track: Track) -> None:
        """Store a new track.

        Raises:
            TrackCodeTakenError: If a stored track already has the same code; nothing is
                stored then.
        """
        ...

    async def get(self, track_id: UUID) -> Track | None:
        """Return the track stored under ``track_id``, or ``None`` if there is none."""
        ...

    async def save(self, track: Track) -> None:
        """Store the current state of a track that was added earlier."""
        ...
