from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tracksvc.domain.model import DomainError, Segment, Track

_LOCK = " FOR UPDATE"
_INSERT_TRACK = text(
    "INSERT INTO tracks (id, code, capacity, status) VALUES (:id, :code, :capacity, :status)"
)
_INSERT_SEGMENT = text(
    "INSERT INTO track_segments (track_id, label, span_start, span_end) "
    "VALUES (:id, :label, :start, :end)"
)
_DELETE_SEGMENT = text("DELETE FROM track_segments WHERE track_id = :id AND label = :label")
_SET_STATUS = text("UPDATE tracks SET status = :status WHERE id = :id")


async def _load(conn: AsyncConnection, track_id: UUID, lock: str = "") -> Track:
    found = await conn.execute(
        text("SELECT code, capacity, status FROM tracks WHERE id = :id" + lock), {"id": track_id}
    )
    row = found.one_or_none()
    if row is None:
        raise DomainError("track_not_found")
    rows = await conn.execute(
        text(
            "SELECT label, span_start, span_end FROM track_segments "
            "WHERE track_id = :id ORDER BY span_start"
        ),
        {"id": track_id},
    )
    segments = [Segment(label, start, end) for label, start, end in rows]
    return Track(track_id, row.code, row.capacity, row.status, segments)


async def create_track(engine: AsyncEngine, code: str, capacity: int) -> Track:
    """Insert a new open track; the unique constraint settles simultaneous creations."""
    track = Track(uuid4(), code, capacity)
    values = {"id": track.id, "code": code, "capacity": capacity, "status": track.status}
    try:
        async with engine.begin() as conn:
            taken = await conn.execute(text("SELECT 1 FROM tracks WHERE code = :code"), values)
            if taken.first() is not None:
                raise DomainError("track_code_taken")
            await conn.execute(_INSERT_TRACK, values)
    except IntegrityError as exc:
        raise DomainError("track_code_taken") from exc
    return track


async def get_track(engine: AsyncEngine, track_id: UUID) -> Track:
    """Read a track and its segments from the database."""
    async with engine.connect() as conn:
        return await _load(conn, track_id)


async def add_segment(engine: AsyncEngine, track_id: UUID, segment: Segment) -> Track:
    """Add a segment in one transaction, holding the row lock of the track."""
    async with engine.begin() as conn:
        track = await _load(conn, track_id, _LOCK)
        track.add_segment(segment)
        values = {"id": track_id, "label": segment.label, "start": segment.start}
        await conn.execute(_INSERT_SEGMENT, {**values, "end": segment.end})
    return track


async def remove_segment(engine: AsyncEngine, track_id: UUID, label: str) -> Track:
    """Remove a segment in one transaction."""
    async with engine.begin() as conn:
        track = await _load(conn, track_id, _LOCK)
        track.remove_segment(label)
        await conn.execute(_DELETE_SEGMENT, {"id": track_id, "label": label})
    return track


async def seal(engine: AsyncEngine, track_id: UUID) -> Track:
    """Seal a track in one transaction."""
    async with engine.begin() as conn:
        track = await _load(conn, track_id, _LOCK)
        track.seal()
        await conn.execute(_SET_STATUS, {"id": track_id, "status": track.status})
    return track
