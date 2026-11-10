"""Step verifier entry point: `python -m benchverify.runner --step N`.

Order of a run:
  1. infrastructure is ensured, agent processes are stopped;
  2. static common criteria (C1, C2, C3, C7, C8) on the working copy;
  3. reset per contract §2.2 (default database), then C4 (`uv run pytest`
     against its own empty, unmigrated database);
  4. criteria of steps 1…N (cascade, rule C5) from `criteria/step<k>.py`,
     each under its own ID;
  5. C6 diagnostic, CJ1 evidence (evidence.py), regression metric, outputs,
     state.

A step criteria module defines
    def register(suite: Suite, ctx: Context) -> None
and optionally `RUBRIC: scoring.Rubric` (used when it is the current step).
"""

from __future__ import annotations

import argparse
import importlib
import json
import shutil
import sys
from pathlib import Path

from . import common, config, evidence, proc, reset, scoring
from .result import Suite
from .state import State, regressions


def _log(message: str) -> None:
    print(message, flush=True)


def _load_step_modules(step: int) -> list[tuple[int, object]]:
    modules: list[tuple[int, object]] = []
    for number in range(1, step + 1):
        try:
            modules.append((number, importlib.import_module(f"criteria.step{number}")))
        except ModuleNotFoundError as exc:
            if exc.name != f"criteria.step{number}":
                raise
            _log(f"no criteria module for step {number} (skeleton)")
    return modules


def _snapshot_meta(step: int) -> dict[str, object] | None:
    path = config.STATE_DIR / "snapshots" / f"step-{step}.json"
    try:
        return dict(json.loads(path.read_text()))
    except (OSError, ValueError):
        return None


def run(step: int) -> float:
    state = State().load()
    work_dir = common.make_work_dir()
    proc_logs = config.LOG_DIR / "proc"
    proc_logs.mkdir(parents=True, exist_ok=True)
    ctx = common.Context(step=step, state=state, work_dir=work_dir)
    try:
        reset.ensure_infrastructure()
        stopped = proc.stop_agent_processes()
        _log(f"stopped {stopped} agent process(es)")

        static = Suite()
        common.register_static(static, ctx)
        results = static.run(_log)

        outcome = reset.reset()
        migrate_rc = None if outcome.migrate is None else outcome.migrate.returncode
        _log(f"reset done; migrate exit code: {migrate_rc}")
        if outcome.migrate is not None:
            (proc_logs / "migrate.log").write_text(outcome.migrate.output)

        dynamic = Suite()
        common.register_pytest(dynamic, ctx)
        rubric = scoring.Rubric()
        for number, module in _load_step_modules(step):
            module.register(dynamic, ctx)  # type: ignore[attr-defined]
            if number == step and hasattr(module, "RUBRIC"):
                rubric = module.RUBRIC
        common.register_diagnostics(dynamic)
        results += dynamic.run(_log)
    finally:
        proc.stop_agent_processes()
        shutil.rmtree(work_dir, ignore_errors=True)

    cj1, cj1_data = evidence.collect(step, state, ctx.pytest_collected)
    if cj1 is not None:
        _log(f"[{cj1.status.value:7}] {cj1.id}")
        results.append(cj1)

    regressed = regressions(state, step, results)
    value = scoring.write_outputs(
        step=step,
        results=results,
        rubric=rubric,
        regressions=regressed,
        extra={
            "snapshot": _snapshot_meta(step),
            "migrate_exit_code": migrate_rc,
            "cj1_evidence": cj1_data,
        },
    )
    state.record_step(step, results, ctx.pytest_passed, ctx.pytest_collected)
    shutil.copyfile(state.path, config.LOG_DIR / "state.json")
    _log(f"step {step}: reward={value} regressions={len(regressed)}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--step", type=int, required=True, choices=range(1, 6))
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    run(args.step)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
