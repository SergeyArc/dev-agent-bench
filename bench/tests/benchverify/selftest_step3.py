"""Self-test of the step-3 criteria (`criteria/step3.py`).

Called from `benchverify.selftest`; `python -m benchverify.selftest_step3` runs
these scenarios alone (same container, see tests/selftest.sh).

The fixture service (tests/selftest_fixtures/step3/base, laid over the step-1
fixture) passes a full step-3 run: the common criteria, the criteria of every
earlier step that has a module, and 3.1–3.16. Each broken variant is one text
replacement in a fixture file and makes exactly the named criteria fail.
"""

from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from criteria import step3

from . import common, config, db, http, proc, reset, runner, selftest
from .result import Status, Suite
from .selftest import expect
from .state import State

FIXTURES = Path(__file__).resolve().parent.parent / "selftest_fixtures" / "step3"
STEP3_IDS = [f"3.{n}" for n in range(1, 17)]

DOMAIN = "src/tracksvc/domain/track.py"
API = "src/tracksvc/api/app.py"


@dataclass(frozen=True)
class Variant:
    """One replacement in a fixture file and the step-3 criteria it must break."""

    path: str
    old: str
    new: str
    broken: frozenset[str]


VARIANTS = {
    # The limit is checked after the segment is stored: the 9th request is
    # answered with 409 and still changes the track.
    "mutate_then_validate": Variant(
        DOMAIN,
        "        if len(self._segments) >= MAX_SEGMENTS:\n"
        '            raise DomainError("segment_limit_reached")\n'
        "        self._segments.append(Segment(label, span))\n",
        "        self._segments.append(Segment(label, span))\n"
        "        if len(self._segments) > MAX_SEGMENTS:\n"
        '            raise DomainError("segment_limit_reached")\n',
        frozenset({"3.9"}),
    ),
    # Closed intervals: spans that only touch count as overlapping.
    "touching_overlaps": Variant(
        DOMAIN,
        "return self.start < other.end and other.start < self.end",
        "return self.start <= other.end and other.start <= self.end",
        frozenset({"3.3"}),
    ),
    # `start < end` treated as a schema rule: 422 instead of 409 invalid_span.
    "empty_span_is_validation": Variant(
        API,
        "    return label, Span(start, end)\n",
        "    if start >= end:\n"
        '        raise InvalidRequestError("span.end")\n'
        "    return label, Span(start, end)\n",
        # 3.15 as well: on an unknown id the empty span gives 422, not 404.
        frozenset({"3.4", "3.15"}),
    ),
    # The span is judged before the track is looked up: an empty span sent to
    # an unknown id gives 409 invalid_span instead of 404 track_not_found.
    "domain_before_not_found": Variant(
        API,
        "    label, span = _segment(await _json_object(request))\n",
        "    label, span = _segment(await _json_object(request))\n"
        "    if span.start >= span.end:\n"
        '        raise DomainError("invalid_span")\n',
        frozenset({"3.15"}),
    ),
    # The label in the path of DELETE is not validated: 404 instead of 422.
    "path_label_unchecked": Variant(
        API,
        "    if LABEL.fullmatch(label) is None:\n"
        '        raise InvalidRequestError("label")\n',
        "",
        frozenset({"3.16"}),
    ),
    # A segment can be removed from a sealed track.
    "sealed_track_shrinks": Variant(
        DOMAIN,
        "        self._require_open()\n        kept = ",
        "        kept = ",
        frozenset({"3.14"}),
    ),
}


def install_fixture(variant: Variant | None = None) -> None:
    """Replace /app content with the step-3 fixture service, optionally broken."""
    selftest.install_step1_fixture()
    root = FIXTURES / "base"
    files = sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    expect(bool(files), f"fixture directory {root} is empty")
    for path in files:
        relative = str(path.relative_to(root))
        content = path.read_text()
        if variant is not None and relative == variant.path:
            expect(content.count(variant.old) == 1, f"{relative}: the text to replace must occur once")
            content = content.replace(variant.old, variant.new)
        selftest.agent_write(relative, content)
    expect(variant is None or (root / variant.path).is_file(), f"no fixture file {variant and variant.path}")


def _cascade_ids(step: int) -> list[str]:
    """IDs registered by the criteria modules of steps 1…step that exist."""
    probe = Suite()
    ctx = common.Context(step=step, state=State(), work_dir=Path("/nonexistent"))
    for number in range(1, step + 1):
        try:
            module = importlib.import_module(f"criteria.step{number}")
        except ModuleNotFoundError as exc:
            if exc.name != f"criteria.step{number}":
                raise
            continue
        module.register(probe, ctx)
    return [entry.id for entry in probe.entries]


