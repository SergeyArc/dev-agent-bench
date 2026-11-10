#!/usr/bin/env python3
"""Which trial x step has no usable code snapshot.

    python3 bench/report/snapshot_audit.py ~/bench-runs/2026* ~/bench-runs/batch-* --out DIR

For every step of every trial found: is the metadata there, is the bundle
there, does the bundle open and is its head the recorded commit. A reason is
given only when the files of the run state one (the `error` of the snapshot
metadata, the line of `bench-snapshot` in trial.log). Read-only on the runs;
writes `snapshots.json` and `snapshots.md` to `--out`.

Directories that look like a job but have no `run.json` (a trial stopped by the
operator) are listed separately: the report scripts do not see them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import runs
from snapshots import Snapshot, SnapshotError, artifact_paths


def audit_step(trial: runs.Trial, step: runs.StepRun) -> dict[str, Any]:
    meta_path, bundle = artifact_paths(step.dir, step.number)
    row: dict[str, Any] = {"step": step.number, "end_reason": step.end_reason, "state": "ok", "reason": ""}
    row["metadata"] = meta_path.is_file()
    row["bundle"] = bundle.is_file()
    row["bundle_bytes"] = bundle.stat().st_size if row["bundle"] else 0
    if not row["metadata"]:
        row["state"] = "no_metadata"
    elif not row["bundle"]:
        row["state"] = "no_bundle"
    try:
        with Snapshot(step.dir, step.number) as snap:
            row["commit"] = snap.commit
            row["files"] = len(snap.files())
    except SnapshotError as exc:
        if row["state"] == "ok":
            row["state"] = "unreadable"
        row["reason"] = str(exc)
    if row["state"] != "ok":
        # What the run itself recorded about the failure.
        log = trial.dir / "trial.log"
        if log.is_file():
            pattern = re.compile(rf"bench-snapshot step-{step.number}'?\. .*|bench-snapshot: step-{step.number} .*")
            lines = pattern.findall(log.read_text(errors="replace"))
            if lines:
                row["trial_log"] = lines[-1][:300]
    return row


def orphans(roots: list[Path], known: set[Path]) -> list[str]:
    """Job-like directories (`steps/` inside) that have no run.json."""
    found = []
    for root in roots:
        for steps_dir in [*root.glob("*/steps"), *root.glob("*/*/steps")]:
            trial_dir = steps_dir.parent.resolve()
            if trial_dir not in known and not any((p / "run.json").is_file() for p in (trial_dir, trial_dir.parent)):
                found.append(str(trial_dir))
    return sorted(set(found))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("runs", nargs="+", type=Path, help="trial, job or batch directories")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    trials = runs.discover(args.runs)
    report: dict[str, Any] = {"trials": [], "without_run_json": orphans(args.runs, {t.dir for t in trials})}
    for trial in trials:
        report["trials"].append(
            {
                "name": trial.name,
                "dir": str(trial.dir),
                "model": trial.model,
                "harness": trial.harness,
                "status": trial.status,
                "steps": [audit_step(trial, step) for _, step in sorted(trial.steps.items())],
            }
        )
    steps = [(t, s) for t in report["trials"] for s in t["steps"]]
    bad = [(t, s) for t, s in steps if s["state"] != "ok"]
    lines = [
        "# Snapshot audit",
        "",
        (
            f"Trials: {len(trials)}; steps: {len(steps)}; with a usable snapshot: {len(steps) - len(bad)}; "
            f"without: {len(bad)}. Directories without run.json: {len(report['without_run_json'])}."
        ),
        "",
        "| trial | cell | trial status | step | state | reason in the files |",
        "|---|---|---|---|---|---|",
    ]
    for t, s in bad:
        reason = "; ".join(x for x in (s["reason"], s.get("trial_log", "")) if x).replace("|", "/")
        lines.append(f"| {t['name']} | {t['model']} / {t['harness']} | {t['status']} | {s['step']} | {s['state']} | {reason} |")
    lines += ["", "Without run.json:", *[f"- {path}" for path in report["without_run_json"]], ""]
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "snapshots.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    (args.out / "snapshots.md").write_text("\n".join(lines))
    print(lines[2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
