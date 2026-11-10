"""Self-test of the step-2 criteria (`criteria/step2.py`); registered from
`selftest.main()`.

The fixture service is `selftest_fixtures/step1/base` with
`selftest_fixtures/step2/base` laid over it; a broken variant is one more
overlay. Scenarios:
  - a full run of step 1 on the step-1 fixture, then of step 2 on the step-2
    fixture: every scored criterion of both steps passes (cascade), no
    regressions, CJ1 evidence is collected;
  - on each broken variant exactly the named step-2 criteria fail;
  - CJ1: a `.py` file of `tests/` that cannot be parsed raises the
    `evidence_incomplete` flag (evidence.py).
"""

from __future__ import annotations

import json
from pathlib import Path

from criteria import step2

from . import common, config, evidence, http, proc, reset, runner
from . import selftest as base
from .result import Suite
from .state import State

FIXTURES = Path(__file__).resolve().parent.parent / "selftest_fixtures" / "step2"
STEP2_IDS = list(step2.IDS)
# Overlay directory -> the step-2 criteria it must break; all others must pass.
VARIANTS = {
    "prefixed_fields": {"2.5", "2.6"},  # `body.capacity`, `path.track_id` instead of the bare names
    "coercing": {"2.6"},  # "5", true and 5.5 accepted as an integer
    "conflict_overwrites": {"2.8"},  # 409, but the stored track takes the new capacity
    "details_always": {"2.9"},  # `details: []` in the 404 and 409 bodies
}


def install_fixture(overlay: str | None = None) -> None:
    """Replace /app content with the step-2 fixture service, optionally a broken variant."""
    base.install_step1_fixture()
    for layer in ("base", overlay):
        if layer is None:
            continue
        root = FIXTURES / layer
        files = sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        base.expect(bool(files), f"fixture directory {root} is empty")
        for path in files:
            base.agent_write(str(path.relative_to(root)), path.read_text())


def s_fixture_passes() -> str:
    """Steps 1 and 2 in a row, as in a trial: all scored criteria pass at step 2."""
    base.install_step1_fixture()
    http.OBSERVED.clear()
    state = State().load()
    state.data = {"steps": {}}
    state.save()
    runner.run(1)
    first = json.loads((config.LOG_DIR / "criteria.json").read_text())
    base.expect(first["reward"] == 1.0, f"step 1 on the step-1 fixture: reward {first['reward']}")

    install_fixture()
    runner.run(2)
    report = json.loads((config.LOG_DIR / "criteria.json").read_text())
    results = {c["id"]: (c["status"], c["evidence"]) for c in report["criteria"]}
    scored = [*base.COMMON_IDS, *base.STEP1_IDS, *STEP2_IDS, "C6"]
    base.expect(list(results) == [*scored, "CJ1"], f"criteria: {list(results)}")
    wanted = {**dict.fromkeys(scored, "pass"), "CJ1": "evidence"}
    problems = base._problems(results, wanted)
    base.expect(not problems, problems)
    base.expect(report["reward"] == 1.0, f"reward: {report['reward']}")
    base.expect(report["regressions"] == {"count": 0, "ids": []}, f"regressions: {report['regressions']}")
    steps = {c["id"]: c["step"] for c in report["criteria"]}
    base.expect(all(steps[cid] == 2 for cid in STEP2_IDS), f"step of the step-2 criteria: {steps}")
    cj1 = report["cj1_evidence"]
    base.expect(
        cj1["missing_node_ids"] == [] and cj1["files"]["added"] == ["tests/test_tracks.py"],
        f"CJ1 evidence: {cj1['missing_node_ids']} {cj1['files']}",
    )
    base.expect(cj1["evidence_incomplete"] is False, f"CJ1 incomplete: {cj1['incomplete']}")
    base.expect(not proc.agent_pids(), "agent processes left after the run")
    return f"{len(scored)} criteria pass at step 2 on the fixture service (cascade included), reward 1.0"


def variant(overlay: str, broken: set[str]):  # type: ignore[no-untyped-def]
    """Step-2 criteria on a broken variant: exactly `broken` fail."""

    def scenario() -> str:
        install_fixture(overlay)
        reset.reset()
        suite = Suite()
        ctx = common.Context(step=2, state=State().load(), work_dir=common.make_work_dir())
        step2.register(suite, ctx)
        results = {r.id: (r.status.value, r.evidence) for r in suite.run()}
        base.expect(list(results) == STEP2_IDS, f"criteria: {list(results)}")
        wanted = {cid: "fail" if cid in broken else "pass" for cid in STEP2_IDS}
        problems = base._problems(results, wanted)
        base.expect(not problems, problems)
        return f"{', '.join(sorted(broken))} fail, the other {len(STEP2_IDS) - len(broken)} pass"

    return scenario


GOOD_TEST = "def test_ok() -> None:\n    assert True\n"
BROKEN_TEST = "def test_ok() -> None:\n    assert True\n\n\ndef broken(:\n"


def s_cj1_unparsable() -> str:
    """A test module that cannot be parsed makes the CJ1 evidence incomplete;
    the same text in a non-Python file does not."""
    good = evidence.Snapshot({"tests/test_a.py": GOOD_TEST, "tests/notes.txt": "def broken(:\n"})
    bad = evidence.Snapshot({"tests/test_a.py": BROKEN_TEST, "tests/notes.txt": "def broken(:\n"})
    flag = [{"path": "tests/test_a.py", "reason": "unparsable", "detail": "SyntaxError"}]

    data = evidence.compare(good, good, None, None)
    base.expect(
        data["evidence_incomplete"] is False
        and data["incomplete"] == {"previous": [], "current": [], "diff": []},
        f"flags on parsable files: {data['incomplete']}",
    )
    data = evidence.compare(good, bad, None, None)
    base.expect(
        data["evidence_incomplete"] is True
        and data["incomplete"] == {"previous": [], "current": flag, "diff": []},
        f"unparsable current file: {data['incomplete']}",
    )
    base.expect(data["unparsable"]["current"] == ["tests/test_a.py: SyntaxError"], f"{data['unparsable']}")
    summary = evidence._summary(data)
    base.expect(
        summary.startswith("EVIDENCE INCOMPLETE: 1 item(s)")
        and "incomplete (current): unparsable tests/test_a.py SyntaxError" in summary,
        f"summary: {summary}",
    )
    data = evidence.compare(bad, good, None, None)
    base.expect(
        data["evidence_incomplete"] is True
        and data["incomplete"] == {"previous": flag, "current": [], "diff": []},
        f"unparsable previous file: {data['incomplete']}",
    )
    return "an unparsable .py in tests/ (current or previous step) raises evidence_incomplete; a .txt does not"


def register(suite: Suite) -> None:
    """Add the step-2 scenarios to the self-test suite."""
    suite.add("step2-fixture-passes", s_fixture_passes)
    for overlay, broken in VARIANTS.items():
        suite.add(f"step2-variant-{overlay}", variant(overlay, broken))
    suite.add("step2-cj1-unparsable", s_cj1_unparsable)
