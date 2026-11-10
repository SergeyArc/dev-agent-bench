"""Errors raised when a business rule rejects an operation."""


class TrackCodeTakenError(Exception):
    """Another track already has the requested code."""

    def __init__(self, code: str) -> None:
        super().__init__(f"Track code {code!r} is already taken.")
        self.code = code


class InvalidSpanError(Exception):
    """A span would be empty: its start is not before its end."""

    def __init__(self, start: int, end: int) -> None:
        super().__init__(f"Span [{start}, {end}) is empty: start must be less than end.")


class SpanOutOfBoundsError(Exception):
    """A span would end beyond the capacity of its track."""

    def __init__(self, end: int, capacity: int) -> None:
        super().__init__(f"Span end {end} exceeds the track capacity {capacity}.")


class SegmentLabelTakenError(Exception):
    """The track already has a segment with the requested label."""

    def __init__(self, label: str) -> None:
        super().__init__(f"The track already has a segment labelled {label!r}.")


class SegmentOverlapError(Exception):
    """A span would overlap a segment of the track."""

    def __init__(self, label: str) -> None:
        super().__init__(f"The span overlaps segment {label!r}.")


class SegmentLimitReachedError(Exception):
    """The track already has as many segments as a track may have."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"A track has at most {limit} segments.")


class SegmentNotFoundError(Exception):
    """The track has no segment with the requested label."""

    def __init__(self, label: str) -> None:
        super().__init__(f"The track has no segment labelled {label!r}.")


class TrackSealedError(Exception):
    """The track is sealed and can no longer be modified."""

    def __init__(self) -> None:
        super().__init__("The track is sealed and cannot be modified.")


class TrackEmptyError(Exception):
    """A track without segments cannot be sealed."""

    def __init__(self) -> None:
        super().__init__("A track without segments cannot be sealed.")
