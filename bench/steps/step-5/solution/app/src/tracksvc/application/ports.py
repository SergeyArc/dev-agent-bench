"""Abstractions the use cases depend on; implemented in the infrastructure layer."""

from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from tracksvc.domain.track import Track


class TrackRepository(Protocol):
    """Storage of tracks that keeps their codes unique.

    It is used inside a unit of work. A track handed out by the storage is detached from
    it: changes made to the track are stored only by ``save``.
    """

    async def add(self, track: Track) -> None:
        """Store a new track.

        Raises:
            TrackCodeTakenError: If a stored track already has the same code; nothing is
                stored then.
        """
        ...

    async def get(self, track_id: UUID) -> Track | None:
        """Return the track stored under ``track_id``, or ``None`` if there is none."""
        ...

    async def get_for_update(self, track_id: UUID) -> Track | None:
        """Return the track stored under ``track_id`` in order to change it.

        Until the current unit of work ends, no other unit of work gets the same track
        for update: it waits and then sees the changes committed by this one. Returns
        ``None`` if there is no such track.
        """
        ...

    async def save(self, track: Track) -> None:
        """Store the current state of a track obtained with ``get_for_update``."""
        ...


class ProcessedMessages(Protocol):
    """Record of the messages whose effect has been applied; used inside a unit of work."""

    async def contains(self, message_id: str) -> bool:
        """Tell whether the message has been recorded."""
        ...

    async def add(self, message_id: str) -> bool:
        """Record the message unless it is recorded already.

        Returns ``False``, recording nothing, if the message is already recorded. When
        another unit of work is recording the same message at this moment, the call
        waits for that unit of work to end and answers according to its outcome.
        """
        ...


class UnitOfWork(Protocol):
    """One transaction over the storage, used as an asynchronous context manager.

    Entering begins the transaction. ``commit`` makes everything written since then
    durable. Leaving the block without a commit, or because of an error, rolls
    everything back.

    Raises:
        StorageUnavailableError: From ``commit``, from leaving the block and from the
            storage operations inside it, if the storage fails.
    """

    @property
    def tracks(self) -> TrackRepository:
        """The tracks, read and written within this transaction."""
        ...

    @property
    def processed_messages(self) -> ProcessedMessages:
        """The record of processed messages, read and written within this transaction."""
        ...

    async def __aenter__(self) -> Self:
        """Begin the transaction."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back whatever has not been committed."""
        ...

    async def commit(self) -> None:
        """Commit everything written in this transaction."""
        ...
