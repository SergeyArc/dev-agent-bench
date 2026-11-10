"""Self-test of the step-4 criteria (criteria/step4.py).

Scenarios are added to the framework self-test by `register(suite)`; they can
also be run on their own in a throwaway container of the base image:

    PYTHONPATH=/tests-selftest /opt/verifier/bin/python -m benchverify.selftest_step4

`step4-fixture-passes` installs the fixture service of
tests/selftest_fixtures/step4 and makes a full verifier run of step 4 (cascade
included): every scored criterion passes. Each `step4-variant-*` scenario
installs a broken variant and runs the step-4 criteria alone: exactly the
criteria named in `VARIANTS` fail.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from criteria import step4

from . import common, config, db, http, proc, reset, runner
from .result import CheckFailed, Status, Suite
from .state import State

FIXTURES = Path(__file__).resolve().parent.parent / "selftest_fixtures" / "step4"
PACKAGES = ("", "domain", "application", "infrastructure", "api")
STEP4_IDS = [f"4.{n}" for n in range(1, 12)]
STORE = "src/tracksvc/infrastructure/store.py"

_SEAL = (
    "    async with engine.begin() as conn:\n"
    "        track = await _load(conn, track_id, _LOCK)\n"
    "        track.seal()\n"
    '        await conn.execute(_SET_STATUS, {"id": track_id, "status": track.status})\n'
    "    return track\n"
)
# The status is written and committed before the domain check.
_SEAL_BEFORE_CHECK = (
    "    async with engine.connect() as conn:\n"
    "        track = await _load(conn, track_id, _LOCK)\n"
    '        await conn.execute(_SET_STATUS, {"id": track_id, "status": "sealed"})\n'
    "        await conn.commit()\n"
    "        track.seal()\n"
    "    return track\n"
)
_ADD_SEGMENT = (
    "    async with engine.begin() as conn:\n"
    "        track = await _load(conn, track_id, _LOCK)\n"
    "        track.add_segment(segment)\n"
    '        values = {"id": track_id, "label": segment.label, "start": segment.start}\n'
    '        await conn.execute(_INSERT_SEGMENT, {**values, "end": segment.end})\n'
    "    return track\n"
)
_ADD_SEGMENT_REWRITE = (
    "    async with engine.begin() as conn:\n"
    "        track = await _load(conn, track_id)\n"
    "        track.add_segment(segment)\n"
    '        await conn.execute(_SET_STATUS, {"id": track_id, "status": track.status})\n'
    '        await conn.execute(text("DELETE FROM track_segments WHERE track_id = :id"), {"id": track_id})\n'
    "        for item in track.segments:\n"
    '            values = {"id": track_id, "label": item.label, "start": item.start}\n'
    '            await conn.execute(_INSERT_SEGMENT, {**values, "end": item.end})\n'
    "    return track\n"
)
_GET = (
    "    async with engine.connect() as conn:\n"
    "        return await _load(conn, track_id)\n"
)
_GET_CACHED = (
    "    if track_id not in _CACHE:\n"
    "        async with engine.connect() as conn:\n"
    "            _CACHE[track_id] = await _load(conn, track_id)\n"
    "    return _CACHE[track_id]\n"
)


@dataclass(frozen=True)
class Variant:
    """A broken fixture service and the step-4 criteria it must fail."""

    broken: frozenset[str]
    overlay: str | None = None  # directory under FIXTURES laid over `base/`
    # (file of `base/`, text, replacement): applied to the base file.
    replacements: tuple[tuple[str, str, str], ...] = ()


VARIANTS = {
    # Tracks stay in process memory (the state of step 3); the schema exists.
    "memory_store": Variant(
        frozenset({"4.3", "4.4", "4.6", "4.7", "4.9", "4.10", "4.11"}), overlay="memory_store"
    ),
    # No unique constraint on `code`: check-then-insert is the only guard.
    "no_code_unique": Variant(frozenset({"4.1", "4.2", "4.9"}), overlay="no_code_unique"),
    # The track row is read without a lock: simultaneous R3 all see no segments.
    "no_row_lock": Variant(
        frozenset({"4.10"}),
        replacements=(
            (STORE, '_LOCK = " FOR UPDATE"', '_LOCK = ""'),
            (STORE, "from uuid import UUID, uuid4\n", "import asyncio\nfrom uuid import UUID, uuid4\n"),
            (
                STORE,
                "        track.add_segment(segment)\n",
                "        track.add_segment(segment)\n        await asyncio.sleep(0.3)\n",
            ),
        ),
    ),
    # The same without the delay, writing as a repository that saves the whole
    # aggregate does (update the track row, replace all segment rows). The update
    # serialises the writers, each then replaces the rows of the one before: the
    # requests overlap only if they hold their database connections already.
    "no_row_lock_rewrite": Variant(
        frozenset({"4.10"}), replacements=((STORE, _ADD_SEGMENT, _ADD_SEGMENT_REWRITE),)
    ),
    # Seal writes and commits first: `409 track_empty` leaves the row sealed.
    "seal_before_check": Variant(
        frozenset({"4.8"}), replacements=((STORE, _SEAL, _SEAL_BEFORE_CHECK),)
    ),
    # GET is served from a per-process cache, dropped only by this process's own writes.
    "cached_reads": Variant(
        frozenset({"4.11"}),
        replacements=(
            (STORE, '_LOCK = " FOR UPDATE"\n', '_LOCK = " FOR UPDATE"\n_CACHE: dict[UUID, Track] = {}\n'),
            (STORE, _GET, _GET_CACHED),
            (STORE, "    return track\n", "    _CACHE.pop(track.id, None)\n    return track\n"),
        ),
    ),
}


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def _agent(command: list[str], what: str) -> None:
    result = proc.run_as_agent(command)
    expect(result.returncode == 0, f"{what} failed: {result.output}")


def restore_app() -> None:
    _agent(["git", "checkout", "-q", "--", "."], "git checkout")
    _agent(["git", "clean", "-fdxq", "-e", ".venv"], "git clean")


def agent_write(relative: str, content: str) -> None:
    script = (
        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
        "p.parent.mkdir(parents=True, exist_ok=True); p.write_text(sys.argv[2])"
    )
    _agent(["python3", "-c", script, str(config.APP_DIR / relative), content], f"writing {relative}")


def _files(root: Path) -> dict[str, str]:
    paths = sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    expect(bool(paths), f"fixture directory {root} is empty")
    return {str(path.relative_to(root)): path.read_text() for path in paths}


def install_fixture(variant: Variant | None = None) -> None:
    """Replace /app content with the step-4 fixture service or a broken variant."""
    restore_app()
    files = {str(Path("src") / config.PACKAGE / package / "__init__.py"): "" for package in PACKAGES}
    files.update(_files(FIXTURES / "base"))
    if variant is not None:
        for relative, old, new in variant.replacements:
            expect(old in files[relative], f"{relative} does not contain {old!r}")
            files[relative] = files[relative].replace(old, new)
        if variant.overlay is not None:
            files.update(_files(FIXTURES / variant.overlay))
    for relative, content in files.items():
        agent_write(relative, content)


def _problems(results: dict[str, tuple[str, str]], wanted: dict[str, str]) -> str:
    return "\n".join(
        f"{cid}: expected {wanted[cid]}, got {status}: {evidence}"
        for cid, (status, evidence) in results.items()
        if status != wanted[cid]
    )


def _left_behind() -> str | None:
    """What a step-4 run must not leave: its database, processes, open ports."""
    left = db.admin_sql(f"SELECT count(*) FROM pg_database WHERE datname = '{step4.STEP_DB}'")
    if left != "0":
        return f"database {step4.STEP_DB} was not dropped"
    if proc.agent_pids():
        return f"agent processes left: {proc.agent_pids()}"
    for port in (config.HTTP_PORT, step4.SECOND_PORT):
        if proc.port_open(port):
            return f"port {port} is still open"
    return None


def s_fixture_passes() -> str:
    """Full step-4 run (cascade included) on the fixture service: all pass."""
    install_fixture()
    http.OBSERVED.clear()
    state = State().load()
    state.data = {"steps": {}}
    state.save()
    runner.run(4)
    report = json.loads((config.LOG_DIR / "criteria.json").read_text())
    criteria = {c["id"]: c for c in report["criteria"]}
    ids = list(criteria)
    step_ids = [cid for cid in ids if cid.startswith("4.")]
    expect(step_ids == STEP4_IDS, f"step-4 criteria: {step_ids}")
    expect(any(cid.startswith("1.") for cid in ids), f"cascade: no step-1 criteria in {ids}")
    scored = {cid: (c["status"], c["evidence"]) for cid, c in criteria.items() if c["scored"]}
    problems = _problems(scored, dict.fromkeys(scored, "pass"))
    expect(not problems, problems)
    expect(criteria["C6"]["status"] == "pass", f"C6: {criteria['C6']}")
    expect(report["reward"] == 1.0, f"reward: {report['reward']}")
    expect(report["regressions"]["count"] == 0, f"regressions: {report['regressions']}")
    for cid, repeat in step4.REPEATED.items():
        expect(criteria[cid]["runs"] == repeat, f"{cid}: {criteria[cid]['runs']} run(s), expected {repeat}")
    expect(not (leftover := _left_behind()), str(leftover))
    return (
        f"{len(scored)} scored criteria pass ({len(ids)} reported, steps "
        f"{sorted({c['step'] for c in criteria.values()})}), reward 1.0; 4.9 and 4.10 ran 3 times"
    )


def variant_scenario(name: str) -> Callable[[], str]:
    """Step-4 criteria on a broken variant: exactly `VARIANTS[name].broken` fail."""

    def scenario() -> str:
        variant = VARIANTS[name]
        install_fixture(variant)
        reset.reset()
        suite = Suite()
        ctx = common.Context(step=4, state=State().load(), work_dir=common.make_work_dir())
        step4.register(suite, ctx)
        results = {r.id: (r.status.value, r.evidence) for r in suite.run()}
        expect(list(results) == STEP4_IDS, f"criteria: {list(results)}")
        wanted = {cid: "fail" if cid in variant.broken else "pass" for cid in STEP4_IDS}
        problems = _problems(results, wanted)
        expect(not problems, problems)
        expect(not (leftover := _left_behind()), str(leftover))
        broken = sorted(variant.broken, key=lambda cid: int(cid.split(".")[1]))
        return f"{', '.join(broken)} fail, the other {len(STEP4_IDS) - len(broken)} pass"

    return scenario


def s_empty_app() -> str:
    """Step-4 criteria on the pristine /app: every one fails, none errors."""
    restore_app()
    reset.reset()
    suite = Suite()
    ctx = common.Context(step=4, state=State().load(), work_dir=common.make_work_dir())
    step4.register(suite, ctx)
    results = {r.id: (r.status.value, r.evidence) for r in suite.run()}
    problems = _problems(results, dict.fromkeys(STEP4_IDS, "fail"))
    expect(list(results) == STEP4_IDS and not problems, f"{list(results)}\n{problems}")
    expect(not (leftover := _left_behind()), str(leftover))
    return f"all {len(STEP4_IDS)} criteria fail on the empty /app, none in error"


def register(suite: Suite) -> None:
    """Add the step-4 scenarios to a self-test suite."""
    suite.add("step4-empty-app", s_empty_app)
    suite.add("step4-fixture-passes", s_fixture_passes)
    for name in VARIANTS:
        suite.add(f"step4-variant-{name}", variant_scenario(name))


def main() -> int:
    (config.LOG_DIR / "selftest").mkdir(parents=True, exist_ok=True)
    reset.ensure_infrastructure()
    suite = Suite()
    register(suite)
    try:
        results = suite.run()
    finally:
        proc.stop_agent_processes()
        restore_app()
    print()
    for result in results:
        print(f"{result.status.value.upper():7} {result.id}: {result.evidence}")
    failed = [r for r in results if r.status is not Status.PASS]
    print(f"\nself-test (step 4): {len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
