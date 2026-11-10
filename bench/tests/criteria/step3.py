"""Step 3 (L2-02) deterministic criteria 3.1–3.16 of contract §9.

Every check is independent: it starts the HTTP app itself with the command of
contract §2.2, creates the tracks it needs through R1 and stops the app again.
Track codes are random, so the checks also work when tracks outlive a process
(steps 4 and 5, where this module is re-run by the cascade, rule C5).

A request that the criterion expects to be rejected breaks exactly one rule
(contract §5), and every rejection is made on a track of its own, so a wrong
answer to one case cannot change the outcome of another.

Time limits: the start command gets the contract limit plus `config.TOLERANCE`
(30 + 5 s). The contract sets no limit for R1–R5; `REQUEST_TIMEOUT` only keeps a
hanging service from blocking the run. Nothing here sleeps for a fixed time.

Judge criteria of this step are not evaluated here; `JUDGE_CRITERIA` is the
hook for the rubric / judge task.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx

from benchverify import config, http, proc
from benchverify.common import Context
from benchverify.result import CheckFailed, Suite

STEP = 3
JUDGE_CRITERIA = ("CJ1", "CJ2", "3.J1", "3.J2", "3.J3", "3.J4")  # hook only

START_LIMIT = config.HEALTH_TIMEOUT + config.TOLERANCE
REQUEST_TIMEOUT = 15.0  # per request; not a contract limit
CAPACITY = 100
MAX_SEGMENTS = 8  # invariant I5
JSON_HEADERS = {"content-type": "application/json"}

Segment = tuple[str, int, int]  # label, span.start, span.end


# --- helpers ------------------------------------------------------------------


def _short(response: httpx.Response) -> str:
    return f"{response.status_code} {response.text[:300]!r}"


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _view(body: object, what: str) -> dict[str, Any]:
    """The required minimum of the Track representation (contract §4.2).

    Extra keys, in the track and in its segments, are allowed and dropped.
    """
    problem = None
    if not isinstance(body, dict):
        problem = "the body is not a JSON object"
    elif not all(isinstance(body.get(key), str) for key in ("id", "code", "status")):
        problem = "`id`, `code` and `status` must be strings"
    elif not _is_int(body.get("capacity")):
        problem = "`capacity` must be an integer"
    elif not isinstance(body.get("segments"), list):
        problem = "`segments` must be an array"
    if problem is not None or not isinstance(body, dict):
        raise CheckFailed(f"{what}: not a Track ({problem}): {json.dumps(body)[:300]}")
    segments = []
    for item in body["segments"]:
        span = item.get("span") if isinstance(item, dict) else None
        if not (
            isinstance(item, dict)
            and isinstance(item.get("label"), str)
            and isinstance(span, dict)
            and _is_int(span.get("start"))
            and _is_int(span.get("end"))
        ):
            raise CheckFailed(f"{what}: malformed segment {json.dumps(item)[:200]}")
        segments.append({"label": item["label"], "span": {"start": span["start"], "end": span["end"]}})
    return {
        "id": body["id"],
        "code": body["code"],
        "capacity": body["capacity"],
        "status": body["status"],
        "segments": segments,
    }


def _segments(*items: Segment) -> list[dict[str, Any]]:
    return [{"label": label, "span": {"start": start, "end": end}} for label, start, end in items]


def _labels(view: dict[str, Any]) -> list[str]:
    return [segment["label"] for segment in view["segments"]]


@dataclass
class _Api:
    """HTTP access to the running app; a lost connection is the service's fault."""

    app: proc.ManagedProcess
    client: httpx.Client

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self.client.request(method, path, timeout=REQUEST_TIMEOUT, **kwargs)
        except httpx.HTTPError as exc:
            state = "running" if self.app.alive() else f"exited with code {self.app.returncode()}"
            raise CheckFailed(
                f"{method} {path}: no response within {REQUEST_TIMEOUT:.0f} s "
                f"({exc.__class__.__name__}); the process is {state}\n{self.app.log_tail(20)}"
            ) from exc

    def ok(self, method: str, path: str, status: int, what: str, **kwargs: Any) -> dict[str, Any]:
        """A request that must succeed with `status` and a Track body."""
        response = self.request(method, path, **kwargs)
        if response.status_code != status:
            raise CheckFailed(f"{what}: expected {status}, got {_short(response)}")
        try:
            body = response.json()
        except ValueError:
            raise CheckFailed(f"{what}: the body is not JSON: {response.text[:300]!r}") from None
        return _view(body, what)

    def create(self, capacity: int = CAPACITY) -> dict[str, Any]:
        """R1 with a fresh code; step-2 behaviour, needed here only as a setup."""
        code = f"S3-{uuid.uuid4().hex[:12].upper()}"
        track = self.ok(
            "POST", "/tracks", 201, f"setup: POST /tracks (capacity {capacity})",
            json={"code": code, "capacity": capacity},
        )
        try:
            uuid.UUID(track["id"])
        except ValueError:
            raise CheckFailed(f"setup: POST /tracks returned id {track['id']!r}, not a UUID") from None
        return track

    def get(self, track_id: str, what: str = "") -> dict[str, Any]:
        return self.ok("GET", f"/tracks/{track_id}", 200, what or f"GET /tracks/{track_id}")

    def get_raw(self, track_id: str) -> Any:
        """The whole GET body, extra keys included (for "same body" comparisons)."""
        response = self.request("GET", f"/tracks/{track_id}")
        if response.status_code != 200:
            raise CheckFailed(f"GET /tracks/{track_id}: expected 200, got {_short(response)}")
        try:
            return response.json()
        except ValueError:
            raise CheckFailed(f"GET /tracks/{track_id}: the body is not JSON") from None

    def add_raw(self, track_id: str, label: str, start: int, end: int) -> httpx.Response:
        body = {"label": label, "span": {"start": start, "end": end}}
        return self.request("POST", f"/tracks/{track_id}/segments", json=body)

    def add(self, track_id: str, label: str, start: int, end: int, what: str = "") -> dict[str, Any]:
        """R3 that must be accepted."""
        what = what or f"POST segments {label} [{start}, {end})"
        body = {"label": label, "span": {"start": start, "end": end}}
        return self.ok("POST", f"/tracks/{track_id}/segments", 201, what, json=body)

    def track_with(self, *items: Segment, capacity: int = CAPACITY) -> dict[str, Any]:
        """A new track holding `items` (added in the given order)."""
        track = self.create(capacity)
        for label, start, end in items:
            track = self.add(track["id"], label, start, end, f"setup: POST segments {label} [{start}, {end})")
        return track

    def remove(self, track_id: str, label: str) -> httpx.Response:
        return self.request("DELETE", f"/tracks/{track_id}/segments/{label}")

    def seal(self, track_id: str) -> httpx.Response:
        return self.request("POST", f"/tracks/{track_id}/seal")


