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

# Task — step 3: segments and sealing

The conventions above are the content of `/app/AGENTS.md`. They apply to this task.

## Starting point

`/app` holds the service built so far: tracks can be created and read over HTTP, errors
have one JSON body, and tracks are kept in process memory behind a storage abstraction.
Everything that works now must keep working, with the same start and migration commands.

## Goal

Turn the track into an aggregate with rules: named, non-overlapping **segments** are
reserved on a track, removed from it, and the track can be **sealed**. Every rule is
enforced by the domain model and is visible over HTTP through a defined error code.

## Requirements

1. **Model.** A segment has a `label` (unique inside its track) and a `span`. A span is the
   half-open interval `[start, end)` of integers. Two spans overlap iff
   `a.start < b.end and b.start < a.end`; spans that only touch (`a.end == b.start`) do
   **not** overlap. The span is a value object: immutable, compared by value. The segments
   of a track change only through the track's own operations; the track exposes no mutable
   collection. In the track JSON `segments` is **always ordered by `span.start`
   ascending**, whatever the insertion order:
   ```json
   {"id": "…", "code": "TRK-A1", "capacity": 100, "status": "open", "segments": [
     {"label": "alpha", "span": {"start": 0, "end": 10}},
     {"label": "beta", "span": {"start": 10, "end": 25}}]}
   ```
2. **Routes.** Each success response is the updated track.

   | Method and path | Request body | Success |
   |---|---|---|
   | `POST /tracks/{track_id}/segments` | `{"label": <string>, "span": {"start": <integer>, "end": <integer>}}` | `201` |
   | `DELETE /tracks/{track_id}/segments/{label}` | none | `200`, track without the segment |
   | `POST /tracks/{track_id}/seal` | none | `200`, track with `status` `"sealed"` |

   The `DELETE` and seal routes have no request body: it is not read and not validated.
   A span freed by `DELETE` can be reserved again. `GET` shows the sealed status.
3. **Request validation** → `422 validation_error`, same rules as before (exact JSON types,
   no coercion; `details[].field` is the dotted body path or the path parameter name):
   - the body of the add route is a JSON object with `label` and `span`; `span` is an
     object with `start` and `end` (fields `label`, `span`, `span.start`, `span.end`, `body`);
   - `label` — in the body and in the path — matches `^[a-z][a-z0-9_]{0,15}$`;
   - `span.start` and `span.end` are JSON integers from 0 to 10000;
   - `track_id` parses as a UUID.

   `start < end` and `end <= capacity` are **not** request validation: they are invariants
   of the model and give `409`, never `422`.
4. **Invariants** — always true for every stored track. A request that would break one is
   rejected with `409` and the code below:

   | Invariant | Code |
   |---|---|
   | a span is non-empty: `start < end` (so `start >= end` is rejected) | `invalid_span` |
   | a span fits the track: `end <= capacity` (`end == capacity` is accepted) | `span_out_of_bounds` |
   | labels are unique within a track (the same label in another track is fine) | `segment_label_taken` |
   | no two segments of a track overlap — partially, containing, contained or identical | `segment_overlap` |
   | a track has at most 8 segments (the 8th is accepted, the 9th is rejected) | `segment_limit_reached` |
   | a sealed track is never modified: adding, removing and sealing are all rejected | `track_sealed` |
   | a track is sealed only if it has at least 1 segment (it stays `open` otherwise) | `track_empty` |

   Each of these seven invariants is enforced in the domain layer, in exactly one place —
   not in routes, schemas or use cases — and is checked before any state changes.
5. **Not found** → `404`: `track_not_found` when no track has this id (all three routes);
   `segment_not_found` when the open track has no segment with the label given to `DELETE`.
6. **Precedence**: validation → track not found → everything else. A schema-invalid body
   sent to the add route is `422` even when the track does not exist. When one request
   breaks several invariants at once, any of the applicable codes is acceptable.
7. **A rejected request has no effect.** After any non-2xx response nothing in any track
   has changed: `GET /tracks/{track_id}` returns the same body as before the request.
8. **Errors.** The new codes use the existing error body (`error` has exactly `code` and
   `message`; `details` only for `validation_error`). Domain errors know nothing about
   HTTP: the central error mapping of the API layer produces status and body. Never `500`.
9. **Storage stays in process memory**, behind the same abstraction, one instance per
   process. There is still no database session, unit of work or transaction for tracks.
10. **Tests.** Add tests for the behaviour of this step; `uv run pytest` passes.

## Self-check

```bash
cd /app
uv run ruff check . && uv run mypy -p tracksvc && uv run pytest

uv run alembic upgrade head
uv run uvicorn tracksvc.api.app:app --host 0.0.0.0 --port 8000 &
H='content-type: application/json'; U=http://127.0.0.1:8000/tracks
curl -si $U -H "$H" -d '{"code":"TRK-A1","capacity":100}'    # 201; T=$U/<id>
add() { curl -si $T/segments -H "$H" -d "{\"label\":\"$1\",\"span\":{\"start\":$2,\"end\":$3}}"; }
curl -si $T/seal -X POST                                     # 409 track_empty
add beta 10 25; add alpha 0 10                               # 201, 201; alpha listed first
add gamma 5 12                                               # 409 segment_overlap
add gamma 9 9; add gamma 90 101                              # 409 invalid_span, span_out_of_bounds
add Gamma 30 40                                              # 422, details field "label"
curl -si $T/segments/beta -X DELETE                          # 200, only alpha left
curl -si $T/seal -X POST                                     # 200, status "sealed"
curl -si $T/segments/alpha -X DELETE                         # 409 track_sealed
kill %1
```

The service is migrated and started only with the same commands as before, never with
your own scripts or Makefile targets. Do not rely on a process you left running.

## Out of scope

- Database tables, ORM models and new Alembic revisions for tracks; a unit of work,
  sessions and transactions for the track routes. Nothing is built ahead for later tasks.
- Changing `code` or `capacity`; updating a segment in place; deleting or listing tracks.
- Code that talks to RabbitMQ; starting infrastructure; changing `ruff.toml` or `mypy.ini`.
