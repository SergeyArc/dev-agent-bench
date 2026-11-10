import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tracksvc.api.errors import error_response
from tracksvc.api.validation import InvalidRequestError, parse_new_track, parse_track_id
from tracksvc.application.tracks import TrackStore, create_track, get_track
from tracksvc.domain.track import Track, TrackCodeTakenError, TrackNotFoundError
from tracksvc.infrastructure.database import is_ready
from tracksvc.infrastructure.memory import InMemoryTrackStore


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Create the engine and the track store at start-up; dispose of the engine at shutdown."""
    engine = create_async_engine(os.environ["DATABASE_URL"])
    application.state.engine = engine
    application.state.tracks = InMemoryTrackStore()
    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(lifespan=lifespan)


def _representation(track: Track) -> dict[str, object]:
    return {
        "id": str(track.id),
        "code": track.code,
        "capacity": track.capacity,
        "status": track.status,
        "segments": [],
    }


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
    return error_response(503, "database_unavailable", "database is not reachable")


@app.post("/tracks")
async def create(request: Request) -> JSONResponse:
    """Create a track."""
    code, capacity = parse_new_track(await request.body())
    store: TrackStore = request.app.state.tracks
    return JSONResponse(_representation(create_track(store, code, capacity)), status_code=201)


@app.get("/tracks/{track_id}")
async def read(track_id: str, request: Request) -> JSONResponse:
    """Return a track."""
    store: TrackStore = request.app.state.tracks
    return JSONResponse(_representation(get_track(store, parse_track_id(track_id))))


@app.exception_handler(InvalidRequestError)
async def invalid_request(request: Request, exc: InvalidRequestError) -> JSONResponse:
    """Map a malformed request to 422."""
    return error_response(422, "validation_error", exc.message, exc.field)


@app.exception_handler(TrackNotFoundError)
async def track_not_found(request: Request, exc: TrackNotFoundError) -> JSONResponse:
    """Map an unknown track id to 404."""
    return error_response(404, "track_not_found", "no track with this id")


@app.exception_handler(TrackCodeTakenError)
async def track_code_taken(request: Request, exc: TrackCodeTakenError) -> JSONResponse:
    """Map a duplicate code to 409."""
    return error_response(409, "track_code_taken", "another track has this code")
