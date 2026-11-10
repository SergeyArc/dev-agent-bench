# dev-agent-bench — external contract of the service under test (tracksvc)

Black-box contract of the service the evaluated agent builds over five bench steps.
Hidden tests, oracle solutions and step instructions are written **only** against this
document; architecture conventions live in `AGENTS.md` (§2.3). Key words MUST / MUST
NOT / MAY are normative. Anything listed as *unspecified* is never tested and may
behave arbitrarily.

## 1. Steps and cascade

| Step | ID | Intent | Sections that activate |
|---|---|---|---|
| 1 | L1-01 | Layered skeleton, FastAPI app, DB session, Alembic baseline | §2, §4.1, §6.1 |
| 2 | L2-01 | Use case + DI: create and read a track | §3.1, §4.2, §4.3 (create, get), §5 (validation, not found, code conflict) |
| 3 | L2-02 | Aggregate, value object, invariants | §3.2, §4.3 (segments, seal), §5 (domain errors) |
| 4 | L3-01 | PostgreSQL persistence, mapping, transactions | §6.2–§6.4 |
| 5 | L3-02 | AMQP consumer: idempotency, retry, reconnect | §7, §2.2 (worker) |

**Cascade rule.** Each step starts from the working copy left by the previous step, in a
new agent session. After step N every requirement activated by steps 1…N MUST hold.
The only superseded behaviour: before step 4 track state MAY live in process memory
(lost on restart); from step 4 on it MUST live in PostgreSQL (§6).

## 2. Runtime interface (what the verifier relies on)

The verifier stops any process left by the agent, resets state, and starts the service
itself. It never calls agent-written scripts or Makefile targets.

### 2.1 Environment

| Item | Value |
|---|---|
| Working directory | `/app` (git repository; `pyproject.toml`, `uv.lock`, `.venv`, `ruff.toml`, `mypy.ini` pre-provisioned) |
| `DATABASE_URL` | `postgresql+asyncpg://<user>:<pass>@<host>:<port>/<db>` |
| `RABBITMQ_URL` | `amqp://<user>:<pass>@<host>:<port>/<vhost>` |
| `CONSUMER_MAX_RETRIES` | optional integer ≥ 0, default `3` (§7.4) |

Variables are read from the process environment at start-up, per process:

| Process | Reads |
|---|---|
| Alembic (`alembic upgrade head` / `downgrade base`) | `DATABASE_URL` only |
| HTTP app | `DATABASE_URL` only |
| Worker | `DATABASE_URL`, `RABBITMQ_URL`, `CONSUMER_MAX_RETRIES` |

A process MUST work when a variable it does not read is unset. Host, port, database
name and credentials MUST NOT be hard-coded: the verifier passes other values (a
freshly created database, a toxiproxy port, an unreachable port).

Tool configuration: ruff and mypy are configured only in `ruff.toml` and `mypy.ini`
(not in `pyproject.toml`); both files MUST stay byte-identical to the initial commit
(C7). The agent MAY change `pyproject.toml` and `uv.lock` (adding dependencies).

### 2.2 Commands

| Purpose | Command (run in `/app`) | Requirement |
|---|---|---|
| Migrate | `uv run alembic upgrade head` | exit code 0; idempotent; uses `DATABASE_URL` |
| HTTP app | `uv run uvicorn tracksvc.api.app:app --host 0.0.0.0 --port 8000` | `GET /health` → 200 within 30 s |
| Worker (step 5) | `uv run python -m tracksvc.worker` | consuming within 30 s (§7.1); exits within 10 s of `SIGTERM` |

Process rules:
- The HTTP app MUST start and answer `/health` even when PostgreSQL is unreachable, and
  MUST NOT need RabbitMQ at any step: it does not read `RABBITMQ_URL` and never
  connects to the broker.
- The HTTP app MUST NOT create or alter database objects; only Alembic does.
- The worker requires a migrated database and a reachable broker at start-up; its
  behaviour when either is unreachable *at start-up* is unspecified.
- Several HTTP app and worker processes MAY run at once against the same database.
- Verifier reset = stop processes → drop and recreate the database → delete the
  contract's queues and exchange → migrate → start. No state outside PostgreSQL and
  RabbitMQ may be needed to start (no local data files).

### 2.3 Prescribed internals (complete list, each with its reason)

| Prescribed | Why it cannot be left free |
|---|---|
| Importable package `tracksvc`, source layout `/app/src/tracksvc` | Start commands and static checks need one fixed root |
| ASGI object `tracksvc.api.app:app` | The verifier starts the app itself (fixed start command and port) |
| Entry module `tracksvc.worker` (runnable with `-m`) | Same, for the consumer process |
| Layer packages `tracksvc.domain`, `tracksvc.application`, `tracksvc.infrastructure`, `tracksvc.api` | Layer rule is a required-pass deterministic criterion (import-linter) |
| Alembic configuration usable as `alembic upgrade head` from `/app` | Schema creation command is part of the reset procedure |
| Table and column names in §6.2 and §7.3 | SQL row checks need fixed names |

Layer rule (C3: import-linter for layer contracts + an AST check of `domain`/`application` imports against `sys.stdlib_module_names`):
- **Foundation allowlist** (treated like the standard library): `pydantic`, `typing_extensions`.
- `tracksvc.domain` may import only itself, the standard library and the foundation allowlist.
- `tracksvc.application` may import only itself, `tracksvc.domain`, the standard library
  and the foundation allowlist. Frameworks and infrastructure (`sqlalchemy`, `fastapi`,
  `starlette`, `aio_pika`, `asyncpg`, `alembic`) and any other distribution are excluded.
