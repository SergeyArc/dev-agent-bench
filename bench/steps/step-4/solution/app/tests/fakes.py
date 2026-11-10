"""Test doubles of the storage abstractions: tracks kept in memory."""

from copy import deepcopy
from types import TracebackType
from typing import Self
from uuid import UUID

from tracksvc.application.ports import TrackRepository
from tracksvc.domain.errors import TrackCodeTakenError
from tracksvc.domain.track import Track


class InMemoryTrackRepository:
    """Tracks kept in the dictionary it is given; it hands out and keeps copies."""

    def __init__(self, tracks_by_id: dict[UUID, Track]) -> None:
        self._tracks_by_id = tracks_by_id

    async def add(self, track: Track) -> None:
        if any(stored.code == track.code for stored in self._tracks_by_id.values()):
            raise TrackCodeTakenError(track.code)
        self._tracks_by_id[track.id] = deepcopy(track)

    async def get(self, track_id: UUID) -> Track | None:
        return deepcopy(self._tracks_by_id.get(track_id))

    async def get_for_update(self, track_id: UUID) -> Track | None:
        return await self.get(track_id)

    async def save(self, track: Track) -> None:
        self._tracks_by_id[track.id] = deepcopy(track)


class InMemoryUnitOfWork:
    """Unit of work over tracks in memory: what is not committed is discarded on exit."""

    def __init__(self) -> None:
        self._committed: dict[UUID, Track] = {}
        self._working: dict[UUID, Track] = {}
        self.commits = 0

    @property
    def tracks(self) -> TrackRepository:
        return InMemoryTrackRepository(self._working)

    async def __aenter__(self) -> Self:
        self._working = deepcopy(self._committed)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._working = {}

    async def commit(self) -> None:
        self._committed = deepcopy(self._working)
        self.commits += 1

    def committed(self, track_id: UUID) -> Track | None:
        """Return the committed state of a track."""
        return deepcopy(self._committed.get(track_id))
