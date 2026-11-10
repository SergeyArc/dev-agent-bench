#!/usr/bin/env python3
"""Heredoc terminators left inside source files of the snapshots.

    python3 bench/report/failures/heredoc_tail.py [--calls-only]

For every step snapshot of the listed trials: Python files with a line that starts with a
heredoc terminator (`EOF`, `EOT`, `PY`, `END`) — the terminator and what follows it were
written into the file instead of ending the heredoc. Prints trial, step, file, line number
and the line; the same goes to `~/bench-runs/analysis/failures/heredoc/summary.json`.
From the trajectories: shell calls with a heredoc per cell, with a quoted (`<<'EOF'`) and
an unquoted (`<<EOF`) delimiter, and in how many of them the tool output shows the
terminator followed by the redirect (`calls.json`). Takes a few minutes: every Python
file of every step snapshot is read. Read-only on the runs.
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any

from common import OUT_ROOT, listed, scrub, turns
from snapshots import Snapshot, SnapshotError

TAIL = re.compile(r"^(EOF|EOT|PY|END|PYEOF)\b.*$", re.MULTILINE)
HEREDOC = re.compile(r"<<\s*-?(['\"]?)(\w+)")
COUNTS = ("quoted", "quoted_doubled", "unquoted", "unquoted_doubled")


def heredoc_calls() -> list[dict[str, Any]]:
    """Shell calls with a heredoc per trial and step: how many, quoted delimiter, doubled output."""
    rows: list[dict[str, Any]] = []
    for item in listed():
        for number, step in sorted(item.trial.steps.items()):
            counts = dict.fromkeys(COUNTS, 0)
            for turn in turns(step):
                for position, (_, arguments) in enumerate(turn.calls):
                    command = (
                        arguments.get("command")
                        if isinstance(arguments, dict)
                        else None
                    )
                    match = (
                        HEREDOC.search(command) if isinstance(command, str) else None
                    )
                    if match is None:
                        continue
                    kind = "quoted" if match.group(1) else "unquoted"
                    counts[kind] += 1
                    result = (
                        turn.results[position] if position < len(turn.results) else ""
                    )
                    # the terminator followed by the redirect, in the tool output
                    if re.search(rf"^{match.group(2)} ?> ?/", result, re.MULTILINE):
                        counts[f"{kind}_doubled"] += 1
            if counts["quoted"] or counts["unquoted"]:
                rows.append(
                    {
                        "trial": item.short,
                        "cell": item.cell,
                        "variant": item.variant,
                        "step": number,
                    }
                    | counts
                )
    return rows


def tails() -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    unreadable = 0
    for item in listed():
        for number, step in sorted(item.trial.steps.items()):
            try:
                with Snapshot(step.dir, number) as snapshot:
                    paths = [p for p in snapshot.files() if p.endswith(".py")]
                    sources = {path: snapshot.read(path) for path in paths}
            except SnapshotError:
                unreadable += 1
                continue
            for path, text in sources.items():
                for match in TAIL.finditer(text):
                    rows.append(
                        {
                            "trial": item.short,
                            "cell": item.cell,
                            "variant": item.variant,
                            "step": number,
                            "file": path,
                            "line": text.count("\n", 0, match.start()) + 1,
                            "lines_in_file": text.count("\n") + 1,
                            "text": scrub(match.group(0))[:200],
                        }
                    )
    return rows, unreadable


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--calls-only", action="store_true", help="skip the slow scan of the snapshots"
    )
    calls_only = parser.parse_args().calls_only

    out = OUT_ROOT / "heredoc"
    out.mkdir(parents=True, exist_ok=True)

    calls = heredoc_calls()
    (out / "calls.json").write_text(
        json.dumps(calls, indent=1, ensure_ascii=False) + "\n"
    )
    cells: dict[str, dict[str, int]] = {}
    for call in calls:
        total = cells.setdefault(
            f"{call['cell']} {call['variant']}", dict.fromkeys(COUNTS, 0)
        )
        for key in COUNTS:
            total[key] += call[key]
    for cell, total in sorted(cells.items()):
        print(
            f"{cell}: heredoc calls with a quoted delimiter {total['quoted']} "
            f"(doubled output {total['quoted_doubled']}), unquoted {total['unquoted']} "
            f"(doubled output {total['unquoted_doubled']})"
        )

    if calls_only:
        return
    rows, unreadable = tails()
    (out / "summary.json").write_text(
        json.dumps(rows, indent=1, ensure_ascii=False) + "\n"
    )
    for row in rows:
        print(
            "{variant} …{trial} {cell} step {step} {file}:{line}/{lines_in_file} {text}".format(
                **row
            )
        )
    print(f"{len(rows)} tails; step snapshots that could not be read: {unreadable}")


if __name__ == "__main__":
    main()
