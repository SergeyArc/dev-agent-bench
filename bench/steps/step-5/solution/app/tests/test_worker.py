"""The worker against the real broker and database.

The tests own the contract's exchange and queues on the broker of ``RABBITMQ_URL``: they
purge the queues before and after themselves. No other worker may consume them meanwhile.
"""

import asyncio
import json
import os
import signal
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import aio_pika
import pytest
import pytest_asyncio
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractIncomingMessage, AbstractQueue
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.conftest import PROJECT_ROOT
from tracksvc.domain.span import Span
from tracksvc.domain.track import Track
from tracksvc.infrastructure.unit_of_work import SqlAlchemyUnitOfWork
from tracksvc.worker.app import unit_of_work_provider
from tracksvc.worker.consumer import DEAD_QUEUE, EXCHANGE, ROUTING_KEY, WORK_QUEUE, Consumer
from tracksvc.worker.handler import MessageHandler

type SessionFactory = async_sessionmaker[AsyncSession]
type StartWorker = Callable[..., Awaitable[None]]

OUTCOME_TIMEOUT_SECONDS = 10.0


async def eventually(condition: Callable[[], Awaitable[bool]], within: float = 10.0) -> None:
    """Wait until ``condition`` holds; fail if it does not within ``within`` seconds."""
    deadline = time.monotonic() + within
    while not await condition():
        assert time.monotonic() < deadline, "condition was not met in time"
        await asyncio.sleep(0.05)


@dataclass
class Broker:
    """The contract's topology plus a tap queue that receives every publish to the exchange."""

    channel: AbstractChannel
    exchange: AbstractExchange
    work: AbstractQueue
    dead: AbstractQueue
    tap: AbstractQueue

    async def publish(
        self,
        body: bytes,
        message_id: str | None = "m-1",
        headers: dict[str, object] | None = None,
    ) -> None:
        message = aio_pika.Message(body, message_id=message_id, headers=dict(headers or {}))
        await self.exchange.publish(message, ROUTING_KEY)

    async def publish_extension(
        self,
        track_id: UUID,
        delta: int = 5,
        message_id: str = "m-1",
        headers: dict[str, object] | None = None,
    ) -> bytes:
        body = json.dumps({"track_id": str(track_id), "delta": delta}).encode()
        await self.publish(body, message_id, headers)
        return body

    async def consumers(self) -> int:
        queue = await self.channel.declare_queue(WORK_QUEUE, durable=True, passive=True)
        return queue.declaration_result.consumer_count or 0

    async def ready_in_work_queue(self) -> int:
        queue = await self.channel.declare_queue(WORK_QUEUE, durable=True, passive=True)
        return queue.declaration_result.message_count or 0

    async def take(self, queue: AbstractQueue, count: int) -> list[AbstractIncomingMessage]:
        """Take exactly ``count`` messages from ``queue``, waiting for them to arrive."""
        messages: list[AbstractIncomingMessage] = []
        deadline = time.monotonic() + OUTCOME_TIMEOUT_SECONDS
        while len(messages) < count:
            message = await queue.get(no_ack=True, fail=False)
            if message is None:
                assert time.monotonic() < deadline, f"got {len(messages)} of {count} messages"
                await asyncio.sleep(0.05)
            else:
                messages.append(message)
        return messages

    async def assert_no_more(self, *queues: AbstractQueue) -> None:
        """Check that nothing else arrives in ``queues`` once the work queue is idle."""
        await asyncio.sleep(0.5)
        for queue in queues:
            assert await queue.get(no_ack=True, fail=False) is None


@pytest_asyncio.fixture
async def broker() -> AsyncIterator[Broker]:
    connection = await aio_pika.connect(os.environ["RABBITMQ_URL"])
    try:
        channel = await connection.channel()
        exchange = await channel.declare_exchange(
            EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True
        )
        work = await channel.declare_queue(WORK_QUEUE, durable=True)
        dead = await channel.declare_queue(DEAD_QUEUE, durable=True)
        await work.bind(exchange, ROUTING_KEY)
        await work.purge()
        await dead.purge()
        tap = await channel.declare_queue(exclusive=True)
        await tap.bind(exchange, ROUTING_KEY)
        yield Broker(channel, exchange, work, dead, tap)
        await work.purge()
        await dead.purge()
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def start_worker(
    broker: Broker, session_factory: SessionFactory
) -> AsyncIterator[StartWorker]:
    """Function that starts a worker in this process; the workers are stopped afterwards."""
    stop = asyncio.Event()
    workers: list[asyncio.Task[None]] = []

    async def start(max_retries: int = 3, url: str | None = None) -> None:
        expected = await broker.consumers() + 1
        handler = MessageHandler(unit_of_work_provider(session_factory), max_retries)
        consumer = Consumer(url or os.environ["RABBITMQ_URL"], handler)
        workers.append(asyncio.create_task(consumer.run(stop)))

        async def consuming() -> bool:
            return await broker.consumers() >= expected

        await eventually(consuming)

    yield start
    stop.set()
    await asyncio.wait_for(asyncio.gather(*workers), timeout=10)


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