- `tracksvc.infrastructure` imports nothing from `tracksvc.api`, `tracksvc.worker`.
- `tracksvc.worker` and `tracksvc.api` are composition roots and may import any layer.
- `domain` and `application` MUST NOT use dynamic imports (`importlib`, `__import__`).

Extra subpackages or modules of `tracksvc` (e.g. `config`, `bootstrap`) are allowed and
count as **outer layer**: by the rule above `domain` and `application` cannot import
them. The location of the Alembic environment and revisions is free.

**Two levels.** This contract fixes only the verifier interface (the table above, §2.1,
§2.2, §4–§7). **Architecture is governed by the project conventions in `AGENTS.md`**
(structure inside the layers, DI, transaction owner, mapping, consumer structure);
adherence is assessed by the `[judge]` criteria of §9. Stated openly: the benchmark
measures adherence to stated project conventions (a convention benchmark).

## 3. Domain

Neutral synthetic domain: a **Track** is a numbered strip of `capacity` units on which
named, non-overlapping **Segments** are reserved. Each segment occupies a **Span**.

### 3.1 Track (aggregate root)

| Field | Type | Rules |
|---|---|---|
| `id` | UUID string (canonical lower-case, hyphenated) | generated by the service on creation; immutable |
| `code` | string | regex `^[A-Z][A-Z0-9-]{2,31}$`; unique among all tracks (case-sensitive); immutable |
| `capacity` | integer | 1 … 10000; changed only by the consumer (§7) |
| `status` | string | `"open"` on creation, `"sealed"` after seal; never returns to `open` |
| `segments` | array of Segment | empty on creation; always ordered by `span.start` ascending |

*Model notes:* "immutable" for `id` / `code` and "never returns to `open`" describe the
model only — no operation of this contract could change them and no criterion depends
on them.

### 3.2 Segment (child) and Span (value object)

| Field | Type | Rules |
|---|---|---|
| `label` | string | regex `^[a-z][a-z0-9_]{0,15}$`; identifies the segment inside its track |
| `span.start` | integer | 0 … 10000 |
| `span.end` | integer | 0 … 10000 |

A span is the half-open interval `[start, end)`. Two spans overlap iff
`a.start < b.end and b.start < a.end`; spans that only touch (`a.end == b.start`) do
not overlap.

Invariants (always true for every stored track):

| # | Invariant | Error code when a request would break it |
|---|---|---|
| I1 | Span is non-empty: `start < end` | `invalid_span` |
| I2 | Span fits the track: `end <= capacity` | `span_out_of_bounds` |
| I3 | Labels are unique within a track | `segment_label_taken` |
| I4 | No two segments of a track overlap | `segment_overlap` |
| I5 | A track has at most 8 segments | `segment_limit_reached` |
| I6 | A sealed track is never modified (add, remove, seal, extend) | `track_sealed` |
| I7 | A track can be sealed only if it has at least 1 segment | `track_empty` |
| I8 | `capacity` never exceeds 10000 | `capacity_limit_exceeded` (§7 only) |

**Atomicity.** A rejected request MUST have no effects: it changes nothing in any
track. The check "a following `GET` returns the same body as before the request"
applies only when the track has no concurrent writers.

## 4. HTTP API

Request bodies of R1 / R3 and all response bodies are JSON
(`Content-Type: application/json`), UTF-8. Port 8000.

### 4.1 Service endpoints (step 1)

| Route | Response |
|---|---|
| `GET /health` | always `200 {"status": "ok"}` while the process is up; performs no I/O |
| `GET /ready` | `200 {"status": "ready"}` if a `SELECT 1` round-trip to PostgreSQL succeeds; otherwise `503` with error code `database_unavailable` (§5). MUST answer within 10 s |

### 4.2 Track representation

```json
{
  "id": "0b9f6c1e-5f0a-4e0e-9d57-3c8f5a1f2b11",
  "code": "TRK-A1",
  "capacity": 100,
  "status": "open",
  "segments": [
    {"label": "alpha", "span": {"start": 0, "end": 10}},
    {"label": "beta",  "span": {"start": 10, "end": 25}}
  ]
}
```
These keys are the required minimum; extra keys are allowed and ignored by tests.

### 4.3 Routes

| # | Step | Method and path | Request body | Success | Possible error codes |
|---|---|---|---|---|---|
| R1 | 2 | `POST /tracks` | `{"code": str, "capacity": int}` | `201` Track | `validation_error`, `track_code_taken` |
| R2 | 2 | `GET /tracks/{track_id}` | — | `200` Track | `validation_error`, `track_not_found` |
| R3 | 3 | `POST /tracks/{track_id}/segments` | `{"label": str, "span": {"start": int, "end": int}}` | `201` Track (updated) | `validation_error`, `track_not_found`, `track_sealed`, `invalid_span`, `span_out_of_bounds`, `segment_label_taken`, `segment_overlap`, `segment_limit_reached` |
| R4 | 3 | `DELETE /tracks/{track_id}/segments/{label}` | — | `200` Track (updated) | `validation_error`, `track_not_found`, `track_sealed`, `segment_not_found` |
| R5 | 3 | `POST /tracks/{track_id}/seal` | — | `200` Track (`status: "sealed"`) | `validation_error`, `track_not_found`, `track_sealed`, `track_empty` |

