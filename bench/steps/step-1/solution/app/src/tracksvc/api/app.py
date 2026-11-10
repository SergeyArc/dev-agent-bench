"""The ASGI application and its lifespan."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from tracksvc.api import operational
from tracksvc.api.errors import register_error_handlers
from tracksvc.config import read_database_url
from tracksvc.infrastructure.database import create_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the database engine: create it at start-up and dispose of it at shutdown."""
    engine = create_engine(read_database_url())
    app.state.engine = engine
    try:
        yield
    finally:
        await engine.dispose()


def create_app() -> FastAPI:
    """Assemble the HTTP application without reading configuration or opening connections."""
    application = FastAPI(title="tracksvc", lifespan=lifespan)
    register_error_handlers(application)
    application.include_router(operational.router)
    return application


app = create_app()
