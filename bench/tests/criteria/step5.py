"""Step 5 (L3-02) deterministic criteria 5.1–5.19 of contract §9.

Every check builds the situation it needs: a freshly migrated database of its
own, an empty broker topology, the HTTP app and the worker started with the
commands of contract §2.2 — and stops everything again. Failures (broker
outage through toxiproxy, a second worker, SIGTERM, a stopped broker) are
created here, never by agent-written code. No check waits without a deadline:
states are polled, and a fixed pause is used only for the length of an outage
and for the window in which "nothing more happens" is observed.

Two pairs of criteria read one scenario run instead of repeating it: 5.3 / 5.4
(one valid message) and 5.7 / 5.8 (one failing message).

Time limits: the contract limit plus `config.TOLERANCE` (30 + 5 s to consume
after start or after an outage, 10 + 5 s for an outcome, 10 + 5 s for SIGTERM).

Scoring metadata of contract §9 (`REQUIRED_PASS`, `GROUPS`) is exported for the
rubric stage and repeated as a tag at the start of every evidence text. The
reward formula is not changed here. Judge criteria are not evaluated;
`JUDGE_CRITERIA` is the hook for the judge stage.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Self, TypeVar

import httpx
import pika  # type: ignore[import-untyped]
import pika.exceptions  # type: ignore[import-untyped]
import psycopg

from benchverify import amqp, config, db, http, proc, reset, toxi
from benchverify.common import Context
from benchverify.result import Check, CheckFailed, Suite

STEP = 5
JUDGE_CRITERIA = ("CJ1", "CJ2", "5.J1", "5.J2", "5.J3", "5.J4")  # hook only

REQUIRED_PASS = frozenset({"5.1", "5.3", "5.4", "5.5"})
GROUPS: dict[str, tuple[str, ...]] = {
    "G-retry": ("5.7", "5.8", "5.9", "5.10", "5.11", "5.12", "5.13", "5.14", "5.19"),
    "G-concurrency": ("5.6",),
    "G-resilience": ("5.2", "5.15", "5.16", "5.17"),
    "G-isolation": ("5.18",),
}

# Not the default database name on purpose: the worker and the app must use
# DATABASE_URL (contract §2.1).
DATABASE = "tracksvc_step5"

MIGRATE_TIMEOUT = 120.0
HEALTH_LIMIT = config.HEALTH_TIMEOUT + config.TOLERANCE
READY_LIMIT = config.WORKER_READY_TIMEOUT + config.TOLERANCE  # §2.2, §7.6: consuming within 30 s
OUTCOME_LIMIT = 10.0 + config.TOLERANCE  # §7.4: outcomes observable within 10 s
OUTAGE = 8.0  # §7.6: the proxy is disabled for 5–30 s
QUIET = 2.0  # window in which no further copy / change may appear
BROKER_TIMEOUT = 120.0  # stopping / starting the broker application (5.18)

DEFAULT_MAX = 3  # CONSUMER_MAX_RETRIES when the variable is unset
BURST = 20  # 5.6: copies of one message_id
BATCH = 50  # 5.16: messages processed across an outage
TRACE_HEADER = "x-trace"  # an "original header" every copy has to keep (§7.4)

_T = TypeVar("_T")


# --- messages -----------------------------------------------------------------


@dataclass(frozen=True)
class _Sent:
    """A message as the verifier published it."""

    body: bytes
    message_id: str | None
    headers: dict[str, Any] | None = None


def _extend(track_id: str, delta: int) -> bytes:
    return json.dumps({"track_id": track_id, "delta": delta}).encode()


def _message_id(tag: str) -> str:
    return f"step5-{tag}-{uuid.uuid4().hex[:12]}"


def _publish(*messages: _Sent) -> None:
    """Publish to the exchange with the contract routing key, one channel, in order."""
    try:
        with amqp.channel() as ch:
            ch.confirm_delivery()
            for message in messages:
                properties = pika.BasicProperties(
                    delivery_mode=2,
                    content_type="application/json",
                    message_id=message.message_id,
                    headers=message.headers,
                )
                ch.basic_publish(config.EXCHANGE, config.ROUTING_KEY, message.body, properties)
    except pika.exceptions.ChannelClosedByBroker as exc:
        raise CheckFailed(f"cannot publish to exchange `{config.EXCHANGE}`: {exc}") from exc


def _same(value: object, wanted: object) -> bool:
    """Equal value of the same type: `True` is not `1`, `"3"` is not `3`."""
    return type(value) is type(wanted) and value == wanted


def _copy_problems(copy: amqp.Message, sent: _Sent, expected: dict[str, Any], what: str) -> list[str]:
    """Why `copy` is not a copy of `sent` in the sense of contract §7.4."""
    problems = []
    if copy.body != sent.body:
        problems.append(f"{what}: body {copy.body[:120]!r} differs from the original {sent.body[:120]!r}")
    if copy.message_id != sent.message_id:
        problems.append(f"{what}: message_id {copy.message_id!r}, expected {sent.message_id!r}")
    if copy.delivery_mode != 2:
        problems.append(f"{what}: delivery_mode {copy.delivery_mode!r}, expected 2 (persistent)")
    for key, value in expected.items():
        if not _same(copy.headers.get(key), value):
            problems.append(f"{what}: header {key} is {copy.headers.get(key)!r}, expected {value!r}")
    for key, value in (sent.headers or {}).items():
        if key not in expected and not _same(copy.headers.get(key), value):
            problems.append(f"{what}: original header {key}={value!r} was not kept ({copy.headers.get(key)!r})")
    return problems


def _retry_counts(messages: list[amqp.Message]) -> list[Any]:
    return [message.headers.get("x-retry-count") for message in messages]


def _republished(tap: list[amqp.Message], sent: list[_Sent]) -> list[amqp.Message]:
    """Tap messages that are not the verifier's own publishes.

    The tap receives every publish to the exchange, the verifier's included; a
    message equal to a sent one (body, message_id, headers) is removed once.
    """
    left = list(tap)
    for message in sent:
        for index, seen in enumerate(left):
            if (
                seen.body == message.body
                and seen.message_id == message.message_id
                and seen.headers == (message.headers or {})
            ):
                del left[index]
                break
        else:
            raise RuntimeError(f"the verifier's own publish {message.message_id!r} is not on the tap")
    return left


def _describe(messages: list[amqp.Message]) -> str:
    return "; ".join(
        f"message_id={m.message_id!r} headers={m.headers!r} body={m.body[:60]!r}" for m in messages[:10]
    ) or "none"


# --- database and broker state --------------------------------------------------


def _processed(message_ids: list[str] | None = None) -> int:
    """Number of `processed_messages` rows, all of them or those of the given ids."""
    try:
        if message_ids is None:
            rows = db.query("SELECT count(*) FROM public.processed_messages", db=DATABASE)
        else:
            rows = db.query(
                "SELECT count(*) FROM public.processed_messages WHERE message_id = ANY(%s)",
                [message_ids],
                db=DATABASE,
            )
    except psycopg.Error as exc:
        raise CheckFailed(f"cannot read table `processed_messages`: {exc}".strip()) from exc
    return int(rows[0][0])


def _expect_rows(message_id: str, wanted: int, when: str) -> None:
    found = _processed([message_id])
    if found != wanted:
        raise CheckFailed(
            f"{when}: `processed_messages` has {found} row(s) with message_id {message_id!r}, expected {wanted}"
        )


def _expect_idle(when: str, limit: float = OUTCOME_LIMIT) -> None:
    """The work queue ends with 0 ready and 0 unacknowledged messages (§7.4)."""
    stats = amqp.wait_idle(timeout=limit)
    if not stats.exists:
        raise CheckFailed(f"{when}: the work queue `{config.WORK_QUEUE}` does not exist")
    if stats.ready or stats.unacked:
        raise CheckFailed(
            f"{when}: the work queue still has {stats.ready} ready and {stats.unacked} unacknowledged "
            f"message(s) after {limit:.0f} s"
        )


def _ctl_json(*args: str) -> list[dict[str, Any]]:
    return list(json.loads(amqp._rabbitmqctl(*args) or "[]"))


def _arguments(raw: object) -> dict[str, Any]:
    """`arguments` of rabbitmqctl's JSON output ([[name, type, value], …]) as a dict."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, list):
        return {str(item[0]): item[-1] for item in raw if isinstance(item, list) and item}
    return {}


