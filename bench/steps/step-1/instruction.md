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

# Task — step 1: service skeleton

The conventions above are the content of `/app/AGENTS.md`. They apply to this task.

## Starting point

`/app` is a git repository with `pyproject.toml`, `uv.lock`, an installed `.venv`,
`ruff.toml`, `mypy.ini` and `AGENTS.md`; there is no source code yet. The dependencies
are installed. `DATABASE_URL` points to an existing, empty database. The `src/` layout is
configured: the package is importable via `uv run` once created. Network access is
available for `uv add`.

## Goal

Create the skeleton of the `tracksvc` service that later tasks build on: the layer
packages, a FastAPI application with two service endpoints, database engine wiring, and
Alembic with a baseline migration.

## Requirements

1. **Package.** An importable package `tracksvc` in `/app/src/tracksvc` with importable
   subpackages `domain`, `application`, `infrastructure` and `api`.
2. **Application object and start command.** The ASGI application is the object `app` in
   module `tracksvc.api.app`. The service is always started from `/app` with exactly:
   `uv run uvicorn tracksvc.api.app:app --host 0.0.0.0 --port 8000`
3. **`GET /health`** → `200` with body `{"status": "ok"}`, always, while the process is
   up. It performs no I/O. It must answer within 30 s of the start command.
4. **`GET /ready`** → performs a `SELECT 1` round-trip to PostgreSQL. On success: `200`
   with body `{"status": "ready"}`. Whenever the round-trip does not succeed (any
   failure): `503` with the JSON body below. Use a bounded connect/query timeout so that
   `/ready` answers within 10 s in both cases, even when the database does not respond at
   all — also when it stops responding while the service is already running.
   ```json
   {"error": {"code": "database_unavailable", "message": "<any non-empty string>"}}
   ```
   `error` has exactly the keys `code` and `message`. All bodies are `application/json`.
   `/ready` is an operational endpoint, not a business feature, and the exception to the
   unit-of-work rule: the handler calls a readiness probe in `infrastructure` directly,
   without a use case. The probe takes a short-lived connection from the engine (not a
   request session), runs the query under that bounded timeout and releases the connection
   itself. It catches driver errors and returns whether the database is ready; the API
   turns "not ready" into the `503` through its central error mapping. No error class in
   `application` is needed: the probe and this mapping are part of the skeleton.
5. **Starts without PostgreSQL.** When `DATABASE_URL` points to an unreachable address
   (for example a closed port), the process still starts and stays up, `/health` returns
   `200` and `/ready` returns the `503` above.
6. **No RabbitMQ.** The HTTP application does not read `RABBITMQ_URL` and never connects
   to the broker; it works the same when the variable is unset or points to a closed port.
7. **Configuration.** `DATABASE_URL` is read from the process environment at start-up,
   never at import time. The service is also started with other `DATABASE_URL` values
   (another host, port or database name), so nothing about the connection may be
   hard-coded. Behaviour with `DATABASE_URL` unset is not specified; never fall back to a
   hard-coded default.
8. **No DDL from the application.** The application never creates or alters database
   objects. Started against an empty, unmigrated database, it leaves it without tables.
9. **Alembic baseline.** Alembic is configured so that `uv run alembic upgrade head`,
   run from `/app`, uses `DATABASE_URL` (and no other variable), exits with code 0 on an
   empty database and leaves table `alembic_version` with exactly one row. Running it
   again exits with code 0 and changes nothing. A baseline revision without schema
   objects is sufficient. Where the Alembic environment and revisions live is up to you.
10. **Tests.** Add tests for the behaviour of this step; `uv run pytest` passes and runs
    at least one test.

## Self-check

```bash
cd /app
# the checks come first: pytest runs against the empty, unmigrated database
uv run ruff check . && uv run mypy -p tracksvc && uv run pytest

uv run alembic upgrade head            # exit code 0; run it twice
uv run uvicorn tracksvc.api.app:app --host 0.0.0.0 --port 8000 &
curl -si http://127.0.0.1:8000/health  # 200 {"status":"ok"}
curl -si http://127.0.0.1:8000/ready   # 200 {"status":"ready"}
kill %1

# database unreachable, RABBITMQ_URL unset: the app still starts
env -u RABBITMQ_URL DATABASE_URL=postgresql+asyncpg://u:p@127.0.0.1:1/none \
  uv run uvicorn tracksvc.api.app:app --host 0.0.0.0 --port 8000 &
curl -si http://127.0.0.1:8000/health  # 200
curl -si http://127.0.0.1:8000/ready   # 503 database_unavailable
kill %1
```

The service is migrated and started only with the commands of requirements 2 and 9, never
with your own scripts or Makefile targets. Do not rely on a process you left running.

## Out of scope

- Domain entities, value objects and any business logic.
- Use cases, a unit of work, repositories or other storage abstractions: none of them is
  needed at this step, and nothing is built ahead for later tasks.
- Endpoints other than `/health` and `/ready`.
- Database tables and ORM models.
- A message consumer or any other code that talks to RabbitMQ.
- Installing or starting infrastructure; changing `ruff.toml` or `mypy.ini`.
