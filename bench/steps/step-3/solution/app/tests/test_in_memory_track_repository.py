from uuid import uuid4

import pytest

from tests.conftest import track_state
from tracksvc.domain.errors import TrackCodeTakenError
from tracksvc.domain.span import Span
from tracksvc.domain.track import Segment, Track, TrackStatus
from tracksvc.infrastructure.memory import InMemoryTrackRepository


@pytest.mark.asyncio
async def test_get_returns_the_added_track() -> None:
    tracks = InMemoryTrackRepository()
    track = Track.create(code="TRK-A1", capacity=100)

    await tracks.add(track)

    found = await tracks.get(track.id)
    assert found is not None
    assert track_state(found) == track_state(track)


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
    found = await tracks.get(first.id)
    assert found is not None
    assert track_state(found) == track_state(first)


@pytest.mark.asyncio
async def test_codes_are_compared_case_sensitively() -> None:
    tracks = InMemoryTrackRepository()
    upper = Track.create(code="TRK-A1", capacity=100)
    lower = Track.create(code="trk-a1", capacity=100)

    await tracks.add(upper)
    await tracks.add(lower)

    assert await tracks.get(lower.id) is not None


@pytest.mark.asyncio
async def test_repositories_do_not_share_tracks() -> None:
    first, second = InMemoryTrackRepository(), InMemoryTrackRepository()
    track = Track.create(code="TRK-A1", capacity=100)

    await first.add(track)

    assert await second.get(track.id) is None


@pytest.mark.asyncio
async def test_save_stores_the_changes_of_a_track() -> None:
    tracks = InMemoryTrackRepository()
    track = Track.create(code="TRK-A1", capacity=100)
    await tracks.add(track)

    track.add_segment("alpha", Span(0, 10))
    track.seal()
    await tracks.save(track)

    found = await tracks.get(track.id)
    assert found is not None
    assert found.segments == (Segment("alpha", Span(0, 10)),)
    assert found.status is TrackStatus.SEALED


@pytest.mark.asyncio
async def test_changes_that_are_not_saved_are_not_stored() -> None:
    tracks = InMemoryTrackRepository()
    track = Track.create(code="TRK-A1", capacity=100)
    await tracks.add(track)

    track.add_segment("alpha", Span(0, 10))
    handed_out = await tracks.get(track.id)
    assert handed_out is not None
    handed_out.add_segment("beta", Span(10, 20))

    found = await tracks.get(track.id)
    assert found is not None
    assert found.segments == ()