def _topology_problems() -> list[str]:
    """Differences between the broker objects and contract §7.1.

    RabbitMQ 4 records `x-queue-type: classic` for a queue declared without
    arguments, so that single argument cannot be told from "no arguments" and
    is accepted; any other argument is a difference.
    """
    problems: list[str] = []
    fmt = ("--formatter", "json")
    exchanges = {
        row["name"]: row
        for row in _ctl_json("list_exchanges", *fmt, "name", "type", "durable", "auto_delete", "arguments")
    }
    queues = {
        row["name"]: row
        for row in _ctl_json("list_queues", *fmt, "name", "durable", "auto_delete", "exclusive", "arguments")
    }
    bindings = _ctl_json(
        "list_bindings", *fmt, "source_name", "destination_name", "destination_kind", "routing_key"
    )

    exchange = exchanges.get(config.EXCHANGE)
    if exchange is None:
        problems.append(f"exchange `{config.EXCHANGE}` does not exist")
    else:
        wanted = {"type": "direct", "durable": True, "auto_delete": False}
        problems += [
            f"exchange `{config.EXCHANGE}`: {key} is {exchange.get(key)!r}, expected {value!r}"
            for key, value in wanted.items()
            if exchange.get(key) != value
        ]
        if _arguments(exchange.get("arguments")):
            problems.append(f"exchange `{config.EXCHANGE}` has arguments {exchange.get('arguments')!r}")

    for name in (config.WORK_QUEUE, config.DEAD_QUEUE):
        queue = queues.get(name)
        if queue is None:
            problems.append(f"queue `{name}` does not exist")
            continue
        wanted = {"durable": True, "auto_delete": False, "exclusive": False}
        problems += [
            f"queue `{name}`: {key} is {queue.get(key)!r}, expected {value!r}"
            for key, value in wanted.items()
            if queue.get(key) != value
        ]
        arguments = _arguments(queue.get("arguments"))
        if arguments not in ({}, {"x-queue-type": "classic"}):
            problems.append(f"queue `{name}` has arguments {arguments!r}, expected none")

    def bound(queue_name: str) -> list[tuple[str, str]]:
        return sorted(
            (str(row["source_name"]), str(row["routing_key"]))
            for row in bindings
            if row["destination_kind"] == "queue"
            and row["destination_name"] == queue_name
            and row["source_name"] != ""  # the default exchange binds every queue
        )

    if config.WORK_QUEUE in queues and (config.EXCHANGE, config.ROUTING_KEY) not in bound(config.WORK_QUEUE):
        problems.append(
            f"queue `{config.WORK_QUEUE}` is not bound to `{config.EXCHANGE}` with routing key "
            f"`{config.ROUTING_KEY}` (bindings: {bound(config.WORK_QUEUE)})"
        )
    if config.DEAD_QUEUE in queues and bound(config.DEAD_QUEUE):
        problems.append(f"queue `{config.DEAD_QUEUE}` has bindings {bound(config.DEAD_QUEUE)}, expected none")
    return problems


# --- the stack under test -------------------------------------------------------


