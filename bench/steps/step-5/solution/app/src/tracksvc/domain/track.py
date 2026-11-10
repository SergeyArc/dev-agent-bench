"""The track aggregate."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from tracksvc.domain.errors import (
    CapacityLimitExceededError,
    SegmentLabelTakenError,
    SegmentLimitReachedError,
    SegmentNotFoundError,
    SegmentOverlapError,
    SpanOutOfBoundsError,
    TrackEmptyError,
    TrackSealedError,
)
from tracksvc.domain.span import Span

MAX_SEGMENTS = 8
MAX_CAPACITY = 10000


class TrackStatus(StrEnum):
    """Lifecycle state of a track."""

    OPEN = "open"
    SEALED = "sealed"


@dataclass(frozen=True, slots=True)
class Segment:
    """A span of a track reserved under a label."""

    label: str
    span: Span


class Track:
    """A numbered strip of ``capacity`` units on which named, non-overlapping segments are reserved.

    A track is identified by ``id`` and by its unique ``code``. Its segments, capacity and
    status change only through its own operations, and not at all once it is sealed.

    The constructor rebuilds a track from the state it was stored with and takes that
    state as it is; ``create`` makes a new track.
    """

    def __init__(
        self,
        track_id: UUID,
        code: str,
        capacity: int,
        status: TrackStatus = TrackStatus.OPEN,
        segments: Iterable[Segment] = (),
    ) -> None:
        self._id = track_id
        self._code = code
        self._capacity = capacity
        self._status = status
        self._segments = list(segments)

    @classmethod
    def create(cls, code: str, capacity: int) -> Self:
        """Return a new open track without segments under a freshly generated id."""
        return cls(uuid4(), code, capacity)

    @property
    def id(self) -> UUID:
        """Identifier of the track."""
        return self._id

    @property
    def code(self) -> str:
        """Code that no other track has."""
        return self._code

    @property
    def capacity(self) -> int:
        """Number of units on the track."""
        return self._capacity

    @property
    def status(self) -> TrackStatus:
        """Whether the track is open or sealed."""
        return self._status

    @property
    def segments(self) -> tuple[Segment, ...]:
        """The reserved segments, ordered by the start of their spans."""
        return tuple(sorted(self._segments, key=lambda segment: segment.span.start))

    def add_segment(self, label: str, span: Span) -> None:
        """Reserve ``span`` under ``label``.

        Raises:
            TrackSealedError: If the track is sealed.
            SpanOutOfBoundsError: If the span ends beyond the capacity.
            SegmentLabelTakenError: If a segment already has the label.
            SegmentOverlapError: If the span overlaps a segment.
            SegmentLimitReachedError: If the track already has ``MAX_SEGMENTS`` segments.
        """
        self._ensure_open()
        if span.end > self._capacity:
            raise SpanOutOfBoundsError(span.end, self._capacity)
        for segment in self._segments:
            if segment.label == label:
                raise SegmentLabelTakenError(label)
            if segment.span.overlaps(span):
                raise SegmentOverlapError(segment.label)
        if len(self._segments) >= MAX_SEGMENTS:
            raise SegmentLimitReachedError(MAX_SEGMENTS)
        self._segments.append(Segment(label, span))

    def remove_segment(self, label: str) -> None:
        """Free the span reserved under ``label``.

        Raises:
            TrackSealedError: If the track is sealed.
            SegmentNotFoundError: If no segment has the label.
        """
        self._ensure_open()
        remaining = [segment for segment in self._segments if segment.label != label]
        if len(remaining) == len(self._segments):
            raise SegmentNotFoundError(label)
        self._segments = remaining

    def seal(self) -> None:
        """Seal the track: from now on it cannot be modified.

        Raises:
            TrackSealedError: If the track is already sealed.
            TrackEmptyError: If the track has no segments.
        """
        self._ensure_open()
        if not self._segments:
            raise TrackEmptyError
        self._status = TrackStatus.SEALED

    def extend(self, delta: int) -> None:
        """Increase the capacity by ``delta`` units.

        Raises:
            TrackSealedError: If the track is sealed.
            CapacityLimitExceededError: If the capacity would exceed ``MAX_CAPACITY``.
        """
        self._ensure_open()
        extended = self._capacity + delta
        if extended > MAX_CAPACITY:
            raise CapacityLimitExceededError(extended, MAX_CAPACITY)
        self._capacity = extended

    def _ensure_open(self) -> None:
        if self._status is TrackStatus.SEALED:
            raise TrackSealedError
