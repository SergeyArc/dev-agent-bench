from dataclasses import dataclass
from typing import Self

MAX_SEGMENTS = 8


class DomainError(Exception):
    """A request rejected by the model; `code` is the error code of the contract."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Span:
    """Half-open interval [start, end)."""

    start: int
    end: int

    def overlaps(self, other: Self) -> bool:
        """Tell whether the two intervals share at least one unit."""
        return self.start < other.end and other.start < self.end


@dataclass(frozen=True)
class Segment:
    """A span reserved on a track under a label."""

    label: str
    span: Span


class Track:
    """A strip of `capacity` units holding named, non-overlapping segments."""

    def __init__(self, track_id: str, code: str, capacity: int) -> None:
        self.id = track_id
        self.code = code
        self.capacity = capacity
        self.sealed = False
        self._segments: list[Segment] = []

    @property
    def segments(self) -> tuple[Segment, ...]:
        """Segments ordered by the start of their spans."""
        return tuple(sorted(self._segments, key=lambda segment: segment.span.start))

    def add(self, label: str, span: Span) -> None:
        """Reserve a span under a label."""
        self._require_open()
        if span.start >= span.end:
            raise DomainError("invalid_span")
        if span.end > self.capacity:
            raise DomainError("span_out_of_bounds")
        if any(segment.label == label for segment in self._segments):
            raise DomainError("segment_label_taken")
        if any(segment.span.overlaps(span) for segment in self._segments):
            raise DomainError("segment_overlap")
        if len(self._segments) >= MAX_SEGMENTS:
            raise DomainError("segment_limit_reached")
        self._segments.append(Segment(label, span))

    def remove(self, label: str) -> None:
        """Release the span reserved under a label."""
        self._require_open()
        kept = [segment for segment in self._segments if segment.label != label]
        if len(kept) == len(self._segments):
            raise DomainError("segment_not_found")
        self._segments = kept

    def seal(self) -> None:
        """Close the track for further changes."""
        self._require_open()
        if not self._segments:
            raise DomainError("track_empty")
        self.sealed = True

    def _require_open(self) -> None:
        if self.sealed:
            raise DomainError("track_sealed")
