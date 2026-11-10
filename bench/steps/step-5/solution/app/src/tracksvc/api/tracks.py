"""Track endpoints."""

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from tracksvc.api.dependencies import (
    get_add_segment,
    get_create_track,
    get_get_track,
    get_remove_segment,
    get_seal_track,
)
from tracksvc.api.schemas import (
    LABEL_PATTERN,
    AddSegmentRequest,
    CreateTrackRequest,
    TrackResponse,
)
from tracksvc.application.tracks import AddSegment, CreateTrack, GetTrack, RemoveSegment, SealTrack

router = APIRouter(prefix="/tracks")


async def _read_body[SchemaT: BaseModel](request: Request, schema: type[SchemaT]) -> SchemaT:
    try:
        payload = json.loads(await request.body())
    # ValueError covers malformed JSON and undecodable bytes; RecursionError is raised
    # for absurdly deep nesting.
    except (ValueError, RecursionError):
        raise RequestValidationError(
            [{"type": "json_invalid", "loc": ("body",), "msg": "The body is not valid JSON."}]
        ) from None
    try:
        return schema.model_validate(payload)
    except ValidationError as error:
        raise RequestValidationError(
            [{**item, "loc": ("body", *item["loc"])} for item in error.errors()]
        ) from None


async def read_create_track_request(request: Request) -> CreateTrackRequest:
    """Parse and validate the body of the request that creates a track.

    The body is read as JSON whatever content type the client declares.

    Raises:
        RequestValidationError: If the body is not valid JSON, is not a JSON object or
            breaks the schema.
    """
    return await _read_body(request, CreateTrackRequest)


async def read_add_segment_request(request: Request) -> AddSegmentRequest:
    """Parse and validate the body of the request that adds a segment.

    The body is read as JSON whatever content type the client declares.

    Raises:
        RequestValidationError: If the body is not valid JSON, is not a JSON object or
            breaks the schema.
    """
    return await _read_body(request, AddSegmentRequest)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_track(
    body: Annotated[CreateTrackRequest, Depends(read_create_track_request)],
    create: Annotated[CreateTrack, Depends(get_create_track)],
) -> TrackResponse:
    """Create a track."""
    return TrackResponse.from_track(await create.execute(code=body.code, capacity=body.capacity))


@router.get("/{track_id}")
async def get_track(
    track_id: UUID, get: Annotated[GetTrack, Depends(get_get_track)]
) -> TrackResponse:
    """Return the track with the given id."""
    return TrackResponse.from_track(await get.execute(track_id))


@router.post("/{track_id}/segments", status_code=status.HTTP_201_CREATED)
async def add_segment(
    track_id: UUID,
    body: Annotated[AddSegmentRequest, Depends(read_add_segment_request)],
    add: Annotated[AddSegment, Depends(get_add_segment)],
) -> TrackResponse:
    """Reserve a segment on the track and return the updated track."""
    track = await add.execute(track_id, label=body.label, start=body.span.start, end=body.span.end)
    return TrackResponse.from_track(track)


@router.delete("/{track_id}/segments/{label}")
async def remove_segment(
    track_id: UUID,
    label: Annotated[str, Path(pattern=LABEL_PATTERN)],
    remove: Annotated[RemoveSegment, Depends(get_remove_segment)],
) -> TrackResponse:
    """Remove a segment from the track and return the updated track."""
    return TrackResponse.from_track(await remove.execute(track_id, label))


@router.post("/{track_id}/seal")
async def seal_track(
    track_id: UUID, seal: Annotated[SealTrack, Depends(get_seal_track)]
) -> TrackResponse:
    """Seal the track and return it."""
    return TrackResponse.from_track(await seal.execute(track_id))