def retry_counts(messages: list[AbstractIncomingMessage]) -> list[object]:
    return [message.headers.get("x-retry-count") for message in messages]


@pytest.mark.asyncio
async def test_the_worker_consumes_the_declared_topology(
    broker: Broker, start_worker: StartWorker
) -> None:
    await start_worker()

    assert await broker.consumers() == 1
    # Redeclaring with the contract's properties fails if the worker declared others.
    await broker.channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
    await broker.channel.declare_queue(WORK_QUEUE, durable=True)
    await broker.channel.declare_queue(DEAD_QUEUE, durable=True)


@pytest.mark.asyncio
async def test_a_valid_message_extends_the_track_once_and_is_acknowledged(
    broker: Broker, start_worker: StartWorker, session_factory: SessionFactory
) -> None:
    track = await store(session_factory, Track.create("TRK-A1", 100))
    await start_worker()

    await broker.publish_extension(track.id, delta=5)

    async def extended() -> bool:
        return await capacity(session_factory, track) == 105

    await eventually(extended)
    assert await processed(session_factory) == ["m-1"]
    assert len(await broker.take(broker.tap, 1)) == 1
    await broker.assert_no_more(broker.tap, broker.dead)


@pytest.mark.asyncio
async def test_a_duplicate_changes_nothing_and_publishes_nothing(
    broker: Broker, start_worker: StartWorker, session_factory: SessionFactory
) -> None:
    track = await store(session_factory, Track.create("TRK-A1", 100))
    await start_worker()
    await broker.publish_extension(track.id, delta=5)

    async def first_applied() -> bool:
        return await processed(session_factory) == ["m-1"]

    await eventually(first_applied)
    await broker.publish_extension(track.id, delta=5)
    await broker.publish_extension(track.id, delta=7)
    await broker.publish(b"not json")
    await broker.publish_extension(uuid4())
    await broker.publish_extension(track.id, message_id="m-2", delta=1)

    async def last_applied() -> bool:
        return await processed(session_factory) == ["m-1", "m-2"]

    await eventually(last_applied)
    assert await capacity(session_factory, track) == 106
    assert len(await broker.take(broker.tap, 6)) == 6
    await broker.assert_no_more(broker.tap, broker.dead)


@pytest.mark.asyncio
async def test_a_failing_message_is_retried_max_times_and_then_dead_lettered(
    broker: Broker, start_worker: StartWorker, session_factory: SessionFactory
) -> None:
    await start_worker()

    body = await broker.publish_extension(uuid4(), headers={"trace": "t-1"})

    published = await broker.take(broker.tap, 4)
    assert retry_counts(published) == [None, 1, 2, 3]
    (dead,) = await broker.take(broker.dead, 1)
    for copy in [*published[1:], dead]:
        assert copy.body == body
        assert copy.message_id == "m-1"
        assert copy.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
        assert copy.headers["trace"] == "t-1"
    assert dead.headers["x-retry-count"] == 3
    assert dead.headers["x-failure"] == "track_not_found"
    assert "x-failure" not in published[-1].headers
    await broker.assert_no_more(broker.tap, broker.dead)
    assert await processed(session_factory) == []


@pytest.mark.asyncio
async def test_a_failing_message_at_the_limit_goes_straight_to_the_dead_queue(
    broker: Broker, start_worker: StartWorker
) -> None:
    await start_worker()

    await broker.publish_extension(uuid4(), headers={"x-retry-count": 3})

    (dead,) = await broker.take(broker.dead, 1)
    assert dead.headers["x-retry-count"] == 3
    assert dead.headers["x-failure"] == "track_not_found"
    assert retry_counts(await broker.take(broker.tap, 1)) == [3]
    await broker.assert_no_more(broker.tap, broker.dead)