class _Stack:
    """One scenario: a migrated database of its own, an empty topology, and the
    HTTP app / worker processes started on them. Everything is removed on exit."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.url = config.database_url(DATABASE)
        self.client = http.client()
        self.app: proc.ManagedProcess | None = None
        self.workers: list[proc.ManagedProcess] = []
        self._started = 0

    def __enter__(self) -> Self:
        try:
            proc.stop_agent_processes()
            toxi.ensure_proxy()
            amqp.delete_topology()
            db.recreate_database(DATABASE)
            result = reset.migrate(self.url, timeout=MIGRATE_TIMEOUT)
            if result.timed_out:
                raise CheckFailed(f"`uv run alembic upgrade head` timed out after {MIGRATE_TIMEOUT:.0f} s")
            if result.returncode != 0:
                raise CheckFailed(
                    f"`uv run alembic upgrade head` exited with {result.returncode}\n{result.tail(20)}"
                )
        except BaseException:
            self._cleanup()
            raise
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, traceback: object) -> None:
        if isinstance(exc, CheckFailed):
            # The cause is usually in the worker's output.
            exc.args = (f"{exc}{self._logs()}",)
        self._cleanup()

    def _logs(self) -> str:
        parts = []
        for process in self.workers:
            tail = process.log_tail(12)
            if tail:
                parts.append(f"\n--- {process.name} (last lines) ---\n{tail}")
        return "".join(parts)

    def _cleanup(self) -> None:
        for process in (*self.workers, self.app):
            if process is not None:
                process.stop()
        self.client.close()
        proc.stop_agent_processes()
        toxi.ensure_proxy()
        amqp.delete_topology()  # removes the taps as well
        db.drop_database(DATABASE)

    # processes

    def start_app(self) -> proc.ManagedProcess:
        log_dir = config.LOG_DIR / "proc"
        app = proc.http_app(self.url, log_dir=log_dir, name=f"step5-{self.name}-app").start()
        self.app = app
        started = time.monotonic()
        while True:
            try:
                healthy = self.client.get("/health", timeout=2.0).status_code == 200
            except httpx.HTTPError:
                healthy = False
            if healthy:
                return app
            elapsed = time.monotonic() - started
            if not app.alive():
                raise CheckFailed(
                    f"the HTTP app exited with code {app.returncode()} after {elapsed:.1f} s\n{app.log_tail(20)}"
                )
            if elapsed >= HEALTH_LIMIT:
                raise CheckFailed(
                    f"GET /health did not return 200 within {HEALTH_LIMIT:.0f} s\n{app.log_tail(20)}"
                )
            time.sleep(0.25)

    def start_worker(
        self, *, via_proxy: bool = False, max_retries: int | None = None, wait: bool = True
    ) -> proc.ManagedProcess:
        """Start a worker; `via_proxy` points RABBITMQ_URL at the toxiproxy listener."""
        self._started += 1
        port = config.TOXI_AMQP_PORT if via_proxy else config.AMQP_PORT
        worker = proc.worker(
            self.url,
            config.rabbitmq_url(port=port),
            log_dir=config.LOG_DIR / "proc",
            name=f"step5-{self.name}-worker{self._started}",
            max_retries=max_retries,
        ).start()
        self.workers.append(worker)
        if wait:
            self.wait_consuming()
        return worker

    def wait_consuming(self, limit: float = READY_LIMIT, since: str = "the worker start command") -> float:
        """Poll until the work queue reports one consumer per running worker."""
        wanted = len(self.workers)
        started = time.monotonic()
        while True:
            count = amqp.consumer_count()
            elapsed = time.monotonic() - started
            if count >= wanted:
                return round(elapsed, 2)
            self.expect_workers_alive(f"{elapsed:.1f} s after {since}")
            if elapsed >= limit:
                raise CheckFailed(
                    f"the work queue reports {count} consumer(s), expected >= {wanted}, "
                    f"{limit:.0f} s after {since}"
                )
            time.sleep(0.25)

    def expect_workers_alive(self, when: str) -> None:
        for worker in self.workers:
            if not worker.alive():
                raise CheckFailed(f"the worker process exited with code {worker.returncode()} ({when})")

    def stop_worker(self, worker: proc.ManagedProcess) -> None:
        worker.stop()
        self.workers.remove(worker)
        if not proc.wait_until(lambda: amqp.consumer_count() <= len(self.workers), 10.0):
            raise RuntimeError("the consumer of a stopped worker is still registered")

    def tap(self) -> amqp.Tap:
        """A tap on the exchange. Created once the worker consumes, so that only
        5.2 depends on a topology declared before the worker starts."""
        try:
            return amqp.Tap()
        except pika.exceptions.ChannelClosedByBroker as exc:
            raise CheckFailed(
                f"exchange `{config.EXCHANGE}` as declared by the worker does not accept a declaration "
                f"with the properties of §7.1: {exc}"
            ) from exc

    # HTTP

    def request(self, method: str, path: str, wanted: int, **kwargs: Any) -> httpx.Response:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise CheckFailed(f"{method} {path}: no HTTP response ({exc.__class__.__name__})") from exc
        if response.status_code != wanted:
            raise CheckFailed(
                f"{method} {path}: expected {wanted}, got {response.status_code}, body {response.text[:300]!r}"
            )
        return response

    def create_track(self, capacity: int = 100) -> str:
        code = f"S5-{uuid.uuid4().hex[:10].upper()}"
        body = self.request("POST", "/tracks", 201, json={"code": code, "capacity": capacity}).json()
        if not isinstance(body, dict) or not isinstance(body.get("id"), str):
            raise CheckFailed(f"POST /tracks: no string `id` in the body {body!r}")
        return str(body["id"])

    def add_segment(self, track_id: str, label: str = "alpha", start: int = 0, end: int = 1) -> None:
        payload = {"label": label, "span": {"start": start, "end": end}}
        self.request("POST", f"/tracks/{track_id}/segments", 201, json=payload)

    def seal(self, track_id: str) -> None:
        self.add_segment(track_id)
        self.request("POST", f"/tracks/{track_id}/seal", 200)

    def track(self, track_id: str) -> dict[str, Any]:
        body = self.request("GET", f"/tracks/{track_id}", 200).json()
        if not isinstance(body, dict):
            raise CheckFailed(f"GET /tracks/{track_id}: the body is not an object: {body!r}")
        return body

    def capacity(self, track_id: str) -> Any:
        return self.track(track_id).get("capacity")

    def wait_capacity(self, track_id: str, wanted: int, limit: float = OUTCOME_LIMIT) -> float:
        """Poll `GET /tracks/{id}` until `capacity` equals `wanted`."""
        started = time.monotonic()
        while True:
            value = self.capacity(track_id)
            elapsed = time.monotonic() - started
            if value == wanted:
                return round(elapsed, 2)
            if elapsed >= limit:
                raise CheckFailed(
                    f"GET /tracks/{track_id}: capacity is {value!r}, expected {wanted} within {limit:.0f} s"
                )
            time.sleep(0.1)

    def expect_capacity(self, track_id: str, wanted: int, when: str) -> None:
        value = self.capacity(track_id)
        if value != wanted:
            raise CheckFailed(f"{when}: capacity of track {track_id} is {value!r}, expected {wanted}")


def _observe_quiet(stack: _Stack) -> None:
    """Let `QUIET` seconds pass while watching that the workers stay up."""
    deadline = time.monotonic() + QUIET
    while time.monotonic() < deadline:
        stack.expect_workers_alive("while idle")
        time.sleep(0.25)


# --- scenarios shared by two criteria --------------------------------------------


class _Shared:
    """Runs a scenario once and hands its outcome (or its failure) to every
    criterion that reads it."""

    def __init__(self) -> None:
        self._outcomes: dict[str, object] = {}

    def get(self, key: str, scenario: Callable[[], _T]) -> _T:
        if key not in self._outcomes:
            try:
                self._outcomes[key] = scenario()
            except Exception as exc:  # noqa: BLE001 - re-raised for every reader
                self._outcomes[key] = exc
        outcome = self._outcomes[key]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


@dataclass
class _SuccessRun:
    message_id: str
    took: float
    idle_problem: str | None
    rows: int | str  # a count, or why it could not be read


def _success_run() -> _SuccessRun:
    """One valid message for an open track (criteria 5.3 and 5.4)."""
    with _Stack("success") as stack:
        stack.start_app()
        stack.start_worker()
        track_id = stack.create_track(100)
        message_id = _message_id("ok")
        _publish(_Sent(_extend(track_id, 5), message_id))
        took = stack.wait_capacity(track_id, 105)
        idle_problem: str | None = None
        try:
            _expect_idle("after the message was processed")
        except CheckFailed as exc:
            idle_problem = str(exc)
        rows: int | str
        try:
            rows = _processed([message_id])
        except CheckFailed as exc:
            rows = str(exc)
        return _SuccessRun(message_id, took, idle_problem, rows)


@dataclass
class _RetryRun:
    sent: _Sent
    tap: list[amqp.Message]
    dead: list[amqp.Message]
    idle_problem: str | None


def _retry_run() -> _RetryRun:
    """One message for a non-existent track, no prior retries, default budget
    (criteria 5.7 and 5.8). No connection is lost in this run."""
    with _Stack("retry") as stack:
        stack.start_worker()
        tap = stack.tap()
        sent = _Sent(_extend(str(uuid.uuid4()), 5), _message_id("lost"), {TRACE_HEADER: "verifier"})
        _publish(sent)
        dead = amqp.read_dead(expect=1, timeout=OUTCOME_LIMIT, settle=QUIET)
        idle_problem: str | None = None
        try:
            _expect_idle("after the message was dead-lettered")
        except CheckFailed as exc:
            idle_problem = str(exc)
        seen = tap.read(expect=1 + DEFAULT_MAX, timeout=OUTCOME_LIMIT, settle=QUIET)
        return _RetryRun(sent, seen, dead, idle_problem)


# --- 5.1, 5.2 topology ------------------------------------------------------------


def c5_1_topology_and_consumer() -> str:
    with _Stack("5.1") as stack:
        worker = stack.start_worker(wait=False)
        started = time.monotonic()
        while True:
            consumers = amqp.consumer_count()
            problems = _topology_problems()
            elapsed = time.monotonic() - started
            if not problems and consumers >= 1:
                return (
                    f"{elapsed:.1f} s after the start command: exchange and both queues match §7.1, "
                    f"work queue has {consumers} consumer(s)"
                )
            if not worker.alive():
                raise CheckFailed(
                    f"the worker process exited with code {worker.returncode()} after {elapsed:.1f} s"
                )
            if elapsed >= READY_LIMIT:
                if consumers < 1:
                    problems.append(f"the work queue has {consumers} consumer(s), expected >= 1")
                raise CheckFailed(
                    f"{READY_LIMIT:.0f} s after the start command:\n" + "\n".join(problems)
                )
            time.sleep(0.5)


def c5_2_predeclared_topology() -> str:
    with _Stack("5.2") as stack:
        amqp.declare_topology()
        stack.tap()  # another queue bound to the exchange with the same routing key
        stack.start_worker(wait=False)
        took = stack.wait_consuming()
        _observe_quiet(stack)
        if amqp.consumer_count() < 1:
            raise CheckFailed(f"the consumer was gone {QUIET:.0f} s after it had appeared")
        problems = _topology_problems()
        if problems:
            raise CheckFailed("the pre-declared topology was changed:\n" + "\n".join(problems))
        return f"topology pre-declared by the verifier plus a tap: consuming after {took} s, still up {QUIET:.0f} s later"


# --- 5.3–5.6 effect and idempotency ---------------------------------------------------


def c5_3_valid_message(shared: _Shared) -> str:
    run = shared.get("success", _success_run)
    if run.idle_problem is not None:
        raise CheckFailed(f"capacity grew by delta after {run.took} s, but {run.idle_problem}")
    return f"capacity 100 -> 105 seen through GET after {run.took} s; work queue: 0 ready, 0 unacknowledged"


def c5_4_processed_row(shared: _Shared) -> str:
    run = shared.get("success", _success_run)
    if isinstance(run.rows, str):
        raise CheckFailed(run.rows)
    if run.rows != 1:
        raise CheckFailed(
            f"`processed_messages` has {run.rows} row(s) with message_id {run.message_id!r} after the "
            "message was processed, expected exactly 1"
        )
    return f"`processed_messages` has exactly one row with message_id {run.message_id!r}"


def c5_5_duplicate() -> str:
    with _Stack("5.5") as stack:
        stack.start_app()
        stack.start_worker()
        tap = stack.tap()
        track_id = stack.create_track(100)
        message_id = _message_id("dup")
        _publish(_Sent(_extend(track_id, 5), message_id))
        stack.wait_capacity(track_id, 105)
        _expect_idle("after the first processing")
        tap.read(expect=1, timeout=QUIET + 1, settle=0.5)  # the first publish itself

        repeats = [_Sent(_extend(track_id, 5), message_id), _Sent(_extend(track_id, 7), message_id)]
        _publish(*repeats)
        _expect_idle("after publishing the same message_id again")
        seen = tap.read(expect=len(repeats), timeout=OUTCOME_LIMIT, settle=QUIET)
        dead = amqp.read_dead(timeout=QUIET + 1, settle=QUIET)

        problems = []
        republished = _republished(seen, repeats)
        if republished:
            problems.append(f"{len(republished)} message(s) published to the exchange: {_describe(republished)}")
        if dead:
            problems.append(f"{len(dead)} message(s) in the dead queue: {_describe(dead)}")
        capacity = stack.capacity(track_id)
        if capacity != 105:
            problems.append(f"capacity is {capacity!r}, expected 105 (unchanged)")
        rows = _processed([message_id])
        if rows != 1:
            problems.append(f"`processed_messages` has {rows} row(s) for the message_id, expected 1")
        if problems:
            raise CheckFailed("same message_id published again (same body, then another delta):\n" + "\n".join(problems))
        return (
            "same message_id again with the same body and with another delta: capacity stays 105, one row, "
            "nothing on the tap or in the dead queue, work queue empty"
        )


def c5_6_concurrent_duplicates() -> str:
    with _Stack("5.6") as stack:
        stack.start_app()
        stack.start_worker(wait=False)
        stack.start_worker(wait=False)
        stack.wait_consuming()
        track_id = stack.create_track(100)
        message_id = _message_id("burst")
        _publish(*[_Sent(_extend(track_id, 5), message_id)] * BURST)
        stack.wait_capacity(track_id, 105)
        _expect_idle(f"after {BURST} copies of one message_id")
        _observe_quiet(stack)
        stack.expect_capacity(track_id, 105, f"after {BURST} copies of one message_id on two workers")
        _expect_rows(message_id, 1, f"after {BURST} copies of one message_id on two workers")
        return f"{BURST} copies of one message_id, two worker processes: capacity 100 -> 105 exactly once, one row"


# --- 5.7–5.14 retry and dead-lettering ---------------------------------------------------


def c5_7_republished_copies(shared: _Shared) -> str:
    run = shared.get("retry", _retry_run)
    copies = _republished(run.tap, [run.sent])
    wanted = list(range(1, DEFAULT_MAX + 1))
    problems = []
    if not all(type(count) is int for count in _retry_counts(copies)) or sorted(_retry_counts(copies)) != wanted:
        problems.append(
            f"{len(copies)} republished copy(ies) on the tap with x-retry-count {_retry_counts(copies)!r}, "
            f"expected exactly {DEFAULT_MAX} with {wanted}"
        )
    for copy in copies:
        count = copy.headers.get("x-retry-count")
        problems += _copy_problems(copy, run.sent, {}, f"copy with x-retry-count {count!r}")
    if problems:
        raise CheckFailed("message for a non-existent track:\n" + "\n".join(problems))
    return (
        f"message for a non-existent track: exactly {DEFAULT_MAX} republished copies on the tap, "
        f"x-retry-count {wanted}, same body and message_id, persistent, original header kept"
    )


def c5_8_dead_copy(shared: _Shared) -> str:
    run = shared.get("retry", _retry_run)
    problems = []
    if len(run.dead) != 1:
        problems.append(f"the dead queue holds {len(run.dead)} message(s), expected exactly 1: {_describe(run.dead)}")
    for copy in run.dead:
        expected = {"x-retry-count": DEFAULT_MAX, "x-failure": "track_not_found"}
        problems += _copy_problems(copy, run.sent, expected, "dead copy")
    if run.idle_problem is not None:
        problems.append(run.idle_problem)
    if problems:
        raise CheckFailed("message for a non-existent track:\n" + "\n".join(problems))
    return (
        f"dead queue: exactly one copy, x-retry-count {DEFAULT_MAX}, x-failure track_not_found, same body "
        "and message_id; work queue: 0 ready, 0 unacknowledged"
    )


def _dead_letter_problems(
    stack: _Stack, tap: amqp.Tap, sent: _Sent, republished: list[int], dead_count: object, what: str
) -> list[str]:
    """Publish a failing message and compare the copies with the expectation:
    `republished` — x-retry-count values of the copies on the tap; `dead_count`
    — x-retry-count of the single dead copy (None: 0 or absent)."""
    _publish(sent)
    dead = amqp.read_dead(expect=1, timeout=OUTCOME_LIMIT, settle=QUIET)
    problems = []
    try:
        _expect_idle("after the message was dead-lettered")
    except CheckFailed as exc:
        problems.append(str(exc))
    copies = _republished(tap.read(expect=1 + len(republished), timeout=OUTCOME_LIMIT, settle=QUIET), [sent])
    if not all(type(c) is int for c in _retry_counts(copies)) or sorted(_retry_counts(copies)) != republished:
        problems.append(
            f"{len(copies)} republished copy(ies) with x-retry-count {_retry_counts(copies)!r}, "
            f"expected {republished or 'none'}"
        )
    if len(dead) != 1:
        problems.append(f"the dead queue holds {len(dead)} message(s), expected exactly 1: {_describe(dead)}")
    for copy in dead:
        expected: dict[str, Any] = {"x-failure": "track_not_found"}
        if dead_count is not None:
            expected["x-retry-count"] = dead_count
        elif not _same(copy.headers.get("x-retry-count", 0), 0):
            problems.append(f"dead copy: header x-retry-count is {copy.headers.get('x-retry-count')!r}, expected 0")
        problems += _copy_problems(copy, sent, expected, "dead copy")
    stack.expect_workers_alive(what)
    return [f"{what}: {problem}" for problem in problems]


def _lost_message(headers: dict[str, Any] | None = None) -> _Sent:
    return _Sent(_extend(str(uuid.uuid4()), 5), _message_id("lost"), headers)


def c5_9_budget_exhausted() -> str:
    with _Stack("5.9") as stack:
        stack.start_worker()
        tap = stack.tap()
        sent = _lost_message({"x-retry-count": DEFAULT_MAX, TRACE_HEADER: "verifier"})
        problems = _dead_letter_problems(
            stack, tap, sent, [], DEFAULT_MAX, f"failing message published with x-retry-count {DEFAULT_MAX}"
        )
        if problems:
            raise CheckFailed("\n".join(problems))
        return (
            f"failing message published with x-retry-count {DEFAULT_MAX}: one dead copy "
            f"(x-retry-count {DEFAULT_MAX}, x-failure track_not_found), no republish"
        )


def c5_10_configured_budget() -> str:
    with _Stack("5.10") as stack:
        worker = stack.start_worker(max_retries=1)
        tap = stack.tap()
        problems = _dead_letter_problems(stack, tap, _lost_message(), [1], 1, "CONSUMER_MAX_RETRIES=1")
        stack.stop_worker(worker)
        stack.start_worker(max_retries=0)
        problems += _dead_letter_problems(stack, tap, _lost_message(), [], None, "CONSUMER_MAX_RETRIES=0")
        if problems:
            raise CheckFailed("\n".join(problems))
        return (
            "CONSUMER_MAX_RETRIES=1: one republished copy (x-retry-count 1), then one dead copy; "
            "CONSUMER_MAX_RETRIES=0: one dead copy, no republish"
        )


def c5_11_domain_failures() -> str:
    with _Stack("5.11") as stack:
        stack.start_app()
        stack.start_worker()
        sealed = stack.create_track(100)
        stack.seal(sealed)
        full = stack.create_track(9990)
        cases = {
            "track_sealed": (sealed, 100, _Sent(_extend(sealed, 5), _message_id("sealed"))),
            "capacity_limit_exceeded": (full, 9990, _Sent(_extend(full, 11), _message_id("overflow"))),
        }
        _publish(*[sent for _, _, sent in cases.values()])
        dead = amqp.read_dead(expect=len(cases), timeout=OUTCOME_LIMIT, settle=QUIET)
        problems = []
        try:
            _expect_idle("after both messages were dead-lettered")
        except CheckFailed as exc:
            problems.append(str(exc))
        for code, (track_id, capacity, sent) in cases.items():
            copies = [m for m in dead if m.message_id == sent.message_id]
            if len(copies) != 1:
                problems.append(f"{code}: {len(copies)} dead copy(ies) of the message, expected exactly 1")
            for copy in copies:
                problems += _copy_problems(copy, sent, {"x-failure": code}, f"{code}: dead copy")
            body = stack.track(track_id)
            if body.get("capacity") != capacity:
                problems.append(f"{code}: capacity changed from {capacity} to {body.get('capacity')!r}")
            if code == "track_sealed" and body.get("status") != "sealed":
                problems.append(f"{code}: status is {body.get('status')!r}, expected 'sealed'")
            if _processed([str(sent.message_id)]):
                problems.append(f"{code}: a `processed_messages` row exists for the failed message")
        stray = [m for m in dead if m.message_id not in {sent.message_id for _, _, sent in cases.values()}]
        if stray:
            problems.append(f"unexpected messages in the dead queue: {_describe(stray)}")
        if problems:
            raise CheckFailed("\n".join(problems))
        return (
            "sealed track -> one dead copy with x-failure track_sealed; capacity 9990 + 11 -> one dead copy "
            "with x-failure capacity_limit_exceeded; both capacities unchanged"
        )


def c5_12_dead_letter_is_not_processed() -> str:
    with _Stack("5.12") as stack:
        stack.start_app()
        stack.start_worker()
        message_id = _message_id("again")
        _publish(_Sent(_extend(str(uuid.uuid4()), 5), message_id))
        dead = amqp.read_dead(expect=1, timeout=OUTCOME_LIMIT, settle=QUIET)
        if not any(m.message_id == message_id for m in dead):
            raise CheckFailed(
                f"the failing message did not reach the dead queue within {OUTCOME_LIMIT:.0f} s: {_describe(dead)}"
            )
        _expect_idle("after the message was dead-lettered")
        _expect_rows(message_id, 0, "after the message was dead-lettered")

        track_id = stack.create_track(100)
        _publish(_Sent(_extend(track_id, 5), message_id))
        took = stack.wait_capacity(track_id, 105)
        _expect_idle("after the same message_id was published with a valid body")
        _expect_rows(message_id, 1, "after the same message_id was processed")
        return (
            "dead-lettered message_id: no `processed_messages` row; published again with a valid body for an "
            f"open track: capacity 100 -> 105 after {took} s, one row"
        )


def c5_13_late_attempt_succeeds() -> str:
    with _Stack("5.13") as stack:
        stack.start_app()
        stack.start_worker()
        tap = stack.tap()
        track_id = stack.create_track(100)
        sent = _Sent(_extend(track_id, 5), _message_id("late"), {"x-retry-count": 2})
        _publish(sent)
        took = stack.wait_capacity(track_id, 105)
        _expect_idle("after the message was processed")
        seen = tap.read(expect=1, timeout=QUIET + 1, settle=QUIET)
        dead = amqp.read_dead(timeout=QUIET + 1, settle=QUIET)
        problems = []
        republished = _republished(seen, [sent])
        if republished:
            problems.append(f"the message was republished: {_describe(republished)}")
        if dead:
            problems.append(f"the dead queue is not empty: {_describe(dead)}")
        capacity = stack.capacity(track_id)
        if capacity != 105:
            problems.append(f"capacity is {capacity!r}, expected 105 (changed once)")
        rows = _processed([str(sent.message_id)])
        if rows != 1:
            problems.append(f"`processed_messages` has {rows} row(s) for the message_id, expected 1")
        if problems:
            raise CheckFailed("valid message published with x-retry-count 2:\n" + "\n".join(problems))
        return f"valid message with x-retry-count 2: capacity 100 -> 105 once after {took} s, one row, no copies"


def _invalid_messages(track_id: str) -> dict[str, _Sent]:
    """One violation per message; every body is unique (copies are matched by body)."""

    def valid_body(delta: int) -> bytes:
        return _extend(track_id, delta)

    def body(index: int, **fields: Any) -> bytes:
        return json.dumps({"case": index, **fields}).encode()

    def with_id(tag: str, payload: bytes, headers: dict[str, Any] | None = None) -> _Sent:
        return _Sent(payload, _message_id(tag), headers)

    return {
        "message_id missing": _Sent(valid_body(1), None),
        "message_id empty": _Sent(valid_body(2), ""),
        "message_id with a control character": _Sent(valid_body(3), "step5-bell-\x07"),
        "message_id with a non-ASCII character": _Sent(valid_body(4), "step5-café"),
        "x-retry-count a string": with_id("hs", valid_body(5), {"x-retry-count": "1"}),
        "x-retry-count negative": with_id("hn", valid_body(6), {"x-retry-count": -1}),
        "x-retry-count a boolean": with_id("hb", valid_body(7), {"x-retry-count": True}),
        "body not JSON": with_id("b1", b"not json"),
        "body not UTF-8": with_id("b2", b"\xff\xfe{}"),
        "body not an object": with_id("b3", json.dumps([track_id, 5]).encode()),
        "track_id missing": with_id("t1", body(11, delta=5)),
        "track_id not a UUID": with_id("t2", body(12, track_id="not-a-uuid", delta=5)),
        "track_id not a string": with_id("t3", body(13, track_id=5, delta=5)),
        "delta missing": with_id("d1", body(21, track_id=track_id)),
        "delta a string": with_id("d2", body(22, track_id=track_id, delta="5")),
        "delta non-integral": with_id("d3", body(23, track_id=track_id, delta=5.5)),
        "delta a boolean": with_id("d4", body(24, track_id=track_id, delta=True)),
        "delta null": with_id("d5", body(25, track_id=track_id, delta=None)),
        "delta 0": with_id("d6", body(26, track_id=track_id, delta=0)),
        "delta 1001": with_id("d7", body(27, track_id=track_id, delta=1001)),
    }


def c5_14_invalid_messages() -> str:
    with _Stack("5.14") as stack:
        stack.start_app()
        stack.start_worker()
        tap = stack.tap()
        track_id = stack.create_track(100)
        cases = _invalid_messages(track_id)
        _publish(*cases.values())
        dead = amqp.read_dead(expect=len(cases), timeout=OUTCOME_LIMIT, settle=QUIET)
        problems = []
        try:
            _expect_idle("after the invalid messages")
        except CheckFailed as exc:
            problems.append(str(exc))
        seen = tap.read(expect=len(cases), timeout=QUIET + 1, settle=QUIET)

        for name, sent in cases.items():
            copies = [m for m in dead if m.body == sent.body]
            if len(copies) != 1:
                problems.append(f"{name}: {len(copies)} dead copy(ies), expected exactly 1")
            for copy in copies:
                if not _same(copy.headers.get("x-failure"), "invalid_message"):
                    problems.append(f"{name}: x-failure is {copy.headers.get('x-failure')!r}, expected 'invalid_message'")
                if not name.startswith("message_id") and copy.message_id != sent.message_id:
                    problems.append(f"{name}: dead copy has message_id {copy.message_id!r}, expected {sent.message_id!r}")
        stray = [m for m in dead if m.body not in {sent.body for sent in cases.values()}]
        if stray:
            problems.append(f"unexpected messages in the dead queue: {_describe(stray)}")
        if len(seen) != len(cases):
            problems.append(
                f"{len(seen)} message(s) on the tap for {len(cases)} published: invalid messages were republished "
                f"(x-retry-count values seen: {_retry_counts(seen)!r})"
            )
        capacity = stack.capacity(track_id)
        if capacity != 100:
            problems.append(f"capacity changed from 100 to {capacity!r}")
        rows = _processed()
        if rows:
            problems.append(f"`processed_messages` has {rows} row(s), expected 0")
        if problems:
            raise CheckFailed("\n".join(problems))
        return (
            f"{len(cases)} invalid messages ({', '.join(cases)}): one dead copy each with x-failure "
            "invalid_message, no republish, capacity unchanged, no `processed_messages` row"
        )


# --- 5.15–5.17 connection loss and shutdown ----------------------------------------------


def _start_outage() -> None:
    """Disable the proxy and make sure the worker's consumer went with it."""
    toxi.disable()
    if not proc.wait_until(lambda: amqp.consumer_count() == 0, 10.0):
        raise CheckFailed(
            "the consumer survived the outage of the listener named in RABBITMQ_URL: "
            "the worker does not connect through RABBITMQ_URL"
        )


