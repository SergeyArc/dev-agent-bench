import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import track_state
from tracksvc.application.errors import StorageUnavailableError
from tracksvc.domain.errors import TrackCodeTakenError
from tracksvc.domain.span import Span
from tracksvc.domain.track import Track
from tracksvc.infrastructure.database import create_engine, create_session_factory
from tracksvc.infrastructure.unit_of_work import SqlAlchemyUnitOfWork

type SessionFactory = async_sessionmaker[AsyncSession]


async def stored(session_factory: SessionFactory, track: Track) -> Track | None:
    async with session_factory() as session, SqlAlchemyUnitOfWork(session) as uow:
        return await uow.tracks.get(track.id)


@pytest.mark.asyncio
async def test_commit_makes_the_writes_visible_to_other_sessions(
    session_factory: SessionFactory,
) -> None:
    track = Track.create(code="TRK-A1", capacity=100)

    async with session_factory() as session, SqlAlchemyUnitOfWork(session) as uow:
        await uow.tracks.add(track)
        assert await stored(session_factory, track) is None
        await uow.commit()

    found = await stored(session_factory, track)
    assert found is not None
    assert track_state(found) == track_state(track)


@pytest.mark.asyncio
async def test_leaving_without_a_commit_rolls_everything_back(
    session_factory: SessionFactory,
) -> None:
    track = Track.create(code="TRK-A1", capacity=100)

    async with session_factory() as session:
        async with SqlAlchemyUnitOfWork(session) as uow:
            await uow.tracks.add(track)
        assert not session.in_transaction()

    assert await stored(session_factory, track) is None


async def change_then_fail(session: AsyncSession, track: Track) -> None:
    async with SqlAlchemyUnitOfWork(session) as uow:
        changed = await uow.tracks.get_for_update(track.id)
        assert changed is not None
        changed.add_segment("alpha", Span(0, 10))
        await uow.tracks.save(changed)
        await uow.tracks.add(Track.create(code=track.code, capacity=5))


@pytest.mark.asyncio
async def test_an_error_rolls_everything_back_and_is_passed_on(
    session_factory: SessionFactory,
) -> None:
    track = Track.create(code="TRK-A1", capacity=100)
    async with session_factory() as session, SqlAlchemyUnitOfWork(session) as uow:
        await uow.tracks.add(track)
        await uow.commit()

    async with session_factory() as session:
        with pytest.raises(TrackCodeTakenError):
            await change_then_fail(session, track)
        assert not session.in_transaction()

    found = await stored(session_factory, track)
    assert found is not None
    assert found.segments == ()


@pytest.mark.asyncio
async def test_a_session_serves_one_unit_of_work_after_another(
    session_factory: SessionFactory,
) -> None:
    first = Track.create(code="TRK-A1", capacity=100)
    second = Track.create(code="TRK-B2", capacity=100)

    async with session_factory() as session:
        uow = SqlAlchemyUnitOfWork(session)
        async with uow:
            await uow.tracks.add(first)
            await uow.commit()
        async with uow:
            await uow.tracks.add(second)

    assert await stored(session_factory, first) is not None
    assert await stored(session_factory, second) is None


@pytest.mark.asyncio
async def test_database_failures_are_reported_as_unavailable_storage(closed_port: int) -> None:
    engine = create_engine(f"postgresql+asyncpg://u:p@127.0.0.1:{closed_port}/none")
    try:
        async with create_session_factory(engine)() as session:
            with pytest.raises(StorageUnavailableError):
                async with SqlAlchemyUnitOfWork(session) as uow:
                    await uow.tracks.add(Track.create(code="TRK-A1", capacity=100))
    finally:
        await engine.dispose()
