"""Use cases that create and read tracks."""

from uuid import UUID

from tracksvc.application.errors import TrackNotFoundError
from tracksvc.application.ports import TrackRepository
from tracksvc.domain.track import Track


class CreateTrack:
    """Create a track with a code no other track has."""

    def __init__(self, tracks: TrackRepository) -> None:
        self._tracks = tracks

    async def execute(self, code: str, capacity: int) -> Track:
        """Create, store and return a new track.

        Raises:
            TrackCodeTakenError: If another track already has ``code``.
        """
        track = Track.create(code=code, capacity=capacity)
        await self._tracks.add(track)
        return track


class GetTrack:
    """Read a track by its id."""

    def __init__(self, tracks: TrackRepository) -> None:
        self._tracks = tracks

    async def execute(self, track_id: UUID) -> Track:
        """Return the track with ``track_id``.

        Raises:
            TrackNotFoundError: If no such track exists.
        """
        track = await self._tracks.get(track_id)
        if track is None:
            raise TrackNotFoundError(track_id)
        return track