def _hold_outage(stack: _Stack, started: float) -> float:
    """Keep the proxy disabled for `OUTAGE` seconds in total; the worker must stay up."""
    while time.monotonic() - started < OUTAGE:
        stack.expect_workers_alive("during the broker outage")
        time.sleep(0.25)
    return round(time.monotonic() - started, 1)


def c5_15_reconnect() -> str:
    with _Stack("5.15") as stack:
        stack.start_app()
        worker = stack.start_worker(via_proxy=True)
        track_id = stack.create_track(100)
        pid = worker.service_pid()

        started = time.monotonic()
        _start_outage()
        outage = _hold_outage(stack, started)
        toxi.enable()
        took = stack.wait_consuming(since="the broker became reachable again")
        if worker.service_pid() != pid:
            raise CheckFailed(f"the worker PID changed across the outage: {pid} -> {worker.service_pid()}")

        _publish(_Sent(_extend(track_id, 5), _message_id("after")))
        processed = stack.wait_capacity(track_id, 105)
        stack.expect_workers_alive("after the outage")
        return (
            f"broker outage of {outage} s through toxiproxy: same PID {pid}, consuming again after {took} s, "
            f"a new message processed in {processed} s"
        )


def c5_16_exactly_once_across_outage() -> str:
    with _Stack("5.16") as stack:
        stack.start_app()
        stack.start_worker(via_proxy=True)
        track_id = stack.create_track(100)
        batch = [_Sent(_extend(track_id, delta), _message_id(f"batch{delta}")) for delta in range(1, BATCH + 1)]
        ids = [str(sent.message_id) for sent in batch]
        wanted = 100 + sum(range(1, BATCH + 1))

        _publish(*batch)
        # The outage begins as soon as the first effect is committed: the rest
        # of the batch is in flight (delivered, being processed, or queued).
        if not proc.wait_until(lambda: _processed(ids) >= 1, OUTCOME_LIMIT, interval=0.005):
            raise CheckFailed(f"none of the {BATCH} messages was processed within {OUTCOME_LIMIT:.0f} s")
        started = time.monotonic()
        toxi.disable()
        before = _processed(ids)
        outage = _hold_outage(stack, started)
        toxi.enable()

        limit = READY_LIMIT + OUTCOME_LIMIT
        if not proc.wait_until(lambda: _processed(ids) >= BATCH, limit, interval=0.25):
            raise CheckFailed(
                f"`processed_messages` has {_processed(ids)} of {BATCH} rows {limit:.0f} s after the broker "
                f"became reachable again; capacity is {stack.capacity(track_id)!r}, expected {wanted}"
            )
        _expect_idle("after the batch was processed")
        _observe_quiet(stack)
        rows = _processed(ids)
        capacity = stack.capacity(track_id)
        if rows != BATCH or capacity != wanted:
            raise CheckFailed(
                f"after recovery: capacity is {capacity!r}, expected {wanted} (100 + the sum of {BATCH} deltas); "
                f"`processed_messages` has {rows} row(s), expected {BATCH}"
            )
        return (
            f"{BATCH} messages, one worker, outage of {outage} s begun with {before} of them processed: "
            f"capacity 100 -> {wanted}, {BATCH} rows (each delta applied exactly once)"
        )


