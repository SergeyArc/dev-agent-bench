"""Errors raised by use cases and by the abstractions they depend on."""

from uuid import UUID


class TrackNotFoundError(Exception):
    """No track has the requested id."""

    def __init__(self, track_id: UUID) -> None:
        super().__init__(f"Track {track_id} does not exist.")
        self.track_id = track_id


class StorageUnavailableError(Exception):
    """The storage could not carry out an operation; nothing was committed by it."""

    def __init__(self) -> None:
        super().__init__("The storage is not available.")
