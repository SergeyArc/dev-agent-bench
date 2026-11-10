"""Criterion result model and the suite that executes checks.

Each check yields {id, status: pass|fail|error|skipped, evidence}:
  pass    — the property holds;
  fail    — the property does not hold (the agent's work is at fault);
  error   — the check itself could not run (verifier or infrastructure fault);
  skipped — the check is not applicable or its precondition is missing.
A `[judge]` criterion whose evidence is collected deterministically (CJ1) is
reported with status `evidence`: no verdict, no weight.
Criterion IDs are those of contract §9 and are preserved 1:1.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

EVIDENCE_LIMIT = 4000


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIPPED = "skipped"
    EVIDENCE = "evidence"


class CheckFailed(Exception):
    """Raise inside a check: the criterion is not met. The message is the evidence."""


class CheckSkipped(Exception):
    """Raise inside a check: the criterion cannot be evaluated here."""


@dataclass
class CriterionResult:
    id: str
    status: Status
    evidence: str = ""
    step: int = 0  # bench step that defines the criterion (0 = common)
    scored: bool = True
    duration_sec: float = 0.0
    runs: int = 1

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


# A check returns evidence text (or None) on success and raises otherwise.
Check = Callable[[], str | None]


def clip(text: str, limit: int = EVIDENCE_LIMIT) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [{len(text) - limit} more characters]"


def run_check(check: Check) -> tuple[Status, str]:
    try:
        evidence = check()
    except CheckSkipped as exc:
        return Status.SKIPPED, clip(str(exc))
    except (CheckFailed, AssertionError) as exc:
        return Status.FAIL, clip(str(exc) or exc.__class__.__name__)
    except Exception:  # noqa: BLE001 - a broken check must not break the run
        return Status.ERROR, clip(traceback.format_exc())
    return Status.PASS, clip(evidence or "")


@dataclass
class _Entry:
    id: str
    check: Check
    step: int
    scored: bool
    repeat: int


@dataclass
class Suite:
    """Ordered list of criteria; executed sequentially (they share one database,
    one broker and one HTTP port)."""

    entries: list[_Entry] = field(default_factory=list)

    def add(
        self, id: str, check: Check, *, step: int = 0, scored: bool = True, repeat: int = 1
    ) -> None:
        if any(e.id == id for e in self.entries):
            raise ValueError(f"duplicate criterion id: {id}")
        self.entries.append(_Entry(id, check, step, scored, repeat))

    def run(self, log: Callable[[str], None] = print) -> list[CriterionResult]:
        results: list[CriterionResult] = []
        for entry in self.entries:
            started = time.monotonic()
            status, evidence, runs = Status.PASS, "", 0
            # repeat > 1 (criteria 4.9, 4.10): passes only if every run passes.
            for _ in range(entry.repeat):
                runs += 1
                status, evidence = run_check(entry.check)
                if status is not Status.PASS:
                    if entry.repeat > 1:
                        evidence = f"run {runs}/{entry.repeat}: {evidence}"
                    break
            result = CriterionResult(
                id=entry.id,
                status=status,
                evidence=evidence,
                step=entry.step,
                scored=entry.scored,
                duration_sec=round(time.monotonic() - started, 3),
                runs=runs,
            )
            log(f"[{result.status.value:7}] {result.id}  ({result.duration_sec}s)")
            results.append(result)
        return results
