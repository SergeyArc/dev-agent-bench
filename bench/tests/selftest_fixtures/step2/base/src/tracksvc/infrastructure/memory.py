from uuid import UUID

from tracksvc.domain.track import Track, TrackCodeTakenError


class InMemoryTrackStore:
    """Tracks kept in the memory of the process."""

    def __init__(self) -> None:
        self._tracks: dict[UUID, Track] = {}

    def add(self, track: Track) -> None:
        """Store a new track; raise TrackCodeTakenError when its code is taken."""
        if any(other.code == track.code for other in self._tracks.values()):
            raise TrackCodeTakenError(track.code)
        self._tracks[track.id] = track

    def get(self, track_id: UUID) -> Track | None:
        """Return the track with this id, or None."""
        return self._tracks.get(track_id)
