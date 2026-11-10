import uuid

import pytest

from tracksvc.domain.model import DomainError, Span, Track


def test_extend_grows_capacity() -> None:
    track = Track(uuid.uuid4(), "TRK-A1", 100)
    track.extend(5)
    assert track.capacity == 105


def test_extend_respects_the_limit() -> None:
    track = Track(uuid.uuid4(), "TRK-A2", 9990)
    with pytest.raises(DomainError, match="capacity_limit_exceeded"):
        track.extend(11)
    assert track.capacity == 9990


def test_segments_do_not_overlap() -> None:
    track = Track(uuid.uuid4(), "TRK-A3", 100)
    track.add_segment("alpha", Span(0, 10))
    with pytest.raises(DomainError, match="segment_overlap"):
        track.add_segment("beta", Span(5, 15))
    track.add_segment("beta", Span(10, 15))
    assert [segment.label for segment in track.segments] == ["alpha", "beta"]
