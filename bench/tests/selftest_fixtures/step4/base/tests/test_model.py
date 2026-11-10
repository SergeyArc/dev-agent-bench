from uuid import uuid4

import pytest

from tracksvc.domain.model import DomainError, Segment, Track


def test_segments_are_ordered_and_overlap_is_rejected() -> None:
    track = Track(uuid4(), "TRK-A1", 100)
    track.add_segment(Segment("beta", 10, 25))
    track.add_segment(Segment("alpha", 0, 10))
    assert [item.label for item in track.segments] == ["alpha", "beta"]
    with pytest.raises(DomainError) as raised:
        track.add_segment(Segment("gamma", 5, 12))
    assert raised.value.code == "segment_overlap"
