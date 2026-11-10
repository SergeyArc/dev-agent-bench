import pytest

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
from tracksvc.domain.track import Segment, Track, TrackStatus


@pytest.fixture
def track() -> Track:
    return Track.create(code="TRK-A1", capacity=100)


@pytest.fixture
def sealed_track(track: Track) -> Track:
    track.add_segment("alpha", Span(0, 10))
    track.seal()
    return track


def test_a_new_track_is_open_and_has_no_segments(track: Track) -> None:
    assert track.code == "TRK-A1"
    assert track.capacity == 100
    assert track.status is TrackStatus.OPEN
    assert track.segments == ()


def test_new_tracks_get_distinct_ids() -> None:
    assert Track.create("TRK-A1", 1).id != Track.create("TRK-A1", 1).id


def test_segments_are_ordered_by_span_start_whatever_the_insertion_order(track: Track) -> None:
    track.add_segment("gamma", Span(50, 60))
    track.add_segment("alpha", Span(0, 10))
    track.add_segment("beta", Span(10, 25))

    assert track.segments == (
        Segment("alpha", Span(0, 10)),
        Segment("beta", Span(10, 25)),
        Segment("gamma", Span(50, 60)),
    )


def test_the_exposed_segments_cannot_be_used_to_change_the_track(track: Track) -> None:
    track.add_segment("alpha", Span(0, 10))

    exposed = track.segments

    assert isinstance(exposed, tuple)
    assert all(isinstance(segment, Segment) for segment in exposed)
    track.add_segment("beta", Span(10, 20))
    assert exposed == (Segment("alpha", Span(0, 10)),)


def test_a_span_may_end_exactly_at_the_capacity(track: Track) -> None:
    track.add_segment("alpha", Span(90, 100))

    assert track.segments == (Segment("alpha", Span(90, 100)),)


@pytest.mark.parametrize("span", [Span(90, 101), Span(100, 101), Span(0, 10000)])
def test_a_span_beyond_the_capacity_is_rejected(track: Track, span: Span) -> None:
    with pytest.raises(SpanOutOfBoundsError):
        track.add_segment("alpha", span)

    assert track.segments == ()


def test_a_taken_label_is_rejected(track: Track) -> None:
    track.add_segment("alpha", Span(0, 10))

    with pytest.raises(SegmentLabelTakenError):
        track.add_segment("alpha", Span(50, 60))

    assert track.segments == (Segment("alpha", Span(0, 10)),)


def test_another_track_may_use_the_same_label(track: Track) -> None:
    other = Track.create(code="TRK-B2", capacity=100)
    track.add_segment("alpha", Span(0, 10))

    other.add_segment("alpha", Span(0, 10))

    assert other.segments == (Segment("alpha", Span(0, 10)),)


@pytest.mark.parametrize(
    "span", [Span(10, 20), Span(5, 15), Span(15, 25), Span(12, 18), Span(0, 30)]
)
def test_an_overlapping_span_is_rejected(track: Track, span: Span) -> None:
    track.add_segment("alpha", Span(10, 20))

    with pytest.raises(SegmentOverlapError):
        track.add_segment("beta", span)

    assert track.segments == (Segment("alpha", Span(10, 20)),)


def test_spans_that_touch_are_accepted(track: Track) -> None:
    track.add_segment("beta", Span(10, 20))

    track.add_segment("alpha", Span(0, 10))
    track.add_segment("gamma", Span(20, 30))

    assert [segment.label for segment in track.segments] == ["alpha", "beta", "gamma"]


def test_the_eighth_segment_is_accepted_and_the_ninth_rejected(track: Track) -> None:
    for index in range(8):
        track.add_segment(f"s{index}", Span(index * 10, index * 10 + 10))
    before = track.segments

    with pytest.raises(SegmentLimitReachedError):
        track.add_segment("s8", Span(80, 90))

    assert len(before) == 8
    assert track.segments == before


def test_removing_a_segment_frees_its_span(track: Track) -> None:
    track.add_segment("alpha", Span(0, 10))
    track.add_segment("beta", Span(10, 25))

    track.remove_segment("beta")
    track.add_segment("gamma", Span(10, 25))

    assert track.segments == (Segment("alpha", Span(0, 10)), Segment("gamma", Span(10, 25)))


def test_removing_an_absent_segment_is_rejected(track: Track) -> None:
    track.add_segment("alpha", Span(0, 10))

    with pytest.raises(SegmentNotFoundError):
        track.remove_segment("beta")

    assert track.segments == (Segment("alpha", Span(0, 10)),)


def test_sealing_a_track_with_a_segment_makes_it_sealed(track: Track) -> None:
    track.add_segment("alpha", Span(0, 10))

    track.seal()

    assert track.status is TrackStatus.SEALED
    assert track.segments == (Segment("alpha", Span(0, 10)),)


def test_an_empty_track_cannot_be_sealed(track: Track) -> None:
    with pytest.raises(TrackEmptyError):
        track.seal()

    assert track.status is TrackStatus.OPEN


def test_a_sealed_track_rejects_a_new_segment(sealed_track: Track) -> None:
    with pytest.raises(TrackSealedError):
        sealed_track.add_segment("beta", Span(10, 20))

    assert sealed_track.segments == (Segment("alpha", Span(0, 10)),)


def test_a_sealed_track_rejects_the_removal_of_a_segment(sealed_track: Track) -> None:
    with pytest.raises(TrackSealedError):
        sealed_track.remove_segment("alpha")

    assert sealed_track.segments == (Segment("alpha", Span(0, 10)),)


def test_a_sealed_track_cannot_be_sealed_again(sealed_track: Track) -> None:
    with pytest.raises(TrackSealedError):
        sealed_track.seal()

    assert sealed_track.status is TrackStatus.SEALED


@pytest.mark.parametrize("attribute", ["id", "code", "capacity", "status", "segments"])
def test_the_state_of_a_track_cannot_be_assigned(track: Track, attribute: str) -> None:
    with pytest.raises(AttributeError):
        setattr(track, attribute, None)


def test_extend_increases_the_capacity(track: Track) -> None:
    track.add_segment("alpha", Span(0, 10))

    track.extend(5)

    assert track.capacity == 105
    assert track.segments == (Segment("alpha", Span(0, 10)),)
    track.add_segment("beta", Span(100, 105))


def test_the_capacity_may_reach_the_limit_exactly() -> None:
    track = Track.create(code="TRK-A1", capacity=9000)

    track.extend(1000)

    assert track.capacity == 10000


@pytest.mark.parametrize(("capacity", "delta"), [(10000, 1), (9001, 1000), (9999, 2)])
def test_an_extension_beyond_the_limit_is_rejected(capacity: int, delta: int) -> None:
    track = Track.create(code="TRK-A1", capacity=capacity)

    with pytest.raises(CapacityLimitExceededError):
        track.extend(delta)

    assert track.capacity == capacity


def test_a_sealed_track_cannot_be_extended(sealed_track: Track) -> None:
    with pytest.raises(TrackSealedError):
        sealed_track.extend(5)

    assert sealed_track.capacity == 100
