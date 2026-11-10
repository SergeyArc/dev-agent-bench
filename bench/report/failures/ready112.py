#!/usr/bin/env python3
"""Criterion 1.12: the `/ready` code of every listed trial and what the agent tried itself.

    python3 bench/report/failures/ready112.py

For the step-1 snapshot of each trial writes to `~/bench-runs/analysis/failures/ready/`:
- `<variant>-<trial>.py.txt` — the source files that implement the probe (anything under
  `src/` that mentions the route, the engine or a timeout);
- `summary.json` / `summary.md` — verdicts of 1.7, 1.11, 1.12 with the verifier message,
  time-limit markers found in the code and the agent's own checks of an unavailable
  database found in the step-1 trajectory, by level (unreachable address, a listener
  silent from the start, silence after an established connection).

The markers are a reading aid, not the classification: the type of implementation in the
report is assigned after reading the code. Read-only on the runs.
"""

from __future__ import annotations

import json
import re
from typing import Any

from common import OUT_ROOT, Listed, call_text, listed, scrub, turns
from snapshots import Snapshot, SnapshotError

CRITERIA = ("1.5", "1.7", "1.11", "1.12")
RELEVANT = re.compile(
    r"/ready|create_async_engine|timeout|wait_for|SELECT 1", re.IGNORECASE
)

# marker -> pattern in the source of the probe
MARKERS: dict[str, re.Pattern[str]] = {
    "asyncio.timeout": re.compile(r"asyncio\.timeout\(|async with timeout\("),
    "asyncio.wait_for": re.compile(r"wait_for\("),
    "anyio": re.compile(r"fail_after|move_on_after"),
    "connect_args.timeout": re.compile(r"[\"']timeout[\"']\s*:"),
    "command_timeout": re.compile(r"command_timeout"),
    "pool_timeout": re.compile(r"pool_timeout"),
    "statement_timeout": re.compile(r"statement_timeout"),
    "pool_pre_ping": re.compile(r"pool_pre_ping"),
    "NullPool": re.compile(r"NullPool"),
    "raw asyncpg.connect": re.compile(r"asyncpg\.connect\("),
    "create_task/shield": re.compile(r"create_task\(|shield\(|ensure_future\("),
    "terminate/invalidate": re.compile(
        r"\.terminate\(|\.invalidate\(|\.abort\(|sync_engine\.dispose|dispose\("
    ),
    "run_in_executor/thread": re.compile(r"run_in_executor|to_thread|threading"),
}

# The agent's own checks of an unavailable database, weakest to strongest. A level is a
# reading aid as well: the calls behind levels 2 and 3 were read before the report.
CHECKS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "1 unreachable address",
        re.compile(
            r"DATABASE_URL[\"']?\s*[=,]\s*[\"']?\w|setenv\(\s*[\"']DATABASE_URL"
        ),
    ),
    (
        "2 listener silent from the start",
        re.compile(r"\.listen\(|socketserver|start_server\(|\bnc -l", re.IGNORECASE),
    ),
    (
        "3 silent after an established connection",
        re.compile(
            r"SIGSTOP|kill -STOP|toxiproxy|iptables|docker pause|AuthenticationOk|ReadyForQuery|"
            r"completes the \w+ startup|goes silent on queries",
            re.IGNORECASE,
        ),
    ),
)


def verdicts(item: Listed) -> dict[str, dict[str, Any]]:
    step = item.trial.steps.get(1)
    found: dict[str, dict[str, Any]] = {}
    if step is None or step.criteria is None:
        return found
    for criterion in step.criteria.get("criteria", []):
        if criterion.get("id") in CRITERIA:
            found[str(criterion["id"])] = {
                "status": criterion.get("status"),
                "evidence": scrub(str(criterion.get("evidence") or ""))[:400],
            }
    return found


def probe_sources(item: Listed) -> tuple[dict[str, str], str]:
    step = item.trial.steps.get(1)
    if step is None:
        return {}, "no step 1"
    try:
        with Snapshot(step.dir, 1) as snapshot:
            paths = [
                p
                for p in snapshot.files()
                if p.startswith("src/") and p.endswith(".py")
            ]
            sources = {path: snapshot.read(path) for path in paths}
    except SnapshotError as exc:
        return {}, str(exc)
    return {path: text for path, text in sources.items() if RELEVANT.search(text)}, ""


def own_checks(item: Listed) -> dict[str, list[dict[str, Any]]]:
    """Calls of step 1 in which the agent itself made the database unavailable, by level."""
    step = item.trial.steps.get(1)
    found: dict[str, list[dict[str, Any]]] = {name: [] for name, _ in CHECKS}
    if step is None:
        return found
    for turn in turns(step):
        if turn.source != "agent":
            continue
        for name, arguments in turn.calls:
            text = call_text(name, arguments)
            for level, pattern in CHECKS:
                if pattern.search(text):
                    found[level].append({"turn": turn.index, "call": scrub(text)[:300]})
    return found


def main() -> None:
    out = OUT_ROOT / "ready"
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for item in listed():
        sources, error = probe_sources(item)
        joined = "\n".join(sources.values())
        row = {
            "trial": item.short,
            "cell": item.cell,
            "variant": item.variant,
            "dir": str(item.trial.dir.relative_to(item.trial.dir.parents[2])),
            "end_reason_step1": item.trial.steps[1].end_reason
            if 1 in item.trial.steps
            else None,
            "verdicts": verdicts(item),
            "snapshot_error": error,
            "files": sorted(sources),
            "markers": [
                name for name, pattern in MARKERS.items() if pattern.search(joined)
            ],
            "own_checks": own_checks(item),
        }
        rows.append(row)
        body = "".join(
            f"##### {path}\n{text}\n" for path, text in sorted(sources.items())
        )
        (out / f"{item.variant}-{item.short}.py.txt").write_text(
            body or f"# no snapshot: {error}\n"
        )

    (out / "summary.json").write_text(
        json.dumps(rows, indent=1, ensure_ascii=False) + "\n"
    )
    lines = [
        "| trial | cell | variant | 1.7 | 1.11 | 1.12 | markers | own checks: calls per level 1 / 2 / 3 | 1.12 message |",
        "|" + "---|" * 9,
    ]
    for row in rows:
        v = row["verdicts"]
        lines.append(
            "| …{trial} | {cell} | {variant} | {a} | {b} | {c} | {markers} | {n} | {msg} |".format(
                trial=row["trial"],
                cell=row["cell"],
                variant=row["variant"],
                a=v.get("1.7", {}).get("status", "—"),
                b=v.get("1.11", {}).get("status", "—"),
                c=v.get("1.12", {}).get("status", "—"),
                markers=", ".join(row["markers"])
                or (row["snapshot_error"] and "no snapshot")
                or "none",
                n=" / ".join(str(len(calls)) for calls in row["own_checks"].values()),
                msg=v.get("1.12", {})
                .get("evidence", "")
                .replace("\n", " ")
                .replace("|", "\\|")[:160],
            )
        )
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
