"""Providers of the resources owned by the application lifespan and of the use cases."""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from tracksvc.application.ports import TrackRepository
from tracksvc.application.tracks import CreateTrack, GetTrack
from tracksvc.infrastructure.memory import InMemoryTrackRepository


def get_engine(request: Request) -> AsyncEngine:
    """Return the database engine created by the application lifespan."""
    engine: AsyncEngine = request.app.state.engine
    return engine


def get_track_repository(request: Request) -> TrackRepository:
    """Return the track storage created by the application lifespan."""
    tracks: InMemoryTrackRepository = request.app.state.tracks
    return tracks


def get_create_track(
    tracks: Annotated[TrackRepository, Depends(get_track_repository)],
) -> CreateTrack:
    """Return the use case that creates a track, wired to the track storage."""
    return CreateTrack(tracks)


def get_get_track(tracks: Annotated[TrackRepository, Depends(get_track_repository)]) -> GetTrack:
    """Return the use case that reads a track, wired to the track storage."""
    return GetTrack(tracks)
