"""Reward computation and output files.

Custom writer (not Harbor rewardkit): see the module docstring and tests for the
rationale. Output:
  /logs/verifier/criteria.json — criterion-level results (IDs of contract §9);
  /logs/verifier/reward.json   — {"reward": float}, read by Harbor.

PLACEHOLDER SCORING. Weights and required-pass sets are decided at the rubric
stage; until a step provides them, the reward is the unweighted share of
scored criteria that passed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config
from .result import CriterionResult, Status


@dataclass
class Rubric:
    """Per-step scoring parameters (to be supplied by the rubric task)."""

    weights: dict[str, float] = field(default_factory=dict)  # default weight 1.0
    required: frozenset[str] = frozenset()  # a failed required criterion zeroes the step
    placeholder: bool = True


def reward(results: list[CriterionResult], rubric: Rubric) -> float:
    scored = [r for r in results if r.scored]
    if not scored:
        return 0.0
    if any(r.id in rubric.required and r.status is not Status.PASS for r in scored):
        return 0.0
    total = sum(rubric.weights.get(r.id, 1.0) for r in scored)
    if total <= 0:
        return 0.0
    gained = sum(rubric.weights.get(r.id, 1.0) for r in scored if r.status is Status.PASS)
    return round(gained / total, 6)


def write_outputs(
    *,
    step: int,
    results: list[CriterionResult],
    rubric: Rubric,
    regressions: list[str],
    extra: dict[str, Any] | None = None,
    log_dir: Path = config.LOG_DIR,
) -> float:
    value = reward(results, rubric)
    counts = {status.value: sum(r.status is status for r in results) for status in Status}
    report = {
        "step": step,
        "reward": value,
        "scoring": "placeholder-unweighted" if rubric.placeholder else "rubric",
        "counts": counts,
        "regressions": {"count": len(regressions), "ids": regressions},
        "criteria": [r.to_dict() for r in results],
        **(extra or {}),
    }
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "criteria.json").write_text(json.dumps(report, indent=2) + "\n")
    # reward.json is written last: its presence tells Harbor the verifier finished.
    (log_dir / "reward.json").write_text(json.dumps({"reward": value}) + "\n")
    return value
