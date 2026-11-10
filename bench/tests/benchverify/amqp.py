"""AMQP helpers (contract §7): publish with properties and headers, tap queue
bound to the exchange, dead queue reader, queue statistics, topology control.

Synchronous (pika): checks run sequentially and need no event loop.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Self

import pika  # type: ignore[import-untyped]
import pika.exceptions  # type: ignore[import-untyped]

from pika.adapters.blocking_connection import BlockingChannel  # type: ignore[import-untyped]  # isort: skip

from . import config

TAP_PREFIX = "verifier.tap."


@dataclass
class Message:
    body: bytes
    message_id: str | None
    headers: dict[str, Any] = field(default_factory=dict)
    delivery_mode: int | None = None
    routing_key: str = ""
    exchange: str = ""

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class QueueStats:
    exists: bool
    ready: int = 0
    unacked: int = 0
    consumers: int = 0


def connection(port: int = config.AMQP_PORT) -> pika.BlockingConnection:
    return pika.BlockingConnection(
        pika.ConnectionParameters(
            host=config.AMQP_HOST,
            port=port,
            virtual_host=config.AMQP_VHOST,
            credentials=pika.PlainCredentials(config.AMQP_USER, config.AMQP_PASS),
            heartbeat=0,
            socket_timeout=5,
            blocked_connection_timeout=10,
            connection_attempts=1,
        )
    )


@contextmanager
def channel(port: int = config.AMQP_PORT) -> Iterator[BlockingChannel]:
    conn = connection(port)
    try:
        yield conn.channel()
    finally:
        if conn.is_open:
            conn.close()


def declare_topology() -> None:
    """Pre-declare the topology of contract §7.1 exactly (criterion 5.2)."""
    with channel() as ch:
        ch.exchange_declare(config.EXCHANGE, exchange_type="direct", durable=True, auto_delete=False)
        ch.queue_declare(config.WORK_QUEUE, durable=True, exclusive=False, auto_delete=False)
        ch.queue_bind(config.WORK_QUEUE, config.EXCHANGE, routing_key=config.ROUTING_KEY)
        ch.queue_declare(config.DEAD_QUEUE, durable=True, exclusive=False, auto_delete=False)


def delete_topology() -> None:
    """Delete the contract's queues and exchange and any verifier tap (reset, §2.2)."""
    names = [config.WORK_QUEUE, config.DEAD_QUEUE]
    names += [q for q in all_queues() if q.startswith(TAP_PREFIX)]
    with channel() as ch:
        for name in names:
            ch.queue_delete(name)
        ch.exchange_delete(config.EXCHANGE)


def publish(
    body: bytes | str | dict[str, Any],
    *,
    message_id: str | None = None,
    headers: dict[str, Any] | None = None,
    exchange: str = config.EXCHANGE,
    routing_key: str = config.ROUTING_KEY,
    properties: dict[str, Any] | None = None,
    port: int = config.AMQP_PORT,
) -> None:
    """Publish one message with publisher confirms.

    `body`: bytes are sent as is; str is UTF-8 encoded; dict is JSON-encoded.
    `properties`: extra BasicProperties fields (override the defaults).
    """
    if isinstance(body, dict):
        body = json.dumps(body)
    if isinstance(body, str):
        body = body.encode()
    props: dict[str, Any] = {"delivery_mode": 2, "content_type": "application/json"}
    if message_id is not None:
        props["message_id"] = message_id
    if headers is not None:
        props["headers"] = headers
    props.update(properties or {})
    with channel(port) as ch:
        ch.confirm_delivery()
        ch.basic_publish(exchange, routing_key, body, pika.BasicProperties(**props))


def _to_message(method: Any, props: pika.BasicProperties, body: bytes) -> Message:
    return Message(
        body=body,
        message_id=props.message_id,
        headers=dict(props.headers or {}),
        delivery_mode=props.delivery_mode,
        routing_key=method.routing_key,
        exchange=method.exchange,
    )


def drain(queue: str, *, expect: int | None = None, timeout: float = 10.0, settle: float = 1.0) -> list[Message]:
    """Consume (and ack) messages from a queue.

    Returns as soon as `expect` messages were read and the queue then stayed
    empty for `settle` seconds (to catch surplus copies), or at `timeout`.
    With expect=None reads until the queue stayed empty for `settle` seconds.
    A missing queue yields an empty list.
    """
    out: list[Message] = []
    deadline = time.monotonic() + timeout
    quiet_since: float | None = None
    try:
        with channel() as ch:
            while time.monotonic() < deadline:
                method, props, body = ch.basic_get(queue, auto_ack=True)
                if method is not None:
                    out.append(_to_message(method, props, body))
                    quiet_since = None
                    continue
                now = time.monotonic()
                if expect is None or len(out) >= expect:
                    quiet_since = quiet_since or now
                    if now - quiet_since >= settle:
                        break
                time.sleep(0.05)
    except pika.exceptions.ChannelClosedByBroker as exc:
        if exc.reply_code != 404:
            raise
    return out