def _wait_listening(app: proc.ManagedProcess) -> None:
    """Poll until the app accepts connections on port 8000."""
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


@contextmanager
def _service(name: str) -> Iterator[_Api]:
    """The HTTP app started with the command of contract §2.2; always stopped."""
    proc.stop_agent_processes()  # port 8000 must be free
    with (
        proc.http_app(config.database_url(), log_dir=config.LOG_DIR / "proc", name=f"step3-{name}") as app,
        http.client() as client,
    ):
        _wait_listening(app)
        yield _Api(app, client)


def _expect_error(
    response: httpx.Response, status: int, code: str, what: str, field: str | None = None
) -> None:
    """`status` with the error body of contract §5 and the given code.

    For `validation_error`, `field` must be named by one `details` item.
    """
    if response.status_code != status or http.error_code(response) != code:
        raise CheckFailed(f"{what}: expected {status} {code}, got {_short(response)}")
    problem = http.error_body_problem(response)
    if problem is not None:
        raise CheckFailed(f"{what} -> {status} {code} but {problem}; body {response.text[:300]!r}")
    if field is not None:
        fields = [item["field"] for item in response.json()["error"]["details"]]
        if field not in fields:
            raise CheckFailed(f"{what} -> 422 but no `details` item has field {field!r}: {fields}")


def _expect_segments(view: dict[str, Any], wanted: list[dict[str, Any]], what: str) -> None:
    if view["segments"] != wanted:
        raise CheckFailed(f"{what}: segments {json.dumps(view['segments'])}, expected {json.dumps(wanted)}")


def _expect_same(before: Any, after: Any, what: str) -> None:
    if after != before:
        raise CheckFailed(
            f"{what}: GET returns another body\nbefore: {json.dumps(before)[:500]}\n"
            f"after:  {json.dumps(after)[:500]}"
        )


