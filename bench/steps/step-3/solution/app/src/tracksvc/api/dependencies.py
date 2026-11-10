"""Providers of the resources owned by the application lifespan and of the use cases."""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from tracksvc.application.ports import TrackRepository
from tracksvc.application.tracks import AddSegment, CreateTrack, GetTrack, RemoveSegment, SealTrack
from tracksvc.infrastructure.memory import InMemoryTrackRepository


def get_engine(request: Request) -> AsyncEngine:
    """Return the database engine created by the application lifespan."""
    engine: AsyncEngine = request.app.state.engine
    return engine


def get_track_repository(request: Request) -> TrackRepository:
    """Return the track storage created by the application lifespan."""
    tracks: InMemoryTrackRepository = request.app.state.tracks
    return tracks


type Tracks = Annotated[TrackRepository, Depends(get_track_repository)]


def get_create_track(tracks: Tracks) -> CreateTrack:
    """Return the use case that creates a track, wired to the track storage."""
    return CreateTrack(tracks)


def get_get_track(tracks: Tracks) -> GetTrack:
    """Return the use case that reads a track, wired to the track storage."""
    return GetTrack(tracks)


def get_add_segment(tracks: Tracks) -> AddSegment:
    """Return the use case that adds a segment, wired to the track storage."""
    return AddSegment(tracks)


def get_remove_segment(tracks: Tracks) -> RemoveSegment:
    """Return the use case that removes a segment, wired to the track storage."""
    return RemoveSegment(tracks)


def get_seal_track(tracks: Tracks) -> SealTrack:
    """Return the use case that seals a track, wired to the track storage."""
    return SealTrack(tracks)