def read_dead(*, expect: int | None = None, timeout: float = 10.0, settle: float = 1.0) -> list[Message]:
    """Read (and remove) the messages of the dead queue."""
    return drain(config.DEAD_QUEUE, expect=expect, timeout=timeout, settle=settle)


class Tap:
    """Verifier queue bound to the exchange with the contract routing key: it
    receives a copy of every publish to the exchange (contract §7.1, §7.5).

    Creating a tap declares the exchange with the contract properties, which
    the contract allows the verifier to do beforehand.
    """

    def __init__(self) -> None:
        self.queue = f"{TAP_PREFIX}{uuid.uuid4().hex[:12]}"
        with channel() as ch:
            ch.exchange_declare(config.EXCHANGE, exchange_type="direct", durable=True, auto_delete=False)
            ch.queue_declare(self.queue, durable=False, exclusive=False, auto_delete=False)
            ch.queue_bind(self.queue, config.EXCHANGE, routing_key=config.ROUTING_KEY)

    def read(self, *, expect: int | None = None, timeout: float = 10.0, settle: float = 1.0) -> list[Message]:
        return drain(self.queue, expect=expect, timeout=timeout, settle=settle)

    def close(self) -> None:
        with channel() as ch:
            ch.queue_delete(self.queue)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _rabbitmqctl(*args: str) -> str:
    result = subprocess.run(
        ["rabbitmqctl", "-q", *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"rabbitmqctl {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _queue_rows() -> list[dict[str, Any]]:
    raw = _rabbitmqctl(
        "list_queues", "--formatter", "json", "name", "messages_ready", "messages_unacknowledged", "consumers"
    )
    return list(json.loads(raw or "[]"))


def all_queues() -> list[str]:
    return [row["name"] for row in _queue_rows()]


def queue_stats(queue: str = config.WORK_QUEUE) -> QueueStats:
    """Authoritative queue counters from the broker (includes unacknowledged)."""
    for row in _queue_rows():
        if row["name"] == queue:
            return QueueStats(True, row["messages_ready"], row["messages_unacknowledged"], row["consumers"])
    return QueueStats(False)


def consumer_count(queue: str = config.WORK_QUEUE) -> int:
    """Fast consumer count via a passive declare; 0 if the queue does not exist.

    "Ready" in contract §7.1 = the work queue reports >= 1 consumer.
    """
    try:
        with channel() as ch:
            return int(ch.queue_declare(queue, passive=True).method.consumer_count)
    except pika.exceptions.ChannelClosedByBroker as exc:
        if exc.reply_code == 404:
            return 0
        raise


def wait_consumers(minimum: int = 1, timeout: float = config.WORKER_READY_TIMEOUT, queue: str = config.WORK_QUEUE) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if consumer_count(queue) >= minimum:
            return True
        time.sleep(0.25)
    return False


def wait_idle(queue: str = config.WORK_QUEUE, timeout: float = 10.0) -> QueueStats:
    """Wait until the queue has 0 ready and 0 unacknowledged messages; returns
    the last observed counters either way."""
    deadline = time.monotonic() + timeout
    stats = queue_stats(queue)
    while (stats.ready or stats.unacked) and time.monotonic() < deadline:
        time.sleep(0.5)
        stats = queue_stats(queue)
    return stats


def ensure_user() -> None:
    """Restore the contract broker user (it has the administrator tag and the
    agent could have changed or removed it)."""
    users = [row["user"] for row in json.loads(_rabbitmqctl("list_users", "--formatter", "json") or "[]")]
    if config.AMQP_USER in users:
        _rabbitmqctl("change_password", config.AMQP_USER, config.AMQP_PASS)
    else:
        _rabbitmqctl("add_user", config.AMQP_USER, config.AMQP_PASS)
    _rabbitmqctl("set_user_tags", config.AMQP_USER, "administrator")
    _rabbitmqctl("set_permissions", "-p", config.AMQP_VHOST, config.AMQP_USER, ".*", ".*", ".*")
