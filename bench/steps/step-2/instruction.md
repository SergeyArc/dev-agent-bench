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

# Task — step 2: create and read a track

The conventions above are the content of `/app/AGENTS.md`. They apply to this task.

## Starting point

`/app` holds the service built so far: the `tracksvc` package with its layers, the FastAPI
application with `/health` and `/ready`, and Alembic. Everything that works now must keep
working, with the same start and migration commands. `DATABASE_URL` points to an existing,
empty database. Network access is available for `uv add`.

## Goal

Add the first business feature: a **track** can be created and read over HTTP. The logic is
a use case in the application layer with explicitly passed dependencies. At this step
tracks are kept in process memory.

## Requirements

1. **Track.** A track has `id` (UUID generated by the service on creation; two tracks never
   share one), `code` (string, unique among all tracks, case-sensitive), `capacity`
   (integer), `status` (`"open"` on creation) and `segments` (empty on creation). No
   operation of this task changes a track after creation. Its JSON representation, used in
   every success response (these keys are required, extra keys are allowed):
   ```json
   {"id": "0b9f6c1e-5f0a-4e0e-9d57-3c8f5a1f2b11", "code": "TRK-A1", "capacity": 100,
    "status": "open", "segments": []}
   ```
   `id` is the canonical lower-case hyphenated UUID string.
2. **`POST /tracks`** with body `{"code": <string>, "capacity": <integer>}` creates a
   track → `201` with the track: the sent `code` and `capacity`, `status` `"open"`,
   `segments` `[]`.
3. **`GET /tracks/{track_id}`** → `200` with the track; the body equals the response that
   created it. No track with this id → `404` with code `track_not_found`.
4. **Request validation** → `422` with code `validation_error`. It depends only on the
   request, never on stored tracks:
   - the body of `POST /tracks` must be valid JSON and a JSON object with both fields;
   - `code` is a JSON string matching `^[A-Z][A-Z0-9-]{2,31}$`;
   - `capacity` is a JSON integer from 1 to 10000;
   - JSON types are exact, nothing is coerced: a string such as `"5"` for an integer, a
     boolean, `null` or a non-integral number such as `5.5` is a validation error, and so
     is a non-string `code`;
   - `track_id` in the path must parse as a UUID (`GET /tracks/not-a-uuid` → `422`).

   Not specified: unknown extra body fields, integral floats such as `5.0`, upper-case
   UUIDs, unknown routes and unsupported methods.
5. **Duplicate code.** `POST /tracks` with a `code` that an existing track already has →
   `409` with code `track_code_taken`. The existing track stays unchanged and no second
   track is created. The clash is found through the storage abstraction of requirement 8
   and raised as a domain or application error, not as an HTTP error.
6. **One error body** for every non-2xx response of these routes (and of `/ready`):
   ```json
   {"error": {"code": "validation_error", "message": "<any non-empty string>",
              "details": [{"field": "capacity", "message": "<any string>"}]}}
   ```
   - `details` is present **only** for `validation_error`; for every other code `error`
     has exactly the keys `code` and `message`.
   - `details` is a non-empty array; each item has string `field` and `message`. At least
     one item's `field` names the offending location: the dotted path of the body field
     without any prefix (`code`, `capacity`), the name of the path parameter (`track_id`),
     or exactly `body` when the body is not valid JSON or is not a JSON object. A missing
     field is reported under its own name. A complete list of violations is not required.
   - FastAPI's default validation response does not have this shape: replace it. All
     errors, validation included, are built in the central error mapping of the API layer.
7. **Precedence and no 500.** When several classes apply, the response is chosen in the
   order validation → not found → conflict. No request described here ever produces `500`.
8. **Storage in process memory.** Tracks live in the memory of the process. Use cases reach
   them through a storage abstraction declared in `application` or `domain`; its in-memory
   implementation is in `infrastructure`. There is exactly one instance per process: the
   composition root (`tracksvc.api`) creates it at start-up, owns it and passes it to the
   use cases explicitly; it is not a module-level global. Data is lost on restart and each
   process has its own — this is expected. Because tracks are not written to PostgreSQL,
   the rules of `AGENTS.md` about a database session, a unit of work and transactions do
   not apply to the track routes: none of them exists at this step.
9. **Tests.** Add tests for the behaviour of this step; `uv run pytest` passes.

## Self-check

```bash
cd /app
uv run ruff check . && uv run mypy -p tracksvc && uv run pytest

uv run alembic upgrade head
uv run uvicorn tracksvc.api.app:app --host 0.0.0.0 --port 8000 &
H='content-type: application/json'; U=http://127.0.0.1:8000/tracks
curl -si $U -H "$H" -d '{"code":"TRK-A1","capacity":100}'    # 201, track with an id
curl -si $U/<id from the response>                           # 200, the same body
curl -si $U -H "$H" -d '{"code":"TRK-A1","capacity":5}'      # 409 track_code_taken
curl -si $U -H "$H" -d '{"code":"TRK-B2","capacity":"5"}'    # 422, details field "capacity"
curl -si $U -H "$H" -d '[1]'                                 # 422, details field "body"
curl -si $U/not-a-uuid                                       # 422, details field "track_id"
curl -si $U/5a2f0c4e-0000-4000-8000-000000000000             # 404 track_not_found
kill %1
```

The service is migrated and started only with the commands above, never with your own
scripts or Makefile targets. Do not rely on a process you left running.

## Out of scope

- Database tables, ORM models and new Alembic revisions for tracks; a unit of work,
  database sessions and transactions for the track routes. The storage is in memory, and
  nothing is built ahead for later tasks.
- Operations on segments or on the status of a track; updating, deleting or listing tracks.
- Endpoints other than `POST /tracks` and `GET /tracks/{track_id}` (plus the existing
  `/health` and `/ready`).
- A message consumer or any other code that talks to RabbitMQ.
- Installing or starting infrastructure; changing `ruff.toml` or `mypy.ini`.
