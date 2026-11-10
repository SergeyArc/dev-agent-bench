import asyncio
import json
import logging
import os
import signal
import uuid
from typing import Any

import aio_pika
from aio_pika.abc import AbstractExchange, AbstractIncomingMessage
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tracksvc.domain.model import DomainError
from tracksvc.infrastructure import store

EXCHANGE = "tracksvc.events"
WORK_QUEUE = "tracksvc.track-events"
DEAD_QUEUE = "tracksvc.track-events.dead"
ROUTING_KEY = "track.extend"
INVALID = "invalid_message"
MAX_ID_LENGTH = 255
MAX_DELTA = 1000

logger = logging.getLogger(__name__)


def _valid_id(message_id: str | None) -> bool:
    if not isinstance(message_id, str) or not 1 <= len(message_id) <= MAX_ID_LENGTH:
        return False
    return all(" " <= char <= "~" for char in message_id)


def _retries(message: AbstractIncomingMessage) -> int | None:
    value = message.headers.get("x-retry-count", 0)
    if type(value) is not int or value < 0:
        return None
    return value


def _command(body: bytes) -> tuple[uuid.UUID, int] | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    raw, delta = data.get("track_id"), data.get("delta")
    if not isinstance(raw, str) or type(delta) is not int or not 1 <= delta <= MAX_DELTA:
        return None
    try:
        return uuid.UUID(raw), delta
    except ValueError:
        return None


class Consumer:
    """Handles the deliveries of the work queue."""

    def __init__(
        self,
        engine: AsyncEngine,
        events: AbstractExchange,
        direct: AbstractExchange,
        max_retries: int,
    ) -> None:
        self._engine = engine
        self._events = events
        self._direct = direct
        self._max_retries = max_retries

    async def on_message(self, message: AbstractIncomingMessage) -> None:
        """Handle one delivery; after a lost connection the broker redelivers it."""
        try:
            await self._handle(message)
        except Exception:
            logger.exception("delivery left unacknowledged")

    async def _handle(self, message: AbstractIncomingMessage) -> None:
        message_id = message.message_id
        if message_id is None or not _valid_id(message_id):
            failure: str | None = INVALID
        else:
            failure = await self._process(message_id, message)
        retries = _retries(message) or 0
        if failure == INVALID:
            await self._copy(message, self._direct, DEAD_QUEUE, {"x-failure": INVALID})
        elif failure is not None and retries < self._max_retries:
            await self._copy(message, self._events, ROUTING_KEY, {"x-retry-count": retries + 1})
        elif failure is not None:
            headers = {"x-retry-count": retries, "x-failure": failure}
            await self._copy(message, self._direct, DEAD_QUEUE, headers)
        await message.ack()

    async def _process(self, message_id: str, message: AbstractIncomingMessage) -> str | None:
        try:
            async with self._engine.begin() as connection:
                if not await store.record_message(connection, message_id):
                    return None
                command = None if _retries(message) is None else _command(message.body)
                if command is None:
                    raise DomainError(INVALID)
                track = await store.load_track(connection, command[0], lock=True)
                track.extend(command[1])
                await store.save_track(connection, track)
        except DomainError as exc:
            return exc.code
        except SQLAlchemyError:
            return "internal_error"
        return None

    async def _copy(
        self,
        message: AbstractIncomingMessage,
        exchange: AbstractExchange,
        routing_key: str,
        headers: dict[str, Any],
    ) -> None:
        copy = aio_pika.Message(
            message.body,
            headers={**message.headers, **headers},
            message_id=message.message_id,
            content_type=message.content_type,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        await exchange.publish(copy, routing_key=routing_key)


async def run() -> None:
    """Consume the work queue until SIGTERM."""
    max_retries = int(os.environ.get("CONSUMER_MAX_RETRIES", "3"))
    engine = create_async_engine(os.environ["DATABASE_URL"])
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    connection = await aio_pika.connect_robust(os.environ["RABBITMQ_URL"])
    try:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=20)
        events = await channel.declare_exchange(
            EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True
        )
        await channel.declare_queue(DEAD_QUEUE, durable=True)
        queue = await channel.declare_queue(WORK_QUEUE, durable=True)
        await queue.bind(events, ROUTING_KEY)
        consumer = Consumer(engine, events, channel.default_exchange, max_retries)
        await queue.consume(consumer.on_message)
        await stop.wait()
    finally:
        await connection.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
