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

# Task — step 4: PostgreSQL persistence

The conventions above are the content of `/app/AGENTS.md`. They apply to this task.

## Starting point

`/app` holds the service built so far: tracks with segments and sealing, served over HTTP
with one error body, kept in process memory behind a storage abstraction. Everything that
works now must keep working: every route, status, error code and precedence rule stays as
it is. `DATABASE_URL` points to an existing, empty database. Network access is available
for `uv add`.

## Goal

Store tracks in PostgreSQL instead of process memory: a schema created by Alembic, a
storage implementation with an explicit mapping between domain objects and rows, and one
transaction per request. The HTTP behaviour does not change.

## Requirements

1. **PostgreSQL replaces the in-memory storage.** The running application reads and writes
   tracks only in PostgreSQL, through an implementation of the storage abstraction in
   `infrastructure`, and keeps no copy of them in memory (no cache): every request reads the
   rows. Remove the in-memory storage from the application; a test double in `tests/` may stay.
2. **Conventions that now apply.** From this step the rules of `AGENTS.md` on persistence
   and transactions and on one database session / unit of work per request apply to the
   track routes: the use case owns the transaction through a unit-of-work abstraction; a
   single provider in the composition root supplies the session; handlers construct no
   engines or sessions; the session is released on every path, including errors. Changing
   the existing use cases for this is expected. `/ready` stays as it is.
3. **Schema**, in schema `public`, created only by Alembic revisions:

   | Table | Required columns | Required constraints |
   |---|---|---|
   | `tracks` | `id` (uuid), `code` (text/varchar), `capacity` (integer), `status` (text/varchar) | primary key `id`; unique `code` |
   | `track_segments` | `track_id` (uuid), `label` (text/varchar), `span_start` (integer), `span_end` (integer) | foreign key `track_id` → `tracks.id`; unique (`track_id`, `label`) |

   Extra columns, tables and indexes are allowed. The revision has explicit, readable
   operations and matches the ORM models; nothing is created through `create_all`, and the
   application itself still creates or alters no database objects.
4. **Migration commands**, run from `/app`, using `DATABASE_URL` only:
   `uv run alembic upgrade head` on an empty database exits 0, creates the schema and is
   idempotent; `uv run alembic downgrade base` exits 0, and a following
   `uv run alembic upgrade head` exits 0 and recreates the schema.
5. **Saved means durable.** Once a request has returned 2xx, its result is returned by
   `GET` from **any** application process on the same database: a second process running
   at the same time, and a process started after all earlier ones were killed with
   `SIGKILL` (a track that was created, given segments and sealed comes back unchanged).
   After the database is dropped, recreated and migrated, earlier ids give
   `404 track_not_found`.
6. **Changes made elsewhere are visible.** A change committed by another process is seen
   by the next `GET`: for example, after `UPDATE tracks SET capacity = …` run directly in
   SQL, the next `GET /tracks/{track_id}` returns the new capacity.
7. **Stored state.** After every successful change there is exactly one `tracks` row per
   track, with `code`, `capacity` and `status` equal to the values the API returns, and
   the `track_segments` rows of the track equal its current segments (`label`,
   `span_start`, `span_end`): one row per segment, no leftovers after a removal. How the
   rows are written is up to you.
8. **One request = one transaction.** Either all effects of a request are committed
   before its 2xx response is sent, or none are. After any non-2xx response the rows of
   `tracks` and `track_segments` are identical to those before the request.
9. **Concurrency.** Two patterns must hold, with the database (not a check in application
   memory) as the arbiter:
   - of N simultaneous `POST /tracks` with the same `code` (for example 10), exactly one
     returns `201` and all the others `409 track_code_taken`; one row exists;
   - of N simultaneous add-segment requests to one track with distinct labels and the
     same span, exactly one returns `201` and all the others `409 segment_overlap`; one
     segment row exists.

   Neither produces `500`. Other races are not specified.
10. **Not found.** An unknown id gives `404 track_not_found` on every route — never a 2xx
    with `null`, never a `500`.
11. **Storage errors do not leak.** Driver and ORM exceptions are translated to domain or
    application errors inside `infrastructure`; no response carries driver or ORM exception
    text as its `code`. Domain classes are not ORM-mapped classes: conversion between
    domain objects and rows is explicit and kept in one place.
12. **Still true.** The application starts and answers `/health` when PostgreSQL is
    unreachable (the track routes are then not specified) and never reads `RABBITMQ_URL`.
    Several application processes may run against one database.
13. **Tests.** Update the tests to the new storage and add tests for this step. `uv run
    pytest` runs against an empty, unmigrated database: tests migrate it and clean up.

## Self-check

```bash
cd /app
uv run ruff check . && uv run mypy -p tracksvc && uv run pytest

uv run alembic upgrade head && uv run alembic downgrade base && uv run alembic upgrade head
uv run uvicorn tracksvc.api.app:app --host 0.0.0.0 --port 8000 &
H='content-type: application/json'; U=http://127.0.0.1:8000/tracks
curl -si $U -H "$H" -d '{"code":"TRK-A1","capacity":100}'    # 201; T=$U/<id>
curl -si $T/segments -H "$H" -d '{"label":"alpha","span":{"start":0,"end":10}}'   # 201
curl -si $T/seal -X POST                                     # 200
kill -9 %1                                                   # SIGKILL, then start again:
uv run uvicorn tracksvc.api.app:app --host 0.0.0.0 --port 8000 &
curl -si $T                                                  # 200, the same sealed track
# UPDATE tracks SET capacity = 500 in SQL → the next GET shows 500
kill %1
```

The service is migrated and started only with these commands, never with your own
scripts or Makefile targets. Do not rely on a process you left running.

## Out of scope

- New routes, fields, error codes or any change of the HTTP behaviour.
- A message consumer or any code that talks to RabbitMQ; tables this task does not need;
  caching, query optimisation. Nothing is built ahead for later tasks.
- Installing or starting infrastructure; changing `ruff.toml` or `mypy.ini`.
