"""The span."""

from dataclasses import dataclass

from tracksvc.domain.errors import InvalidSpanError


@dataclass(frozen=True, slots=True)
class Span:
    """The half-open interval ``[start, end)`` of integers; never empty.

    Raises:
        InvalidSpanError: If ``start`` is not less than ``end``.
    """

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start >= self.end:
            raise InvalidSpanError(self.start, self.end)

    def overlaps(self, other: "Span") -> bool:
        """Tell whether the two spans share a unit; spans that only touch do not."""
        return self.start < other.end and other.start < self.end
