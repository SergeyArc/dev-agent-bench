"""Providers of the resources owned by the application lifespan and of the use cases."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from tracksvc.application.ports import UnitOfWork
from tracksvc.application.tracks import AddSegment, CreateTrack, GetTrack, RemoveSegment, SealTrack
from tracksvc.infrastructure.unit_of_work import SqlAlchemyUnitOfWork


def get_engine(request: Request) -> AsyncEngine:
    """Return the database engine created by the application lifespan."""
    engine: AsyncEngine = request.app.state.engine
    return engine


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Provide the database session of the current request and close it afterwards.

    This is the only place where sessions are created. The session is closed on every
    path, whether the request succeeds or fails.
    """
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:
        yield session


def get_unit_of_work(session: Annotated[AsyncSession, Depends(get_session)]) -> UnitOfWork:
    """Return the unit of work of the current request, bound to its session."""
    return SqlAlchemyUnitOfWork(session)


type RequestUnitOfWork = Annotated[UnitOfWork, Depends(get_unit_of_work)]


def get_create_track(uow: RequestUnitOfWork) -> CreateTrack:
    """Return the use case that creates a track, wired to the unit of work."""
    return CreateTrack(uow)


def get_get_track(uow: RequestUnitOfWork) -> GetTrack:
    """Return the use case that reads a track, wired to the unit of work."""
    return GetTrack(uow)


def get_add_segment(uow: RequestUnitOfWork) -> AddSegment:
    """Return the use case that adds a segment, wired to the unit of work."""
    return AddSegment(uow)


def get_remove_segment(uow: RequestUnitOfWork) -> RemoveSegment:
    """Return the use case that removes a segment, wired to the unit of work."""
    return RemoveSegment(uow)


def get_seal_track(uow: RequestUnitOfWork) -> SealTrack:
    """Return the use case that seals a track, wired to the unit of work."""
    return SealTrack(uow)
