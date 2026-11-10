"""Storage of tracks in the memory of the process."""

from copy import deepcopy
from uuid import UUID

from tracksvc.domain.errors import TrackCodeTakenError
from tracksvc.domain.track import Track


class InMemoryTrackRepository:
    """Tracks kept in this object; they are gone when it is discarded.

    The storage keeps its own copies: a track it hands out can be changed without
    affecting what is stored until the track is saved.
    """

    def __init__(self) -> None:
        self._tracks_by_id: dict[UUID, Track] = {}
        self._codes: set[str] = set()

    async def add(self, track: Track) -> None:
        """Store a new track.

        Raises:
            TrackCodeTakenError: If a stored track already has the same code; nothing is
                stored then.
        """
        if track.code in self._codes:
            raise TrackCodeTakenError(track.code)
        self._codes.add(track.code)
        self._tracks_by_id[track.id] = deepcopy(track)

    async def get(self, track_id: UUID) -> Track | None:
        """Return the track stored under ``track_id``, or ``None`` if there is none."""
        return deepcopy(self._tracks_by_id.get(track_id))

    async def save(self, track: Track) -> None:
        """Store the current state of a track that was added earlier."""
        self._tracks_by_id[track.id] = deepcopy(track)