def s_fixture_passes() -> str:
    """Full step-3 run on the fixture service: every scored criterion passes."""
    install_fixture()
    http.OBSERVED.clear()
    state = State().load()
    state.data = {"steps": {}}
    state.save()
    runner.run(3)
    report = json.loads((config.LOG_DIR / "criteria.json").read_text())
    results = {c["id"]: (c["status"], c["evidence"]) for c in report["criteria"]}
    cascade = _cascade_ids(3)
    expect(cascade[-len(STEP3_IDS) :] == STEP3_IDS, f"step-3 criteria are not last in the cascade: {cascade}")
    ids = [*selftest.COMMON_IDS, *cascade, "C6"]
    expect(list(results) == [*ids, "CJ1"], f"criteria: {list(results)}")
    problems = selftest._problems({cid: results[cid] for cid in ids}, dict.fromkeys(ids, "pass"))
    expect(not problems, problems)
    cj1 = report["criteria"][-1]
    # `skipped` when no snapshot of step 2 is on disk, `evidence` when an earlier
    # scenario left one; never scored.
    expect(cj1["status"] in ("skipped", "evidence") and not cj1["scored"], f"CJ1 entry: {cj1}")
    expect(report["reward"] == 1.0, f"reward: {report['reward']}")
    expect(report["regressions"] == {"count": 0, "ids": []}, f"regressions: {report['regressions']}")
    left = db.admin_sql("SELECT count(*) FROM pg_database WHERE datname LIKE 'tracksvc\\_step%'")
    expect(left == "0", "databases of the step checks were not dropped")
    expect(not proc.agent_pids(), "agent processes left after the run")
    steps = sorted({cid.split(".")[0] for cid in cascade})
    return f"{len(ids)} criteria pass on the fixture service (steps {', '.join(steps)}), reward 1.0"


def variant_scenario(name: str):  # type: ignore[no-untyped-def]
    """Step-3 criteria on a broken variant: exactly `broken` fail."""
    variant = VARIANTS[name]

    def scenario() -> str:
        install_fixture(variant)
        reset.reset()
        suite = Suite()
        ctx = common.Context(step=3, state=State().load(), work_dir=common.make_work_dir())
        step3.register(suite, ctx)
        results = {r.id: (r.status.value, r.evidence) for r in suite.run()}
        expect(list(results) == STEP3_IDS, f"criteria: {list(results)}")
        wanted = {cid: "fail" if cid in variant.broken else "pass" for cid in STEP3_IDS}
        problems = selftest._problems(results, wanted)
        expect(not problems, problems)
        return (
            f"{', '.join(sorted(variant.broken))} fail "
            f"({results[min(variant.broken)][1].splitlines()[0][:160]}), "
            f"the other {len(STEP3_IDS) - len(variant.broken)} pass"
        )

    return scenario


def s_empty_app() -> str:
    """No service in /app: every step-3 criterion fails, none errors."""
    selftest.restore_app()
    reset.reset()
    suite = Suite()
    ctx = common.Context(step=3, state=State().load(), work_dir=common.make_work_dir())
    step3.register(suite, ctx)
    statuses = {r.id: r.status.value for r in suite.run()}
    expect(set(statuses.values()) == {"fail"}, f"step-3 criteria on the empty /app: {statuses}")
    return f"all {len(statuses)} step-3 criteria fail on the empty /app, none errors"


def register(suite: Suite) -> None:
    """Add the step-3 scenarios to a self-test suite."""
    suite.add("step3-fixture-passes", s_fixture_passes)
    for name in VARIANTS:
        suite.add(f"step3-variant-{name}", variant_scenario(name))
    suite.add("step3-empty-app", s_empty_app)


def main() -> int:
    (config.LOG_DIR / "selftest").mkdir(parents=True, exist_ok=True)
    reset.ensure_infrastructure()
    suite = Suite()
    register(suite)
    try:
        results = suite.run()
    finally:
        proc.stop_agent_processes()
        selftest.restore_app()
    print()
    for result in results:
        print(f"{result.status.value.upper():7} {result.id}: {result.evidence}")
    failed = [r for r in results if r.status is not Status.PASS]
    print(f"\nself-test (step 3): {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
