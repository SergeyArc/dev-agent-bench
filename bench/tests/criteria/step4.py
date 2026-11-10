"""Step 4 (L3-01) deterministic criteria 4.1–4.11 of contract §9.

Every check is independent: it creates its own empty database (not the default
one: the service must use DATABASE_URL, §2.1), migrates it with the command of
contract §2.2, starts the HTTP app itself and stops it again. The situations
the criteria need (SIGKILL, a second app process, a recreated database, a row
changed by SQL, simultaneous requests) are created here, never by agent code.

Rows are read with SQL as the service role from schema `public` (§6.2). Track
bodies are compared by the keys of contract §4.2 only; extra keys are ignored.

Time limits: the start command gets the contract limit plus `config.TOLERANCE`
(30 + 5 s); migrations, single requests and bursts have verifier limits of
their own. Nothing waits without a deadline; waiting is polling.

Criteria 4.9 and 4.10 run `REPEATED` times in a row and pass only if every run
passes; they are not required-pass (`NOT_REQUIRED`, for the rubric task).
Judge criteria are not evaluated here; `JUDGE_CRITERIA` is the hook.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import psycopg

from benchverify import config, db, http, proc, reset
from benchverify.common import Context
from benchverify.result import CheckFailed, Suite

STEP = 4
JUDGE_CRITERIA = ("CJ1", "CJ2", "4.J1", "4.J2", "4.J3", "4.J4", "4.J5")  # hook only
REPEATED = {"4.9": 3, "4.10": 3}  # contract §9: executed 3 times in a row
NOT_REQUIRED = frozenset(REPEATED)  # hook for the rubric: never required-pass

# Database owned by these checks; its name differs from the default one on purpose.
STEP_DB = "tracksvc_step4"
SECOND_PORT = 8001  # the second app process of 4.4

MIGRATE_TIMEOUT = 120.0
START_LIMIT = config.HEALTH_TIMEOUT + config.TOLERANCE
REQUEST_TIMEOUT = 15.0
BURST_LIMIT = 60.0  # one burst of simultaneous requests as a whole
BURST_SIZE = 10

TEXT_TYPES = ("text", "character varying")
# Contract §6.2: table -> required column -> accepted `information_schema` data types.
SCHEMA: dict[str, dict[str, tuple[str, ...]]] = {
    "tracks": {
        "id": ("uuid",),
        "code": TEXT_TYPES,
        "capacity": ("integer",),
        "status": TEXT_TYPES,
    },
    "track_segments": {
        "track_id": ("uuid",),
        "label": TEXT_TYPES,
        "span_start": ("integer",),
        "span_end": ("integer",),
    },
}

_KEY_COLUMNS = (
    "ARRAY(SELECT a.attname::text FROM unnest({keys}) WITH ORDINALITY AS k(attnum, ord) "
    "JOIN pg_attribute a ON a.attrelid = {rel} AND a.attnum = k.attnum {where} ORDER BY k.ord)"
)
# Unique indexes over plain columns (no predicate, no expressions): primary keys,
# unique constraints and unique indexes alike. INCLUDE columns are not key columns.
_UNIQUE_SQL = (
    "SELECT i.indisprimary, "
    + _KEY_COLUMNS.format(keys="i.indkey::int2[]", rel="i.indrelid", where="WHERE k.ord <= i.indnkeyatts")
    + " FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relname = %s AND i.indisunique AND i.indisvalid "
    "AND i.indpred IS NULL AND i.indexprs IS NULL"
)
_FOREIGN_SQL = (
    "SELECT "
    + _KEY_COLUMNS.format(keys="con.conkey", rel="con.conrelid", where="")
    + ", fn.nspname::text, fc.relname::text, "
    + _KEY_COLUMNS.format(keys="con.confkey", rel="con.confrelid", where="")
    + " FROM pg_constraint con JOIN pg_class c ON c.oid = con.conrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "JOIN pg_class fc ON fc.oid = con.confrelid "
    "JOIN pg_namespace fn ON fn.oid = fc.relnamespace "
    "WHERE con.contype = 'f' AND n.nspname = 'public' AND c.relname = %s"
)


@dataclass(frozen=True)
class Track:
    """The keys of a Track body that contract §4.2 requires."""

    id: str
    code: str
    capacity: Any
    status: Any
    segments: tuple[tuple[Any, Any, Any], ...]  # (label, span.start, span.end), API order

    def rows(self) -> list[tuple[Any, Any, Any]]:
        """The `track_segments` rows this body stands for, in a fixed order."""
        return sorted(self.segments, key=repr)


# --- database -------------------------------------------------------------------


def _expect_exit_0(result: proc.CommandResult, command: Sequence[str]) -> None:
    line = " ".join(command)
    if result.timed_out:
        raise CheckFailed(f"`{line}` timed out after {MIGRATE_TIMEOUT:.0f} s")
    if result.returncode != 0:
        raise CheckFailed(f"`{line}` exited with {result.returncode}\n{result.tail(20)}")


def _upgrade() -> proc.CommandResult:
    result = reset.migrate(config.database_url(STEP_DB), timeout=MIGRATE_TIMEOUT)
    _expect_exit_0(result, config.MIGRATE_CMD)
    return result


def _downgrade() -> proc.CommandResult:
    result = reset.downgrade(config.database_url(STEP_DB), timeout=MIGRATE_TIMEOUT)
    _expect_exit_0(result, config.DOWNGRADE_CMD)
    return result


@contextmanager
def _database(*, migrated: bool = True) -> Iterator[str]:
    """A fresh database of this step; yields its DATABASE_URL, drops it afterwards."""
    proc.stop_agent_processes()
    db.recreate_database(STEP_DB)
    try:
        if migrated:
            _upgrade()
        yield config.database_url(STEP_DB)
    finally:
        proc.stop_agent_processes()
        db.drop_database(STEP_DB)


def _sql(sql: str, params: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
    """Rows of a statement; a missing table / column or a wrong type is a failure."""
    try:
        return db.query(sql, params, db=STEP_DB)
    except (psycopg.ProgrammingError, psycopg.DataError) as exc:
        raise CheckFailed(f"SQL check failed: {str(exc).strip()}\nstatement: {sql}") from exc


def _track_rows(track_id: str) -> list[tuple[Any, ...]]:
    return _sql(
        "SELECT code, capacity, status FROM public.tracks WHERE id = %s::uuid", [track_id]
    )


def _segment_rows(track_id: str) -> list[tuple[Any, ...]]:
    rows = _sql(
        "SELECT label, span_start, span_end FROM public.track_segments WHERE track_id = %s::uuid",
        [track_id],
    )
    return sorted(rows, key=repr)


def _dump() -> dict[str, list[str]]:
    """Every row of both tables with all of its columns, as sorted JSON texts."""
    return {
        table: [row[0] for row in _sql(f"SELECT to_jsonb(t)::text FROM public.{table} t ORDER BY 1")]
        for table in SCHEMA
    }


def _schema_problems() -> list[str]:
    """Deviations of the migrated schema from contract §6.2 (empty = conforms)."""
    problems: list[str] = []
    for table, required in SCHEMA.items():
        found = db.columns(table, db=STEP_DB)
        if not found:
            problems.append(f"table public.{table} does not exist")
            continue
        for column, accepted in required.items():
            if column not in found:
                problems.append(f"{table}.{column}: column is missing")
            elif found[column] not in accepted:
                problems.append(
                    f"{table}.{column}: type {found[column]!r}, expected {' / '.join(accepted)}"
                )
    if problems:
        return problems

    unique = {table: _sql(_UNIQUE_SQL, [table]) for table in SCHEMA}
    primary = [columns for is_primary, columns in unique["tracks"] if is_primary]
    if primary != [["id"]]:
        problems.append(f"tracks: primary key is {primary or 'missing'}, expected (id)")
    if not any(columns == ["code"] for _, columns in unique["tracks"]):
        problems.append("tracks: no unique constraint or unique index on (code)")
    if not any(sorted(columns) == ["label", "track_id"] for _, columns in unique["track_segments"]):
        problems.append("track_segments: no unique constraint or unique index on (track_id, label)")
    foreign = [tuple(row) for row in _sql(_FOREIGN_SQL, ["track_segments"])]
    if (["track_id"], "public", "tracks", ["id"]) not in foreign:
        problems.append(
            f"track_segments: no foreign key track_id -> tracks.id (foreign keys found: {foreign})"
        )
    return problems


# --- HTTP app ---------------------------------------------------------------------


def _start(name: str, database_url: str, port: int = config.HTTP_PORT) -> proc.ManagedProcess:
    """Start the HTTP app with the command of contract §2.2 (the second process
    of 4.4 differs in the port only) and poll until it listens."""
    if not proc.wait_until(lambda: not proc.port_open(port), 5.0):
        raise RuntimeError(f"port {port} is in use before the app is started")
    command = [*config.HTTP_APP_CMD[:-1], str(port)]
    app = proc.ManagedProcess(
        f"step4-{name}", command, {"DATABASE_URL": database_url}, config.LOG_DIR / "proc"
    ).start()
    started = time.monotonic()
    while not proc.port_open(port):
        elapsed = time.monotonic() - started
        problem = None
        if not app.alive():
            problem = f"the process exited with code {app.returncode()} after {elapsed:.1f} s"
        elif elapsed >= START_LIMIT:
            problem = f"the app did not listen within {START_LIMIT:.0f} s"
        if problem is not None:
            tail = app.log_tail(20)
            app.stop()
            raise CheckFailed(f"{problem} (port {port})\n{tail}")
        time.sleep(0.25)
    return app


@contextmanager
def _service(name: str) -> Iterator[httpx.Client]:
    """A migrated database of this step with one app process on port 8000."""
    with _database() as url:
        app = _start(name, url)
        try:
            with http.client(timeout=REQUEST_TIMEOUT) as client:
                yield client
        finally:
            app.stop()


def _send(client: httpx.Client, method: str, path: str, body: Any = None) -> httpx.Response:
    try:
        return client.request(method, path, json=body)
    except httpx.HTTPError as exc:
        raise CheckFailed(f"{method} {path}: no HTTP response ({exc.__class__.__name__})") from exc


def _track(response: httpx.Response, what: str) -> Track:
    try:
        body = response.json()
        segments = tuple(
            (item["label"], item["span"]["start"], item["span"]["end"]) for item in body["segments"]
        )
        return Track(str(body["id"]), str(body["code"]), body["capacity"], body["status"], segments)
    except (ValueError, KeyError, TypeError) as exc:
        raise CheckFailed(
            f"{what}: the body is not a Track of contract §4.2 ({exc.__class__.__name__}: {exc}); "
            f"body {response.text[:300]!r}"
        ) from exc


def _ok(client: httpx.Client, method: str, path: str, status: int, body: Any = None) -> Track:
    """A request that must succeed with `status` and return a Track."""
    response = _send(client, method, path, body)
    if response.status_code != status:
        raise CheckFailed(
            f"{method} {path}: expected {status}, got {response.status_code}, "
            f"body {response.text[:300]!r}"
        )
    return _track(response, f"{method} {path}")


def _code() -> str:
    return f"T4-{uuid.uuid4().hex[:10].upper()}"


def _segment(label: str, start: int, end: int) -> dict[str, Any]:
    return {"label": label, "span": {"start": start, "end": end}}


def _create(client: httpx.Client, capacity: int = 100) -> Track:
    return _ok(client, "POST", "/tracks", 201, {"code": _code(), "capacity": capacity})


def _add(client: httpx.Client, track_id: str, label: str, start: int, end: int) -> Track:
    return _ok(client, "POST", f"/tracks/{track_id}/segments", 201, _segment(label, start, end))


def _remove(client: httpx.Client, track_id: str, label: str) -> Track:
    return _ok(client, "DELETE", f"/tracks/{track_id}/segments/{label}", 200)


def _seal(client: httpx.Client, track_id: str) -> Track:
    return _ok(client, "POST", f"/tracks/{track_id}/seal", 200)


def _get(client: httpx.Client, track_id: str) -> Track:
    return _ok(client, "GET", f"/tracks/{track_id}", 200)


def _expect_not_found(client: httpx.Client, track_id: str) -> None:
    response = _send(client, "GET", f"/tracks/{track_id}")
    code = http.error_code(response)
    if response.status_code != 404 or code != "track_not_found":
        raise CheckFailed(
            f"GET /tracks/{track_id}: expected 404 track_not_found, got {response.status_code} "
            f"{code!r}, body {response.text[:300]!r}"
        )
    problem = http.error_body_problem(response)
    if problem is not None:
        raise CheckFailed(f"GET /tracks/{track_id} -> 404 track_not_found but {problem}")


def _burst(method: str, path: str, bodies: Sequence[Any]) -> list[tuple[int, str | None]]:
    """Send the requests simultaneously over connections opened beforehand.

    Returns (status, error code) per request, in the order of `bodies`; a
    request without a response is reported as (0, exception class name).
    """

    async def run() -> list[httpx.Response | BaseException]:
        async with http.async_client(timeout=REQUEST_TIMEOUT) as client:
            # One keep-alive connection per request, so that the burst itself
            # is not spread out by connection set-up. The warm-up requests read
            # the database (an unknown id -> 404): an app that opens its database
            # connections on demand then holds several of them already. With a
            # single open connection the first request of the burst commits
            # while the others are still connecting, and nothing runs at once.
            await asyncio.gather(
                *(client.get(f"/tracks/{uuid.uuid4()}") for _ in bodies), return_exceptions=True
            )
            return await asyncio.gather(
                *(client.request(method, path, json=body) for body in bodies),
                return_exceptions=True,
            )

    try:
        answers = asyncio.run(asyncio.wait_for(run(), BURST_LIMIT))
    except TimeoutError:
        raise CheckFailed(
            f"{len(bodies)} simultaneous {method} {path} did not finish within {BURST_LIMIT:.0f} s"
        ) from None
    return [
        (answer.status_code, http.error_code(answer))
        if isinstance(answer, httpx.Response)
        else (0, answer.__class__.__name__)
        for answer in answers
    ]


def _expect_one_winner(outcomes: list[tuple[int, str | None]], loser_code: str, what: str) -> None:
    wanted = Counter({(201, None): 1, (409, loser_code): len(outcomes) - 1})
    found = Counter(outcomes)
    if found != wanted:
        seen = ", ".join(
            f"{count} x {status or 'no response'} {code or ''}".rstrip()
            for (status, code), count in sorted(found.items(), key=repr)
        )
        raise CheckFailed(
            f"{what}: expected exactly one 201 and {len(outcomes) - 1} x 409 {loser_code}, got {seen}"
        )


# --- 4.1, 4.2 schema ----------------------------------------------------------------


def c4_1_schema() -> str:
    with _database():
        problems = _schema_problems()
    if problems:
        raise CheckFailed("after `alembic upgrade head`:\n" + "\n".join(problems))
    return (
        "public.tracks and public.track_segments have the columns of §6.2, primary key (id), "
        "unique (code), foreign key track_id -> tracks.id, unique (track_id, label)"
    )


def c4_2_downgrade_and_upgrade() -> str:
    with _database(migrated=False):
        _upgrade()
        down = _downgrade()
        up = _upgrade()
        problems = _schema_problems()
    if problems:
        raise CheckFailed("after downgrade base and upgrade head:\n" + "\n".join(problems))
    return (
        f"`alembic downgrade base` exited 0 ({down.duration_sec} s), `alembic upgrade head` "
        f"exited 0 ({up.duration_sec} s); the schema of §6.2 is back"
    )


# --- 4.3–4.5 durability -------------------------------------------------------------


def c4_3_survives_sigkill() -> str:
    with _database() as url:
        app = _start("4.3-before", url)
        try:
            with http.client(timeout=REQUEST_TIMEOUT) as client:
                track = _create(client)
                _add(client, track.id, "beta", 10, 25)
                _add(client, track.id, "alpha", 0, 10)
                other = _create(client, capacity=40)
                other = _add(client, other.id, "solo", 5, 15)
                sealed = _seal(client, track.id)
            # Killed right after the last 2xx: "saved" means committed before the response.
            app.kill()
        finally:
            app.stop()
        if sealed.status != "sealed" or len(sealed.segments) != 2:
            raise CheckFailed(f"the seal response is not a sealed track with 2 segments: {sealed}")

        app = _start("4.3-after", url)
        try:
            with http.client(timeout=REQUEST_TIMEOUT) as client:
                for before in (sealed, other):
                    after = _get(client, before.id)
                    if after != before:
                        raise CheckFailed(
                            f"track changed across SIGKILL and restart:\nbefore: {before}\nafter:  {after}"
                        )
        finally:
            app.stop()
    return (
        "a sealed track with 2 segments and an open track with 1 segment are returned unchanged "
        "by GET after SIGKILL and a new start"
    )


def c4_4_second_process() -> str:
    second_base = f"http://{config.HTTP_HOST}:{SECOND_PORT}"
    with _database() as url:
        first = _start("4.4-first", url)
        try:
            second = _start("4.4-second", url, SECOND_PORT)
            try:
                with (
                    http.client(timeout=REQUEST_TIMEOUT) as one,
                    http.client(second_base, timeout=REQUEST_TIMEOUT) as two,
                ):
                    for writer, reader, route in ((one, two, "8000 -> 8001"), (two, one, "8001 -> 8000")):
                        created = _create(writer)
                        created = _add(writer, created.id, "alpha", 0, 10)
                        seen = _get(reader, created.id)
                        if seen != created:
                            raise CheckFailed(
                                f"{route}: the other process returned a different track:\n"
                                f"written: {created}\nread:    {seen}"
                            )
            finally:
                second.stop()
        finally:
            first.stop()
    return (
        f"two app processes (ports {config.HTTP_PORT} and {SECOND_PORT}) on one database: a track "
        "created through either one is returned by the other"
    )


def c4_5_recreated_database() -> str:
    with _database() as url:
        app = _start("4.5-before", url)
        try:
            with http.client(timeout=REQUEST_TIMEOUT) as client:
                first = _create(client)
                second = _add(client, _create(client).id, "alpha", 0, 10)
                ids = [first.id, second.id]
                for track_id in ids:
                    _get(client, track_id)
        finally:
            app.stop()
        # Reset of contract §2.2: stop -> drop and recreate -> migrate -> start.
        proc.stop_agent_processes()
        db.recreate_database(STEP_DB)
        _upgrade()
        app = _start("4.5-after", url)
        try:
            with http.client(timeout=REQUEST_TIMEOUT) as client:
                for track_id in ids:
                    _expect_not_found(client, track_id)
        finally:
            app.stop()
    return "2 ids issued before the database was recreated and migrated -> 404 track_not_found"


# --- 4.6–4.8 stored state -----------------------------------------------------------


def _expect_track_row(track: Track, when: str) -> None:
    rows = _track_rows(track.id)
    wanted = [(track.code, track.capacity, track.status)]
    if rows != wanted:
        raise CheckFailed(
            f"{when}: `tracks` rows with id {track.id} (code, capacity, status) are {rows}, "
            f"expected exactly {wanted} (API values)"
        )


def c4_6_track_rows() -> str:
    with _service("4.6") as client:
        small = _create(client, capacity=1)
        large = _create(client, capacity=10000)
        for track in (small, large):
            _expect_track_row(_get(client, track.id), "after POST /tracks")
        track = _add(client, _create(client).id, "alpha", 0, 10)
        _expect_track_row(track, "after adding a segment")
        _seal(client, track.id)
        sealed = _get(client, track.id)
        if sealed.status != "sealed":
            raise CheckFailed(f"GET after seal returns status {sealed.status!r}")
        _expect_track_row(sealed, "after seal")
    return "3 tracks (capacity 1, 10000, 100; one sealed): one `tracks` row each, equal to the API values"


def _expect_segment_rows(client: httpx.Client, answered: Track, when: str) -> None:
    current = _get(client, answered.id)
    if current != answered:
        raise CheckFailed(f"{when}: GET differs from the response:\nresponse: {answered}\nGET: {current}")
    rows = _segment_rows(answered.id)
    if rows != current.rows():
        raise CheckFailed(
            f"{when}: `track_segments` rows of {answered.id} are {rows}, "
            f"the API segments are {current.rows()}"
        )


def c4_7_segment_rows() -> str:
    with _service("4.7") as client:
        track = _create(client)
        other = _add(client, _create(client).id, "alpha", 0, 50)
        steps = 0
        for label, start, end in (("gamma", 40, 60), ("alpha", 0, 10), ("beta", 10, 25)):
            _expect_segment_rows(client, _add(client, track.id, label, start, end), f"after adding {label}")
            steps += 1
        _expect_segment_rows(client, _remove(client, track.id, "alpha"), "after removing alpha")
        _expect_segment_rows(client, _add(client, track.id, "delta", 0, 10), "after reusing the span")
        steps += 2
        for label in ("gamma", "delta", "beta"):
            _expect_segment_rows(client, _remove(client, track.id, label), f"after removing {label}")
            steps += 1
        if _segment_rows(track.id):
            raise CheckFailed(f"leftover rows after removing every segment: {_segment_rows(track.id)}")
        _expect_segment_rows(client, other, "second track, untouched by the sequence")
    return f"rows equal the API segments after each of {steps} R3/R4 requests; no rows left at the end"


def _rejections(client: httpx.Client) -> list[tuple[str, str, str, Any]]:
    """Requests that must be rejected: (expected code, method, path, body)."""
    busy = _create(client)
    _add(client, busy.id, "alpha", 0, 10)
    _add(client, busy.id, "beta", 20, 30)
    empty = _create(client)
    sealed = _create(client)
    _add(client, sealed.id, "alpha", 0, 10)
    _seal(client, sealed.id)
    full = _create(client)
    for index in range(8):
        _add(client, full.id, f"s{index}", index * 10, index * 10 + 10)
    unknown = str(uuid.uuid4())

    def segments(track_id: str) -> str:
        return f"/tracks/{track_id}/segments"

    return [
        ("track_code_taken", "POST", "/tracks", {"code": busy.code, "capacity": 50}),
        ("validation_error", "POST", "/tracks", {"code": _code(), "capacity": 0}),
        ("invalid_span", "POST", segments(busy.id), _segment("gamma", 15, 15)),
        ("span_out_of_bounds", "POST", segments(busy.id), _segment("gamma", 90, 101)),
        ("segment_label_taken", "POST", segments(busy.id), _segment("alpha", 40, 50)),
        ("segment_overlap", "POST", segments(busy.id), _segment("gamma", 5, 25)),
        ("segment_limit_reached", "POST", segments(full.id), _segment("s8", 80, 90)),
        ("validation_error", "POST", segments(busy.id), _segment("Bad", 40, 50)),
        ("track_not_found", "POST", segments(unknown), _segment("gamma", 40, 50)),
        ("segment_not_found", "DELETE", f"{segments(busy.id)}/missing", None),
        ("track_not_found", "DELETE", f"{segments(unknown)}/alpha", None),
        ("track_empty", "POST", f"/tracks/{empty.id}/seal", None),
        ("track_not_found", "POST", f"/tracks/{unknown}/seal", None),
        ("track_sealed", "POST", segments(sealed.id), _segment("beta", 20, 30)),
        ("track_sealed", "DELETE", f"{segments(sealed.id)}/alpha", None),
        ("track_sealed", "POST", f"/tracks/{sealed.id}/seal", None),
    ]


def _dump_difference(before: dict[str, list[str]], after: dict[str, list[str]]) -> str:
    lines = []
    for table in SCHEMA:
        gone = sorted(set(before[table]) - set(after[table]))
        new = sorted(set(after[table]) - set(before[table]))
        lines += [f"  {table}: row gone or changed: {row}" for row in gone[:3]]
        lines += [f"  {table}: row new or changed:  {row}" for row in new[:3]]
        if len(before[table]) != len(after[table]):
            lines.append(f"  {table}: {len(before[table])} row(s) before, {len(after[table])} after")
    return "\n".join(lines)


def c4_8_rejection_changes_no_row() -> str:
    with _service("4.8") as client:
        requests = _rejections(client)
        before = _dump()
        rejected = 0
        problems: list[str] = []
        accepted: list[str] = []
        for wanted, method, path, body in requests:
            response = _send(client, method, path, body)
            label = f"{method} {path} ({wanted})"
            after = _dump()
            if response.is_success:
                # Not a rejection: criteria of steps 2–3 report it; rows may change.
                accepted.append(f"{label} -> {response.status_code}")
            else:
                rejected += 1
                if after != before:
                    problems.append(
                        f"{label} -> {response.status_code} {http.error_code(response)!r} changed rows:\n"
                        + _dump_difference(before, after)
                    )
            before = after
    if problems:
        raise CheckFailed("\n".join(problems))
    if not rejected:
        raise CheckFailed("none of the requests was rejected: " + "; ".join(accepted))
    note = f"; answered 2xx and left out: {'; '.join(accepted)}" if accepted else ""
    return (
        f"{rejected} rejected request(s): all rows of `tracks` and `track_segments` "
        f"(every column) identical before and after each{note}"
    )


# --- 4.9, 4.10 concurrency ------------------------------------------------------------


def c4_9_simultaneous_create() -> str:
    with _service("4.9"):
        code = _code()
        outcomes = _burst("POST", "/tracks", [{"code": code, "capacity": 100}] * BURST_SIZE)
        _expect_one_winner(outcomes, "track_code_taken", f"{BURST_SIZE} simultaneous POST /tracks")
        rows = _sql("SELECT id FROM public.tracks WHERE code = %s", [code])
    if len(rows) != 1:
        raise CheckFailed(f"{len(rows)} `tracks` row(s) with code {code}, expected exactly 1")
    return f"{BURST_SIZE} simultaneous POST /tracks with one code: one 201, {BURST_SIZE - 1} x 409 track_code_taken, one row"


def c4_10_simultaneous_segments() -> str:
    with _service("4.10") as client:
        track = _create(client)
        bodies = [_segment(f"s{index}", 0, 10) for index in range(BURST_SIZE)]
        outcomes = _burst("POST", f"/tracks/{track.id}/segments", bodies)
        _expect_one_winner(outcomes, "segment_overlap", f"{BURST_SIZE} simultaneous R3")
        winner = bodies[outcomes.index((201, None))]["label"]
        rows = _segment_rows(track.id)
    if rows != [(winner, 0, 10)]:
        raise CheckFailed(
            f"`track_segments` rows of the track are {rows}, expected exactly [({winner!r}, 0, 10)] "
            "(the request answered 201)"
        )
    return (
        f"{BURST_SIZE} simultaneous R3 with distinct labels and one span: one 201, "
        f"{BURST_SIZE - 1} x 409 segment_overlap, one segment row"
    )


# --- 4.11 no in-memory copy -------------------------------------------------------------


def c4_11_sql_change_is_visible() -> str:
    with _service("4.11") as client:
        track = _create(client, capacity=100)
        _get(client, track.id)  # gives a cache, if there is one, the chance to fill
        try:
            updated = db.execute(
                "UPDATE public.tracks SET capacity = 500 WHERE id = %s::uuid", [track.id], db=STEP_DB
            )
        except (psycopg.ProgrammingError, psycopg.DataError) as exc:
            raise CheckFailed(f"UPDATE public.tracks failed: {str(exc).strip()}") from exc
        if updated != 1:
            raise CheckFailed(f"UPDATE of the `tracks` row with id {track.id} changed {updated} row(s)")
        seen = _get(client, track.id)
    if seen.capacity != 500:
        raise CheckFailed(f"capacity set to 500 by SQL, the next GET returns {seen.capacity!r}")
    return "capacity 100 -> 500 written by SQL; the next GET returns 500"


def register(suite: Suite, ctx: Context) -> None:
    """Criteria 4.1–4.11, in contract order; each one owns its database."""
    del ctx  # step 4 needs nothing from the run context
    suite.add("4.1", c4_1_schema, step=STEP)
    suite.add("4.2", c4_2_downgrade_and_upgrade, step=STEP)
    suite.add("4.3", c4_3_survives_sigkill, step=STEP)
    suite.add("4.4", c4_4_second_process, step=STEP)
    suite.add("4.5", c4_5_recreated_database, step=STEP)
    suite.add("4.6", c4_6_track_rows, step=STEP)
    suite.add("4.7", c4_7_segment_rows, step=STEP)
    suite.add("4.8", c4_8_rejection_changes_no_row, step=STEP)
    suite.add("4.9", c4_9_simultaneous_create, step=STEP, repeat=REPEATED["4.9"])
    suite.add("4.10", c4_10_simultaneous_segments, step=STEP, repeat=REPEATED["4.10"])
    suite.add("4.11", c4_11_sql_change_is_visible, step=STEP)