Request validation (schema level, independent of stored state):
- R4 and R5 have no request body: it is not read and not validated; tests send none.
- R1 / R3 body MUST be a JSON object with all listed fields; field types are exact JSON types:
  a string where an integer is expected (`"5"`), a boolean, `null`, or a non-integral
  number is a validation error.
- Ranges and regexes of §3.1–§3.2 apply to `code`, `capacity`, `label`, `span.start`,
  `span.end`.
- `track_id` in the path MUST parse as a UUID; `label` in the path MUST match its regex.
- `start < end` and `end <= capacity` are **not** schema rules: they are invariants I1
  and I2 and produce domain errors.
- Unspecified: unknown extra body fields, integral floats such as `5.0`, upper-case
  UUIDs, unknown routes and unsupported methods.

## 5. Errors

Single error body for every non-2xx response defined here:

```json
{"error": {"code": "segment_overlap", "message": "non-empty string", "details": [{"field": "span.start", "message": "..."}]}}
```
- `code`: one of the table below. `message`: non-empty string, content unspecified.
- `details`: present only for `validation_error`; non-empty array; each item has
  string `field` and `message`. At least one item's `field` MUST equal the offending
  location: dotted body path (`capacity`, `span.end`), path parameter name
  (`track_id`, `label`), or `body` when the body is not a JSON object / not valid JSON.
  Tests violate one field at a time; one matching item is sufficient (a complete list
  of violations is not required).

| Class | Meaning | Status | Codes |
|---|---|---|---|
| Validation | Request is malformed regardless of stored state | 422 | `validation_error` |
| Not found | Addressed resource does not exist | 404 | `track_not_found`, `segment_not_found` |
| Conflict | Uniqueness clash with another stored track | 409 | `track_code_taken` |
| Domain | Well-formed request rejected by an invariant of §3 | 409 | `track_sealed`, `track_empty`, `invalid_span`, `span_out_of_bounds`, `segment_label_taken`, `segment_overlap`, `segment_limit_reached` |
| Unavailable | Dependency down (`/ready` only) | 503 | `database_unavailable` |

Precedence between classes: **validation → not found (track) → domain / conflict**.
Hidden tests break exactly one domain rule per request; when a request breaks several
domain rules at once, any of the applicable domain codes is accepted. `segment_not_found`
is never combined with `track_sealed` in tests.

No scenario of this contract may produce status `500`. Behaviour of R1–R5 while
PostgreSQL is unreachable is unspecified.

## 6. Persistence

### 6.1 Baseline (step 1)
`uv run alembic upgrade head` on an empty database exits 0 and leaves table
`alembic_version` with exactly one row. Running it again exits 0 and changes nothing.
Whether the step-1 baseline revision creates other objects is unspecified.

### 6.2 Schema (step 4)
All tables named in this contract live in schema `public`. After `alembic upgrade head`
it contains at least:

| Table | Required columns | Required constraints |
|---|---|---|
| `tracks` | `id` (uuid), `code` (text/varchar), `capacity` (integer), `status` (text/varchar) | primary key `id`; unique `code` |
| `track_segments` | `track_id` (uuid), `label` (text/varchar), `span_start` (integer), `span_end` (integer) | foreign key `track_id → tracks.id`; unique (`track_id`, `label`) |

Extra columns, tables and indexes are allowed. `alembic downgrade base` exits 0 and a
subsequent `alembic upgrade head` exits 0 and recreates the schema.

### 6.3 Meaning of "saved" (step 4)
- A track is saved when the request that changed it returned 2xx. From that moment it
  is returned by `GET` from **any** HTTP app process on the same database, including
  one started after all earlier processes were killed (`SIGKILL`).
- The HTTP app keeps no authoritative copy in memory: a change committed by another
  process (a second app instance, the worker) is visible to the next `GET`.
- After the verifier recreates the database and migrates, all earlier ids return
  `track_not_found`.

### 6.4 Stored state, conflict and not-found semantics (step 4)
- **Resulting state.** After every committed change (R1, R3–R5, §7) there is exactly
  one `tracks` row per track `id`, and the `track_segments` rows of the track equal its
  current segments: one row per segment, no leftovers. How the rows are written is free.
- **One request (or one message) = one transaction.** Either all of its effects are
  committed before the 2xx response is sent, or none are. A non-2xx response leaves no
  row of `tracks`, `track_segments`, `processed_messages` changed (other tables are
  unspecified).
- **Conflict.** Creating a track whose `code` is already stored → `track_code_taken`.
  This also holds under concurrency: of N simultaneous `POST /tracks` with the same
  `code`, exactly one returns `201`, the rest return `409 track_code_taken`.
- **Concurrent updates of one track** MUST NOT break invariants: of N simultaneous R3
  requests with distinct labels and the same span on one track, exactly one returns
  `201`, the rest return `409 segment_overlap`.
- Every race other than these two patterns (criteria 4.9, 4.10) and those of §7.3 is
  unspecified and not tested (e.g. 8th vs 9th segment, delete vs seal, add vs extend,
  different messages for one track handled by two worker processes — §7.3).
- **Not found.** Any operation addressing an unknown `id` yields `track_not_found`
  (HTTP 404, or a handler failure in §7). It is never a 2xx with `null`, never a 500.
