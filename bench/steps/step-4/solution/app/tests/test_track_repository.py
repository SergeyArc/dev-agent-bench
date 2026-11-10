import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import track_state
from tracksvc.domain.errors import TrackCodeTakenError
from tracksvc.domain.span import Span
from tracksvc.domain.track import Segment, Track, TrackStatus
from tracksvc.infrastructure.repositories import SqlAlchemyTrackRepository

type SessionFactory = async_sessionmaker[AsyncSession]


async def add(session_factory: SessionFactory, track: Track) -> None:
    async with session_factory() as session, session.begin():
        await SqlAlchemyTrackRepository(session).add(track)


async def save(session_factory: SessionFactory, track: Track) -> None:
    async with session_factory() as session, session.begin():
        await SqlAlchemyTrackRepository(session).save(track)


async def get(session_factory: SessionFactory, track: Track) -> Track | None:
    async with session_factory() as session:
        return await SqlAlchemyTrackRepository(session).get(track.id)


async def count(session_factory: SessionFactory, table: str) -> int:
    async with session_factory() as session:
        rows: int = (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
    return rows


@pytest.mark.asyncio
async def test_get_returns_the_added_track(session_factory: SessionFactory) -> None:
    track = Track.create(code="TRK-A1", capacity=100)

    await add(session_factory, track)

    found = await get(session_factory, track)
    assert found is not None
    assert track_state(found) == track_state(track)


@pytest.mark.asyncio
async def test_get_returns_none_for_an_unknown_id(session_factory: SessionFactory) -> None:
    await add(session_factory, Track.create(code="TRK-A1", capacity=100))

    async with session_factory() as session:
        repository = SqlAlchemyTrackRepository(session)
        assert await repository.get(uuid4()) is None
        assert await repository.get_for_update(uuid4()) is None


@pytest.mark.asyncio
async def test_add_rejects_a_taken_code_and_stores_nothing(session_factory: SessionFactory) -> None:
    first = Track.create(code="TRK-A1", capacity=100)
    await add(session_factory, first)
    second = Track.create(code="TRK-A1", capacity=5)

    with pytest.raises(TrackCodeTakenError) as raised:
        await add(session_factory, second)

    assert raised.value.code == "TRK-A1"
    assert await get(session_factory, second) is None
    found = await get(session_factory, first)
    assert found is not None
    assert track_state(found) == track_state(first)
    assert await count(session_factory, "tracks") == 1


@pytest.mark.asyncio
async def test_codes_are_compared_case_sensitively(session_factory: SessionFactory) -> None:
    upper = Track.create(code="TRK-A1", capacity=100)
    lower = Track.create(code="trk-a1", capacity=100)

    await add(session_factory, upper)
    await add(session_factory, lower)

    assert await get(session_factory, lower) is not None


@pytest.mark.asyncio
async def test_add_stores_the_segments_of_the_track(session_factory: SessionFactory) -> None:
    track = Track.create(code="TRK-A1", capacity=100)
    track.add_segment("beta", Span(10, 25))
    track.add_segment("alpha", Span(0, 10))

    await add(session_factory, track)

    found = await get(session_factory, track)
    assert found is not None
    assert found.segments == (Segment("alpha", Span(0, 10)), Segment("beta", Span(10, 25)))


@pytest.mark.asyncio
async def test_save_stores_the_changes_of_a_track(session_factory: SessionFactory) -> None:
    track = Track.create(code="TRK-A1", capacity=100)
    await add(session_factory, track)

    track.add_segment("alpha", Span(0, 10))
    track.seal()
    await save(session_factory, track)

    found = await get(session_factory, track)
    assert found is not None
    assert found.segments == (Segment("alpha", Span(0, 10)),)
    assert found.status is TrackStatus.SEALED
    assert await count(session_factory, "tracks") == 1


@pytest.mark.asyncio
async def test_save_leaves_no_rows_of_removed_segments(session_factory: SessionFactory) -> None:
    track = Track.create(code="TRK-A1", capacity=100)
    track.add_segment("alpha", Span(0, 10))
    track.add_segment("beta", Span(10, 25))
    await add(session_factory, track)

    track.remove_segment("alpha")
    track.add_segment("gamma", Span(0, 10))
    await save(session_factory, track)

    found = await get(session_factory, track)
    assert found is not None
    assert found.segments == (Segment("gamma", Span(0, 10)), Segment("beta", Span(10, 25)))
    assert await count(session_factory, "track_segments") == 2


@pytest.mark.asyncio
async def test_save_does_not_touch_other_tracks(session_factory: SessionFactory) -> None:
    track = Track.create(code="TRK-A1", capacity=100)
    other = Track.create(code="TRK-B2", capacity=50)
    other.add_segment("alpha", Span(0, 10))
    await add(session_factory, track)
    await add(session_factory, other)

    track.add_segment("alpha", Span(0, 10))
    await save(session_factory, track)

    found = await get(session_factory, other)
    assert found is not None
    assert track_state(found) == track_state(other)


@pytest.mark.asyncio
async def test_changes_that_are_not_saved_are_not_stored(session_factory: SessionFactory) -> None:
    track = Track.create(code="TRK-A1", capacity=100)
    await add(session_factory, track)

    handed_out = await get(session_factory, track)
    assert handed_out is not None
    handed_out.add_segment("beta", Span(10, 20))

    found = await get(session_factory, track)
    assert found is not None
    assert found.segments == ()


@pytest.mark.asyncio
async def test_the_repository_neither_commits_nor_rolls_back(
    session_factory: SessionFactory,
) -> None:
    track = Track.create(code="TRK-A1", capacity=100)

    async with session_factory() as session:
        await SqlAlchemyTrackRepository(session).add(track)
        assert session.in_transaction()
        assert await get(session_factory, track) is None
        await session.rollback()

    assert await get(session_factory, track) is None


@pytest.mark.asyncio
async def test_get_reads_the_rows_as_they_are_now(session_factory: SessionFactory) -> None:
    track = Track.create(code="TRK-A1", capacity=100)
    await add(session_factory, track)

    async with session_factory() as session:
        repository = SqlAlchemyTrackRepository(session)
        assert await repository.get(track.id) is not None
        await session.commit()
        async with session_factory() as other, other.begin():
            await other.execute(text("UPDATE tracks SET capacity = 500"))
        found = await repository.get(track.id)

    assert found is not None
    assert found.capacity == 500


@pytest.mark.asyncio
async def test_get_for_update_makes_a_second_caller_wait_for_the_first(
    session_factory: SessionFactory,
) -> None:
    track = Track.create(code="TRK-A1", capacity=100)
    await add(session_factory, track)
    events: list[str] = []

    async def second_caller() -> Track | None:
        async with session_factory() as session, session.begin():
            events.append("second asks")
            found = await SqlAlchemyTrackRepository(session).get_for_update(track.id)
            events.append("second gets")
            return found

    async with session_factory() as session, session.begin():
        repository = SqlAlchemyTrackRepository(session)
        mine = await repository.get_for_update(track.id)
        assert mine is not None
        waiting = asyncio.create_task(second_caller())
        await asyncio.sleep(0.5)
        mine.add_segment("alpha", Span(0, 10))
        await repository.save(mine)
        events.append("first saves")

    seen_by_second = await waiting
    assert events == ["second asks", "first saves", "second gets"]
    assert seen_by_second is not None
    assert seen_by_second.segments == (Segment("alpha", Span(0, 10)),)


@pytest.mark.asyncio
async def test_simultaneous_additions_of_one_code_store_exactly_one_track(
    session_factory: SessionFactory,
) -> None:
    async def try_add() -> bool:
        try:
            await add(session_factory, Track.create(code="TRK-A1", capacity=100))
        except TrackCodeTakenError:
            return False
        return True

    results = await asyncio.gather(*(try_add() for _ in range(10)))

    assert sorted(results) == [False] * 9 + [True]
    assert await count(session_factory, "tracks") == 1