@pytest.mark.asyncio
@pytest.mark.parametrize(("max_retries", "republished"), [(0, []), (1, [1])])
async def test_the_retry_budget_is_configurable(
    broker: Broker, start_worker: StartWorker, max_retries: int, republished: list[int]
) -> None:
    await start_worker(max_retries=max_retries)

    await broker.publish_extension(uuid4())

    (dead,) = await broker.take(broker.dead, 1)
    assert dead.headers["x-retry-count"] == max_retries
    published = await broker.take(broker.tap, 1 + len(republished))
    assert retry_counts(published) == [None, *republished]
    await broker.assert_no_more(broker.tap, broker.dead)


@pytest.mark.asyncio
async def test_sealed_tracks_and_overflows_are_dead_lettered_with_their_codes(
    broker: Broker, start_worker: StartWorker, session_factory: SessionFactory
) -> None:
    sealed = Track.create("TRK-A1", 100)
    sealed.add_segment("alpha", Span(0, 10))
    sealed.seal()
    await store(session_factory, sealed)
    full = await store(session_factory, Track.create("TRK-B2", 9500))
    await start_worker(max_retries=0)

    await broker.publish_extension(sealed.id, message_id="sealed")
    await broker.publish_extension(full.id, delta=501, message_id="overflow")

    dead = await broker.take(broker.dead, 2)
    assert {message.message_id: message.headers["x-failure"] for message in dead} == {
        "sealed": "track_sealed",
        "overflow": "capacity_limit_exceeded",
    }
    assert await capacity(session_factory, sealed) == 100
    assert await capacity(session_factory, full) == 9500
    assert await processed(session_factory) == []


@pytest.mark.asyncio
async def test_a_dead_lettered_message_id_is_processed_when_published_again(
    broker: Broker, start_worker: StartWorker, session_factory: SessionFactory
) -> None:
    track = await store(session_factory, Track.create("TRK-A1", 100))
    await start_worker(max_retries=0)
    await broker.publish_extension(uuid4())
    await broker.take(broker.dead, 1)

    await broker.publish_extension(track.id, headers={"x-retry-count": 2})

    async def extended() -> bool:
        return await capacity(session_factory, track) == 105

    await eventually(extended)
    assert await processed(session_factory) == ["m-1"]
    await broker.assert_no_more(broker.dead)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_id", "headers", "body"),
    [
        ("line\nbreak", {}, None),
        ("m-1", {"x-retry-count": "1"}, None),
        ("m-1", {"x-retry-count": -1}, None),
        ("m-1", {"x-retry-count": True}, None),
        ("m-1", {}, b"not json"),
        ("m-1", {}, b'{"track_id": "not-a-uuid", "delta": 5}'),
        ("m-1", {}, b"[]"),
    ],
)
async def test_an_invalid_message_is_dead_lettered_as_it_came(
    broker: Broker,
    start_worker: StartWorker,
    session_factory: SessionFactory,
    message_id: str,
    headers: dict[str, object],
    body: bytes | None,
) -> None:
    # A message without a message id cannot be published from here: the client library
    # generates one. That case is covered by the tests of the handler.
    track = await store(session_factory, Track.create("TRK-A1", 100))
    sent = body or json.dumps({"track_id": str(track.id), "delta": 5}).encode()
    await start_worker()

    await broker.publish(sent, message_id, {**headers, "trace": "t-1"})

    (dead,) = await broker.take(broker.dead, 1)
    assert dead.body == sent
    assert dead.message_id == message_id
    assert dead.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert dead.headers == {**headers, "trace": "t-1", "x-failure": "invalid_message"}
    assert len(await broker.take(broker.tap, 1)) == 1
    await broker.assert_no_more(broker.tap, broker.dead)
    assert await capacity(session_factory, track) == 100
    assert await processed(session_factory) == []


@pytest.mark.asyncio
async def test_many_copies_of_one_message_for_two_workers_extend_the_track_once(
    broker: Broker, start_worker: StartWorker, session_factory: SessionFactory
) -> None:
    track = await store(session_factory, Track.create("TRK-A1", 100))
    await start_worker()
    await start_worker()

    await asyncio.gather(*(broker.publish_extension(track.id) for _ in range(20)))
    await broker.publish_extension(track.id, delta=1, message_id="last")

    async def all_handled() -> bool:
        return await processed(session_factory) == ["last", "m-1"]

    await eventually(all_handled)
    await broker.assert_no_more(broker.dead)
    assert await capacity(session_factory, track) == 106


