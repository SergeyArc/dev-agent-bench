from uuid import uuid4

import pytest

from tests.conftest import track_state
from tracksvc.application.errors import TrackNotFoundError
from tracksvc.application.tracks import AddSegment, CreateTrack, GetTrack, RemoveSegment, SealTrack
from tracksvc.domain.errors import (
    InvalidSpanError,
    SegmentNotFoundError,
    SegmentOverlapError,
    TrackCodeTakenError,
    TrackEmptyError,
    TrackSealedError,
)
from tracksvc.domain.span import Span
from tracksvc.domain.track import Segment, Track, TrackStatus
from tracksvc.infrastructure.memory import InMemoryTrackRepository


@pytest.fixture
def tracks() -> InMemoryTrackRepository:
    return InMemoryTrackRepository()


async def stored(tracks: InMemoryTrackRepository, track: Track) -> Track:
    found = await tracks.get(track.id)
    assert found is not None
    return found


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

    assert track_state(await stored(tracks, track)) == track_state(track)


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

    assert track_state(await stored(tracks, first)) == track_state(first)


@pytest.mark.asyncio
async def test_get_track_returns_the_created_track(tracks: InMemoryTrackRepository) -> None:
    created = await CreateTrack(tracks).execute(code="TRK-A1", capacity=100)

    found = await GetTrack(tracks).execute(created.id)

    assert track_state(found) == track_state(created)


@pytest.mark.asyncio
async def test_get_track_reports_an_unknown_id(tracks: InMemoryTrackRepository) -> None:
    await CreateTrack(tracks).execute(code="TRK-A1", capacity=100)
    unknown_id = uuid4()

    with pytest.raises(TrackNotFoundError) as raised:
        await GetTrack(tracks).execute(unknown_id)

    assert raised.value.track_id == unknown_id


@pytest.mark.asyncio
async def test_add_segment_returns_and_stores_the_updated_track(
    tracks: InMemoryTrackRepository,
) -> None:
    created = await CreateTrack(tracks).execute(code="TRK-A1", capacity=100)

    updated = await AddSegment(tracks).execute(created.id, label="alpha", start=0, end=10)

    assert updated.segments == (Segment("alpha", Span(0, 10)),)
    assert track_state(await stored(tracks, created)) == track_state(updated)


@pytest.mark.asyncio
async def test_a_rejected_segment_leaves_the_stored_track_unchanged(
    tracks: InMemoryTrackRepository,
) -> None:
    created = await CreateTrack(tracks).execute(code="TRK-A1", capacity=100)
    before = await AddSegment(tracks).execute(created.id, label="alpha", start=0, end=10)

    with pytest.raises(SegmentOverlapError):
        await AddSegment(tracks).execute(created.id, label="beta", start=5, end=15)
    with pytest.raises(InvalidSpanError):
        await AddSegment(tracks).execute(created.id, label="beta", start=20, end=20)

    assert track_state(await stored(tracks, created)) == track_state(before)


@pytest.mark.asyncio
async def test_remove_segment_returns_and_stores_the_updated_track(
    tracks: InMemoryTrackRepository,
) -> None:
    created = await CreateTrack(tracks).execute(code="TRK-A1", capacity=100)
    await AddSegment(tracks).execute(created.id, label="alpha", start=0, end=10)
    await AddSegment(tracks).execute(created.id, label="beta", start=10, end=25)

    updated = await RemoveSegment(tracks).execute(created.id, "beta")

    assert updated.segments == (Segment("alpha", Span(0, 10)),)
    assert track_state(await stored(tracks, created)) == track_state(updated)


@pytest.mark.asyncio
async def test_remove_segment_reports_an_absent_label(tracks: InMemoryTrackRepository) -> None:
    created = await CreateTrack(tracks).execute(code="TRK-A1", capacity=100)

    with pytest.raises(SegmentNotFoundError):
        await RemoveSegment(tracks).execute(created.id, "alpha")


@pytest.mark.asyncio
async def test_seal_track_returns_and_stores_the_sealed_track(
    tracks: InMemoryTrackRepository,
) -> None:
    created = await CreateTrack(tracks).execute(code="TRK-A1", capacity=100)
    await AddSegment(tracks).execute(created.id, label="alpha", start=0, end=10)

    sealed = await SealTrack(tracks).execute(created.id)

    assert sealed.status is TrackStatus.SEALED
    assert (await stored(tracks, created)).status is TrackStatus.SEALED
    with pytest.raises(TrackSealedError):
        await AddSegment(tracks).execute(created.id, label="beta", start=10, end=20)


@pytest.mark.asyncio
async def test_seal_track_rejects_an_empty_track(tracks: InMemoryTrackRepository) -> None:
    created = await CreateTrack(tracks).execute(code="TRK-A1", capacity=100)

    with pytest.raises(TrackEmptyError):
        await SealTrack(tracks).execute(created.id)

    assert (await stored(tracks, created)).status is TrackStatus.OPEN


@pytest.mark.asyncio
async def test_changing_use_cases_report_an_unknown_track_before_anything_else(
    tracks: InMemoryTrackRepository,
) -> None:
    with pytest.raises(TrackNotFoundError):
        await AddSegment(tracks).execute(uuid4(), label="alpha", start=5, end=5)
    with pytest.raises(TrackNotFoundError):
        await RemoveSegment(tracks).execute(uuid4(), "alpha")
    with pytest.raises(TrackNotFoundError):
        await SealTrack(tracks).execute(uuid4())