def c5_17_sigterm() -> str:
    with _Stack("5.17") as stack:
        worker = stack.start_worker()
        took = worker.terminate(timeout=config.SIGTERM_LIMIT)
        if took is None:
            raise CheckFailed(f"the worker did not exit within {config.SIGTERM_LIMIT:.0f} s of SIGTERM")
        return f"the worker exited {took} s after SIGTERM (exit code {worker.returncode()})"


# --- 5.18 isolation of the HTTP app ----------------------------------------------------


def _broker_ctl(command: str) -> None:
    result = subprocess.run(
        ["rabbitmqctl", "-q", command], capture_output=True, text=True, timeout=BROKER_TIMEOUT, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"rabbitmqctl {command} failed: {result.stderr.strip()}")


def _stop_broker() -> None:
    _broker_ctl("stop_app")
    if not proc.wait_until(lambda: not proc.port_open(config.AMQP_PORT), 30.0):
        raise RuntimeError(f"port {config.AMQP_PORT} is still open after stopping the broker")


def _start_broker() -> None:
    _broker_ctl("start_app")
    if not proc.wait_until(lambda: proc.port_open(config.AMQP_PORT), BROKER_TIMEOUT):
        raise RuntimeError(f"the broker does not listen on port {config.AMQP_PORT} after start_app")
    amqp.ensure_user()
    toxi.ensure_proxy()


