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

from tracksvc.domain.model import DomainError, Span, Track
from tracksvc.infrastructure import store
from tracksvc.infrastructure.database import is_ready

CODE = re.compile(r"[A-Z][A-Z0-9-]{2,31}")
LABEL = re.compile(r"[a-z][a-z0-9_]{0,15}")
NOT_FOUND = frozenset({"track_not_found", "segment_not_found"})
MAX_UNITS = 10000


class InvalidRequestError(Exception):
    """A request that violates the schema; `field` is the offending location."""

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Create the engine at start-up and dispose of it at shutdown."""
    engine = create_async_engine(os.environ["DATABASE_URL"])
    application.state.engine = engine
    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(InvalidRequestError)
async def _on_invalid(_: Request, exc: InvalidRequestError) -> JSONResponse:
    details = [{"field": exc.field, "message": "invalid value"}]
    error = {"code": "validation_error", "message": "the request is malformed", "details": details}
    return JSONResponse({"error": error}, status_code=422)


@app.exception_handler(DomainError)
async def _on_rejected(_: Request, exc: DomainError) -> JSONResponse:
    error = {"code": exc.code, "message": f"the request was rejected: {exc.code}"}
    return JSONResponse({"error": error}, status_code=404 if exc.code in NOT_FOUND else 409)


def _engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.engine
    return engine


async def _object(request: Request) -> dict[str, Any]:
    try:
        body = json.loads(await request.body())
    except ValueError as exc:
        raise InvalidRequestError("body") from exc
    if not isinstance(body, dict):
        raise InvalidRequestError("body")
    return body


def _integer(source: dict[str, Any], key: str, field: str, low: int) -> int:
    value = source.get(key)
    if type(value) is not int or not low <= value <= MAX_UNITS:
        raise InvalidRequestError(field)
    return value


def _string(value: object, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise InvalidRequestError(field)
    return value


def _track_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise InvalidRequestError("track_id") from exc


def _body(track: Track) -> dict[str, Any]:
    segments = [
        {"label": segment.label, "span": {"start": segment.span.start, "end": segment.span.end}}
        for segment in track.segments
    ]
    return {
        "id": str(track.id),
        "code": track.code,
        "capacity": track.capacity,
        "status": track.status,
        "segments": segments,
    }


@app.get("/health")
async def health() -> dict[str, str]:
    """Report that the process is up."""
    return {"status": "ok"}


@app.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Report whether the database answers."""
    if await is_ready(_engine(request)):
        return JSONResponse({"status": "ready"})
    error = {"code": "database_unavailable", "message": "database is not reachable"}
    return JSONResponse({"error": error}, status_code=503)


@app.post("/tracks", status_code=201)
async def create_track(request: Request) -> dict[str, Any]:
    """Create a track."""
    body = await _object(request)
    code = _string(body.get("code"), "code", CODE)
    track = Track(uuid.uuid4(), code, _integer(body, "capacity", "capacity", 1))
    async with _engine(request).begin() as connection:
        await store.insert_track(connection, track)
    return _body(track)


@app.get("/tracks/{track_id}")
async def get_track(track_id: str, request: Request) -> dict[str, Any]:
    """Return a track."""
    identifier = _track_id(track_id)
    async with _engine(request).begin() as connection:
        return _body(await store.load_track(connection, identifier, lock=False))


@app.post("/tracks/{track_id}/segments", status_code=201)
async def add_segment(track_id: str, request: Request) -> dict[str, Any]:
    """Reserve a segment on a track."""
    identifier = _track_id(track_id)
    body = await _object(request)
    label = _string(body.get("label"), "label", LABEL)
    span = body.get("span")
    if not isinstance(span, dict):
        raise InvalidRequestError("span")
    start = _integer(span, "start", "span.start", 0)
    end = _integer(span, "end", "span.end", 0)
    async with _engine(request).begin() as connection:
        track = await store.load_track(connection, identifier, lock=True)
        track.add_segment(label, Span(start, end))
        await store.save_track(connection, track)
    return _body(track)


@app.delete("/tracks/{track_id}/segments/{label}")
async def remove_segment(track_id: str, label: str, request: Request) -> dict[str, Any]:
    """Release a segment of a track."""
    identifier = _track_id(track_id)
    _string(label, "label", LABEL)
    async with _engine(request).begin() as connection:
        track = await store.load_track(connection, identifier, lock=True)
        track.remove_segment(label)
        await store.save_track(connection, track)
    return _body(track)


@app.post("/tracks/{track_id}/seal")
async def seal_track(track_id: str, request: Request) -> dict[str, Any]:
    """Seal a track."""
    identifier = _track_id(track_id)
    async with _engine(request).begin() as connection:
        track = await store.load_track(connection, identifier, lock=True)
        track.seal()
        await store.save_track(connection, track)
    return _body(track)