- Storage errors MUST NOT leak: no response body or dead-letter header may contain
  driver or ORM exception text as `code`.

## 7. AMQP consumer (step 5)

### 7.1 Topology

| Object | Name | Properties |
|---|---|---|
| Exchange | `tracksvc.events` | type `direct`, durable, not auto-delete, no arguments |
| Work queue | `tracksvc.track-events` | durable, not exclusive, not auto-delete, **no arguments**; bound to the exchange with routing key `track.extend` |
| Dead queue | `tracksvc.track-events.dead` | durable, not exclusive, not auto-delete, **no arguments**; no bindings (reached through the default exchange with routing key = queue name) |

The worker declares all three at start-up. The verifier MAY have declared them
beforehand with the same properties, and MAY bind its own queues to the exchange with
the same routing key (a tap that receives every publish to the exchange). "No
arguments" excludes `x-dead-letter-exchange`, TTL, `x-queue-type` and the like.
**Ready** = the work queue reports ≥ 1 consumer. Prefetch and processing order are
unspecified.

### 7.2 Message

| Part | Requirement |
|---|---|
| Routing | published to `tracksvc.events` with routing key `track.extend` |
| Body | UTF-8 JSON object `{"track_id": "<uuid>", "delta": <integer 1…1000>}`. Field types are exact JSON types as in §4.3: `track_id` is a string that parses as a UUID, `delta` is an integer; a string such as `"5"`, a boolean (a boolean is not an integer), `null` or a non-integral number is invalid. Integral floats such as `5.0` are unspecified, as in §4.3 |
| Property `message_id` | required string of 1–255 printable ASCII characters (0x20–0x7E), i.e. 1–255 bytes; the idempotency key (global, not per track) |
| Header `x-retry-count` | optional integer ≥ 0 (a boolean is not an integer); absent means 0; number of failed attempts so far |
| Other properties | ignored by the worker |

**Effect of a successful message:** `capacity := capacity + delta` on the addressed
track, visible through `GET /tracks/{id}`.

Handler failure codes (the handler *fails* when processing cannot complete):

| Code | Cause |
|---|---|
| `track_not_found` | no track with `track_id` |
| `track_sealed` | track is sealed (I6) |
| `capacity_limit_exceeded` | `capacity + delta > 10000` (I8) |
| `internal_error` | anything else (e.g. database error) |

### 7.3 Idempotency

Table `processed_messages` with column `message_id` (text/varchar, primary key or
unique), created by the Alembic migration. A row is inserted **in the same database
transaction** as the capacity change, and only on success.

| Situation | Required observable result |
|---|---|
| First successful processing | capacity changed once; exactly one `processed_messages` row for the id |
| Message whose `message_id` is already in `processed_messages` (any body, any headers) | no capacity change; no new row; nothing published to exchange or dead queue; message acknowledged |
| Same `message_id` delivered many times concurrently, including to two worker processes | capacity changed exactly once; exactly one row |
| Failed or invalid message | no `processed_messages` row; the same `message_id` published again later is processed as new |

Across two worker processes only the same-`message_id` race above is specified (5.6);
races between **different** messages for the same track are unspecified and not tested.

### 7.4 Outcomes, retry, dead-lettering

Let `c` = `x-retry-count` of the delivery (0 if absent) and `MAX` =
`CONSUMER_MAX_RETRIES` (default 3). Headers of a delivery cannot be changed on requeue,
therefore retry is **republish + ack**; `nack`/`reject` with `requeue=true` MUST NOT be
used as the retry mechanism.

| Case | Database | Broker actions (in this order) |
|---|---|---|
| Success | effect + `processed_messages` row, one transaction | ack after commit |
| Duplicate | none | ack |
| Invalid message: `message_id` missing, not a string or violating §7.2; `x-retry-count` present but not a non-negative integer; body not JSON or violating §7.2 | none | publish copy to dead queue with header `x-failure: invalid_message`; ack. Never retried |
| Handler failure, `c < MAX` | none (rolled back) | publish copy to `tracksvc.events` / `track.extend` with `x-retry-count: c+1`; ack |
| Handler failure, `c >= MAX` | none (rolled back) | publish copy to dead queue with `x-retry-count: c` and `x-failure: <code of §7.2>`; ack |

**Order of checks:** `message_id` (missing or violating §7.2 → invalid message) →
duplicate (§7.3, whatever the body and headers) → the rest of the validation
(`x-retry-count`, body).

A **copy** has the same body bytes and the same `message_id`, is persistent
(`delivery_mode = 2`), and keeps the original headers except those set above. The
`message_id` of the copy of an invalid message whose `message_id` is missing or invalid
is unspecified (the client library may substitute its own). **While
no connection is lost**, a failing message with no prior retries is attempted `MAX + 1`
times in total, yields exactly `MAX` republished copies and then exactly one dead-letter
copy. Under connection loss (§7.6) `publish → ack` is not atomic: delivery of retry and
dead copies is **at-least-once** (duplicates allowed), while the database effect stays
exactly-once (§7.3). Nothing is ever silently dropped. In every case the
work queue ends with 0 ready and 0 unacknowledged messages for that `message_id`.
Outcomes MUST be observable within 10 s of publishing on an idle system. Retries are
immediate (no delay is required or tested).

### 7.5 Deterministic failure injection (verifier side, no hooks in code)

