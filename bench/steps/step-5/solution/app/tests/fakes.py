"""Test doubles of the storage abstractions: tracks kept in memory."""

from copy import deepcopy
from types import TracebackType
from typing import Self
from uuid import UUID

from tracksvc.application.ports import ProcessedMessages, TrackRepository
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


class InMemoryProcessedMessages:
    """Processed messages kept in the set it is given."""

    def __init__(self, message_ids: set[str]) -> None:
        self._message_ids = message_ids

    async def contains(self, message_id: str) -> bool:
        return message_id in self._message_ids

    async def add(self, message_id: str) -> bool:
        if message_id in self._message_ids:
            return False
        self._message_ids.add(message_id)
        return True


class InMemoryUnitOfWork:
    """Unit of work over state in memory: what is not committed is discarded on exit."""

    def __init__(self) -> None:
        self._committed: dict[UUID, Track] = {}
        self._working: dict[UUID, Track] = {}
        self._committed_message_ids: set[str] = set()
        self._working_message_ids: set[str] = set()
        self.commits = 0

    @property
    def tracks(self) -> TrackRepository:
        return InMemoryTrackRepository(self._working)

    @property
    def processed_messages(self) -> ProcessedMessages:
        return InMemoryProcessedMessages(self._working_message_ids)

    async def __aenter__(self) -> Self:
        self._working = deepcopy(self._committed)
        self._working_message_ids = set(self._committed_message_ids)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._working = {}
        self._working_message_ids = set()

    async def commit(self) -> None:
        self._committed = deepcopy(self._working)
        self._committed_message_ids = set(self._working_message_ids)
        self.commits += 1

    def committed(self, track_id: UUID) -> Track | None:
        """Return the committed state of a track."""
        return deepcopy(self._committed.get(track_id))

    def committed_message_ids(self) -> set[str]:
        """Return the ids of the messages recorded by committed units of work."""
        return set(self._committed_message_ids)
