"""Cross-step verifier state (pytest node IDs, earlier criterion statuses).

Lives in STATE_DIR inside the task container: root-owned, mode 0700. The agent
runs as the unprivileged `agent` user and can neither read nor alter it. The
container is the same for all steps of a trial, so the state survives between
steps. Every step also writes a copy to /logs/verifier for the host archive.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import config
from .result import CriterionResult, Status


class State:
    def __init__(self, directory: Path = config.STATE_DIR) -> None:
        self.directory = directory
        self.path = directory / "state.json"
        self.data: dict[str, Any] = {"steps": {}}

    def load(self) -> State:
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chown(self.directory, 0, 0)
        os.chmod(self.directory, 0o700)
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
        return self

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def step(self, number: int) -> dict[str, Any] | None:
        record: dict[str, Any] | None = self.data["steps"].get(str(number))
        return record

    def passed_before(self, step: int) -> set[str]:
        """IDs of criteria that passed at any earlier step."""
        passed: set[str] = set()
        for key, record in self.data["steps"].items():
            if int(key) < step:
                passed |= {
                    cid for cid, st in record.get("criteria", {}).items() if st == Status.PASS
                }
        return passed

    def record_step(
        self,
        step: int,
        results: list[CriterionResult],
        pytest_passed: list[str] | None,
        pytest_collected: list[str] | None = None,
    ) -> None:
        self.data["steps"][str(step)] = {
            "criteria": {r.id: r.status.value for r in results},
            "pytest_passed": pytest_passed,
            "pytest_collected": pytest_collected,  # CJ1 evidence at the next step
        }
        self.save()


def regressions(state: State, step: int, results: list[CriterionResult]) -> list[str]:
    """C5 metric: criteria that passed at an earlier step and fail now."""
    before = state.passed_before(step)
    return sorted(r.id for r in results if r.id in before and r.status is Status.FAIL)
