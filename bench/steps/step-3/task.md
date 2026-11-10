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