| Goal | Defined input |
|---|---|
| Handler fails on every attempt | `track_id` of a track that does not exist (or a sealed track, or `delta` pushing capacity over 10000) |
| Exactly `k` failures before dead-lettering, `1 ≤ k ≤ MAX+1` | the failing message above, published with `x-retry-count: MAX+1-k` |
| Change the budget | start the worker with `CONSUMER_MAX_RETRIES=n` (`0` = dead-letter on first failure) |
| Success on a late attempt | valid message for an existing open track, published with `x-retry-count: k` (`k ≤ MAX`): processed normally, capacity changes once |
| Count attempts | tap queue (§7.1): one republished copy per failed attempt with `c < MAX`, headers `1 … MAX` |

The service MUST NOT contain any switch, env var or special value whose purpose is to
make processing fail.

### 7.6 Connection loss

The verifier points `RABBITMQ_URL` at a toxiproxy listener and disables the proxy for
5–30 s while the worker is consuming, then re-enables it.
- The worker process MUST stay alive (same PID) during and after the outage.
- Within 30 s after the proxy is re-enabled the work queue again reports ≥ 1 consumer,
  and a message published after that is processed within 10 s.
- A message that was delivered but not acknowledged when the connection broke is
  redelivered by the broker; its effect is applied exactly once (§7.3).
- Loss of the **database** connection by the worker is unspecified and not tested.
- `SIGTERM` at any time: process exits within 10 s; exit code unspecified. A delivery
  left unacknowledged is redelivered by the broker and handled per §7.3.
  (Verifier note: the measured limit is 10 s + 5 s tolerance.)

## 8. Resolved ambiguities

| # | Ambiguity | Resolution |
|---|---|---|
| 1 | Who owns the DB session and the commit when "injectable via `Depends`", "no global state", and "transaction boundary owned by the repository" (with `async with session` inside it) all appear as requirements — three owners of one resource | The contract fixes outcomes, not owners: no I/O at import or start-up (§2.2: app starts with DB down), one request/message = one atomic transaction (§6.4, §7.3). The owner of the session and of the commit is named by a project convention in `AGENTS.md` (§2.3), not by the contract. Step-5 atomicity of effect + idempotency row cannot be met by per-repository commits, so that reading is excluded by behaviour, not by text |
| 2 | Whether the agent must write tests while "Out of scope" excludes the layers needed to exercise them, and whether integration tests require Docker Compose the agent has to start | Agent-written tests are **in scope at every step** in one fixed form: `uv run pytest` exits 0 with ≥ 1 test (criterion C4). Test-suite hygiene — updating tests with behaviour, removing obsolete ones, never deleting/skipping/weakening tests to pass — is a project convention assessed by CJ1. PostgreSQL and RabbitMQ are already running; the agent starts no infrastructure |
| 3 | `save()` semantics undefined (insert vs upsert); "raises on conflict" untestable | Only the resulting state is fixed: one `tracks` row per `id`, `track_segments` rows equal to the current segments, one transaction per request; the write algorithm is free. The only conflict is duplicate `code` → `track_code_taken`, also under concurrency (§6.4) |
| 4 | "Retry counter using message headers" together with "`nack` without requeue": headers are immutable on requeue, and a counter cannot survive it | Retry = republish a copy with `x-retry-count + 1`, then ack (§7.4). Retrying permanent domain errors (`track_not_found`, `track_sealed`, `capacity_limit_exceeded`) is a deliberate simplification for deterministic failure injection; a transient/permanent split is out of scope |
| 5 | Idempotency storage unspecified (in-memory sets passed review) | `processed_messages` table in PostgreSQL, written in the same transaction as the effect (§7.3) |
| 6 | After max retries: "nack, not requeued" while a dead-letter queue is out of scope → the message vanishes and is unobservable | Explicit publish to `tracksvc.track-events.dead` with `x-failure`, then ack (§7.4) |
| 7 | "Message schema validation out of scope", but `message_id` was required | Invalid messages defined: dead-lettered at once with `invalid_message`, never retried (§7.4) |
| 8 | A domain-only step: invariants visible only in unit tests the agent wrote itself | Invariants are exercised through R3–R5 with defined error codes and atomicity (§3, §5) |
| 9 | "Error handling" and "validation" marked out of scope while project rules still require error-handling conventions | One error format and the validation / not-found / conflict classes are active from step 2 (§5) |
| 10 | Not-found semantics undefined (`get_by_id → None` accepted) | `track_not_found` everywhere, never `null`, never 500 (§6.4) |
| 11 | Domain not fixed (agents invent Invoice/Order); a placeholder entity at step 1 | Domain, names and limits fixed in §3; step 1 has no entity at all |
| 12 | Dependency installation mixed with structure; start command and port unspecified | Dependencies pre-provisioned; commands, port, env fixed in §2 |
| 13 | "Consumer reconnects without crashing" had no measurable meaning | §7.6: same PID, consuming again within 30 s, exactly-once effect |

## 9. Criteria index

**Scope.** §9 is the index of functional and contract-level criteria. Code-quality
metrics (CRAP/erosion) and gaming detectors (suppressions, package stubs, etc.) are a
separate rubric layer with their own stable IDs (`Q…`, `N…`) defined at the rubric
stage. Test-suite changes between steps are collected as evidence for CJ1.

