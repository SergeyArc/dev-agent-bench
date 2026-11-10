# Task — step 5: message consumer

The conventions above are the content of `/app/AGENTS.md`. They apply to this task.

## Starting point

`/app` holds the service built so far: the HTTP API for tracks, segments and sealing, with
tracks stored in PostgreSQL through Alembic migrations and one transaction per request.
Everything that works now must keep working. `DATABASE_URL` points to an existing, empty
database and `RABBITMQ_URL` to a running broker. Network access is available for `uv add`.

## Goal

Add a second process, the **worker**: an AMQP consumer that extends the capacity of a track
on a message — exactly once per message id, with retries, dead-lettering, reconnection and
a clean shutdown.

## Requirements

1. **Start command.** The worker is the module `tracksvc.worker`, always started from `/app`
   with exactly `uv run python -m tracksvc.worker`. It runs until it is stopped and is
   consuming within 30 s of the start (the work queue reports at least one consumer).
2. **Configuration**, read from the environment at worker start-up, never at import time:
   `DATABASE_URL`, `RABBITMQ_URL` (`amqp://user:pass@host:port/vhost`) and the optional
   `CONSUMER_MAX_RETRIES` — an integer ≥ 0, default `3`, called `MAX` below. Nothing about
   either connection is hard-coded: the worker is also started with other values. It needs
   a migrated database and a reachable broker at start-up; its behaviour when either is
   unreachable at start-up is not specified.
3. **Topology**, declared by the worker at start-up, with exactly these properties:

   | Object | Name | Properties |
   |---|---|---|
   | exchange | `tracksvc.events` | type `direct`, durable, not auto-delete, no arguments |
   | work queue | `tracksvc.track-events` | durable, not exclusive, not auto-delete, **no arguments**; bound to the exchange with routing key `track.extend` |
   | dead queue | `tracksvc.track-events.dead` | durable, not exclusive, not auto-delete, **no arguments**; no bindings |

   "No arguments" excludes `x-dead-letter-exchange`, TTL, `x-queue-type` and the like. The
   dead queue is reached through the default exchange with routing key = its name. All
   three may already exist with the same properties when the worker starts, and other
   queues may be bound to the exchange: the worker starts and works all the same.
4. **Message.** Published to `tracksvc.events` with routing key `track.extend`:
   - body: a UTF-8 JSON object `{"track_id": "<uuid>", "delta": <integer 1…1000>}`. JSON
     types are exact, as in the HTTP API: `track_id` is a string that parses as a UUID,
     `delta` an integer — not `"5"`, not `5.5`, not `null`; a boolean is not an integer;
   - property `message_id`: required string of 1–255 printable ASCII characters (0x20–0x7E),
     the idempotency key — global, not per track;
   - header `x-retry-count`: optional integer ≥ 0 (not a boolean), absent means 0: the
     number of failed attempts so far, called `c` below. Other properties are ignored.
5. **Effect.** A successful message sets `capacity := capacity + delta` on the track,
   visible through `GET /tracks/{track_id}` of the running HTTP application. The rule is
   one operation of the track in the domain; `capacity` never exceeds 10000 and a sealed
   track is never modified. Processing **fails** with one of these codes:

   | Code | Cause |
   |---|---|
   | `track_not_found` | no track with `track_id` |
   | `track_sealed` | the track is sealed |
   | `capacity_limit_exceeded` | `capacity + delta > 10000` |
   | `internal_error` | anything else, for example a database error |
6. **Idempotency.** A new Alembic revision adds table `processed_messages` with column
   `message_id` (text/varchar, primary key or unique); `downgrade base` and `upgrade head`
   keep working. A row is inserted in the **same database transaction** as the capacity
   change, and only on success.
   - First successful processing: capacity changed once, exactly one row for the id.
   - A message whose `message_id` already has a row is a duplicate **whatever its body and
     headers**: no capacity change, no new row, nothing published anywhere, acknowledged.
   - The same `message_id` delivered many times at once, also to two worker processes:
     capacity changed exactly once, exactly one row.
   - A failed or invalid message leaves no row: the same `message_id` published again
     later is processed as new.
