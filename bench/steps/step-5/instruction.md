You work autonomously. There is no human to answer questions: do not ask for confirmation or
clarification — make reasonable decisions yourself and finish the task.

# AGENTS.md — tracksvc engineering conventions

These conventions apply to every change in this repository. Follow them even when a task
does not repeat them. Where a task is more specific than this file, the task wins.

## Stack and environment

- Python 3.13, FastAPI, SQLAlchemy 2 (async, asyncpg), Alembic, Pydantic v2, aio-pika,
  PostgreSQL, RabbitMQ. Packages are managed with uv.
- The project root is `/app`. Dependencies are declared in `pyproject.toml`, locked in
  `uv.lock` and already installed in `.venv`. Add a dependency with `uv add <name>`
  (`uv add --dev <name>` for development tools). Never call `pip` directly.
- PostgreSQL and RabbitMQ are already running and reachable. Do not install, start or
  configure infrastructure (no Docker, no Compose, no local servers).
- Connection settings come only from environment variables: `DATABASE_URL`
  (`postgresql+asyncpg://user:pass@host:port/db`) and `RABBITMQ_URL`
  (`amqp://user:pass@host:port/vhost`). Both are already set in the shell. Never hard-code
  hosts, ports, database names or credentials.
- Each entry point reads its configuration during its own startup: the HTTP app in the
  application lifespan, Alembic while its environment script runs; modules of `tracksvc`
  read no configuration at import time.
- A process reads only the variables it needs and starts when the others are unset.
- Nothing outside PostgreSQL and RabbitMQ is needed to start: no local data files, no
  state kept between runs. Several processes may run against the same database.

## Checks

Run from `/app` before you finish a task; all three must pass:

```bash
uv run ruff check .
uv run mypy -p tracksvc
uv run pytest
```

- `ruff.toml` and `mypy.ini` are picked up automatically. Do not modify, move or override
  them (this includes `[tool.ruff]` / `[tool.mypy]` sections in `pyproject.toml`).
- Fix findings in the code. Do not silence the tools: no `# noqa`, no `# type: ignore`.
- `Any` is used only for genuinely untyped external data at a boundary. `cast` is used
  only after a runtime check that narrows the type. Neither is used to silence the checker.
- All functions and methods are fully type-annotated. Line length is 100.

## Layout and layers

```
src/tracksvc/
  domain/          business model: entities, value objects, invariants, domain errors
  application/     use cases and the abstractions they depend on
  infrastructure/  database and other technical implementations of those abstractions
  api/             FastAPI application: routes, request/response schemas, error mapping
tests/             the test suite
```

Import rules:

- **Foundation allowlist**, treated like the standard library: `pydantic`, `typing_extensions`.
- `tracksvc.domain` imports only itself, the standard library and the foundation allowlist.
- `tracksvc.application` imports only itself, `tracksvc.domain`, the standard library and
  the foundation allowlist. Frameworks and drivers (`fastapi`, `starlette`, `sqlalchemy`,
  `asyncpg`, `alembic`, `aio_pika`) and every other third-party package are excluded.
- Entry points (`tracksvc.api`, and any other entry point a task introduces) are
  composition roots: they wire the layers together and may import any of them.
- `tracksvc.infrastructure` never imports from entry points.
- Other subpackages or modules of `tracksvc` (for example `config`, `bootstrap`) are
  allowed and count as outer layer: `domain` and `application` must not import them.
- `domain` and `application` use no dynamic imports (`importlib`, `__import__`).

## Scope of a change

- Implement what the task asks for and nothing more: no entities, business logic,
  endpoints or abstractions beyond it.
- The conventions below apply to the code a task requires: a rule starts to apply when a
  task introduces such code. Do **not** build abstractions ahead for later tasks.

## Domain

- Business invariants are enforced in the domain layer, each in exactly one place.
  Routes, schemas and use cases do not re-check or duplicate them.
