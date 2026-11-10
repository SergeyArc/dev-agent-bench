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
