"""Errors raised by use cases."""

from uuid import UUID


class TrackNotFoundError(Exception):
    """No track has the requested id."""

    def __init__(self, track_id: UUID) -> None:
        super().__init__(f"Track {track_id} does not exist.")
        self.track_id = track_id