@pytest.mark.asyncio
async def test_stopping_the_worker_leaves_nothing_in_the_work_queue(
    broker: Broker, session_factory: SessionFactory
) -> None:
    track = await store(session_factory, Track.create("TRK-A1", 100))
    stop = asyncio.Event()
    handler = MessageHandler(unit_of_work_provider(session_factory), max_retries=3)
    worker = asyncio.create_task(Consumer(os.environ["RABBITMQ_URL"], handler).run(stop))

    async def consuming() -> bool:
        return await broker.consumers() == 1

    await eventually(consuming)
    for index in range(30):
        await broker.publish_extension(track.id, delta=1, message_id=f"m-{index}")
    await broker.publish_extension(uuid4(), message_id="failing")
    await broker.take(broker.dead, 1)

    async def all_applied() -> bool:
        return await capacity(session_factory, track) == 130

    await eventually(all_applied)
    started = time.monotonic()
    stop.set()
    await asyncio.wait_for(worker, timeout=10)

    assert time.monotonic() - started < 10
    assert await broker.consumers() == 0
    # Unacknowledged deliveries would have returned to the queue when the worker left.
    assert await broker.ready_in_work_queue() == 0


class TcpProxy:
    """TCP forwarder in front of the broker that can drop its connections and come back."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self._target = (target_host, target_port)
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        for writer in list(self._writers):
            writer.close()
        await self._server.wait_closed()
        self._server = None

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(*self._target)
        except OSError:
            writer.close()
            return
        self._writers.update({writer, upstream_writer})
        try:
            await asyncio.gather(
                self._pipe(reader, upstream_writer), self._pipe(upstream_reader, writer)
            )
        finally:
            self._writers.difference_update({writer, upstream_writer})

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with suppress(OSError):
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        writer.close()


@pytest_asyncio.fixture
async def proxy() -> AsyncIterator[tuple[TcpProxy, str]]:
    """Proxy in front of the broker and the broker URL that goes through it."""
    url = urlsplit(os.environ["RABBITMQ_URL"])
    assert url.hostname is not None
    tcp_proxy = TcpProxy(url.hostname, url.port or 5672)
    await tcp_proxy.start()
    credentials = url.netloc.rpartition("@")[0]
    proxied = url._replace(netloc=f"{credentials}@127.0.0.1:{tcp_proxy.port}")
    yield tcp_proxy, urlunsplit(proxied)
    with suppress(AssertionError):
        await tcp_proxy.stop()


@pytest.mark.asyncio
async def test_the_worker_consumes_again_after_the_connection_was_lost(
    broker: Broker,
    start_worker: StartWorker,
    session_factory: SessionFactory,
    proxy: tuple[TcpProxy, str],
) -> None:
    tcp_proxy, proxied_url = proxy
    track = await store(session_factory, Track.create("TRK-A1", 100))
    await start_worker(url=proxied_url)

    for index in range(50):
        await broker.publish_extension(track.id, delta=1, message_id=f"m-{index}")
    await tcp_proxy.stop()

    async def nobody_consumes() -> bool:
        return await broker.consumers() == 0

    await eventually(nobody_consumes)
    await asyncio.sleep(3)
    assert await broker.consumers() == 0
    await tcp_proxy.start()

    async def consuming() -> bool:
        return await broker.consumers() == 1

    await eventually(consuming, within=30)
    await broker.publish_extension(track.id, delta=7, message_id="after")

    async def all_applied() -> bool:
        return len(await processed(session_factory)) == 51

    await eventually(all_applied)
    assert await capacity(session_factory, track) == 157
    assert await broker.consumers() == 1
    await broker.assert_no_more(broker.dead)


@pytest.mark.asyncio
@pytest.mark.usefixtures("database")
async def test_the_worker_process_starts_with_its_command_and_exits_on_sigterm(
    broker: Broker,
) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "tracksvc.worker", cwd=PROJECT_ROOT
    )
    try:

        async def consuming() -> bool:
            return await broker.consumers() == 1

        await eventually(consuming, within=30)
        assert process.returncode is None

        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(process.wait(), timeout=10)

        assert time.monotonic() - started < 10
        assert await broker.consumers() == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
