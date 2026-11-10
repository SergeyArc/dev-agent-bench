from dataclasses import dataclass, field
from uuid import UUID

MAX_SEGMENTS = 8


class DomainError(Exception):
    """A request rejected by a rule of the track model; `code` is the error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Segment:
    """A labelled half-open span `[start, end)` of a track."""

    label: str
    start: int
    end: int


@dataclass
class Track:
    """A strip of `capacity` units with non-overlapping segments."""

    id: UUID
    code: str
    capacity: int
    status: str = "open"
    segments: list[Segment] = field(default_factory=list)

    def _require_open(self) -> None:
        if self.status != "open":
            raise DomainError("track_sealed")

    def add_segment(self, segment: Segment) -> None:
        """Reserve a segment; every invariant is checked before the change."""
        self._require_open()
        if segment.start >= segment.end:
            raise DomainError("invalid_span")
        if segment.end > self.capacity:
            raise DomainError("span_out_of_bounds")
        if any(other.label == segment.label for other in self.segments):
            raise DomainError("segment_label_taken")
        if any(segment.start < other.end and other.start < segment.end for other in self.segments):
            raise DomainError("segment_overlap")
        if len(self.segments) >= MAX_SEGMENTS:
            raise DomainError("segment_limit_reached")
        self.segments = sorted([*self.segments, segment], key=lambda item: item.start)

    def remove_segment(self, label: str) -> None:
        """Release the segment with this label."""
        self._require_open()
        if all(other.label != label for other in self.segments):
            raise DomainError("segment_not_found")
        self.segments = [other for other in self.segments if other.label != label]

    def seal(self) -> None:
        """Seal the track; it must hold at least one segment."""
        self._require_open()
        if not self.segments:
            raise DomainError("track_empty")
        self.status = "sealed"
