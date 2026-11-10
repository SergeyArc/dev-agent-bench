"""Shared pieces of the failure-trajectory scripts. Read-only on the runs.

The trial list is the composition of the main matrix
(G0, 24 trials) and the two ablation blocks (G2, G2b). The turn-limit-300 block of
Nemotron x OpenHands and the `infra_failure` trial are not in it.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import runs

RUNS_ROOT = Path.home() / "bench-runs"
OUT_ROOT = RUNS_ROOT / "analysis" / "failures"

# (cell, variant, trial directory under RUNS_ROOT)
TRIALS: tuple[tuple[str, str, str], ...] = (
    (
        "Devstral × opencode",
        "G0",
        "batch-20261006-200513-devstral-2-123b/20261006-200513-13966d/bench__Lz8c6RD",
    ),
    (
        "Devstral × opencode",
        "G0",
        "batch-20261006-200513-devstral-2-123b/20261006-225016-e5ba28/bench__6XKz9an",
    ),
    (
        "Devstral × opencode",
        "G0",
        "batch-20261006-200513-devstral-2-123b/20261007-010220-b61544/bench__sPQ6dST",
    ),
    (
        "Devstral × OpenHands",
        "G0",
        "batch-20261009-221545-devstral-2-123b/20261009-221545-b08488/bench__DqouTVx",
    ),
    (
        "Devstral × OpenHands",
        "G0",
        "batch-20261009-221545-devstral-2-123b/20261010-013404-28756c/bench__4snwgzT",
    ),
    (
        "Devstral × OpenHands",
        "G0",
        "batch-20261009-221545-devstral-2-123b/20261010-031904-a27a7d/bench__sy3v69H",
    ),
    (
        "Gemma × opencode",
        "G0",
        "batch-20261004-211847-gemma-4-31b/20261004-211847-5411de/bench__bvTaFQu",
    ),
    (
        "Gemma × opencode",
        "G0",
        "batch-20261004-211847-gemma-4-31b/20261004-224013-0427ac/bench__aAm6QsD",
    ),
    (
        "Gemma × opencode",
        "G0",
        "batch-20261004-211847-gemma-4-31b/20261004-235247-e8a27d/bench__rFbb8E7",
    ),
    (
        "Gemma × OpenHands",
        "G0",
        "batch-20261005-123411-gemma-4-31b/20261005-123411-cdd6a7/bench__SnYYtCJ",
    ),
    (
        "Gemma × OpenHands",
        "G0",
        "batch-20261005-123411-gemma-4-31b/20261005-142926-920af2/bench__XtuYXxb",
    ),
    (
        "Gemma × OpenHands",
        "G0",
        "batch-20261005-123411-gemma-4-31b/20261005-153250-83293c/bench__uvxscaC",
    ),
    (
        "Nemotron × opencode",
        "G0",
        "batch-20261005-181906-nemotron-3-super-120b-a12b/20261005-181906-78a499/bench__2CDucdB",
    ),
    (
        "Nemotron × opencode",
        "G0",
        "batch-20261005-181906-nemotron-3-super-120b-a12b/20261005-201636-1a9a0a/bench__MyGNHER",
    ),
    (
        "Nemotron × opencode",
        "G0",
        "batch-20261005-181906-nemotron-3-super-120b-a12b/20261005-221402-c2c48a/bench__vewAfh9",
    ),
    (
        "Nemotron × OpenHands",
        "G0",
        "batch-20261007-081403-nemotron-3-super-120b-a12b/20261007-081403-334158/bench__rVQJnqB",
    ),
    (
        "Nemotron × OpenHands",
        "G0",
        "batch-20261007-081403-nemotron-3-super-120b-a12b/20261007-110143-ec3cd7/bench__DstzxhA",
    ),
    (
        "Nemotron × OpenHands",
        "G0",
        "batch-20261007-081403-nemotron-3-super-120b-a12b/20261007-191745-f445dc/bench__8TrMkG4",
    ),
    (
        "Qwen × opencode",
        "G0",
        "batch-20261006-084903-qwen3.8-27b/20261006-084903-5c556f/bench__Ev4cais",
    ),
    (
        "Qwen × opencode",
        "G0",
        "batch-20261006-084903-qwen3.8-27b/20261006-124829-6d3d51/bench__FkCfoav",
    ),
    (
        "Qwen × opencode",
        "G0",
        "batch-20261006-084903-qwen3.8-27b/20261006-153449-1e0609/bench__raxhE6g",
    ),
    (
        "Qwen × OpenHands",
        "G0",
        "batch-20261008-204155-qwen3.8-27b/20261008-204155-480728/bench__xJAGkQz",
    ),
    (
        "Qwen × OpenHands",
        "G0",
        "batch-20261008-204155-qwen3.8-27b/20261009-021654-062ee8/bench__MpJpYbK",
    ),
    (
        "Qwen × OpenHands",
        "G0",
        "batch-20261009-153907-qwen3.8-27b/20261009-153907-d1cfe6/bench__rJW9Jwu",
    ),
    (
        "Gemma × opencode",
        "G2",
        "batch-20261008-105431-gemma-4-31b/20261008-105431-0c1879/bench__TmdPxsz",
    ),
    (
        "Gemma × opencode",
        "G2",
        "batch-20261008-105431-gemma-4-31b/20261008-134427-f28ce0/bench__Kd87zzJ",
    ),
    (
        "Gemma × opencode",
        "G2",
        "batch-20261008-105431-gemma-4-31b/20261008-150429-b13c72/bench__65ME99A",
    ),
    (
        "Qwen × opencode",
        "G2",
        "batch-20261007-225747-qwen3.8-27b/20261007-225747-84ce94/bench__m4EQdfd",
    ),
    (
        "Qwen × opencode",
        "G2",
        "batch-20261007-225747-qwen3.8-27b/20261008-014915-c029a5/bench__AnSecVE",
    ),
    (
        "Qwen × opencode",
        "G2",
        "batch-20261007-225747-qwen3.8-27b/20261008-050649-d5b627/bench__PXv5yBy",
    ),
    (
        "Gemma × opencode",
        "G2b",
        "batch-20261010-075159-gemma-4-31b/20261010-075159-325cf2/bench__mvQswrQ",
    ),
    (
        "Gemma × opencode",
        "G2b",
        "batch-20261010-075159-gemma-4-31b/20261010-092119-382dd2/bench__aFg3VVt",
    ),
    (
        "Gemma × opencode",
        "G2b",
        "batch-20261010-075159-gemma-4-31b/20261010-111725-3b5c34/bench__T6pzmZK",
    ),
)


@dataclass
class Listed:
    cell: str
    variant: str
    trial: runs.Trial

    @property
    def short(self) -> str:
        """`…5c556f`-style name: the last six characters of the job directory."""
        return self.trial.dir.parent.name.rsplit("-", 1)[-1]


def listed(variants: tuple[str, ...] = ("G0", "G2", "G2b")) -> list[Listed]:
    return [
        Listed(cell, variant, runs.load_trial(RUNS_ROOT / path))
        for cell, variant, path in TRIALS
        if variant in variants
    ]


def find(short: str) -> Listed:
    for item in listed():
        if item.short == short:
            return item
    raise KeyError(short)


_SCRUB: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"/(?:Users|home)/[^/\s\"']+"), "<path>"),
    (re.compile(r"\b(?:sk|key|tok)-[A-Za-z0-9_\-]{16,}"), "<key>"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"), r"\1<key>"),
    (re.compile(r"\bhost\.docker\.internal(?::\d+)?"), "<host>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?"), "<host>"),
)


def scrub(text: str) -> str:
    """Hide host addresses, user paths and keys in an excerpt. Loopback is kept readable."""
    kept = text.replace("127.0.0.1", "\0LOOP\0").replace("0.0.0.0", "\0ANY\0")
    for pattern, repl in _SCRUB:
        kept = pattern.sub(repl, kept)
    return kept.replace("\0LOOP\0", "127.0.0.1").replace("\0ANY\0", "0.0.0.0")


@dataclass
class Turn:
    """One step of an ATIF trajectory, flattened."""

    index: int
    source: str
    timestamp: str
    message: str
    reasoning: str
    calls: list[tuple[str, dict[str, Any]]]
    results: list[str]


def trajectory_path(step: runs.StepRun) -> Path:
    return step.dir / "agent" / "trajectory.json"


def turns(step: runs.StepRun) -> Iterator[Turn]:
    path = trajectory_path(step)
    if not path.is_file():
        return
    data = json.loads(path.read_text())
    for record in data.get("steps", []):
        calls = [
            (str(call.get("function_name")), call.get("arguments") or {})
            for call in record.get("tool_calls") or []
        ]
        observation = record.get("observation") or {}
        results = [
            str(result.get("content") or "")
            for result in observation.get("results") or []
        ]
        yield Turn(
            index=int(record.get("step_id") or 0),
            source=str(record.get("source")),
            timestamp=str(record.get("timestamp") or ""),
            message=str(record.get("message") or ""),
            reasoning=str(record.get("reasoning_content") or ""),
            calls=calls,
            results=results,
        )


def call_text(name: str, arguments: dict[str, Any]) -> str:
    """The part of a tool call a reader needs: the shell command, or the edit as JSON."""
    if (
        isinstance(arguments, dict)
        and isinstance(arguments.get("command"), str)
        and "path" not in arguments
    ):
        return f"{name}: {arguments['command']}"
    return f"{name}: {json.dumps(arguments, ensure_ascii=False)}"