# --- 3.1–3.3 accepted segments ------------------------------------------------

_LONG_LABEL = "s0123456789_abcd"  # 16 characters, the longest allowed


def c3_1_add_segment() -> str:
    with _service("3.1") as api:
        track = api.create()
        wanted = _segments(("alpha", 0, 10))
        updated = api.add(track["id"], "alpha", 0, 10)
        if updated != {**track, "segments": wanted}:
            raise CheckFailed(
                f"POST segments alpha [0, 10) -> 201 but the body is not the updated track: "
                f"{json.dumps(updated)}; created track: {json.dumps(track)}"
            )
        _expect_same(updated, api.get(track["id"]), "after the accepted segment")
        # Boundary values of §3.2: shortest and longest label, the whole strip.
        for label, capacity, start, end in (("a", 1, 0, 1), (_LONG_LABEL, 10000, 0, 10000)):
            other = api.create(capacity)
            what = f"POST segments {label} [{start}, {end}) on capacity {capacity}"
            _expect_segments(api.add(other["id"], label, start, end, what), _segments((label, start, end)), what)
        return "segment alpha [0, 10) -> 201 with the updated track, GET confirms; boundary label / span values accepted"


def c3_2_segments_ordered() -> str:
    order: list[Segment] = [("mid", 40, 50), ("last", 80, 90), ("first", 0, 10), ("second", 20, 30)]
    with _service("3.2") as api:
        track = api.create()
        for index, (label, start, end) in enumerate(order, start=1):
            view = api.add(track["id"], label, start, end)
            wanted = _segments(*sorted(order[:index], key=lambda item: item[1]))
            _expect_segments(view, wanted, f"response to segment {index} ({label} [{start}, {end}))")
        _expect_segments(api.get(track["id"]), wanted, "GET after four segments")
        return f"inserted as {[item[0] for item in order]}, returned as {_labels(view)} by R3 and GET"


def c3_3_touching_spans() -> str:
    with _service("3.3") as api:
        track = api.track_with(("mid", 10, 20))
        api.add(track["id"], "after", 20, 30, "POST segments after [20, 30) touching [10, 20)")
        view = api.add(track["id"], "before", 0, 10, "POST segments before [0, 10) touching [10, 20)")
        _expect_segments(view, _segments(("before", 0, 10), ("mid", 10, 20), ("after", 20, 30)), "after both")
        return "[20, 30) and [0, 10) accepted next to [10, 20)"


# --- 3.4–3.9 rejected segments ------------------------------------------------


def c3_4_invalid_span() -> str:
    cases = ((5, 5), (7, 3), (0, 0))
    with _service("3.4") as api:
        for start, end in cases:
            track = api.create()
            response = api.add_raw(track["id"], "alpha", start, end)
            _expect_error(response, 409, "invalid_span", f"POST segments [{start}, {end})")
        return f"spans {list(cases)} -> 409 invalid_span"


def c3_5_span_out_of_bounds() -> str:
    with _service("3.5") as api:
        for start, end in ((90, 101), (100, 110)):
            track = api.create()
            response = api.add_raw(track["id"], "alpha", start, end)
            _expect_error(
                response, 409, "span_out_of_bounds", f"POST segments [{start}, {end}) on capacity {CAPACITY}"
            )
        track = api.create()
        what = f"POST segments [90, {CAPACITY}) on capacity {CAPACITY}"
        _expect_segments(api.add(track["id"], "alpha", 90, CAPACITY, what), _segments(("alpha", 90, CAPACITY)), what)
        return f"end 101 and 110 on capacity {CAPACITY} -> 409 span_out_of_bounds; end {CAPACITY} accepted"


def c3_6_label_taken() -> str:
    with _service("3.6") as api:
        track = api.track_with(("alpha", 0, 10))
        response = api.add_raw(track["id"], "alpha", 50, 60)
        _expect_error(response, 409, "segment_label_taken", "POST segments alpha [50, 60) with alpha present")
        other = api.create()
        api.add(other["id"], "alpha", 0, 10, "POST segments alpha [0, 10) in another track")
        return "second `alpha` in the track -> 409 segment_label_taken; `alpha` in another track accepted"


