import json
import os
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tracksvc.domain.track import DomainError, Span, Track
from tracksvc.infrastructure.database import is_ready

CODE = re.compile(r"[A-Z][A-Z0-9-]{2,31}")
LABEL = re.compile(r"[a-z][a-z0-9_]{0,15}")
NOT_FOUND = frozenset({"track_not_found", "segment_not_found"})


class InvalidRequestError(Exception):
    """A request that violates the schema; `field` is the offending location."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Create the engine and the in-memory storage; dispose of the engine at shutdown."""
    engine = create_async_engine(os.environ["DATABASE_URL"])
    application.state.engine = engine
    application.state.tracks = {}
    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(lifespan=lifespan)


def _error(status: int, code: str, field: str | None = None) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": code.replace("_", " ")}
    if field is not None:
        error["details"] = [{"field": field, "message": "invalid value"}]
    return JSONResponse({"error": error}, status_code=status)


@app.exception_handler(InvalidRequestError)
async def invalid_request(request: Request, exc: InvalidRequestError) -> JSONResponse:
    """Translate a schema violation into the error body."""
    return _error(422, "validation_error", exc.field)


@app.exception_handler(DomainError)
async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
    """Translate a rejection of the model into the error body."""
    return _error(404 if exc.code in NOT_FOUND else 409, exc.code)


def _view(track: Track) -> dict[str, Any]:
    return {
        "id": track.id,
        "code": track.code,
        "capacity": track.capacity,
        "status": "sealed" if track.sealed else "open",
        "segments": [
            {"label": segment.label, "span": {"start": segment.span.start, "end": segment.span.end}}
            for segment in track.segments
        ],
    }


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        data = json.loads(await request.body())
    except ValueError:
        raise InvalidRequestError("body") from None
    if not isinstance(data, dict):
        raise InvalidRequestError("body")
    return data


def _text(data: dict[str, Any], key: str, pattern: re.Pattern[str]) -> str:
    value = data.get(key)
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise InvalidRequestError(key)
    return value


def _integer(data: dict[str, Any], key: str, field: str, low: int, high: int) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidRequestError(field)
    return value


def _segment(data: dict[str, Any]) -> tuple[str, Span]:
    label = _text(data, "label", LABEL)
    span = data.get("span")
    if not isinstance(span, dict):
        raise InvalidRequestError("span")
    start = _integer(span, "start", "span.start", 0, 10000)
    end = _integer(span, "end", "span.end", 0, 10000)
    return label, Span(start, end)


def _track_key(track_id: str) -> str:
    try:
        return str(uuid.UUID(track_id))
    except ValueError:
        raise InvalidRequestError("track_id") from None


def _tracks(request: Request) -> dict[str, Track]:
    tracks: dict[str, Track] = request.app.state.tracks
    return tracks


def _lookup(request: Request, key: str) -> Track:
    track = _tracks(request).get(key)
    if track is None:
        raise DomainError("track_not_found")
    return track


@app.get("/health")
async def health() -> dict[str, str]:
    """Report that the process is up."""
    return {"status": "ok"}


@app.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Report whether the database answers."""
    engine: AsyncEngine = request.app.state.engine
    if await is_ready(engine):
        return JSONResponse({"status": "ready"})
    return _error(503, "database_unavailable")


@app.post("/tracks", status_code=201)
async def create_track(request: Request) -> dict[str, Any]:
    """Create a track with a unique code."""
    data = await _json_object(request)
    code = _text(data, "code", CODE)
    capacity = _integer(data, "capacity", "capacity", 1, 10000)
    tracks = _tracks(request)
    if any(track.code == code for track in tracks.values()):
        raise DomainError("track_code_taken")
    created = Track(str(uuid.uuid4()), code, capacity)
    tracks[created.id] = created
    return _view(created)


@app.get("/tracks/{track_id}")
async def get_track(track_id: str, request: Request) -> dict[str, Any]:
    """Return a track by its id."""
    return _view(_lookup(request, _track_key(track_id)))


@app.post("/tracks/{track_id}/segments", status_code=201)
async def add_segment(track_id: str, request: Request) -> dict[str, Any]:
    """Reserve a segment on a track."""
    key = _track_key(track_id)
    label, span = _segment(await _json_object(request))
    track = _lookup(request, key)
    track.add(label, span)
    return _view(track)


@app.delete("/tracks/{track_id}/segments/{label}")
async def remove_segment(track_id: str, label: str, request: Request) -> dict[str, Any]:
    """Remove a segment from a track."""
    key = _track_key(track_id)
    if LABEL.fullmatch(label) is None:
        raise InvalidRequestError("label")
    track = _lookup(request, key)
    track.remove(label)
    return _view(track)


@app.post("/tracks/{track_id}/seal")
async def seal_track(track_id: str, request: Request) -> dict[str, Any]:
    """Seal a track."""
    track = _lookup(request, _track_key(track_id))
    track.seal()
    return _view(track)
