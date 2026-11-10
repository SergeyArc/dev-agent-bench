"""Discovery and loading of collected trials (read-only).

A trial directory is the Harbor trial directory written by `bench/run`:
`run.json` next to `steps/step-N/{verifier/criteria.json, artifacts/…}`;
`run.recomputed.json`, when present, replaces `run.json` (same schema).
The task as the agent saw it is kept next to the trial: `../bench-run/task/bench`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RECOMPUTED = "run.recomputed.json"
BASE_VARIANT = "G0"  # the task as it is in the repository: run.json carries no `task_variant`


@dataclass
class StepRun:
    number: int
    dir: Path
    run: dict[str, Any]  # the step record of run.json
    criteria: dict[str, Any] | None  # criteria.json, None when the verifier wrote none

    @property
    def end_reason(self) -> str | None:
        return self.run.get("end_reason")


@dataclass
class Trial:
    dir: Path
    run: dict[str, Any]
    steps: dict[int, StepRun] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return str(self.run.get("job_name") or self.dir.name)

    @property
    def model(self) -> str:
        return str(self.run["model"]["key"])

    @property
    def harness(self) -> str:
        harness = self.run["harness"]
        return f"{harness['name']} {harness.get('version_reported') or harness.get('version_pinned') or ''}".strip()

    @property
    def variant(self) -> str:
        """Task variant (`G0`, `G2`, `G2b`, …): a part of the cell key, next to model and harness."""
        name = str((self.run.get("task_variant") or {}).get("variant") or "")
        return name[:1].upper() + name[1:] if name else BASE_VARIANT

    @property
    def max_turns(self) -> int | None:
        """Turn limit of the harness: a setting, blocks that differ in it are not one cell."""
        value = (self.run.get("limits") or {}).get("max_turns")
        return int(value) if value is not None else None

    @property
    def cell(self) -> tuple[str, str, str]:
        return (self.model, self.harness, self.variant)

    def task_text(self, step: int, bench_dir: Path) -> tuple[str, str]:
        """(text, source) of the step's task.md: the copy the agent was given, else the repository's."""
        seen = self.dir.parent / "bench-run" / "task" / "bench" / "steps" / f"step-{step}" / "task.md"
        if seen.is_file():
            return seen.read_text(), "trial"
        return (bench_dir / "steps" / f"step-{step}" / "task.md").read_text(), "repository"

    @property
    def status(self) -> str:
        return str(self.run.get("status"))

    @property
    def run_source(self) -> str:
        return RECOMPUTED if "recomputed_from" in self.run else "run.json"


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return dict(json.loads(path.read_text()))
    except (OSError, ValueError):
        return None


def load_trial(trial_dir: Path) -> Trial:
    # `bench/run/recompute.py` re-labels a trial by the current rules and writes the result
    # next to the untouched original; when it exists, it is the record to report from.
    run = _load_json(trial_dir / RECOMPUTED) or _load_json(trial_dir / "run.json")
    if run is None:
        raise ValueError(f"unreadable run.json in {trial_dir}")
    trial = Trial(dir=trial_dir, run=run)
    for record in run.get("steps", []):
        number = int(str(record["step"]).rsplit("-", 1)[-1])
        step_dir = trial_dir / "steps" / f"step-{number}"
        criteria = _load_json(step_dir / "verifier" / "criteria.json")
        trial.steps[number] = StepRun(number, step_dir, record, criteria)
    return trial


def discover(paths: list[Path]) -> list[Trial]:
    """Trials under the given directories (a trial, a job or a batch directory)."""
    found: dict[Path, Trial] = {}
    for root in paths:
        candidates = [root / "run.json", *sorted(root.glob("*/run.json")), *sorted(root.glob("*/*/run.json"))]
        for run_json in candidates:
            trial_dir = run_json.parent.resolve()
            if run_json.is_file() and (trial_dir / "steps").is_dir() and trial_dir not in found:
                found[trial_dir] = load_trial(trial_dir)
    return sorted(found.values(), key=lambda t: (*t.cell, str(t.run.get("started_at")), t.name))
