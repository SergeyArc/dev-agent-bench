import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tracksvc.infrastructure.database import is_ready


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


@app.get("/health")
async def health() -> dict[str, str]:
    """Report that the process is up."""
    return {"status": "ok"}


@app.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Report whether the database answers (broken on purpose: extra key in the 503)."""
    engine: AsyncEngine = request.app.state.engine
    if await is_ready(engine):
        return JSONResponse({"status": "ready"})
    error = {"code": "database_unavailable", "message": "database is not reachable", "status": 503}
    return JSONResponse({"error": error}, status_code=503)
