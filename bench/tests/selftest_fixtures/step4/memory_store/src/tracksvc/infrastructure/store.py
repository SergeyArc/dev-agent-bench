from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine

from tracksvc.domain.model import DomainError, Segment, Track

_TRACKS: dict[UUID, Track] = {}


def _find(track_id: UUID) -> Track:
    track = _TRACKS.get(track_id)
    if track is None:
        raise DomainError("track_not_found")
    return track


async def create_track(engine: AsyncEngine, code: str, capacity: int) -> Track:
    """Keep a new open track in process memory."""
    if any(track.code == code for track in _TRACKS.values()):
        raise DomainError("track_code_taken")
    track = Track(uuid4(), code, capacity)
    _TRACKS[track.id] = track
    return track


async def get_track(engine: AsyncEngine, track_id: UUID) -> Track:
    """Return the track kept in process memory."""
    return _find(track_id)


async def add_segment(engine: AsyncEngine, track_id: UUID, segment: Segment) -> Track:
    """Add a segment to the track kept in process memory."""
    track = _find(track_id)
    track.add_segment(segment)
    return track


async def remove_segment(engine: AsyncEngine, track_id: UUID, label: str) -> Track:
    """Remove a segment from the track kept in process memory."""
    track = _find(track_id)
    track.remove_segment(label)
    return track


async def seal(engine: AsyncEngine, track_id: UUID) -> Track:
    """Seal the track kept in process memory."""
    track = _find(track_id)
    track.seal()
    return track