7. **Outcomes.** Every delivery ends in exactly one of these; broker actions in this order:

   | Case | Database | Broker actions |
   |---|---|---|
   | success | effect + `processed_messages` row, one transaction | ack after the commit |
   | duplicate | nothing | ack |
   | invalid message | nothing | publish a copy to the dead queue with header `x-failure: invalid_message`; ack. Never retried |
   | processing failed, `c < MAX` | nothing (rolled back) | publish a copy to `tracksvc.events` / `track.extend` with `x-retry-count: c+1`; ack |
   | processing failed, `c >= MAX` | nothing (rolled back) | publish a copy to the dead queue with `x-retry-count: c` and `x-failure: <code of requirement 5>`; ack |

   - **Invalid** means: `message_id` missing, not a string or violating requirement 4;
     `x-retry-count` present but not a non-negative integer (a string, a negative number,
     a boolean); body not JSON, not an object or violating requirement 4. Order of checks:
     `message_id` → duplicate → the rest of the validation (header, body).
   - A **copy** has the same body bytes and the same `message_id`, is persistent
     (`delivery_mode = 2`) and keeps the original headers except those set above. The
     `message_id` of the copy of an invalid message whose `message_id` is missing or invalid
     is unspecified (the client library may substitute its own).
   - Retry is **republish + ack**. `nack` / `reject` with `requeue=true` is never used as
     the retry mechanism: headers cannot be changed on requeue.
   - While no connection is lost, a failing message with no prior retries is attempted
     `MAX + 1` times: exactly `MAX` republished copies with `x-retry-count` 1…`MAX`, then
     exactly one dead copy with `x-retry-count: MAX`. A failing message that arrives with
     `x-retry-count: MAX` goes straight to the dead queue with no republish; with
     `CONSUMER_MAX_RETRIES=0` the first failure is dead-lettered.
   - A valid message that arrives with `x-retry-count` ≤ `MAX` is processed normally.
   - Retries are immediate. On an idle system every outcome is observable within 10 s of
     publishing. Nothing is silently dropped, and the work queue ends with 0 ready and
     0 unacknowledged messages.
8. **Connection loss.** When the broker connection breaks for 5–30 s, the worker process
   stays alive — the same PID, no restart. Within 30 s after the broker is reachable again
   it is consuming again, and a message published after that is processed within 10 s. A
   message delivered but not acknowledged when the connection broke is redelivered by the
   broker; its effect is still applied exactly once. Under connection loss retry and dead
   copies may be published more than once (at-least-once); the database effect stays
   exactly-once. Loss of the database connection by the worker is not specified.
9. **Shutdown.** On `SIGTERM`, at any time, the worker exits within 10 s. An unacknowledged
   delivery is redelivered by the broker later and handled as above.
10. **The HTTP application is unchanged**: it does not read `RABBITMQ_URL`, never connects
    to the broker, serves every route while RabbitMQ is down, and returns a capacity
    changed by the worker without being restarted. Several workers and application
    processes may run against one database.
11. **No failure switches.** The service contains no switch, environment variable, flag or
    special value whose purpose is to make processing fail.
12. **Tests.** Add tests for the behaviour of this step; `uv run pytest` passes against an
    empty, unmigrated database and a reachable broker.

## Conventions for message handling

These rules extend `AGENTS.md` for the worker introduced by this task.

- `tracksvc.worker` is an entry point and a composition root like `tracksvc.api`: it wires
  the layers together and may import any of them; `tracksvc.infrastructure` never imports
  from it.
- A message handler is an entry point like an HTTP handler: it calls the same
  capacity-change rule in `domain` / `application` that the rest of the code uses and
  never re-implements it. Transport details (acknowledgements, headers, retry counters,
  routing) stay out of `domain` and `application`.
- One database session / unit of work per consumed message, exactly as for a request: it
  comes from a single provider in the worker's composition root, the use case owns the
  transaction, and the session is released on every path, including errors. The worker
  never keeps one session for the lifetime of the process.
- The capacity change and the record of the processed message are written in that single
  transaction by construction. Never get the effect with two commits in a row.
- A message is acknowledged only after its processing has finished and its transaction
  has been committed. Acknowledge exactly once, await the result, and never acknowledge
  before the handler has run.
- The broker connection, the channel and the database session each have exactly one
  explicit owner that creates and releases them — at shutdown, and whenever the resource
  is replaced after a reconnect. A reconnect leaks no connection, channel or consumer.
- Retry and dead-letter decisions (counting attempts, republishing, routing) are made in
  the worker's transport code, not in `domain` or `application`; a message handler never
  commits or rolls back itself.

## Self-check

```bash
cd /app
uv run ruff check . && uv run mypy -p tracksvc && uv run pytest

uv run alembic upgrade head
uv run uvicorn tracksvc.api.app:app --host 0.0.0.0 --port 8000 &
uv run python -m tracksvc.worker &
# create a track over HTTP, then publish with a short aio-pika script of your own
# (exchange tracksvc.events, routing key track.extend, message_id set):
#   {"track_id": "<id>", "delta": 5}   → GET shows capacity + 5; one processed_messages row
#   the same message_id again           → nothing changes, nothing is published
#   track_id of a non-existent track    → 3 republished copies, then one dead copy
#   body "not json"                     → dead queue, x-failure invalid_message
kill -TERM %2   # the worker exits within 10 s
kill %1
```

Both processes are started only with these commands, never with your own scripts or
Makefile targets. Do not rely on a process you left running.

## Out of scope

- Producers, new HTTP routes or any change of the HTTP behaviour.
- Other message types, routing keys or queues; delays between retries; a transient/permanent split.
- Broker-side dead-lettering or TTL; changing `capacity` by any other means.
- Installing or starting infrastructure; changing `ruff.toml` or `mypy.ini`.
