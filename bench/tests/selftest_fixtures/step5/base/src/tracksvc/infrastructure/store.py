import uuid

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from tracksvc.domain.model import DomainError, Segment, Span, Track


async def insert_track(connection: AsyncConnection, track: Track) -> None:
    """Store a new track; a used code is rejected."""
    statement = text(
        "INSERT INTO tracks (id, code, capacity, status) VALUES (:id, :code, :capacity, :status)"
    )
    values = {
        "id": track.id,
        "code": track.code,
        "capacity": track.capacity,
        "status": track.status,
    }
    try:
        await connection.execute(statement, values)
    except IntegrityError as exc:
        raise DomainError("track_code_taken") from exc


async def load_track(connection: AsyncConnection, track_id: uuid.UUID, *, lock: bool) -> Track:
    """Read a track with its segments, optionally locking its row."""
    query = "SELECT code, capacity, status FROM tracks WHERE id = :id"
    if lock:
        query += " FOR UPDATE"
    row = (await connection.execute(text(query), {"id": track_id})).first()
    if row is None:
        raise DomainError("track_not_found")
    rows = await connection.execute(
        text(
            "SELECT label, span_start, span_end FROM track_segments "
            "WHERE track_id = :id ORDER BY span_start"
        ),
        {"id": track_id},
    )
    segments = [Segment(label, Span(start, end)) for label, start, end in rows]
    return Track(track_id, row[0], row[1], row[2], segments)


async def save_track(connection: AsyncConnection, track: Track) -> None:
    """Write the current state of a stored track."""
    await connection.execute(
        text("UPDATE tracks SET capacity = :capacity, status = :status WHERE id = :id"),
        {"id": track.id, "capacity": track.capacity, "status": track.status},
    )
    await connection.execute(
        text("DELETE FROM track_segments WHERE track_id = :id"), {"id": track.id}
    )
    for segment in track.segments:
        await connection.execute(
            text(
                "INSERT INTO track_segments (track_id, label, span_start, span_end) "
                "VALUES (:id, :label, :start, :end)"
            ),
            {
                "id": track.id,
                "label": segment.label,
                "start": segment.span.start,
                "end": segment.span.end,
            },
        )


async def record_message(connection: AsyncConnection, message_id: str) -> bool:
    """Record a message id; False when it is already recorded."""
    result = await connection.execute(
        text(
            "INSERT INTO processed_messages (message_id) VALUES (:id) "
            "ON CONFLICT (message_id) DO NOTHING RETURNING message_id"
        ),
        {"id": message_id},
    )
    return result.first() is not None
