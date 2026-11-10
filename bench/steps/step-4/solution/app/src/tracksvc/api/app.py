"""The ASGI application and its lifespan."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from tracksvc.api import operational, tracks
from tracksvc.api.errors import register_error_handlers
from tracksvc.config import read_database_url
from tracksvc.infrastructure.database import create_engine, create_session_factory


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the database engine: create it at start-up and dispose of it at shutdown.

    The session factory bound to the engine is created alongside it.
    """
    engine = create_engine(read_database_url())
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
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
