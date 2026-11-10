import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tracksvc.application.ports import UnitOfWork
from tracksvc.domain.span import Span
from tracksvc.domain.track import Track
from tracksvc.infrastructure.database import create_engine, create_session_factory
from tracksvc.infrastructure.unit_of_work import SqlAlchemyUnitOfWork
from tracksvc.worker.app import unit_of_work_provider
from tracksvc.worker.handler import DeadLetter, Done, MessageHandler, Retry
from tracksvc.worker.messages import Delivery

type SessionFactory = async_sessionmaker[AsyncSession]


def delivery(
    track_id: UUID,
    delta: object = 5,
    message_id: str | None = "m-1",
    headers: dict[str, object] | None = None,
) -> Delivery:
    body = json.dumps({"track_id": str(track_id), "delta": delta}).encode()
    return Delivery(body, message_id, headers or {})


class BrokenUnitOfWorkScope:
    """Scope of a unit of work that cannot be provided."""

    async def __aenter__(self) -> UnitOfWork:
        raise RuntimeError("no storage")

    async def __aexit__(self, *exc_info: object) -> None:
        return None


@pytest.fixture
def handler(session_factory: SessionFactory) -> MessageHandler:
    return MessageHandler(unit_of_work_provider(session_factory), max_retries=3)


async def store(session_factory: SessionFactory, track: Track) -> Track:
    async with session_factory() as session, SqlAlchemyUnitOfWork(session) as uow:
        await uow.tracks.add(track)
        await uow.commit()
    return track


async def capacity(session_factory: SessionFactory, track: Track) -> int:
    async with session_factory() as session, SqlAlchemyUnitOfWork(session) as uow:
        found = await uow.tracks.get(track.id)
    assert found is not None
    return found.capacity


async def processed(session_factory: SessionFactory) -> list[str]:
    async with session_factory() as session:
        rows = await session.execute(text("SELECT message_id FROM processed_messages"))
        return sorted(rows.scalars())


@pytest.mark.asyncio
async def test_a_valid_message_extends_the_track_and_is_recorded(
    handler: MessageHandler, session_factory: SessionFactory
) -> None:
    track = await store(session_factory, Track.create("TRK-A1", 100))

    outcome = await handler.handle(delivery(track.id))

    assert outcome == Done()
    assert await capacity(session_factory, track) == 105
    assert await processed(session_factory) == ["m-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_count", [1, 2, 3, 4, 50])
async def test_a_valid_message_is_processed_whatever_its_retry_count(
    handler: MessageHandler, session_factory: SessionFactory, retry_count: int
) -> None:
    track = await store(session_factory, Track.create("TRK-A1", 100))

    outcome = await handler.handle(delivery(track.id, headers={"x-retry-count": retry_count}))

    assert outcome == Done()
    assert await capacity(session_factory, track) == 105


@pytest.mark.asyncio
async def test_a_processed_message_is_a_duplicate_whatever_it_carries(
    handler: MessageHandler, session_factory: SessionFactory
) -> None:
    track = await store(session_factory, Track.create("TRK-A1", 100))
    await handler.handle(delivery(track.id))

    outcomes = [
        await handler.handle(delivery(track.id)),
        await handler.handle(delivery(track.id, delta=7)),
        await handler.handle(delivery(uuid4())),
        await handler.handle(delivery(track.id, delta="5")),
        await handler.handle(delivery(track.id, headers={"x-retry-count": "x"})),
        await handler.handle(Delivery(b"not json", "m-1", {})),
    ]

    assert outcomes == [Done()] * 6
    assert await capacity(session_factory, track) == 105
    assert await processed(session_factory) == ["m-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("message_id", [None, "", "x" * 256, "line\n", "é"])
async def test_an_invalid_message_id_is_dead_lettered(
    handler: MessageHandler, session_factory: SessionFactory, message_id: str | None
) -> None:
    track = await store(session_factory, Track.create("TRK-A1", 100))

    outcome = await handler.handle(delivery(track.id, message_id=message_id))

    assert outcome == DeadLetter("invalid_message")
    assert await capacity(session_factory, track) == 100
    assert await processed(session_factory) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_count", ["1", -1, True, 1.5])
async def test_an_invalid_retry_count_is_dead_lettered(
    handler: MessageHandler, session_factory: SessionFactory, retry_count: object
) -> None:
    track = await store(session_factory, Track.create("TRK-A1", 100))

    outcome = await handler.handle(delivery(track.id, headers={"x-retry-count": retry_count}))

    assert outcome == DeadLetter("invalid_message")
    assert await capacity(session_factory, track) == 100
    assert await processed(session_factory) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not json", b"[1]", b'{"delta": 5}', b""])
async def test_an_invalid_body_is_dead_lettered_and_never_retried(
    handler: MessageHandler, session_factory: SessionFactory, body: bytes
) -> None:
    outcome = await handler.handle(Delivery(body, "m-1", {"x-retry-count": 0}))

    assert outcome == DeadLetter("invalid_message")
    assert await processed(session_factory) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("delta", ["5", 5.5, True, None, 0, 1001])
async def test_an_invalid_delta_is_dead_lettered(
    handler: MessageHandler, session_factory: SessionFactory, delta: object
) -> None:
    track = await store(session_factory, Track.create("TRK-A1", 100))

    outcome = await handler.handle(delivery(track.id, delta=delta))

    assert outcome == DeadLetter("invalid_message")
    assert await capacity(session_factory, track) == 100


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "next_count"), [({}, 1), ({"x-retry-count": 0}, 1), ({"x-retry-count": 2}, 3)]
)
async def test_a_failure_below_the_limit_is_retried_with_the_next_count(
    handler: MessageHandler,
    session_factory: SessionFactory,
    headers: dict[str, object],
    next_count: int,
) -> None:
    outcome = await handler.handle(delivery(uuid4(), headers=headers))

    assert outcome == Retry(next_count)
    assert await processed(session_factory) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_count", [3, 4, 10])
