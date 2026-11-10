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
