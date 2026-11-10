"""Use cases that create, read and change tracks."""

from uuid import UUID

from tracksvc.application.errors import TrackNotFoundError
from tracksvc.application.ports import TrackRepository
from tracksvc.domain.span import Span
from tracksvc.domain.track import Track


async def _load(tracks: TrackRepository, track_id: UUID) -> Track:
    track = await tracks.get(track_id)
    if track is None:
        raise TrackNotFoundError(track_id)
    return track


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
        return await _load(self._tracks, track_id)


class AddSegment:
    """Reserve a segment on a track."""

    def __init__(self, tracks: TrackRepository) -> None:
        self._tracks = tracks

    async def execute(self, track_id: UUID, label: str, start: int, end: int) -> Track:
        """Reserve ``[start, end)`` under ``label`` and return the updated track.

        Raises:
            TrackNotFoundError: If no such track exists.
            InvalidSpanError: If the span is empty.
            TrackSealedError: If the track is sealed.
            SpanOutOfBoundsError: If the span ends beyond the capacity of the track.
            SegmentLabelTakenError: If the track already has a segment with the label.
            SegmentOverlapError: If the span overlaps a segment of the track.
            SegmentLimitReachedError: If the track cannot take another segment.
        """
        track = await _load(self._tracks, track_id)
        track.add_segment(label, Span(start, end))
        await self._tracks.save(track)
        return track


class RemoveSegment:
    """Remove a segment from a track."""

    def __init__(self, tracks: TrackRepository) -> None:
        self._tracks = tracks

    async def execute(self, track_id: UUID, label: str) -> Track:
        """Free the span reserved under ``label`` and return the updated track.

        Raises:
            TrackNotFoundError: If no such track exists.
            TrackSealedError: If the track is sealed.
            SegmentNotFoundError: If the track has no segment with the label.
        """
        track = await _load(self._tracks, track_id)
        track.remove_segment(label)
        await self._tracks.save(track)
        return track


class SealTrack:
    """Seal a track."""

    def __init__(self, tracks: TrackRepository) -> None:
        self._tracks = tracks

    async def execute(self, track_id: UUID) -> Track:
        """Seal the track and return it.

        Raises:
            TrackNotFoundError: If no such track exists.
            TrackSealedError: If the track is already sealed.
            TrackEmptyError: If the track has no segments.
        """
        track = await _load(self._tracks, track_id)
        track.seal()
        await self._tracks.save(track)
        return track
