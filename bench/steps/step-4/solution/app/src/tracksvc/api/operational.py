"""Operational endpoints: liveness and readiness."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncEngine

from tracksvc.api.dependencies import get_engine
from tracksvc.api.errors import DatabaseUnavailableError
from tracksvc.api.schemas import StatusResponse
from tracksvc.infrastructure.database import is_database_ready

router = APIRouter()


@router.get("/health")
async def health() -> StatusResponse:
    """Report that the process is up."""
    return StatusResponse(status="ok")


@router.get("/ready")
async def ready(engine: Annotated[AsyncEngine, Depends(get_engine)]) -> StatusResponse:
    """Report whether the database answers.

    Raises:
        DatabaseUnavailableError: If the readiness probe fails.
    """
    if not await is_database_ready(engine):
        raise DatabaseUnavailableError
    return StatusResponse(status="ready")
