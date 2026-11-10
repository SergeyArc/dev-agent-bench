"""The ASGI application and its lifespan."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from tracksvc.api import operational, tracks
from tracksvc.api.errors import register_error_handlers
from tracksvc.config import read_database_url
from tracksvc.infrastructure.database import create_engine
from tracksvc.infrastructure.memory import InMemoryTrackRepository


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the resources of the process: the database engine and the track storage.

    Both are created at start-up; the engine is disposed of at shutdown.
    """
    engine = create_engine(read_database_url())
    app.state.engine = engine
    app.state.tracks = InMemoryTrackRepository()
    try:
        yield
    finally:
        await engine.dispose()


def create_app() -> FastAPI:
    """Assemble the HTTP application without reading configuration or opening connections."""
    application = FastAPI(title="tracksvc", lifespan=lifespan)
    register_error_handlers(application)
    application.include_router(operational.router)
    application.include_router(tracks.router)
    return application


app = create_app()
