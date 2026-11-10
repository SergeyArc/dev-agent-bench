from uuid import uuid4

import pytest

from tracksvc.domain.errors import TrackCodeTakenError
from tracksvc.domain.track import Track
from tracksvc.infrastructure.memory import InMemoryTrackRepository


@pytest.mark.asyncio
async def test_get_returns_the_added_track() -> None:
    tracks = InMemoryTrackRepository()
    track = Track.create(code="TRK-A1", capacity=100)

    await tracks.add(track)

    assert await tracks.get(track.id) == track


@pytest.mark.asyncio
async def test_get_returns_none_for_an_unknown_id() -> None:
    tracks = InMemoryTrackRepository()
    await tracks.add(Track.create(code="TRK-A1", capacity=100))

    assert await tracks.get(uuid4()) is None


@pytest.mark.asyncio
async def test_add_rejects_a_taken_code_and_stores_nothing() -> None:
    tracks = InMemoryTrackRepository()
    first = Track.create(code="TRK-A1", capacity=100)
    await tracks.add(first)
    second = Track.create(code="TRK-A1", capacity=5)

    with pytest.raises(TrackCodeTakenError) as raised:
        await tracks.add(second)

    assert raised.value.code == "TRK-A1"
    assert await tracks.get(second.id) is None
    assert await tracks.get(first.id) == first


@pytest.mark.asyncio
async def test_codes_are_compared_case_sensitively() -> None:
    tracks = InMemoryTrackRepository()
    upper = Track.create(code="TRK-A1", capacity=100)
    lower = Track.create(code="trk-a1", capacity=100)

    await tracks.add(upper)
    await tracks.add(lower)

    assert await tracks.get(lower.id) == lower


@pytest.mark.asyncio
async def test_repositories_do_not_share_tracks() -> None:
    first, second = InMemoryTrackRepository(), InMemoryTrackRepository()
    track = Track.create(code="TRK-A1", capacity=100)

    await first.add(track)

    assert await second.get(track.id) is None
