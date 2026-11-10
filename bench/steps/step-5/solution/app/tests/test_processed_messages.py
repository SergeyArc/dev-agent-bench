import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracksvc.application.tracks import ExtendTrack
from tracksvc.domain.track import Track
from tracksvc.infrastructure.repositories import SqlAlchemyProcessedMessages
from tracksvc.infrastructure.unit_of_work import SqlAlchemyUnitOfWork

type SessionFactory = async_sessionmaker[AsyncSession]


async def add(session_factory: SessionFactory, message_id: str) -> bool:
    async with session_factory() as session, session.begin():
        return await SqlAlchemyProcessedMessages(session).add(message_id)


async def contains(session_factory: SessionFactory, message_id: str) -> bool:
    async with session_factory() as session:
        return await SqlAlchemyProcessedMessages(session).contains(message_id)


async def count(session_factory: SessionFactory) -> int:
    async with session_factory() as session:
        statement = text("SELECT count(*) FROM processed_messages")
        rows: int = (await session.execute(statement)).scalar_one()
    return rows


@pytest.mark.asyncio
async def test_an_added_message_is_contained(session_factory: SessionFactory) -> None:
    assert await contains(session_factory, "m-1") is False

    assert await add(session_factory, "m-1") is True

    assert await contains(session_factory, "m-1") is True
    assert await contains(session_factory, "m-2") is False


@pytest.mark.asyncio
async def test_a_message_is_recorded_only_once(session_factory: SessionFactory) -> None:
    await add(session_factory, "m-1")

    assert await add(session_factory, "m-1") is False
    assert await add(session_factory, "M-1") is True
    assert await count(session_factory) == 2


@pytest.mark.asyncio
async def test_the_longest_message_id_is_stored(session_factory: SessionFactory) -> None:
    message_id = "~" * 255

    assert await add(session_factory, message_id) is True
    assert await contains(session_factory, message_id) is True


@pytest.mark.asyncio
async def test_a_record_that_is_rolled_back_is_not_kept(session_factory: SessionFactory) -> None:
    async with session_factory() as session:
        assert await SqlAlchemyProcessedMessages(session).add("m-1") is True
        await session.rollback()

    assert await contains(session_factory, "m-1") is False
    assert await add(session_factory, "m-1") is True


@pytest.mark.asyncio
async def test_simultaneous_records_of_one_message_succeed_exactly_once(
    session_factory: SessionFactory,
) -> None:
    results = await asyncio.gather(*(add(session_factory, "m-1") for _ in range(10)))

    assert sorted(results) == [False] * 9 + [True]
    assert await count(session_factory) == 1


@pytest.mark.asyncio
async def test_simultaneous_extensions_by_one_message_change_the_capacity_once(
    session_factory: SessionFactory,
) -> None:
    track = Track.create(code="TRK-A1", capacity=100)
    async with session_factory() as session, SqlAlchemyUnitOfWork(session) as uow:
        await uow.tracks.add(track)
        await uow.commit()

    async def extend() -> bool:
        async with session_factory() as session:
            return await ExtendTrack(SqlAlchemyUnitOfWork(session)).execute("m-1", track.id, 5)

    results = await asyncio.gather(*(extend() for _ in range(10)))

    assert sorted(results) == [False] * 9 + [True]
    async with session_factory() as session, SqlAlchemyUnitOfWork(session) as uow:
        found = await uow.tracks.get(track.id)
    assert found is not None
    assert found.capacity == 105
    assert await count(session_factory) == 1


@pytest.mark.asyncio
async def test_the_extension_and_the_record_are_committed_together(
    session_factory: SessionFactory,
) -> None:
    track = Track.create(code="TRK-A1", capacity=100)
    async with session_factory() as session, SqlAlchemyUnitOfWork(session) as uow:
        await uow.tracks.add(track)
        await uow.commit()

    async with session_factory() as session, SqlAlchemyUnitOfWork(session) as uow:
        assert await uow.processed_messages.add("m-1") is True
        changed = await uow.tracks.get_for_update(track.id)
        assert changed is not None
        changed.extend(5)
        await uow.tracks.save(changed)
        assert await contains(session_factory, "m-1") is False

    assert await contains(session_factory, "m-1") is False
    async with session_factory() as session, SqlAlchemyUnitOfWork(session) as uow:
        found = await uow.tracks.get(track.id)
    assert found is not None
    assert found.capacity == 100
