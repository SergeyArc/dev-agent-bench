from dataclasses import FrozenInstanceError

import pytest

from tracksvc.domain.errors import InvalidSpanError
from tracksvc.domain.span import Span


def test_spans_with_the_same_bounds_are_equal() -> None:
    assert Span(0, 10) == Span(0, 10)
    assert hash(Span(0, 10)) == hash(Span(0, 10))
    assert Span(0, 10) != Span(0, 11)
    assert Span(0, 10) != Span(1, 10)


@pytest.mark.parametrize("attribute", ["start", "end"])
def test_a_span_cannot_be_changed(attribute: str) -> None:
    span = Span(0, 10)

    with pytest.raises(FrozenInstanceError):
        setattr(span, attribute, 5)

    assert span == Span(0, 10)


@pytest.mark.parametrize(("start", "end"), [(5, 5), (6, 5), (0, 0), (10000, 0)])
def test_an_empty_span_cannot_exist(start: int, end: int) -> None:
    with pytest.raises(InvalidSpanError):
        Span(start, end)


@pytest.mark.parametrize(
    "other",
    [
        Span(10, 20),
        Span(5, 15),
        Span(15, 25),
        Span(12, 18),
        Span(0, 30),
        Span(19, 20),
        Span(10, 11),
    ],
)
def test_spans_that_share_a_unit_overlap(other: Span) -> None:
    span = Span(10, 20)

    assert span.overlaps(other)
    assert other.overlaps(span)


@pytest.mark.parametrize("other", [Span(0, 10), Span(20, 30), Span(0, 5), Span(25, 30)])
def test_spans_that_touch_or_lie_apart_do_not_overlap(other: Span) -> None:
    span = Span(10, 20)

    assert not span.overlaps(other)
    assert not other.overlaps(span)
