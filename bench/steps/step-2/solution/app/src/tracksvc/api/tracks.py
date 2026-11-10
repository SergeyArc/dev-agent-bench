"""Track endpoints."""

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from tracksvc.api.dependencies import get_create_track, get_get_track
from tracksvc.api.schemas import CreateTrackRequest, TrackResponse
from tracksvc.application.tracks import CreateTrack, GetTrack

router = APIRouter(prefix="/tracks")


async def read_create_track_request(request: Request) -> CreateTrackRequest:
    """Parse and validate the body of the request that creates a track.

    The body is read as JSON whatever content type the client declares.

    Raises:
        RequestValidationError: If the body is not valid JSON, is not a JSON object or
            breaks the schema.
    """
    try:
        payload = json.loads(await request.body())
    # ValueError covers malformed JSON and undecodable bytes; RecursionError is raised
    # for absurdly deep nesting.
    except (ValueError, RecursionError):
        raise RequestValidationError(
            [{"type": "json_invalid", "loc": ("body",), "msg": "The body is not valid JSON."}]
        ) from None
    try:
        return CreateTrackRequest.model_validate(payload)
    except ValidationError as error:
        raise RequestValidationError(
            [{**item, "loc": ("body", *item["loc"])} for item in error.errors()]
        ) from None


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