def c3_7_overlap() -> str:
    cases: list[tuple[str, int, int]] = [
        ("partial, left", 10, 25),
        ("partial, right", 35, 50),
        ("containing", 10, 50),
        ("contained", 25, 30),
        ("identical", 20, 40),
    ]
    with _service("3.7") as api:
        for kind, start, end in cases:
            track = api.track_with(("alpha", 20, 40))
            response = api.add_raw(track["id"], "beta", start, end)
            _expect_error(
                response, 409, "segment_overlap", f"POST segments [{start}, {end}) over [20, 40) ({kind})"
            )
        return "partial (both sides), containing, contained and identical spans -> 409 segment_overlap"


def _eight() -> list[Segment]:
    """Eight spans with gaps between them (no touching: that is 3.3)."""
    return [(f"seg{index}", index * 10, index * 10 + 5) for index in range(MAX_SEGMENTS)]


def c3_8_segment_limit() -> str:
    with _service("3.8") as api:
        track = api.create()
        for index, (label, start, end) in enumerate(_eight(), start=1):
            view = api.add(track["id"], label, start, end, f"POST segments, segment {index} of {MAX_SEGMENTS}")
        _expect_segments(view, _segments(*_eight()), "after the 8th segment")
        response = api.add_raw(track["id"], "ninth", 90, 95)
        _expect_error(response, 409, "segment_limit_reached", "POST segments, 9th segment")
        return "8 segments accepted; the 9th -> 409 segment_limit_reached"


def c3_9_rejection_has_no_effect() -> str:
    """Only the absence of effects is judged here; the codes are 3.4–3.8.

    A request that the service accepts (2xx) is not a rejection and is left to
    its own criterion; with no rejection at all the criterion fails.
    """
    two: list[Segment] = [("alpha", 20, 40), ("beta", 60, 70)]
    cases: list[tuple[str, list[Segment], Segment]] = [
        ("3.4 invalid span", two, ("gamma", 50, 50)),
        ("3.5 span out of bounds", two, ("gamma", 90, CAPACITY + 1)),
        ("3.6 label taken", two, ("alpha", 45, 55)),
        ("3.7 overlap", two, ("gamma", 30, 65)),
        ("3.8 segment limit", _eight(), ("ninth", 90, 95)),
    ]
    rejected, accepted = [], []
    with _service("3.9") as api:
        for name, existing, (label, start, end) in cases:
            track = api.track_with(*existing)
            before = api.get_raw(track["id"])
            response = api.add_raw(track["id"], label, start, end)
            if response.is_success:
                accepted.append(name)
                continue
            _expect_same(
                before,
                api.get_raw(track["id"]),
                f"{name}: after POST segments {label} [{start}, {end}) -> {response.status_code}",
            )
            rejected.append(name)
    if not rejected:
        raise CheckFailed(f"none of the requests was rejected, nothing to compare: {accepted}")
    note = f"; not rejected, see their own criteria: {accepted}" if accepted else ""
    return f"GET body unchanged after {len(rejected)} rejection(s): {rejected}{note}"


# --- 3.10–3.11 remove ---------------------------------------------------------


def c3_10_remove_segment() -> str:
    with _service("3.10") as api:
        track = api.track_with(("alpha", 0, 10), ("beta", 20, 30))
        what = "DELETE segments/alpha"
        view = api.ok("DELETE", f"/tracks/{track['id']}/segments/alpha", 200, what)
        if view != {**track, "segments": _segments(("beta", 20, 30))}:
            raise CheckFailed(f"{what} -> 200 but the body is not the track without alpha: {json.dumps(view)}")
        _expect_same(view, api.get(track["id"]), f"after {what}")
        reused = api.add(track["id"], "gamma", 0, 10, "POST segments gamma [0, 10) on the freed span")
        _expect_segments(reused, _segments(("gamma", 0, 10), ("beta", 20, 30)), "after reusing the freed span")
        return "DELETE alpha -> 200 with the track without it, GET confirms; [0, 10) reserved again"


def c3_11_remove_absent_segment() -> str:
    with _service("3.11") as api:
        track = api.track_with(("alpha", 0, 10))
        _expect_error(
            api.remove(track["id"], "beta"), 404, "segment_not_found", "DELETE segments/beta (track has alpha)"
        )
        empty = api.create()
        _expect_error(
            api.remove(empty["id"], "alpha"), 404, "segment_not_found", "DELETE segments/alpha (empty track)"
        )
        return "absent label on a track with another segment and on an empty track -> 404 segment_not_found"


# --- 3.12–3.14 seal -----------------------------------------------------------


