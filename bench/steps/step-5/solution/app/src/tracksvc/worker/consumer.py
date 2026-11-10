"""The AMQP side of the worker: connection, topology, consuming, copies and acknowledgements."""

import asyncio
import logging
from collections.abc import Awaitable

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractQueue,
    HeadersType,
)
from aio_pika.exceptions import AMQPError

from tracksvc.worker.handler import DeadLetter, MessageHandler, Outcome, Retry
from tracksvc.worker.messages import RETRY_COUNT_HEADER, Delivery

logger = logging.getLogger(__name__)

EXCHANGE = "tracksvc.events"
ROUTING_KEY = "track.extend"
WORK_QUEUE = "tracksvc.track-events"
DEAD_QUEUE = "tracksvc.track-events.dead"
FAILURE_HEADER = "x-failure"

PREFETCH_COUNT = 10
CONNECT_TIMEOUT_SECONDS = 5.0
RECONNECT_DELAY_SECONDS = 1.0
# Together well below the 10 seconds the worker has to exit after SIGTERM.
DRAIN_TIMEOUT_SECONDS = 4.0
CLOSE_TIMEOUT_SECONDS = 2.0

# What a failing broker connection surfaces as. Timeouts are OSError (TimeoutError); the
# use of a channel that has just been closed is reported as RuntimeError.
_BROKER_FAILURES = (AMQPError, OSError, RuntimeError)


async def _wait_first(*awaitables: Awaitable[object]) -> None:
    """Wait until the first of ``awaitables`` finishes and cancel the others."""
    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class Consumer:
    """Consume the work queue and end every delivery the way the handler decides.

    The consumer owns the broker connection and its channel: it opens them, replaces
    them after a connection loss and closes them when it stops. A delivery is
    acknowledged once, after the handler has finished and the copies its outcome asks
    for are confirmed by the broker. If that cannot be done, the delivery stays
    unacknowledged and the connection is replaced, so the broker delivers it again.
    """

    def __init__(self, rabbitmq_url: str, handler: MessageHandler) -> None:
        self._rabbitmq_url = rabbitmq_url
        self._handler = handler

    async def run(self, stop: asyncio.Event) -> None:
        """Consume until ``stop`` is set, reconnecting whenever the connection is lost."""
        while not stop.is_set():
            try:
                await self._consume_on_one_connection(stop)
            except _BROKER_FAILURES as error:
                logger.warning("Broker connection failed: %r", error)
            if not stop.is_set():
                logger.info("Reconnecting to the broker in %s s", RECONNECT_DELAY_SECONDS)
                await _wait_first(stop.wait(), asyncio.sleep(RECONNECT_DELAY_SECONDS))

    async def _consume_on_one_connection(self, stop: asyncio.Event) -> None:
        connection = await aio_pika.connect(self._rabbitmq_url, timeout=CONNECT_TIMEOUT_SECONDS)
        in_flight: set[asyncio.Task[object]] = set()
        try:
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=PREFETCH_COUNT)
            exchange, queue = await _declare_topology(channel)
            # Set when the connection is lost or a delivery could not be completed on it.
            broken = asyncio.Event()

            def on_connection_close(_sender: object, _error: BaseException | None) -> None:
                broken.set()

            connection.close_callbacks.add(on_connection_close)

            async def on_message(message: AbstractIncomingMessage) -> None:
                task = asyncio.current_task()
                if task is not None:
                    in_flight.add(task)
                    task.add_done_callback(in_flight.discard)
                try:
                    await self._process(message, exchange, channel.default_exchange)
                except Exception:
                    logger.exception("Delivery %r could not be completed", message.message_id)
                    broken.set()

            consumer_tag = await queue.consume(on_message)
            logger.info("Consuming %s", WORK_QUEUE)
            await _wait_first(stop.wait(), broken.wait())
            if not broken.is_set():
                await queue.cancel(consumer_tag)
        finally:
            await _finish(in_flight)
            await _close(connection)

    async def _process(
        self,
        message: AbstractIncomingMessage,
        exchange: AbstractExchange,
        default_exchange: AbstractExchange,
    ) -> None:
        delivery = Delivery(message.body, message.message_id, message.headers)
        outcome = await self._handler.handle(delivery)
        if isinstance(outcome, Retry):
            await exchange.publish(_copy(message, outcome), ROUTING_KEY)
        elif isinstance(outcome, DeadLetter):
            await default_exchange.publish(_copy(message, outcome), DEAD_QUEUE)
        await message.ack()


async def _declare_topology(channel: AbstractChannel) -> tuple[AbstractExchange, AbstractQueue]:
    exchange = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
    queue = await channel.declare_queue(WORK_QUEUE, durable=True)
    await channel.declare_queue(DEAD_QUEUE, durable=True)
    await queue.bind(exchange, ROUTING_KEY)
    return exchange, queue


def _copy(message: AbstractIncomingMessage, outcome: Outcome) -> aio_pika.Message:
    """Build the persistent copy of ``message`` that ``outcome`` asks for."""
    headers: HeadersType = dict(message.headers)
    if isinstance(outcome, Retry):
        headers[RETRY_COUNT_HEADER] = outcome.retry_count
    elif isinstance(outcome, DeadLetter):
        headers[FAILURE_HEADER] = outcome.failure
        if outcome.retry_count is not None:
            headers[RETRY_COUNT_HEADER] = outcome.retry_count
    return aio_pika.Message(
        message.body,
        headers=headers,
        message_id=message.message_id,
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        content_type=message.content_type,
        content_encoding=message.content_encoding,
        correlation_id=message.correlation_id,
        type=message.type,
        app_id=message.app_id,
        timestamp=message.timestamp,
    )


async def _finish(in_flight: set[asyncio.Task[object]]) -> None:
    """Let the deliveries being processed finish; cancel those that take too long."""
    if not in_flight:
        return
    _, pending = await asyncio.wait(in_flight, timeout=DRAIN_TIMEOUT_SECONDS)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


async def _close(connection: aio_pika.abc.AbstractConnection) -> None:
    """Close the connection and with it the channel and the consumer."""
    try:
        await asyncio.wait_for(connection.close(), CLOSE_TIMEOUT_SECONDS)
    except _BROKER_FAILURES as error:
        logger.warning("Closing the broker connection failed: %r", error)