async def test_a_failure_at_the_limit_is_dead_lettered_with_its_count(
    handler: MessageHandler, session_factory: SessionFactory, retry_count: int
) -> None:
    outcome = await handler.handle(delivery(uuid4(), headers={"x-retry-count": retry_count}))

    assert outcome == DeadLetter("track_not_found", retry_count)
    assert await processed(session_factory) == []


@pytest.mark.asyncio
async def test_without_retries_the_first_failure_is_dead_lettered(
    session_factory: SessionFactory,
) -> None:
    handler = MessageHandler(unit_of_work_provider(session_factory), max_retries=0)

    assert await handler.handle(delivery(uuid4())) == DeadLetter("track_not_found", 0)


@pytest.mark.asyncio
async def test_a_sealed_track_fails_with_its_code_and_stays_unchanged(
    handler: MessageHandler, session_factory: SessionFactory
) -> None:
    sealed = Track.create("TRK-A1", 100)
    sealed.add_segment("alpha", Span(0, 10))
    sealed.seal()
    await store(session_factory, sealed)

    outcome = await handler.handle(delivery(sealed.id, headers={"x-retry-count": 3}))

    assert outcome == DeadLetter("track_sealed", 3)
    assert await capacity(session_factory, sealed) == 100
    assert await processed(session_factory) == []


@pytest.mark.asyncio
async def test_a_capacity_overflow_fails_with_its_code_and_changes_nothing(
    handler: MessageHandler, session_factory: SessionFactory
) -> None:
    track = await store(session_factory, Track.create("TRK-A1", 9500))

    outcome = await handler.handle(delivery(track.id, delta=501, headers={"x-retry-count": 3}))

    assert outcome == DeadLetter("capacity_limit_exceeded", 3)
    assert await capacity(session_factory, track) == 9500
    assert await processed(session_factory) == []


@pytest.mark.asyncio
async def test_a_message_that_failed_is_processed_as_new_later(
    handler: MessageHandler, session_factory: SessionFactory
) -> None:
    assert await handler.handle(delivery(uuid4())) == Retry(1)
    track = await store(session_factory, Track.create("TRK-A1", 100))

    outcome = await handler.handle(delivery(track.id))

    assert outcome == Done()
    assert await capacity(session_factory, track) == 105
    assert await processed(session_factory) == ["m-1"]


@pytest.mark.asyncio
async def test_an_unexpected_error_is_an_internal_error() -> None:
    handler = MessageHandler(BrokenUnitOfWorkScope, max_retries=1)

    assert await handler.handle(delivery(uuid4())) == Retry(1)
    assert await handler.handle(delivery(uuid4(), headers={"x-retry-count": 1})) == DeadLetter(
        "internal_error", 1
    )


@pytest.mark.asyncio
async def test_an_unreachable_database_is_an_internal_error(closed_port: int) -> None:
    engine = create_engine(f"postgresql+asyncpg://u:p@127.0.0.1:{closed_port}/none")
    handler = MessageHandler(unit_of_work_provider(create_session_factory(engine)), max_retries=0)
    try:
        assert await handler.handle(delivery(uuid4())) == DeadLetter("internal_error", 0)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_every_delivery_releases_its_session(
    handler: MessageHandler, session_factory: SessionFactory
) -> None:
    track = await store(session_factory, Track.create("TRK-A1", 100))

    # More deliveries of every outcome than the pool has connections.
    for index in range(20):
        assert await handler.handle(delivery(track.id, message_id=f"ok-{index}")) == Done()
        assert await handler.handle(delivery(uuid4(), message_id=f"fail-{index}")) == Retry(1)
        assert await handler.handle(Delivery(b"x", f"bad-{index}", {})) == DeadLetter(
            "invalid_message"
        )

    assert await capacity(session_factory, track) == 200
