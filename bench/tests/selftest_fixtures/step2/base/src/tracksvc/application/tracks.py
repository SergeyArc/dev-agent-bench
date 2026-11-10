from typing import Protocol
from uuid import UUID, uuid4

from tracksvc.domain.track import Track, TrackNotFoundError


class TrackStore(Protocol):
    """Storage of tracks."""

    def add(self, track: Track) -> None:
        """Store a new track; raise TrackCodeTakenError when its code is taken."""
        ...

    def get(self, track_id: UUID) -> Track | None:
        """Return the track with this id, or None."""
        ...


def create_track(store: TrackStore, code: str, capacity: int) -> Track:
    """Create a track with a fresh id and store it."""
    track = Track(id=uuid4(), code=code, capacity=capacity)
    store.add(track)
    return track


def get_track(store: TrackStore, track_id: UUID) -> Track:
    """Return the track with this id or raise TrackNotFoundError."""
    track = store.get(track_id)
    if track is None:
        raise TrackNotFoundError(str(track_id))
    return track
