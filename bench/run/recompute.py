#!/usr/bin/env python3
"""Rebuild the steps of saved trials with the current rules of runjson.py / turncats.py.

    python3 bench/run/recompute.py ~/bench-runs/batch-*/2026* ~/bench-runs/2026*

For every job directory: Harbor's result.json, the proxy state
(``bench-run/traffic/state.json``) and the traffic log are read again and the
steps are rebuilt. The original ``run.json`` is never changed: the result goes
to ``run.recomputed.json`` next to it, with ``recomputed_from`` naming the
commit of the rules. Prints one line per step whose end reason changed and
per trial whose status changed.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import runjson

# status_reason of a trial whose only infrastructure note is about requests outside a step.
ONLY_OUTSIDE_STEP = re.compile(r"\d+ generation request\(s\) outside any step were refused")


def recompute(job: Path) -> list[str]:
    found = sorted(job.glob("*/run.json"))
    if not found:
        return []
    run_path = found[0]
    run = json.loads(run_path.read_text())
    trial_dir = run_path.parent
    traffic_dir = job / "bench-run" / "traffic"
    state = runjson._load(traffic_dir / "state.json")
    result = runjson._load(trial_dir / "result.json")
    if not state or not result or not run.get("steps"):
        return []
    names = [step["step"] for step in run["steps"]]
    # Trial-level infra notes are not stored apart from status_reason: steps that were
    # infra_failure only because of them keep their recorded end reason.
    steps = runjson.build_steps(trial_dir, result, state, names, traffic_dir, [], (run.get("limits") or {}).get("max_turns"))
    changes = []
    for old, new in zip(run["steps"], steps):
        if old["end_reason"] == "infra_failure" and new["end_reason"] != "infra_failure" and not old.get("end_reason_notes"):
            new["end_reason"], new["end_reason_notes"] = old["end_reason"], old.get("end_reason_notes", [])
        if old["end_reason"] != new["end_reason"]:
            changes.append(f"{job.name} {run['model']['key']} {run['harness']['name']} {new['step']}: {old['end_reason']} -> {new['end_reason']}")
    run["steps"] = steps
    run["recomputed_from"] = runjson.git_state(Path(__file__).resolve().parent.parent.parent)
    failed = [s["step"] for s in steps if s["end_reason"] == "infra_failure"]
    refusals = runjson.outside_step_refusals(traffic_dir, state, result)
    run["outside_step_refusals"] = refusals
    if run["status"] == "ok" and failed:
        run["status"], run["status_reason"] = "infra_failure", f"infra failure in {failed}"
    elif (run["status"] == "infra_failure" and ONLY_OUTSIDE_STEP.fullmatch(run.get("status_reason") or "")
          and not refusals["other"] and not failed):
        # The trial was failed only by requests refused right after an agent timeout.
        changes.append(f"{job.name} {run['model']['key']} {run['harness']['name']} trial: infra_failure -> ok")
        run["status"], run["status_reason"] = "ok", None
        run["warnings"] = [*(run.get("warnings") or []), runjson.refused_after_timeout_warning(refusals["after_agent_timeout"])]
    runjson.write_run_json(trial_dir / "run.recomputed.json", run)
    return changes


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    jobs = [Path(arg) for arg in sys.argv[1:]]
    done = 0
    for job in jobs:
        if not job.is_dir():
            continue
        for line in recompute(job):
            print(line)
        done += bool(sorted(job.glob("*/run.recomputed.json")))
    print(f"recomputed {done} trial(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