def c3_12_seal() -> str:
    with _service("3.12") as api:
        track = api.track_with(("alpha", 0, 10), ("beta", 20, 30))
        view = api.ok("POST", f"/tracks/{track['id']}/seal", 200, "POST seal")
        if view != {**track, "status": "sealed"}:
            raise CheckFailed(
                f"POST seal -> 200 but the body is not the same track with status `sealed`: {json.dumps(view)}"
            )
        _expect_same(view, api.get(track["id"]), "after POST seal")
        return 'POST seal on a track with 2 segments -> 200, status "sealed"; GET confirms'


def c3_13_seal_empty() -> str:
    with _service("3.13") as api:
        track = api.create()
        _expect_error(api.seal(track["id"]), 409, "track_empty", "POST seal on a track without segments")
        after = api.get(track["id"])
        if after["status"] != "open":
            raise CheckFailed(f"after the rejected seal GET returns status {after['status']!r}, expected 'open'")
        return 'POST seal on an empty track -> 409 track_empty; status stays "open"'


def c3_14_sealed_track() -> str:
    with _service("3.14") as api:
        track = api.track_with(("alpha", 0, 10))
        api.ok("POST", f"/tracks/{track['id']}/seal", 200, "setup: POST seal")
        before = api.get_raw(track["id"])
        requests: tuple[tuple[str, Callable[[], httpx.Response]], ...] = (
            ("POST segments beta [20, 30)", lambda: api.add_raw(track["id"], "beta", 20, 30)),
            ("DELETE segments/alpha", lambda: api.remove(track["id"], "alpha")),
            ("POST seal", lambda: api.seal(track["id"])),
        )
        for what, send in requests:
            _expect_error(send(), 409, "track_sealed", f"{what} on a sealed track")
            _expect_same(before, api.get_raw(track["id"]), f"after {what} on a sealed track")
        return "add, remove and seal on a sealed track -> 409 track_sealed; GET body unchanged after each"


# --- 3.15 unknown id, request validation ---------------------------------------

_VALID = {"label": "alpha", "span": {"start": 0, "end": 10}}


def _body(**changes: Any) -> dict[str, Any]:
    """A valid R3 body with top-level fields replaced (`...` removes a field)."""
    body = {**_VALID, **changes}
    return {key: value for key, value in body.items() if value is not ...}


def _span(**changes: Any) -> dict[str, Any]:
    span = {"start": 0, "end": 10, **changes}
    return {"label": "alpha", "span": {key: value for key, value in span.items() if value is not ...}}


def _invalid_bodies() -> list[tuple[str, bytes, str]]:
    """(description, raw body, offending location): one field violated per body."""
    cases: list[tuple[str, Any, str]] = [
        ("label missing", _body(label=...), "label"),
        ("label is a number", _body(label=5), "label"),
        ("label is null", _body(label=None), "label"),
        ("label `Gamma` (upper case)", _body(label="Gamma"), "label"),
        ("label `1abc` (leading digit)", _body(label="1abc"), "label"),
        ("label `a-b` (hyphen)", _body(label="a-b"), "label"),
        ("label of 17 characters", _body(label=_LONG_LABEL + "e"), "label"),
        ("label empty", _body(label=""), "label"),
        ("span missing", _body(span=...), "span"),
        ("span is a string", _body(span="0-10"), "span"),
        ("span is null", _body(span=None), "span"),
        ("span is an array", _body(span=[0, 10]), "span"),
    ]
    for name, good in (("start", 0), ("end", 10)):
        location = f"span.{name}"
        cases += [
            (f"{location} missing", _span(**{name: ...}), location),
            (f"{location} is the string {str(good)!r}", _span(**{name: str(good)}), location),
            (f"{location} is a boolean", _span(**{name: True}), location),
            (f"{location} is null", _span(**{name: None}), location),
            (f"{location} is 2.5", _span(**{name: 2.5}), location),
            (f"{location} is -1", _span(**{name: -1}), location),
            (f"{location} is 10001", _span(**{name: 10001}), location),
        ]
    raw = [(name, json.dumps(body).encode(), location) for name, body, location in cases]
    raw.append(("body is not JSON", b'{"label": "alpha", "span": ', "body"))
    raw.append(("body is a JSON array", b"[1]", "body"))
    return raw


