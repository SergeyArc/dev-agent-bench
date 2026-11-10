import json
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tracksvc.domain.model import DomainError, Segment, Track
from tracksvc.infrastructure import store
from tracksvc.infrastructure.database import is_ready

CODE = re.compile(r"[A-Z][A-Z0-9-]{2,31}")
LABEL = re.compile(r"[a-z][a-z0-9_]{0,15}")
NOT_FOUND = frozenset({"track_not_found", "segment_not_found"})
MAX_UNITS = 10000


class RequestError(Exception):
    """A malformed request; `field` is the offending location."""

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


@app.exception_handler(RequestError)
async def request_error(request: Request, exc: RequestError) -> JSONResponse:
    """Answer 422 with the offending field."""
    details = [{"field": exc.field, "message": "invalid value"}]
    error = {"code": "validation_error", "message": "request is malformed", "details": details}
    return JSONResponse({"error": error}, status_code=422)


@app.exception_handler(DomainError)
async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
    """Answer 404 or 409 with the code of the broken rule."""
    error = {"code": exc.code, "message": exc.code.replace("_", " ")}
    return JSONResponse({"error": error}, status_code=404 if exc.code in NOT_FOUND else 409)


def _engine(request: Request) -> AsyncEngine:
    engine: AsyncEngine = request.app.state.engine
    return engine


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = json.loads(await request.body())
    except ValueError as exc:
        raise RequestError("body") from exc
    if not isinstance(body, dict):
        raise RequestError("body")
    return body


def _string(body: dict[str, Any], name: str, pattern: re.Pattern[str]) -> str:
    value = body.get(name)
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise RequestError(name)
    return value


def _integer(body: dict[str, Any], name: str, low: int, field: str) -> int:
    value = body.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= MAX_UNITS:
        raise RequestError(field)
    return value


def _track_id(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as exc:
        raise RequestError("track_id") from exc


def _view(track: Track) -> dict[str, Any]:
    segments = [
        {"label": item.label, "span": {"start": item.start, "end": item.end}}
        for item in track.segments
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
    body = await _body(request)
    code = _string(body, "code", CODE)
    capacity = _integer(body, "capacity", 1, "capacity")
    return _view(await store.create_track(_engine(request), code, capacity))


@app.get("/tracks/{track_id}")
async def get_track(track_id: str, request: Request) -> dict[str, Any]:
    """Return a track."""
    return _view(await store.get_track(_engine(request), _track_id(track_id)))


@app.post("/tracks/{track_id}/segments", status_code=201)
async def add_segment(track_id: str, request: Request) -> dict[str, Any]:
    """Reserve a segment on a track."""
    parsed = _track_id(track_id)
    body = await _body(request)
    label = _string(body, "label", LABEL)
    span = body.get("span")
    if not isinstance(span, dict):
        raise RequestError("span")
    segment = Segment(
        label, _integer(span, "start", 0, "span.start"), _integer(span, "end", 0, "span.end")
    )
    return _view(await store.add_segment(_engine(request), parsed, segment))


@app.delete("/tracks/{track_id}/segments/{label}")
async def remove_segment(track_id: str, label: str, request: Request) -> dict[str, Any]:
    """Release a segment of a track."""
    parsed = _track_id(track_id)
    if LABEL.fullmatch(label) is None:
        raise RequestError("label")
    return _view(await store.remove_segment(_engine(request), parsed, label))


@app.post("/tracks/{track_id}/seal")
async def seal(track_id: str, request: Request) -> dict[str, Any]:
    """Seal a track."""
    return _view(await store.seal(_engine(request), _track_id(track_id)))
