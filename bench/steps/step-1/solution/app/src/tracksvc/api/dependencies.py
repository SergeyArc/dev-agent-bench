"""Providers of the resources owned by the application lifespan."""

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine


def get_engine(request: Request) -> AsyncEngine:
    """Return the database engine created by the application lifespan."""
    engine: AsyncEngine = request.app.state.engine
    return engine