`[det]` = deterministic check (HTTP, AMQP, SQL, command exit code). `[judge]` = needs
an LLM judge reading code. IDs are stable and become rubric criteria; weights are
decided in the rubric. Criteria 4.9 and 4.10 are **not required-pass**; each is executed
3 times in a row (fresh `code` / fresh track per run) and passes only if all 3 runs pass.

**Rule for `[judge]` criteria.** Every `[judge]` criterion MUST trace to an explicit
rule in `AGENTS.md` or in the step instruction; without such a rule it is not scored.
Criteria state a property; a pattern is named only where a convention states it. Base
properties: (a) the domain does not depend on FastAPI / ORM / AMQP; (b) the transaction
boundary is explicit; (c) storage and transport are replaceable; (d) mapping between
layers is local and understandable; (e) no unnecessary abstractions.

### Common criteria (evaluated at every step)

- C1 `[det]` `ruff check` on `/app` reports zero violations with the supplied `ruff.toml`.
- C2 `[det]` `mypy --strict` on package `tracksvc` reports zero errors with the supplied `mypy.ini`.
- C3 `[det]` The import-linter contracts of §2.3 pass.
- C4 `[det]` Hygiene, not test quality: `uv run pytest` exits 0 and runs ≥ 1 test.
- CJ1 `[judge]` (from step 2; common, outside the 2–4 per-step judge limit) Test-suite hygiene: tests removed since the previous step were obsolete (their behaviour is no longer required by the contract); tests were updated together with changed behaviour; no test was deleted, skipped (`skip`/`xfail`) or weakened (assertions removed) to make the suite pass. Evidence collected deterministically, no weight of its own: pytest node IDs present at the previous step and missing now, `skip`/`xfail` markers added, assertions removed in changed tests, plus the test diff.
- CJ2 `[judge]` (every step; common, outside the 2–4 per-step judge limit) Docstrings of the public API (classes, methods, functions) are short and meaningful: they state purpose / contract, do not restate the name, and contain no stale or misleading text. Presence of docstrings is covered by C1 (ruff `D`).
- C5 (execution rule, no weight) Cascade: at every step all `[det]` criteria of earlier steps are re-run under **their own IDs**. Regressions are reported as a separate metric: the number of criteria that passed at an earlier step and fail now.
- C6 `[det]` (diagnostic, no weight) No HTTP 500 in any HTTP response observed during the step's checks (HTTP only).
- C7 `[det]` `ruff.toml` and `mypy.ini` are byte-identical to the initial commit (only these files; `pyproject.toml` and `uv.lock` may change; the import-linter config lives only in the verifier).
- C8 `[det]` No dynamic imports (`importlib`, `__import__`) in `tracksvc.domain` and `tracksvc.application`.
- N1 `[det]` No `# noqa` / `# ruff: noqa` comment in `src/` and `tests/` (AGENTS.md: "Do not silence the tools"). Computed from the step snapshot after the run, not by the verifier in the container.
- N2 `[det]` No `# type: ignore` / `# mypy: ignore-errors` comment in `src/` and `tests/` (same rule). Computed from the step snapshot after the run.

### Step 1 — L1-01

- 1.1 `[det]` `uv run alembic upgrade head` on an empty database exits 0.
- 1.2 `[det]` After 1.1 table `alembic_version` has exactly one row.
- 1.3 `[det]` A second `uv run alembic upgrade head` exits 0 and `alembic_version` is unchanged.
- 1.4 `[det]` The HTTP start command of §2.2 yields `GET /health` → `200 {"status": "ok"}` within 30 s.
- 1.5 `[det]` `GET /ready` → `200 {"status": "ready"}` when PostgreSQL is reachable.
- 1.6 `[det]` Started with `DATABASE_URL` pointing to a closed port, the process stays up and `GET /health` → 200.
- 1.7 `[det]` In the situation of 1.6 `GET /ready` → 503 with error body code `database_unavailable` within 10 s; the body matches the schema of §5 (`error` has exactly the keys `code` and `message`).
- 1.8 `[det]` Started with `RABBITMQ_URL` unset or pointing to a closed port, `GET /health` and `GET /ready` → 200.
- 1.9 `[det]` Packages `tracksvc.domain`, `tracksvc.application`, `tracksvc.infrastructure`, `tracksvc.api` are importable.
- 1.10 `[det]` After the app has started on an empty (unmigrated) database, the database still contains no tables.
- 1.11 `[det]` Started with `DATABASE_URL` pointing to a listener that accepts connections but never answers (toxiproxy `timeout` toxic in front of PostgreSQL), `GET /ready` → 503 with error body code `database_unavailable` within 10 s; the body matches the schema of §5 as in 1.7.
- 1.12 `[det]` After `GET /ready` → 200, PostgreSQL stops answering while established connections stay open (the same toxic added to a running proxy): the next `GET /ready` → 503 with error body code `database_unavailable` within 10 s; the body matches the schema of §5 as in 1.7.
- 1.13 `[det]` Importing package `tracksvc` and the ASGI module `tracksvc.api.app` with `DATABASE_URL` and `RABBITMQ_URL` unset succeeds.
- 1.J1 `[judge]` Database connections / the engine are created once per process and released at shutdown; there are no module-level connection or session objects.
- 1.J2 (moved to step 4 → 4.J5: at step 1 no endpoint uses a request session; `/ready` uses a short engine connection)
- 1.J3 `[judge]` The step adds no business logic, entities or abstractions beyond the skeleton (no unnecessary abstractions).

### Step 2 — L2-01

