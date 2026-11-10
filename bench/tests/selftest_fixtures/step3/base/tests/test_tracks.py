import pytest

from tracksvc.domain.track import DomainError, Span, Track


def test_segments_are_ordered() -> None:
    track = Track("id", "TRK-A1", 100)
    track.add("beta", Span(10, 25))
    track.add("alpha", Span(0, 10))
    assert [segment.label for segment in track.segments] == ["alpha", "beta"]


def test_overlap_is_rejected() -> None:
    track = Track("id", "TRK-A1", 100)
    track.add("alpha", Span(0, 10))
    with pytest.raises(DomainError, match="segment_overlap"):
        track.add("beta", Span(5, 12))