def c3_15_unknown_id_and_validation() -> str:
    with _service("3.15") as api:
        unknown = str(uuid.uuid4())
        api.create()  # the storage is not empty
        requests: tuple[tuple[str, Callable[[], httpx.Response]], ...] = (
            ("POST segments", lambda: api.add_raw(unknown, "alpha", 0, 10)),
            # Passes the schema, breaks I1: not found still comes before domain (§5).
            ("POST segments [7, 3)", lambda: api.add_raw(unknown, "alpha", 7, 3)),
            ("POST segments [5, 5)", lambda: api.add_raw(unknown, "alpha", 5, 5)),
            ("DELETE segments/alpha", lambda: api.remove(unknown, "alpha")),
            ("POST seal", lambda: api.seal(unknown)),
        )
        for what, send in requests:
            _expect_error(send(), 404, "track_not_found", f"{what} on an unknown id")

        bodies = _invalid_bodies()
        existing = api.create(10000)  # every `end` up to 10000 fits: only the schema is broken
        for target, track_id in (("an existing track", existing["id"]), ("an unknown id", unknown)):
            for name, content, location in bodies:
                response = api.request(
                    "POST", f"/tracks/{track_id}/segments", content=content, headers=JSON_HEADERS
                )
                what = f"POST segments on {target}, {name} ({content[:80].decode()})"
                _expect_error(response, 422, "validation_error", what, field=location)
        return (
            "R3 (also with an empty span: not found before domain), R4, R5 on an unknown id -> "
            f"404 track_not_found; {len(bodies)} schema-invalid R3 bodies "
            "-> 422 validation_error naming the field, on an existing track and on an unknown id"
        )


# --- 3.16 path parameters ------------------------------------------------------

_BAD_PATH_LABELS = ("Gamma", "1abc", "a-b", _LONG_LABEL + "e")


def c3_16_path_validation() -> str:
    bad_id = "not-a-uuid"
    with _service("3.16") as api:
        track = api.track_with(("alpha", 0, 10))
        requests: tuple[tuple[str, Callable[[], httpx.Response]], ...] = (
            ("POST segments", lambda: api.add_raw(bad_id, "beta", 20, 30)),
            ("DELETE segments/alpha", lambda: api.remove(bad_id, "alpha")),
            ("POST seal", lambda: api.seal(bad_id)),
        )
        for what, send in requests:
            _expect_error(
                send(), 422, "validation_error", f"{what} with track_id `{bad_id}`", field="track_id"
            )
        # Validation comes before not found (§5): the unknown id changes nothing.
        for target, track_id in (("an existing track", track["id"]), ("an unknown id", str(uuid.uuid4()))):
            for label in _BAD_PATH_LABELS:
                _expect_error(
                    api.remove(track_id, label), 422, "validation_error",
                    f"DELETE segments/{label} on {target}", field="label",
                )
        return (
            f"R3, R4, R5 with track_id `{bad_id}` -> 422 validation_error, field track_id; DELETE with labels "
            f"{list(_BAD_PATH_LABELS)} -> 422, field label (existing track and unknown id)"
        )


def register(suite: Suite, ctx: Context) -> None:
    """Criteria 3.1–3.16, in contract order; each starts its own app process."""
    del ctx  # step 3 needs nothing from the run context
    suite.add("3.1", c3_1_add_segment, step=STEP)
    suite.add("3.2", c3_2_segments_ordered, step=STEP)
    suite.add("3.3", c3_3_touching_spans, step=STEP)
    suite.add("3.4", c3_4_invalid_span, step=STEP)
    suite.add("3.5", c3_5_span_out_of_bounds, step=STEP)
    suite.add("3.6", c3_6_label_taken, step=STEP)
    suite.add("3.7", c3_7_overlap, step=STEP)
    suite.add("3.8", c3_8_segment_limit, step=STEP)
    suite.add("3.9", c3_9_rejection_has_no_effect, step=STEP)
    suite.add("3.10", c3_10_remove_segment, step=STEP)
    suite.add("3.11", c3_11_remove_absent_segment, step=STEP)
    suite.add("3.12", c3_12_seal, step=STEP)
    suite.add("3.13", c3_13_seal_empty, step=STEP)
    suite.add("3.14", c3_14_sealed_track, step=STEP)
    suite.add("3.15", c3_15_unknown_id_and_validation, step=STEP)
    suite.add("3.16", c3_16_path_validation, step=STEP)