- 2.1 `[det]` `POST /tracks` with a valid body → 201 and a Track body with the sent `code` and `capacity`, `status: "open"`, `segments: []`.
- 2.2 `[det]` `id` in 2.1 is a canonical UUID string; two created tracks have different ids.
- 2.3 `[det]` `GET /tracks/{id}` for a created track → 200 with a body equal to the creation response.
- 2.4 `[det]` `GET /tracks/{random uuid}` → 404 `track_not_found`.
- 2.5 `[det]` `GET /tracks/not-a-uuid` → 422 `validation_error` with a `details` item whose `field` is `track_id`.
- 2.6 `[det]` `POST /tracks` with a missing, wrongly typed, out-of-range or regex-violating `code` / `capacity` (one field violated per request) → 422 `validation_error` with a `details` item naming that field.
- 2.7 `[det]` `POST /tracks` with a body that is not valid JSON or not an object → 422 `validation_error` with `field` = `body`.
- 2.8 `[det]` `POST /tracks` with a `code` already used → 409 `track_code_taken`; the first track is unchanged and no second track exists.
- 2.9 `[det]` Every error response matches the error body schema of §5 (`details` only for `validation_error`).
- 2.J1 `[judge]` The create/get logic lives in the application layer; the API layer only translates between HTTP and application calls and holds no business logic; use cases get their dependencies passed in explicitly (not created inside, not global).
- 2.J2 `[judge]` Use cases depend on a storage abstraction declared in an inner layer (domain or application); replacing the implementation needs no change in the use cases.
- 2.J3 `[judge]` Use cases contain no HTTP concepts (status codes, request/response objects) and no storage-specific details (SQL, sessions, table names).
- 2.J4 (moved to step 4 → 4.J5: at steps 2–3 tracks live in process memory and no endpoint uses a request session)

### Step 3 — L2-02

- 3.1 `[det]` R3 with a valid segment → 201 and the Track containing it.
- 3.2 `[det]` `segments` is ordered by `span.start` ascending regardless of insertion order.
- 3.3 `[det]` A span touching an existing one (`start` = other `end`) is accepted.
- 3.4 `[det]` `start >= end` → 409 `invalid_span` (not `validation_error`).
- 3.5 `[det]` `end > capacity` → 409 `span_out_of_bounds`; `end == capacity` is accepted.
- 3.6 `[det]` A label already present in the track → 409 `segment_label_taken`; the same label in another track is accepted.
- 3.7 `[det]` A span overlapping an existing one (partial, containing, contained, identical) → 409 `segment_overlap`.
- 3.8 `[det]` A 9th segment → 409 `segment_limit_reached`; the 8th is accepted.
- 3.9 `[det]` After each rejection of 3.4–3.8 (no concurrent writers) `GET` returns the same body as before the request.
- 3.10 `[det]` R4 for an existing label → 200 and the Track without it; the freed span can be reused.
- 3.11 `[det]` R4 for an absent label → 404 `segment_not_found`.
- 3.12 `[det]` R5 on a track with ≥ 1 segment → 200 with `status: "sealed"`; `GET` confirms.
- 3.13 `[det]` R5 on a track with no segments → 409 `track_empty`; status stays `open`.
- 3.14 `[det]` R3, R4 and R5 on a sealed track → 409 `track_sealed`; the track is unchanged.
- 3.15 `[det]` R3–R5 on an unknown id → 404 `track_not_found`; schema-invalid R3 bodies → 422 `validation_error`, also for an unknown id.
- 3.16 `[det]` R3, R4 and R5 with a `track_id` that is not a UUID → 422 `validation_error` with a `details` item whose `field` is `track_id`; R4 with a `label` in the path that violates its regex → 422 `validation_error` with `field` `label`.
- 3.J1 `[judge]` Each invariant I1–I7 is enforced in the domain layer, in one place; not in routers, schemas or use cases.
- 3.J2 `[judge]` Span is immutable and compared by value; a track's segments can be changed only through the track's own operations (no externally mutable collection is exposed).
- 3.J3 `[judge]` Invariants are checked before any state change (no mutate-then-validate).
- 3.J4 `[judge]` Domain errors are expressed without HTTP concepts; their translation to HTTP status and body is localized in the API layer.

### Step 4 — L3-01

Step 4 has five `[judge]` criteria (4.J1–4.J5): an exception to the 2–4 per-step judge
limit, because 4.J5 was moved here from step 2.

