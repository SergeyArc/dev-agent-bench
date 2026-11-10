#!/usr/bin/env python3
"""What repeated before the OpenHands loop detector stopped a step.

    python3 bench/report/failures/loops.py [--tail 8]

For every step of the listed trials with `loop_detected.detected` in run.json: the number
of agent actions, the most repeated action of the step, the length of the run of
identical actions the step ends with, and the last `--tail` actions with their results
(one line each). Also written to `~/bench-runs/analysis/failures/loops/summary.md`.
Read-only on the runs.
"""

from __future__ import annotations

import argparse
import hashlib
from collections import Counter

from common import OUT_ROOT, call_text, listed, scrub, turns


def one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--tail", type=int, default=8)
    args = parser.parse_args()

    lines: list[str] = []
    for item in listed():
        for number, step in sorted(item.trial.steps.items()):
            detected = step.run.get("loop_detected") or {}
            if not detected.get("detected"):
                continue
            actions: list[tuple[int, str, str]] = []  # turn, call, result
            for turn in turns(step):
                if turn.source != "agent":
                    continue
                for position, (name, arguments) in enumerate(turn.calls):
                    result = (
                        turn.results[position] if position < len(turn.results) else ""
                    )
                    actions.append((turn.index, call_text(name, arguments), result))
            keys = [hashlib.sha1(call.encode()).hexdigest() for _, call, _ in actions]
            trailing = 0
            for key in reversed(keys):
                if key != keys[-1]:
                    break
                trailing += 1
            top_key, top_count = Counter(keys).most_common(1)[0] if keys else ("", 0)
            top_call = next(
                (
                    call
                    for (_, call, _), key in zip(actions, keys, strict=True)
                    if key == top_key
                ),
                "",
            )
            lines.append(
                f"\n## {item.variant} …{item.short} {item.cell} step {number}: {detected.get('message')}; "
                f"end_reason={step.end_reason}, actions={len(actions)}, identical at the end={trailing}, "
                f"most repeated ×{top_count}: {one_line(scrub(top_call), 160)}"
            )
            for index, call, result in actions[-args.tail :]:
                lines.append(f"- [{index}] {one_line(scrub(call), 260)}")
                lines.append(f"    -> {one_line(scrub(result), 200)}")
    out = OUT_ROOT / "loops"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