def c5_18_http_app_without_broker() -> str:
    with _Stack("5.18") as stack:
        app = stack.start_app()
        worker = stack.start_worker()
        pid = app.service_pid()
        track_id = stack.create_track(100)
        stack.expect_capacity(track_id, 100, "before the message")
        _publish(_Sent(_extend(track_id, 5), _message_id("seen")))
        took = stack.wait_capacity(track_id, 105)
        stack.stop_worker(worker)

        _stop_broker()
        try:
            other = stack.create_track(50)  # R1
            stack.track(other)  # R2
            stack.add_segment(other, "alpha", 0, 10)  # R3
            stack.request("DELETE", f"/tracks/{other}/segments/alpha", 200)  # R4
            stack.add_segment(other, "beta", 10, 20)
            sealed = stack.request("POST", f"/tracks/{other}/seal", 200).json()  # R5
            if not isinstance(sealed, dict) or sealed.get("status") != "sealed":
                raise CheckFailed(f"POST /tracks/{other}/seal with the broker down: body {sealed!r}")
            stack.expect_capacity(track_id, 105, "with the broker down")
            if not app.alive() or app.service_pid() != pid:
                raise CheckFailed("the HTTP app process was replaced or exited while the broker was down")
        finally:
            _start_broker()
        return (
            f"capacity changed by the worker returned by the running app after {took} s (PID {pid}, no restart); "
            "with RabbitMQ stopped: R1–R5 answered 201 / 200 / 201 / 200 / 200"
        )


