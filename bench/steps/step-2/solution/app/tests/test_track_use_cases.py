from uuid import uuid4

import pytest

from tracksvc.application.errors import TrackNotFoundError
from tracksvc.application.tracks import CreateTrack, GetTrack
from tracksvc.domain.errors import TrackCodeTakenError
from tracksvc.domain.track import TrackStatus
from tracksvc.infrastructure.memory import InMemoryTrackRepository


@pytest.fixture
def tracks() -> InMemoryTrackRepository:
    return InMemoryTrackRepository()


@pytest.mark.asyncio
async def test_create_track_returns_an_open_track_without_segments(
    tracks: InMemoryTrackRepository,
) -> None:
    track = await CreateTrack(tracks).execute(code="TRK-A1", capacity=100)

    assert track.code == "TRK-A1"
    assert track.capacity == 100
    assert track.status is TrackStatus.OPEN
    assert track.segments == ()


@pytest.mark.asyncio
async def test_create_track_stores_the_track_it_returns(tracks: InMemoryTrackRepository) -> None:
    track = await CreateTrack(tracks).execute(code="TRK-A1", capacity=100)

    assert await tracks.get(track.id) == track


@pytest.mark.asyncio
async def test_create_track_gives_every_track_its_own_id(tracks: InMemoryTrackRepository) -> None:
    create = CreateTrack(tracks)

    first = await create.execute(code="TRK-A1", capacity=1)
    second = await create.execute(code="TRK-B2", capacity=1)

    assert first.id != second.id


@pytest.mark.asyncio
async def test_create_track_rejects_a_taken_code(tracks: InMemoryTrackRepository) -> None:
    create = CreateTrack(tracks)
    first = await create.execute(code="TRK-A1", capacity=100)

    with pytest.raises(TrackCodeTakenError):
        await create.execute(code="TRK-A1", capacity=5)

    assert await tracks.get(first.id) == first


@pytest.mark.asyncio
async def test_get_track_returns_the_created_track(tracks: InMemoryTrackRepository) -> None:
    created = await CreateTrack(tracks).execute(code="TRK-A1", capacity=100)

    assert await GetTrack(tracks).execute(created.id) == created


@pytest.mark.asyncio
async def test_get_track_reports_an_unknown_id(tracks: InMemoryTrackRepository) -> None:
    await CreateTrack(tracks).execute(code="TRK-A1", capacity=100)
    unknown_id = uuid4()

    with pytest.raises(TrackNotFoundError) as raised:
        await GetTrack(tracks).execute(unknown_id)

    assert raised.value.track_id == unknown_id