- Request-shape validation of individual fields (types, ranges, formats) belongs to the
  API schemas and is not an invariant. Rules that depend on stored state or on relations
  between values are domain invariants.
- Value objects are immutable and compared by value.
- An entity never exposes its internal collections in mutable form. Its state changes
  only through its own operations.
- An operation checks every invariant first and changes state only afterwards. Never
  mutate and then validate or roll back.
- Domain errors carry no HTTP status codes and know nothing about transport or storage.

## Application

- Business logic lives in use cases in the application layer.
- A use case receives its dependencies explicitly from outside; it neither constructs
  them nor reaches for global objects.
- A use case depends on storage through an abstraction declared in an inner layer
  (`domain` or `application`) and implemented in `infrastructure`. Replacing the
  implementation must require no change in the use case.
- The application layer contains no HTTP concepts (status codes, request or response
  objects) and no storage details (SQL, sessions, table names).

## API

- The API layer only translates HTTP to use case calls and back; it holds no business logic.
- Domain and application errors are translated to HTTP status and error body in one
  place in the API layer (the central error mapping), not in individual handlers.
- Operational endpoints (liveness, readiness) are not business features and are the exception
  to the unit-of-work rule: no use case, no request session. The API calls an infrastructure probe
  that takes a short-lived connection from the engine with a bounded timeout, releases it itself,
  catches driver errors and returns whether the database is ready. The API turns "not ready" into
  the error response through the central error mapping; no application error is needed.

## Resources and lifecycle

- No I/O at import time. Importing any module opens no connection.
- The database engine (and its pool) is created at most once per process by its lifespan owner and
  disposed at shutdown. There are no module-level engine, connection or session objects.
- One database session / unit of work per request or message. It is provided by a single provider
  in the composition root; nothing else creates engines or sessions. API code does not use session
  APIs beyond this wiring. The session is released on every path, including errors.
- Every long-lived resource (the engine, any client connection) has exactly one explicit
  owner that creates it and releases it — at shutdown, and whenever the resource is
  replaced. Nothing is leaked.

## Persistence and transactions

- Domain classes are not ORM-mapped classes. Conversion between domain objects and ORM
  rows is explicit and kept in one place in `infrastructure`, not scattered through query
  code. A separate mapper class is not required.
- Storage errors are translated to domain or application errors at the infrastructure
  boundary. Driver and ORM exception types are never handled outside `infrastructure`.
- One unit of work — one request or one message — is one database transaction. **The use
  case owns it**: it begins the transaction and commits or rolls back through a
  unit-of-work abstraction declared in an inner layer and implemented in
  `infrastructure`. That is the only place where a transaction is begun or ended:
  repositories and entry-point handlers never commit or roll back.
- Every path ends in commit or rollback and releases the connection. The commit is
  finished before the response is returned.
- Everything written within one unit of work goes through that single transaction. Never
  get the effect of one transaction with two commits in a row.
- The schema changes only through Alembic revisions with explicit, readable operations
  that stay consistent with the ORM models. The application never creates or alters
  database objects and never calls `create_all`.

## Docstrings

- Every public class, method and function has a docstring (Google style) that is short
  and meaningful: it states the purpose or contract — what, not how — and does not restate
  the name. Update it when behaviour changes; never leave stale or misleading text.
- Tests and migrations are exempt.

## Tests

- Tests live in `tests/`. Every task adds tests for the behaviour it introduces.
- When the checks run, `DATABASE_URL` points to a reachable **empty, unmigrated** database
  and `RABBITMQ_URL` to a reachable broker. Tests prepare and clean up their own state
  (for example, run the migrations in a fixture) and do not depend on execution order or
  on leftovers of earlier runs.
- When behaviour changes, update the affected tests in the same change. Remove tests
  whose behaviour is no longer required. Moving or renaming tests is fine.
- Never delete, skip (`skip`, `xfail`) or weaken a test (for example by removing assertions) to
  make the suite pass. A failing test means the code or the test's expectation has to be fixed.

---

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