# --- 5.19 duplicate check precedes the rest of the validation ----------------------------


def c5_19_invalid_duplicate() -> str:
    """§7.4 order of checks: message_id -> duplicate -> x-retry-count, body."""
    with _Stack("5.19") as stack:
        stack.start_app()
        stack.start_worker()
        tap = stack.tap()
        track_id = stack.create_track(100)
        message_id = _message_id("known")
        _publish(_Sent(_extend(track_id, 5), message_id))
        stack.wait_capacity(track_id, 105)
        _expect_idle("after the first processing")
        tap.read(expect=1, timeout=QUIET + 1, settle=0.5)  # the first publish itself
        rows_before = _processed()

        repeats = {
            "body not JSON": _Sent(b"not json", message_id),
            "body with a string delta": _Sent(json.dumps({"track_id": track_id, "delta": "5"}).encode(), message_id),
            "body without track_id": _Sent(json.dumps({"delta": 5}).encode(), message_id),
            "x-retry-count a string": _Sent(_extend(track_id, 7), message_id, {"x-retry-count": "1"}),
            "x-retry-count negative": _Sent(_extend(track_id, 8), message_id, {"x-retry-count": -1}),
        }
        _publish(*repeats.values())
        problems = []
        try:
            _expect_idle("after publishing the processed message_id with an invalid body or header")
        except CheckFailed as exc:
            problems.append(str(exc))
        seen = tap.read(expect=len(repeats), timeout=OUTCOME_LIMIT, settle=QUIET)
        dead = amqp.read_dead(timeout=QUIET + 1, settle=QUIET)

        republished = _republished(seen, list(repeats.values()))
        if republished:
            problems.append(f"{len(republished)} message(s) published to the exchange: {_describe(republished)}")
        for name, sent in repeats.items():
            copies = [m for m in dead if m.body == sent.body and m.headers.get("x-retry-count") == (sent.headers or {}).get("x-retry-count")]
            if copies:
                problems.append(f"{name}: {len(copies)} copy(ies) in the dead queue, x-failure {copies[0].headers.get('x-failure')!r}")
        if dead and not any("dead queue" in problem for problem in problems):
            problems.append(f"{len(dead)} message(s) in the dead queue: {_describe(dead)}")
        capacity = stack.capacity(track_id)
        if capacity != 105:
            problems.append(f"capacity is {capacity!r}, expected 105 (unchanged)")
        rows = _processed()
        if rows != rows_before or _processed([message_id]) != 1:
            problems.append(f"`processed_messages` has {rows} row(s), expected {rows_before} (unchanged)")
        if problems:
            raise CheckFailed("processed message_id published again with an invalid body or x-retry-count:\n" + "\n".join(problems))
        return (
            f"processed message_id published again as: {', '.join(repeats)} — all acknowledged, nothing on the tap "
            "or in the dead queue, capacity stays 105, `processed_messages` unchanged"
        )


