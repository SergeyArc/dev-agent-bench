"""Step 2 (L2-01) deterministic criteria 2.1–2.9 of contract §9.

Every check starts the HTTP app itself with the command of contract §2.2,
talks to it over HTTP only and stops it again; no check relies on a process or
on tracks left by another one. Track codes are generated per request, so the
checks also hold from step 4 on, when tracks outlive the process (cascade, C5).

Split of responsibilities between the criteria:
  2.4–2.8 look at the status, the error code and (validation) the `field`
          of a `details` item;
  2.9     looks at the shape of every error body (exact keys of §5).

Time limits: the contract limit plus `config.TOLERANCE` for the start command
(30 + 5 s); the contract sets no limit for R1 / R2, every request has its own
timeout `REQUEST_TIMEOUT`. Nothing here sleeps for a fixed time: the start is
polled.

Judge criteria of this step are not evaluated here; `JUDGE_CRITERIA` is the
hook for the rubric / judge task.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from benchverify import config, http, proc
from benchverify.common import Context
from benchverify.result import CheckFailed, Suite

STEP = 2
IDS = tuple(f"2.{n}" for n in range(1, 10))
JUDGE_CRITERIA = ("CJ1", "CJ2", "2.J1", "2.J2", "2.J3")  # hook only: evaluated by the judge stage

START_LIMIT = config.HEALTH_TIMEOUT + config.TOLERANCE
REQUEST_TIMEOUT = 15.0

CANONICAL_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
JSON_HEADERS = {"content-type": "application/json"}
MAX_CAPACITY = 10000
CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"

# Path values that no UUID parser accepts (2.5).
BAD_TRACK_IDS = (
    "not-a-uuid",
    "123",
    "5a2f0c4e-0000-4000-8000-00000000000",  # one character short
    "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz",
)

# Bodies of `POST /tracks` that are not valid JSON or not a JSON object (2.7).
BAD_BODIES: tuple[tuple[str, bytes], ...] = (
    ("truncated JSON", b'{"code": "TRK-A1", "capacity": '),
    ("plain text", b"not json"),
    ("empty body", b""),
    ("array [1]", b"[1]"),
    ("empty array", b"[]"),
    ("array holding a valid object", b'[{"code": "TRK-A1", "capacity": 100}]'),
    ("string", b'"TRK-A1"'),
    ("number", b"5"),
    ("null", b"null"),
    ("true", b"true"),
)


# --- helpers ------------------------------------------------------------------


def _fresh_code() -> str:
    """A valid `code` not used before in this run (13 characters, random)."""
    return "T" + uuid.uuid4().hex[:12].upper()


def _random_code(length: int) -> str:
    """A valid `code` of exactly `length` characters over the whole alphabet."""
    digits = uuid.uuid4().int
    out = ["Z"]
    while len(out) < length:
        digits, index = divmod(digits, len(CODE_ALPHABET))
        out.append(CODE_ALPHABET[index])
    return "".join(out)


def _short(response: httpx.Response) -> str:
    return f"{response.status_code} {response.text[:300]!r}"


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


@dataclass
class Service:
    """The HTTP app under test and a client bound to it."""

    app: proc.ManagedProcess
    client: httpx.Client

    def request(self, method: str, path: str, *, content: bytes | None = None) -> httpx.Response:
        """One request with its own timeout; no answer is a failed criterion."""
        try:
            return self.client.request(
                method,
                path,
                content=content,
                headers=JSON_HEADERS if content is not None else None,
                timeout=REQUEST_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            state = "running" if self.app.alive() else f"exited with code {self.app.returncode()}"
            raise CheckFailed(
                f"{method} {path}: no response within {REQUEST_TIMEOUT:.0f} s "
                f"({exc.__class__.__name__}); the process is {state}\n{self.app.log_tail(20)}"
            ) from exc

    def post_raw(self, raw: bytes) -> httpx.Response:
        return self.request("POST", "/tracks", content=raw)

    def post(self, body: Any) -> httpx.Response:
        return self.post_raw(json.dumps(body).encode())

    def get(self, track_id: str) -> httpx.Response:
        return self.request("GET", f"/tracks/{track_id}")

    def create(self, code: str | None = None, capacity: int = 100) -> dict[str, Any]:
        """Create a track that a criterion needs as a precondition; returns its body."""
        code = code or _fresh_code()
        response = self.post({"code": code, "capacity": capacity})
        problem = _track_problem(response, code, capacity)
        if problem is not None:
            raise CheckFailed(f"precondition: POST /tracks with a valid body: {problem}")
        return dict(response.json())


@contextmanager
def _service(name: str) -> Iterator[Service]:
    """The HTTP app started with the command of contract §2.2; always stopped."""
    proc.stop_agent_processes()  # port 8000 must be free
    with (
        proc.http_app(config.database_url(), log_dir=config.LOG_DIR / "proc", name=f"step2-{name}") as app,
        http.client() as client,
    ):
        started = time.monotonic()
        while not proc.port_open(config.HTTP_PORT):
            elapsed = time.monotonic() - started
            if not app.alive():
                raise CheckFailed(
                    f"the process exited with code {app.returncode()} after {elapsed:.1f} s "
                    f"without listening on port {config.HTTP_PORT}\n{app.log_tail(20)}"
                )
            if elapsed >= START_LIMIT:
                raise CheckFailed(
                    f"the app did not listen on port {config.HTTP_PORT} within {START_LIMIT:.0f} s\n"
                    f"{app.log_tail(20)}"
                )
            time.sleep(0.25)
        yield Service(app, client)


def _track_problem(response: httpx.Response, code: str, capacity: int, status: int = 201) -> str | None:
    """Why the response is not `status` with the Track of §4.2 just created; None if it is."""
    if response.status_code != status:
        return f"expected {status}, got {_short(response)}"
    body = _json(response)
    if not isinstance(body, dict):
        return f"the body is not a JSON object: {response.text[:300]!r}"
    if not isinstance(body.get("id"), str):
        return f"`id` is not a string: {body.get('id')!r}"
    wanted = {"code": code, "capacity": capacity, "status": "open", "segments": []}
    for key, value in wanted.items():
        if key not in body:
            return f"key `{key}` is missing: {body!r}"
        if body[key] != value or type(body[key]) is not type(value):
            return f"`{key}` is {body[key]!r}, expected {value!r}"
    return None


def _error_problem(response: httpx.Response, status: int, code: str, field: str | None = None) -> str | None:
    """Why the response is not `status` with error code `code` (and, for a
    validation error, a `details` item whose `field` is `field`); None if it is.
    The exact shape of the error body is criterion 2.9, not checked here."""
    if response.status_code != status:
        return f"expected {status} {code}, got {_short(response)}"
    found = http.error_code(response)
    if found != code:
        return f"expected error code {code!r}, got {found!r}: {response.text[:300]!r}"
    if field is None:
        return None
    details = response.json()["error"].get("details")
    items = details if isinstance(details, list) else []
    fields = [item.get("field") for item in items if isinstance(item, dict)]
    if field not in fields:
        return f"no `details` item with `field` = {field!r}; fields: {fields!r}"
    return None


def _fail_if(problems: list[str], total: int, what: str) -> None:
    if problems:
        raise CheckFailed(f"{len(problems)} of {total} {what} failed:\n" + "\n".join(problems))


# --- request matrices ---------------------------------------------------------


def _invalid_field_cases() -> list[tuple[str, str, dict[str, Any]]]:
    """(label, violated field, body) for 2.6: one field violated per request,
    the other one valid and fresh."""
    missing = object()
    code_values: list[tuple[str, Any]] = [
        ("missing", missing),
        ("a number", 123),
        ("true", True),
        ("null", None),
        ("an array", ["TRK-A1"]),
        ("an object", {"value": "TRK-A1"}),
        ("a non-integral number", 12.5),
        ("an empty string", ""),
        ("2 characters", "AB"),
        ("33 characters", "A" * 33),
        ("lower case", "trk-a1"),
        ("a lower-case letter inside", "TRKa1"),
        ("a leading digit", "1TRK"),
        ("a leading hyphen", "-TRK"),
        ("an underscore", "TRK_A1"),
        ("a space", "TRK A1"),
        ("a dot", "TRK.A1"),
        ("a non-ASCII letter", "TRK-Ä1"),
    ]
    capacity_values: list[tuple[str, Any]] = [
        ("missing", missing),
        ('the string "5"', "5"),
        ("true", True),
        ("false", False),
        ("null", None),
        ("5.5", 5.5),
        ("an array", [5]),
        ("an object", {"value": 5}),
        ("0", 0),
        ("-1", -1),
        ("10001", MAX_CAPACITY + 1),
        ("2**63", 2**63),
    ]
    cases: list[tuple[str, str, dict[str, Any]]] = []
    for label, value in code_values:
        body: dict[str, Any] = {"capacity": 100}
        if value is not missing:
            body["code"] = value
        cases.append((f"`code` {label}", "code", body))
    for label, value in capacity_values:
        body = {"code": _fresh_code()}
        if value is not missing:
            body["capacity"] = value
        cases.append((f"`capacity` {label}", "capacity", body))
    return cases


# --- 2.1–2.3 create and read ---------------------------------------------------


def c2_1_create() -> str:
    cases = [
        (_fresh_code(), 100),
        (_random_code(3), 1),  # shortest code, smallest capacity
        (_random_code(32), MAX_CAPACITY),  # longest code, largest capacity
        (f"T-{uuid.uuid4().hex[:8].upper()}-9", 5000),
    ]
    with _service("2.1") as service:
        problems = []
        for code, capacity in cases:
            body = {"code": code, "capacity": capacity}
            problem = _track_problem(service.post(body), code, capacity)
            if problem is not None:
                problems.append(f"POST /tracks {json.dumps(body)}: {problem}")
    _fail_if(problems, len(cases), "valid requests")
    return f"{len(cases)} valid bodies -> 201 with the sent code and capacity, status open, segments []"


def c2_2_ids() -> str:
    count = 5
    with _service("2.2") as service:
        ids = [service.create()["id"] for _ in range(count)]
    bad = [value for value in ids if CANONICAL_UUID.fullmatch(value) is None]
    if bad:
        raise CheckFailed(f"`id` is not a canonical lower-case hyphenated UUID string: {bad!r}")
    if len(set(ids)) != len(ids):
        repeated = sorted(value for value, seen in Counter(ids).items() if seen > 1)
        raise CheckFailed(f"created tracks share an id: {repeated!r}")
    return f"{count} created tracks: canonical UUID ids, all different"


def c2_3_read() -> str:
    count = 3
    with _service("2.3") as service:
        created = [service.create(capacity=10 * (n + 1)) for n in range(count)]
        problems = []
        for attempt in (1, 2):  # the second pass: reading does not change the track
            for track in reversed(created):
                response = service.get(track["id"])
                if response.status_code != 200 or _json(response) != track:
                    problems.append(
                        f"GET /tracks/{track['id']} (pass {attempt}): got {_short(response)}, "
                        f"expected 200 {json.dumps(track)}"
                    )
    _fail_if(problems, 2 * count, "reads")
    return f"{count} tracks read twice each: 200, body equal to the creation response"


# --- 2.4–2.8 errors -------------------------------------------------------------


def c2_4_not_found() -> str:
    with _service("2.4") as service:
        problems = []

        def probe(label: str, track_id: str) -> None:
            problem = _error_problem(service.get(track_id), 404, "track_not_found")
            if problem is not None:
                problems.append(f"GET /tracks/{track_id} ({label}): {problem}")

        probe("no track created yet", str(uuid.uuid4()))
        existing = str(service.create()["id"])
        probe("another track exists", str(uuid.uuid4()))
        probe("last digit differs from an existing id", existing[:-1] + ("1" if existing[-1] == "0" else "0"))
    _fail_if(problems, 3, "requests for an unknown id")
    return "3 unknown ids -> 404 track_not_found"


def c2_5_invalid_track_id() -> str:
    with _service("2.5") as service:
        problems = []
        for track_id in BAD_TRACK_IDS:
            problem = _error_problem(service.get(track_id), 422, "validation_error", "track_id")
            if problem is not None:
                problems.append(f"GET /tracks/{track_id}: {problem}")
    _fail_if(problems, len(BAD_TRACK_IDS), "requests with a malformed track_id")
    return f"{len(BAD_TRACK_IDS)} malformed ids -> 422 validation_error with field track_id"


def c2_6_invalid_fields() -> str:
    cases = _invalid_field_cases()
    with _service("2.6") as service:
        problems = []
        for label, field, body in cases:
            problem = _error_problem(service.post(body), 422, "validation_error", field)
            if problem is not None:
                problems.append(f"{label} — POST /tracks {json.dumps(body)}: {problem}")
        # Validation never depends on stored tracks and precedes the conflict.
        taken = service.create()["code"]
        body = {"code": taken, "capacity": 0}
        problem = _error_problem(service.post(body), 422, "validation_error", "capacity")
        if problem is not None:
            problems.append(f"`capacity` 0 with a code that is already taken — POST /tracks {json.dumps(body)}: {problem}")
    _fail_if(problems, len(cases) + 1, "requests violating one field")
    return f"{len(cases) + 1} requests violating one field -> 422 validation_error naming that field"


def c2_7_invalid_body() -> str:
    with _service("2.7") as service:
        problems = []
        for label, raw in BAD_BODIES:
            problem = _error_problem(service.post_raw(raw), 422, "validation_error", "body")
            if problem is not None:
                problems.append(f"{label} — POST /tracks {raw!r}: {problem}")
    _fail_if(problems, len(BAD_BODIES), "requests with a malformed body")
    return f"{len(BAD_BODIES)} bodies that are not a JSON object -> 422 validation_error with field body"


def c2_8_duplicate_code() -> str:
    with _service("2.8") as service:
        first = service.create(capacity=100)
        code = first["code"]
        # The differing capacity goes last: an overwrite by it must stay visible below.
        for label, capacity in (("the same capacity", 100), ("another capacity", 5)):
            response = service.post({"code": code, "capacity": capacity})
            problem = _error_problem(response, 409, "track_code_taken")
            if problem is not None:
                raise CheckFailed(f"second POST /tracks with code {code!r} and {label}: {problem}")
        response = service.get(first["id"])
        if response.status_code != 200 or _json(response) != first:
            raise CheckFailed(
                f"the first track changed after the rejected requests: GET /tracks/{first['id']} -> "
                f"{_short(response)}, expected 200 {json.dumps(first)}"
            )
        other = service.create()
        if other["id"] == first["id"]:
            raise CheckFailed(f"a track with another code got the id of the first one: {other!r}")
    return "duplicate code -> 409 track_code_taken (twice); the first track reads back unchanged"


# --- 2.9 error body schema -------------------------------------------------------


def c2_9_error_schema() -> str:
    with _service("2.9") as service:
        taken = service.create()["code"]
        sent: list[tuple[str, httpx.Response]] = [
            (f"GET /tracks/{track_id}", service.get(track_id))
            for track_id in (str(uuid.uuid4()), *BAD_TRACK_IDS)
        ]
        for _label, _field, body in _invalid_field_cases():
            sent.append((f"POST /tracks {json.dumps(body)}", service.post(body)))
        for _label, raw in BAD_BODIES:
            sent.append((f"POST /tracks {raw!r}", service.post_raw(raw)))
        duplicate = {"code": taken, "capacity": 7}
        sent.append((f"POST /tracks {json.dumps(duplicate)} (code taken)", service.post(duplicate)))
    errors = [(what, response) for what, response in sent if response.status_code >= 400]
    if not errors:
        raise CheckFailed(f"none of {len(sent)} erroneous requests got an error response")
    problems = []
    for what, response in errors:
        problem = http.error_body_problem(response)
        if problem is not None:
            problems.append(f"{what} -> {response.status_code}: {problem}; body {response.text[:300]!r}")
    _fail_if(problems, len(errors), "error responses")
    codes = Counter(http.error_code(response) for _what, response in errors)
    summary = ", ".join(f"{code} x{count}" for code, count in sorted(codes.items(), key=str))
    return f"{len(errors)} error responses match the error body schema ({summary})"


def register(suite: Suite, ctx: Context) -> None:
    """Criteria 2.1–2.9, in contract order; each one is independent."""
    del ctx  # step 2 needs nothing from the run context
    suite.add("2.1", c2_1_create, step=STEP)
    suite.add("2.2", c2_2_ids, step=STEP)
    suite.add("2.3", c2_3_read, step=STEP)
    suite.add("2.4", c2_4_not_found, step=STEP)
    suite.add("2.5", c2_5_invalid_track_id, step=STEP)
    suite.add("2.6", c2_6_invalid_fields, step=STEP)
    suite.add("2.7", c2_7_invalid_body, step=STEP)
    suite.add("2.8", c2_8_duplicate_code, step=STEP)
    suite.add("2.9", c2_9_error_schema, step=STEP)
