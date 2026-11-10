#!/usr/bin/env python3
"""Flatten the ATIF trajectories of the listed trials into greppable text.

    python3 bench/report/failures/dump_traj.py [--variants G0 G2 G2b] [--limit 4000]

Writes `~/bench-runs/analysis/failures/traj/<variant>-<trial>-step-N.txt`: one block per
turn (reasoning, message, tool calls, tool results cut to `--limit` characters).
Addresses, user paths and keys are scrubbed. Read-only on the runs.
"""

from __future__ import annotations

import argparse

from common import OUT_ROOT, call_text, listed, scrub, trajectory_path, turns


def cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[: limit // 2]}\n… [{len(text) - limit} characters cut] …\n{text[-limit // 2 :]}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--variants", nargs="+", default=["G0", "G2", "G2b"])
    parser.add_argument("--limit", type=int, default=4000)
    args = parser.parse_args()

    out = OUT_ROOT / "traj"
    out.mkdir(parents=True, exist_ok=True)
    written = missing = 0
    for item in listed(tuple(args.variants)):
        for number, step in sorted(item.trial.steps.items()):
            if not trajectory_path(step).is_file():
                missing += 1
                print(f"no trajectory: {item.variant} {item.short} step {number}")
                continue
            lines = [
                f"# {item.cell} {item.variant} …{item.short} step {number} end_reason={step.end_reason}"
            ]
            for turn in turns(step):
                lines.append(f"\n=== [{turn.index}] {turn.source} {turn.timestamp}")
                if turn.reasoning:
                    lines.append(f"--- reasoning\n{cut(turn.reasoning, args.limit)}")
                if turn.message:
                    lines.append(f"--- message\n{cut(turn.message, args.limit)}")
                for name, arguments in turn.calls:
                    lines.append(
                        f"--- call\n{cut(call_text(name, arguments), args.limit * 2)}"
                    )
                for result in turn.results:
                    lines.append(f"--- result\n{cut(result, args.limit)}")
            target = out / f"{item.variant}-{item.short}-step-{number}.txt"
            target.write_text(scrub("\n".join(lines)) + "\n")
            written += 1
    print(f"written {written}, without trajectory {missing} -> {out}")


if __name__ == "__main__":
    main()
