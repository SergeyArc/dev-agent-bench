"""Storage of tracks in PostgreSQL."""

from uuid import UUID

from sqlalchemy import Select, delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from tracksvc.domain.errors import TrackCodeTakenError
from tracksvc.domain.track import Track
from tracksvc.infrastructure.mapping import to_segment_values, to_track, to_track_values
from tracksvc.infrastructure.models import SegmentRow, TrackRow


class SqlAlchemyTrackRepository:
    """Tracks stored in ``tracks`` and ``track_segments``.

    Every operation runs in the transaction of the session it is given and neither
    commits nor rolls back.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, track: Track) -> None:
        """Store a new track.

        The database decides whether the code is free, so of several simultaneous
        additions of one code exactly one succeeds.

        Raises:
            TrackCodeTakenError: If a stored track already has the same code; nothing is
                stored then.
        """
        inserted_id = await self._session.scalar(
            postgresql_insert(TrackRow)
            .values(to_track_values(track))
            .on_conflict_do_nothing(index_elements=[TrackRow.code])
            .returning(TrackRow.id)
        )
        if inserted_id is None:
            raise TrackCodeTakenError(track.code)
        await self._insert_segments(track)

    async def get(self, track_id: UUID) -> Track | None:
        """Return the track stored under ``track_id``, or ``None`` if there is none."""
        return await self._find(select(TrackRow).where(TrackRow.id == track_id))

    async def get_for_update(self, track_id: UUID) -> Track | None:
        """Return the track stored under ``track_id`` in order to change it.

        The row of the track stays locked until the transaction ends: another
        transaction asking for the same track waits and then reads what this one
        committed. Returns ``None`` if there is no such track.
        """
        return await self._find(select(TrackRow).where(TrackRow.id == track_id).with_for_update())

    async def save(self, track: Track) -> None:
        """Store the current state of a track obtained with ``get_for_update``."""
        await self._session.execute(
            update(TrackRow).where(TrackRow.id == track.id).values(to_track_values(track))
        )
        await self._session.execute(delete(SegmentRow).where(SegmentRow.track_id == track.id))
        await self._insert_segments(track)

    async def _find(self, statement: Select[TrackRow]) -> Track | None:
        # The session is told to refresh what it may already hold: a row is always
        # returned as the database has it now.
        row = await self._session.scalar(statement.execution_options(populate_existing=True))
        if row is None:
            return None
        segment_rows = await self._session.scalars(
            select(SegmentRow).where(SegmentRow.track_id == row.id)
        )
        return to_track(row, segment_rows)

    async def _insert_segments(self, track: Track) -> None:
        values = to_segment_values(track)
        if values:
            await self._session.execute(insert(SegmentRow), values)