- 4.1 `[det]` After `alembic upgrade head` tables `tracks` and `track_segments` exist with the columns and constraints of §6.2.
- 4.2 `[det]` `alembic downgrade base` exits 0, then `alembic upgrade head` exits 0 and 4.1 holds again.
- 4.3 `[det]` A track created, given segments and sealed is returned unchanged by `GET` after the app is killed with `SIGKILL` and started again.
- 4.4 `[det]` A track created through one app process is returned by a second app process running on the same database.
- 4.5 `[det]` After the database is recreated and migrated, previously issued ids → 404 `track_not_found`.
- 4.6 `[det]` Each created track has exactly one `tracks` row whose `code`, `capacity`, `status` equal the API values.
- 4.7 `[det]` After any sequence of R3/R4 the `track_segments` rows of the track equal its API segments (labels and bounds), with no leftover rows.
- 4.8 `[det]` After a request rejected with 4xx the rows of `tracks` and `track_segments` are identical to those before it.
- 4.9 `[det]` (×3, not required-pass) 10 simultaneous `POST /tracks` with the same `code` → exactly one 201, nine 409 `track_code_taken`, one row.
- 4.10 `[det]` (×3, not required-pass) 10 simultaneous R3 with distinct labels and the same span → exactly one 201, nine 409 `segment_overlap`, one segment row.
- 4.11 `[det]` A capacity value written directly to the `tracks` row by SQL is returned by the next `GET` (no in-memory copy).
- 4.J1 `[judge]` Domain classes are not ORM-mapped classes; domain ↔ ORM conversion is explicit and localized, not scattered across query code. A separate mapper class is not required.
- 4.J2 `[judge]` Storage errors are translated to domain/application errors at the infrastructure boundary; no driver or ORM exception type is handled outside infrastructure.
- 4.J3 `[judge]` The transaction boundary is explicit: one identifiable place per request begins the transaction and commits or rolls back; every path ends in commit or rollback and releases the connection.
- 4.J4 `[judge]` The migration is a readable revision with explicit operations, consistent with the ORM models; the schema is never created through `create_all`.
- 4.J5 `[judge]` A database session (or unit of work) has request scope: handlers do not construct engines or sessions themselves, and the session is released on every path, including errors.

### Step 5 — L3-02

Required-pass at this step: **5.1, 5.3, 5.4, 5.5** only. The other `[det]` criteria are
scored in groups (weights decided in the rubric): **G-retry** 5.7–5.14, 5.19, **G-concurrency**
5.6, **G-resilience** 5.2, 5.15–5.17, **G-isolation** 5.18. Exact copy counts (5.7–5.11, 5.14) are asserted only in runs with
no connection loss.

- 5.1 `[det]` Within 30 s of the worker start command the exchange and both queues exist with the properties of §7.1 and the work queue has ≥ 1 consumer.
- 5.2 `[det]` The worker starts when the verifier has pre-declared the topology of §7.1.
- 5.3 `[det]` A valid message for an open track increases `capacity` by `delta` (seen through `GET`) within 10 s; the work queue ends empty with nothing unacknowledged.
- 5.4 `[det]` After 5.3 `processed_messages` has exactly one row with that `message_id`.
- 5.5 `[det]` Publishing the same `message_id` again (same or different body) changes nothing and publishes nothing to the tap or the dead queue.
- 5.6 `[det]` 20 copies of one `message_id` published at once, with two worker processes running, change `capacity` exactly once.
- 5.7 `[det]` A message for a non-existent track produces exactly `MAX` republished copies on the tap with `x-retry-count` 1…`MAX`, same body and `message_id`.
- 5.8 `[det]` After 5.7 the dead queue holds exactly one copy with `x-retry-count: MAX`, `x-failure: track_not_found`, same body and `message_id`; the work queue is empty.
- 5.9 `[det]` The same failing message published with `x-retry-count: MAX` goes to the dead queue with no republish.
- 5.10 `[det]` With `CONSUMER_MAX_RETRIES=1` a failing message yields one republished copy and then one dead copy; with `0`, no republish.
- 5.11 `[det]` Messages for a sealed track and for a capacity overflow end in the dead queue with `x-failure` `track_sealed` / `capacity_limit_exceeded`; capacity is unchanged.
- 5.12 `[det]` A dead-lettered `message_id` has no `processed_messages` row; the same `message_id` published later with a valid body for an existing open track is processed.
- 5.13 `[det]` A valid message published with `x-retry-count: 2` is processed normally (capacity changes once, row inserted).
- 5.14 `[det]` Messages without `message_id` or with one violating §7.2, with an `x-retry-count` that is not a non-negative integer (string, negative, boolean), with a non-JSON body, or with a schema-invalid body go straight to the dead queue with `x-failure: invalid_message`, with no republish and no database change.
- 5.15 `[det]` After a 5–30 s broker outage through toxiproxy the worker has the same PID, consumes again within 30 s, and processes a new message within 10 s.
- 5.16 `[det]` With **one** worker process running, 50 valid messages (no failing or invalid ones) with distinct `message_id`s for one open track are published (initial capacity + sum of the 50 deltas ≤ 10000) and the proxy is disabled while they are being processed; after recovery `capacity` equals the initial value plus the sum of all 50 deltas (each applied exactly once) and `processed_messages` has 50 rows. Only the database effect is asserted; tap and dead-queue contents are not.
- 5.17 `[det]` The worker exits within 10 s of `SIGTERM` (verifier tolerance +5 s).
- 5.18 `[det]` The HTTP app serves R1–R5 while RabbitMQ is down, and returns capacity changed by the worker without restart.
- 5.19 `[det]` A message whose `message_id` is already in `processed_messages` but whose body or `x-retry-count` is invalid is acknowledged; nothing is published to the tap or the dead queue; no database change.
- 5.J1 `[judge]` Acknowledgement happens only after processing has completed and its transaction committed (ack awaited, never issued twice, never before the handler).
- 5.J2 `[judge]` Broker connection, channel and database session each have one explicit owner and are released on reconnect and on shutdown; no leaked resources.
- 5.J3 `[judge]` The capacity-change business rule is not duplicated in consumer code: it is reused from domain/application. AMQP specifics (acks, headers, retry counters, routing) stay out of domain and application.
- 5.J4 `[judge]` The capacity change and the idempotency record share one transaction by construction, not by two sequential commits.
