"""Request and response bodies of the HTTP API."""

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from tracksvc.domain.track import Segment, Track

LABEL_PATTERN = r"^[a-z][a-z0-9_]{0,15}$"


class StatusResponse(BaseModel):
    """Body of a successful operational endpoint."""

    status: Literal["ok", "ready"]


class CreateTrackRequest(BaseModel):
    """Body of the request that creates a track; JSON types are exact, nothing is coerced."""

    model_config = ConfigDict(strict=True)

    code: str = Field(pattern=r"^[A-Z][A-Z0-9-]{2,31}$")
    capacity: int = Field(ge=1, le=10000)


class SpanRequest(BaseModel):
    """Span of the segment to add; JSON types are exact, nothing is coerced."""

    model_config = ConfigDict(strict=True)

    start: int = Field(ge=0, le=10000)
    end: int = Field(ge=0, le=10000)


class AddSegmentRequest(BaseModel):
    """Body of the request that adds a segment; JSON types are exact, nothing is coerced."""

    model_config = ConfigDict(strict=True)

    label: str = Field(pattern=LABEL_PATTERN)
    span: SpanRequest


class SpanResponse(BaseModel):
    """Representation of a span."""

    start: int
    end: int


class SegmentResponse(BaseModel):
    """Representation of a segment."""

    label: str
    span: SpanResponse

    @classmethod
    def from_segment(cls, segment: Segment) -> Self:
        """Build the representation of ``segment``."""
        span = SpanResponse(start=segment.span.start, end=segment.span.end)
        return cls(label=segment.label, span=span)


class TrackResponse(BaseModel):
    """Representation of a track in every success response."""

    id: UUID
    code: str
    capacity: int
    status: str
    segments: list[SegmentResponse]

    @classmethod
    def from_track(cls, track: Track) -> Self:
        """Build the representation of ``track``."""
        return cls(
            id=track.id,
            code=track.code,
            capacity=track.capacity,
            status=track.status.value,
            segments=[SegmentResponse.from_segment(segment) for segment in track.segments],
        )


class FieldViolation(BaseModel):
    """One violation found while validating a request."""

    field: str
    message: str


class ErrorDetail(BaseModel):
    """Machine-readable code and human-readable message of an error.

    ``details`` lists the violations of a validation error and is absent otherwise.
    """

    code: str
    message: str
    details: list[FieldViolation] | None = None


class ErrorResponse(BaseModel):
    """Body of every error response."""

    error: ErrorDetail
