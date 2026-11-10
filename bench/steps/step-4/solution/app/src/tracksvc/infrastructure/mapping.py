"""Conversion between the track aggregate and its rows; the only place that knows both."""

from collections.abc import Iterable

from tracksvc.domain.span import Span
from tracksvc.domain.track import Segment, Track, TrackStatus
from tracksvc.infrastructure.models import SegmentRow, TrackRow


def to_track(row: TrackRow, segment_rows: Iterable[SegmentRow]) -> Track:
    """Rebuild a track from its row and the rows of its segments."""
    segments = [
        Segment(segment_row.label, Span(segment_row.span_start, segment_row.span_end))
        for segment_row in segment_rows
    ]
    return Track(row.id, row.code, row.capacity, TrackStatus(row.status), segments)


def to_track_values(track: Track) -> dict[str, object]:
    """Return the column values of the ``tracks`` row of ``track``."""
    return {
        "id": track.id,
        "code": track.code,
        "capacity": track.capacity,
        "status": track.status.value,
    }


def to_segment_values(track: Track) -> list[dict[str, object]]:
    """Return the column values of the ``track_segments`` rows of ``track``."""
    return [
        {
            "track_id": track.id,
            "label": segment.label,
            "span_start": segment.span.start,
            "span_end": segment.span.end,
        }
        for segment in track.segments
    ]
