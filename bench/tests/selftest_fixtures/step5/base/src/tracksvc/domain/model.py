from __future__ import annotations

import uuid
from dataclasses import dataclass, field

MAX_CAPACITY = 10000
MAX_SEGMENTS = 8


class DomainError(Exception):
    """A request rejected by a rule; `code` is the error code of the contract."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Span:
    """Half-open interval [start, end)."""

    start: int
    end: int

    def overlaps(self, other: Span) -> bool:
        """Tell whether the two intervals share a unit."""
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class Segment:
    """Named span reserved on a track."""

    label: str
    span: Span


@dataclass
class Track:
    """Strip of `capacity` units with non-overlapping segments."""

    id: uuid.UUID
    code: str
    capacity: int
    status: str = "open"
    segments: list[Segment] = field(default_factory=list)

    def _require_open(self) -> None:
        if self.status != "open":
            raise DomainError("track_sealed")

    def add_segment(self, label: str, span: Span) -> None:
        """Reserve a span under a label."""
        self._require_open()
        if span.start >= span.end:
            raise DomainError("invalid_span")
        if span.end > self.capacity:
            raise DomainError("span_out_of_bounds")
        if any(segment.label == label for segment in self.segments):
            raise DomainError("segment_label_taken")
        if any(segment.span.overlaps(span) for segment in self.segments):
            raise DomainError("segment_overlap")
        if len(self.segments) >= MAX_SEGMENTS:
            raise DomainError("segment_limit_reached")
        self.segments = sorted([*self.segments, Segment(label, span)], key=lambda s: s.span.start)

    def remove_segment(self, label: str) -> None:
        """Release the span reserved under a label."""
        self._require_open()
        kept = [segment for segment in self.segments if segment.label != label]
        if len(kept) == len(self.segments):
            raise DomainError("segment_not_found")
        self.segments = kept

    def seal(self) -> None:
        """Forbid further changes."""
        self._require_open()
        if not self.segments:
            raise DomainError("track_empty")
        self.status = "sealed"

    def extend(self, delta: int) -> None:
        """Grow the capacity by `delta` units."""
        self._require_open()
        if self.capacity + delta > MAX_CAPACITY:
            raise DomainError("capacity_limit_exceeded")
        self.capacity += delta