# --- registration ---------------------------------------------------------------------


def _tag(criterion: str) -> str:
    if criterion in REQUIRED_PASS:
        return "[required-pass] "
    for group, members in GROUPS.items():
        if criterion in members:
            return f"[{group}] "
    return ""


def _tagged(criterion: str, check: Check) -> Check:
    """Prefix the evidence, of a pass and of a failure, with the scoring tag of §9."""
    tag = _tag(criterion)

    def run() -> str | None:
        try:
            return f"{tag}{check() or ''}"
        except CheckFailed as exc:
            raise CheckFailed(f"{tag}{exc}") from exc

    return run


def register(suite: Suite, ctx: Context) -> None:
    """Criteria 5.1–5.19 in contract order; 5.18 stops the broker and restarts it itself."""
    del ctx  # step 5 needs nothing from the run context
    shared = _Shared()
    checks: dict[str, Check] = {
        "5.1": c5_1_topology_and_consumer,
        "5.2": c5_2_predeclared_topology,
        "5.3": lambda: c5_3_valid_message(shared),
        "5.4": lambda: c5_4_processed_row(shared),
        "5.5": c5_5_duplicate,
        "5.6": c5_6_concurrent_duplicates,
        "5.7": lambda: c5_7_republished_copies(shared),
        "5.8": lambda: c5_8_dead_copy(shared),
        "5.9": c5_9_budget_exhausted,
        "5.10": c5_10_configured_budget,
        "5.11": c5_11_domain_failures,
        "5.12": c5_12_dead_letter_is_not_processed,
        "5.13": c5_13_late_attempt_succeeds,
        "5.14": c5_14_invalid_messages,
        "5.15": c5_15_reconnect,
        "5.16": c5_16_exactly_once_across_outage,
        "5.17": c5_17_sigterm,
        "5.18": c5_18_http_app_without_broker,
        "5.19": c5_19_invalid_duplicate,
    }
    for criterion, check in checks.items():
        suite.add(criterion, _tagged(criterion, check), step=STEP)
